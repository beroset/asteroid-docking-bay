# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""The compile cluster the dock itself runs on — icecream nodes.

moWerk's framing: the watches dock to the asteroid, and the asteroid has its own
compute. Showing the build nodes here rather than in a separate tool is the same
instinct as the Orbit port — one place that knows the whole fleet.

**Why this is an instrument and not decoration.** icecream fails SILENTLY.
Yocto's icecc.bbclass reports a non-distributing build at bb.note level, which
scrolls past unread, and a cluster that is doing nothing looks identical to one
that is working: scheduler up, every node registered, no errors anywhere. A
build can run for hours entirely local while every indicator says the cluster is
fine. Job counts moving on the nodes are the only reliable tell, which is what
makes this panel worth its code.

Two rules shape everything below.

**Degrade to nothing.** A host that is not part of a cluster must render no
panel, no empty state and no error strip — see `configured()`. a-d-b runs on
machines that have never heard of icecream.

**Never stall the fleet.** The scheduler lives on ANOTHER machine, and
/api/status is polled continuously. A dead e15 or a dropped LAN link must not
freeze the watch UI, so the socket read happens off the status path behind a
short TTL and the last known answer is served, marked stale, when a refresh
fails. Watches are the primary function; compile nodes are secondary and never
get to degrade them.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time

from .util import log

CONF_PATH = "/etc/icecream.conf"
# The Arch package installs under /usr/lib/icecream/ and puts nothing in PATH;
# the /usr/bin symlink was added by hand on this host and may not exist on
# another. Checking both is what stops a working cluster reading as absent.
ICECC_BINARIES = ("/usr/lib/icecream/bin/icecc", "/usr/bin/icecc")
SCHEDULER_PORT = 8766          # command port; line protocol, NOT http
SOCKET_TIMEOUT = 2.0           # hard cap: another machine must not stall us
CACHE_TTL = 6.0

# Only these two are ever sent. removecs, blockcs and unblockcs mutate cluster
# state and have no business behind a status poll — a monitor that can change
# what it monitors is not a monitor.
SAFE_COMMANDS = ("listcs", "listjobs")


def parse_conf(text: str) -> dict:
    """/etc/icecream.conf is shell syntax: KEY="value". Parsed rather than
    sourced — this file is read by a long-running service and evaluating it
    would hand a config file the ability to run code."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def configured(conf_path: str = CONF_PATH,
               binaries: "tuple[str, ...]" = ICECC_BINARIES) -> "dict | None":
    """The cluster config for this host, or None if it is not a member.

    Three conditions, all required. An empty scheduler host is the subtle one:
    it means the daemon relies on UDP broadcast discovery and there is simply no
    address to ask, so there is nothing to show even though icecream is
    installed and running.
    """
    try:
        with open(conf_path) as fh:
            conf = parse_conf(fh.read())
    except OSError:
        return None
    if not any(os.path.exists(b) for b in binaries):
        return None
    scheduler = (conf.get("ICECREAM_SCHEDULER_HOST") or "").strip()
    if not scheduler:
        return None
    return {"scheduler": scheduler,
            "netname": (conf.get("ICECREAM_NETNAME") or "").strip() or None,
            "max_jobs": (conf.get("ICECREAM_MAX_JOBS") or "").strip() or None}


_BANNER = re.compile(r"(\d+)s uptime,\s*(\d+) hosts?,\s*(\d+) jobs in queue")
# Note the leading space on every node line — the scheduler indents them.
_NODE = re.compile(
    r"^\s+(?P<host>\S+)\s+\((?P<ip>[\d.]+):(?P<port>\d+)\)\s+"
    r"\[(?P<arch>[^\]]*)\]\s+speed=(?P<speed>[\d.]+)\s+"
    r"jobs=(?P<used>\d+)/(?P<max>\d+)\s+load=(?P<load>\d+)")


def parse_banner(text: str) -> dict:
    """Uptime, host count and queue depth, straight from the greeting.

    The banner arrives unprompted on connect and is live whether or not a build
    is running, so it alone answers "is the cluster there".
    """
    m = _BANNER.search(text or "")
    if not m:
        return {}
    return {"uptime_s": int(m.group(1)), "hosts": int(m.group(2)),
            "queue": int(m.group(3))}


def parse_nodes(text: str) -> list:
    """One dict per node from `listcs` output.

    `speed` is 0.00 until a node has completed real jobs, so a freshly stood-up
    cluster reports zeros across the board. That is not a fault and must not be
    rendered as one.
    """
    out = []
    for line in (text or "").splitlines():
        m = _NODE.match(line)
        if not m:
            continue
        g = m.groupdict()
        out.append({"host": g["host"], "ip": g["ip"], "arch": g["arch"],
                    "speed": float(g["speed"]),
                    "jobs_used": int(g["used"]), "jobs_max": int(g["max"]),
                    # 0..1000, not a Unix load average. 1000 is saturated.
                    "load": int(g["load"])})
    return out


def query(host: str, commands: "tuple[str, ...]" = SAFE_COMMANDS,
          port: int = SCHEDULER_PORT, timeout: float = SOCKET_TIMEOUT) -> dict:
    """Run commands against the scheduler's command port.

    Line protocol: two banner lines arrive on connect, each command's output
    ends with a line `200 done`, and `quit` answers `200 Good Bye!`. Every read
    is bounded by the socket timeout, so a scheduler that accepts the connection
    and then says nothing costs us `timeout` seconds, not forever — which is a
    real failure mode for a host that is up but wedged.

    Returns {"ok": bool, "banner": str, "out": {command: text}, "error": str}.
    """
    for c in commands:
        if c not in SAFE_COMMANDS:
            raise ValueError(f"refusing to send {c!r}: not a read-only command")
    result = {"ok": False, "banner": "", "out": {}, "error": ""}
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        result["error"] = str(exc)
        return result
    try:
        sock.settimeout(timeout)
        buf = ""

        def read_until(marker: str) -> str:
            nonlocal buf
            deadline = time.monotonic() + timeout
            while marker not in buf:
                if time.monotonic() > deadline:
                    raise TimeoutError("scheduler accepted the connection but "
                                       "did not answer")
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("scheduler closed the connection")
                buf += chunk.decode("utf-8", "replace")
            text, _, buf = buf.partition(marker)
            return text

        result["banner"] = read_until("Use 'help' for help")
        buf = ""
        for cmd in commands:
            sock.sendall((cmd + "\n").encode())
            result["out"][cmd] = read_until("200 done")
        try:
            sock.sendall(b"quit\n")
        except OSError:
            pass
        result["ok"] = True
    except (OSError, TimeoutError, ConnectionError) as exc:
        result["error"] = str(exc)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return result


def build_summary(conf: dict, answer: dict) -> dict:
    """Everything the UI needs, from one scheduler round-trip.

    `building` is the whole point of the panel: it is true when any node is
    actually running jobs, which is the only trustworthy answer to "is my build
    distributing, or has it silently fallen back to local?"
    """
    nodes = parse_nodes(answer.get("out", {}).get("listcs", ""))
    jobs_text = answer.get("out", {}).get("listjobs", "")
    running = [ln for ln in jobs_text.splitlines()
               if ln.strip() and not ln.startswith("200")]
    used = sum(n["jobs_used"] for n in nodes)
    return {
        "netname": conf.get("netname"),
        "scheduler": conf.get("scheduler"),
        "reachable": bool(answer.get("ok")),
        "error": answer.get("error") or "",
        "nodes": nodes,
        "slots": sum(n["jobs_max"] for n in nodes),
        "jobs_used": used,
        "jobs_listed": len(running),
        "building": used > 0 or bool(running),
        **parse_banner(answer.get("banner", "")),
    }


# ── cached access, off the status path ───────────────────────────────────────

_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def summary(force: bool = False) -> "dict | None":
    """The cluster as the UI should see it, or None when this host is not a
    member. Cheap to call: at most one scheduler round-trip per CACHE_TTL.

    A failed refresh serves the last good answer marked `stale`, rather than
    blinking the panel out — an unreachable scheduler is itself information
    worth keeping on screen, because it means builds are running local *now*.
    """
    conf = configured()
    if not conf:
        return None
    now = time.time()
    with _lock:
        fresh = _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL
        if fresh and not force:
            return _cache["data"]
    answer = query(conf["scheduler"])
    data = build_summary(conf, answer)
    with _lock:
        if not data["reachable"] and _cache["data"]:
            prev = dict(_cache["data"])
            prev.update(reachable=False, stale=True, error=data["error"])
            data = prev
        else:
            data["stale"] = False
        _cache.update(ts=now, data=data)
    if not data.get("reachable"):
        log.debug("icecc: scheduler %s unreachable: %s",
                  conf["scheduler"], data.get("error"))
    return data


_refreshing = threading.Event()


def _refresh_worker() -> None:
    try:
        summary(force=True)
    finally:
        _refreshing.clear()


def summary_cached() -> "dict | None":
    """What /api/status must call: never blocks, not once, not ever.

    `summary()` is cheap when the cache is warm but pays a socket round-trip
    when it is not — and an UNREACHABLE scheduler pays the full timeout, every
    TTL, forever. Doing that inline would hand a dead e15 the power to stall the
    watch fleet UI on a timer, which is exactly the failure the handover warned
    about and exactly the wrong trade: watches are the primary function.

    So the poll thread only ever reads the cache and, when it is stale, asks a
    single background thread to go and refresh it. The panel lags by at most one
    poll; the fleet never waits.
    """
    if configured() is None:
        return None
    data = _cache["data"]
    if data is None or time.time() - _cache["ts"] >= CACHE_TTL:
        if not _refreshing.is_set():
            _refreshing.set()
            threading.Thread(target=_refresh_worker, daemon=True).start()
    return data
