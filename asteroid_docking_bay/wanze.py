# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Host side of wanze — the probe that records while the watch is away.

wanze samples opportunistically: its timer never wakes the watch, so rows
appear only when the watch was awake anyway. Everything here exists to read
that irregular trace honestly.

Two hazards drive the whole design, both found on live hardware:

* **The watch's wall clock cannot be trusted.** catfish's RTC was 130 DAYS
  behind the host while recording perfectly well. So `epoch` is useless on its
  own, and every timing conclusion is drawn from `uptime` instead, which no
  clock adjustment can move. The skew is measured at harvest and reported
  rather than silently corrected.
* **A gap is data, not a hole.** The probe not firing for three hours means the
  watch slept for three hours. Treating that as missing data throws away the
  one thing a wearable telemetry probe is uniquely able to measure.

Sensor classification is deliberately NOT reimplemented here: the rows carry
the same keys drainlog already understands, so `drainlog.classify` handles the
per-watch sign convention and the always-zero case for both.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from .drainlog import classify
from .util import log

REMOTE_LOG = "/var/log/wanze.csv"
REMOTE_BIN = "/usr/bin/wanze-sample"
UNIT = "wanze.timer"
SCHEMA = 1

# The timer's nominal period. Only used to decide what counts as a gap; the
# real interval is whatever the watch's wakefulness allowed.
INTERVAL_S = 300

# A sample later than this multiple of the interval means the watch was not
# awake in between. Generous, because a loaded watch legitimately runs late and
# calling that "asleep" would inflate every standby figure.
GAP_FACTOR = 3


def parse(csv_text: str) -> "list[dict]":
    """CSV -> rows, driven by the header rather than by column position.

    Header-driven on purpose: wanze stamps a schema version and starts a fresh
    file when the columns change, but a trace harvested across an upgrade can
    still hold both shapes. Reading by name means an added column cannot
    silently shift every later field. Pure — see tests.
    """
    lines = [ln for ln in (csv_text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    cols = [c.strip() for c in lines[0].split(",")]
    if "uptime" not in cols:                    # not a wanze file at all
        return []

    def num(v, cast):
        v = (v or "").strip()
        try:
            return cast(v)
        except ValueError:
            return None

    rows = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < len(cols):
            continue                            # torn row, e.g. power lost mid-write
        rec = dict(zip(cols, parts))
        row = {
            "epoch": num(rec.get("epoch"), int),
            "uptime": num(rec.get("uptime"), float),
            # Same key names drainlog uses, so classify() works on these rows
            # unchanged rather than being duplicated for a second format.
            "current_ua": num(rec.get("current_ua"), int),
            "capacity": num(rec.get("capacity"), int),
            "status": (rec.get("status") or "").strip(),
            "voltage_uv": num(rec.get("voltage_uv"), int),
            "temp": num(rec.get("temp"), int),
            "charger": num(rec.get("charger"), int),
            "backlight": num(rec.get("backlight"), int),
            "cpu_online": num(rec.get("cpu_online"), int),
            "cpu_freq": num(rec.get("cpu_freq"), int),
            "load1": num(rec.get("load1"), float),
            "gauge": (rec.get("gauge") or "").strip(),
        }
        if row["uptime"] is None:
            continue
        rows.append(row)
    return rows


def segments(rows: "list[dict]") -> "list[dict]":
    """Split the trace where the watch rebooted.

    `uptime` only ever rises inside one boot, so a DROP is a reboot — which
    makes reboot forensics fall out of a battery trace for free. Timing must
    never be computed across such a break, because the uptime delta there is
    meaningless. Pure — see tests.
    """
    out: "list[dict]" = []
    cur: "list[dict]" = []
    for row in rows:
        if cur and row["uptime"] < cur[-1]["uptime"]:
            out.append({"rows": cur})
            cur = []
        cur.append(row)
    if cur:
        out.append({"rows": cur})
    return out


def gaps(rows: "list[dict]", interval_s: int = INTERVAL_S) -> "list[dict]":
    """Stretches where the probe did not fire — i.e. the watch was not awake.

    Measured on `uptime`, never on `epoch`: a watch whose clock is wrong (or
    which syncs mid-trace) would otherwise report invented gaps or hide real
    ones. Pure — see tests.
    """
    out = []
    for seg in segments(rows):
        srows = seg["rows"]
        for a, b in zip(srows, srows[1:]):
            delta = b["uptime"] - a["uptime"]
            if delta > interval_s * GAP_FACTOR:
                out.append({
                    "from_uptime": a["uptime"], "to_uptime": b["uptime"],
                    "seconds": round(delta),
                    "capacity_from": a["capacity"], "capacity_to": b["capacity"],
                })
    return out


def clock_check(rows: "list[dict]") -> dict:
    """Does the watch's wall clock agree with its own uptime?

    Both advance in real time, so within one boot their deltas should match.
    A divergence means the clock was adjusted mid-trace (an NTP sync, say),
    which is exactly the event that would corrupt any epoch-based reading and
    which is invisible if you only look at one of the two. Pure — see tests.
    """
    worst = 0.0
    for seg in segments(rows):
        srows = [r for r in seg["rows"] if r["epoch"] is not None]
        for a, b in zip(srows, srows[1:]):
            drift = (b["epoch"] - a["epoch"]) - (b["uptime"] - a["uptime"])
            worst = max(worst, abs(drift))
    return {"max_step_s": round(worst), "clock_stepped": worst > 60}


def analyse(rows: "list[dict]", host_epoch: "float | None" = None) -> dict:
    """What the trace says, with every timing figure taken from uptime.

    `host_epoch` is the host's clock AT HARVEST. It is used only to report the
    watch's skew — never to rewrite the rows, because a corrected timestamp
    that looks trustworthy is worse than an obviously wrong one. Pure.
    """
    if not rows:
        return {"ok": False, "error": "no wanze rows"}

    segs = segments(rows)
    covered = sum(s["rows"][-1]["uptime"] - s["rows"][0]["uptime"] for s in segs)
    holes = gaps(rows)
    asleep = sum(g["seconds"] for g in holes)

    out = {
        "ok": True,
        "samples": len(rows),
        "boots": len(segs),
        "reboots": len(segs) - 1,
        "covered_s": round(covered),
        "asleep_s": asleep,
        # The headline the probe exists to produce: of the time we watched, how
        # much did the watch spend not awake? A high number is a healthy watch.
        "asleep_fraction": round(asleep / covered, 3) if covered else None,
        "gaps": holes[:20],
        "gap_count": len(holes),
        "gauge": rows[-1]["gauge"],
        **clock_check(rows),
    }
    if host_epoch is not None and rows[-1]["epoch"] is not None:
        skew = rows[-1]["epoch"] - host_epoch
        out["clock_skew_s"] = round(skew)
        out["clock_skew_days"] = round(skew / 86400, 1)
    # Sensor + discharge direction, from the module that already knows how.
    out["battery"] = classify(rows)
    # Screen-on time is what makes a drain figure attributable rather than
    # merely true; a watch that drained while lit is a different story.
    lit = [r for r in rows if r["backlight"]]
    out["samples_screen_on"] = len(lit)
    return out


# --- watch-side control ---------------------------------------------------

# wanze lives in its own repo, so a-d-b has to FIND its files rather than own
# them. Duplicating the sampler here would give the fleet two sources of truth
# for the thing that produces every number, which is worse than a search path.
SRC_CANDIDATES = (
    Path(__file__).resolve().parent / "wanze-probe",   # a bundled copy, if built with one
    Path.home() / "Git/wanze/src",                     # a developer checkout
    Path("/usr/share/wanze"),                          # installed from the ipk
)
SRC_FILES = ("wanze-sample", "wanze.service", "wanze.timer")


def find_src() -> "Path | None":
    """The first candidate that holds a COMPLETE set. A directory carrying only
    some of the files would install a probe that cannot run, and the failure
    would not show up until the trace came back empty days later."""
    for cand in SRC_CANDIDATES:
        if all((cand / f).is_file() for f in SRC_FILES):
            return cand
    return None


def install(watch, src_dir: "Path | None" = None) -> "str | None":
    """Push the sampler and its units, then enable the timer.

    Pushes files rather than piping a script: `adb shell` reads stdin and will
    swallow the remainder of a heredoc, which truncated several scripts during
    the benchmark work.
    """
    src_dir = src_dir or find_src()
    if src_dir is None:
        return ("wanze sources not found — looked in: "
                + ", ".join(str(c) for c in SRC_CANDIDATES))
    for name, dest in (("wanze-sample", REMOTE_BIN),
                       ("wanze.service", "/etc/systemd/system/wanze.service"),
                       ("wanze.timer", "/etc/systemd/system/wanze.timer")):
        src = Path(src_dir) / name
        if not src.exists():
            return f"missing {src}"
        rc, _, err = watch.t.push(str(src), dest, timeout=30)
        if rc != 0:
            return f"push {name} failed: {err.strip()[:80]}"
    rc, out, err = watch.t.shell(
        shlex.quote(f"chmod +x {REMOTE_BIN} && systemctl daemon-reload && "
                    f"systemctl enable --now {UNIT}"), timeout=40)
    if rc != 0:
        return f"enable failed: {(err or out).strip()[:120]}"
    # Confirm rather than assume: a timer that never armed produces an empty
    # trace days later, by which time the window is gone.
    if watch.t.shell(f"systemctl is-active {UNIT}", timeout=15)[1].strip() != "active":
        return "timer did not become active"
    log.info("%s: wanze installed and armed", watch.serial)
    return None


def stop(watch) -> None:
    watch.t.shell(f"systemctl disable --now {UNIT}", timeout=25)


def uninstall(watch, clear_trace: bool = False) -> "str | None":
    """Remove the probe from the watch: stop it, then delete what install put
    there. Returns None on success, or a message.

    The mirror of install(), file for file — anything install pushes, this
    removes, so a half-removed probe cannot be left behind to re-arm on the
    next boot.

    The TRACE is kept by default and only deleted when explicitly asked. It is
    the sole copy of whatever the probe recorded until it has been harvested,
    and deleting a measurement as a side effect of tidying up the tool that
    produced it is the kind of quiet loss this project keeps finding: it took
    days of a run to collect and a moment to destroy.
    """
    stop(watch)
    targets = [REMOTE_BIN,
               "/etc/systemd/system/wanze.service",
               "/etc/systemd/system/wanze.timer"]
    if clear_trace:
        targets.append(REMOTE_LOG)
    rc, out, err = watch.t.shell(
        shlex.quote("rm -f " + " ".join(targets) + " && systemctl daemon-reload"),
        timeout=30)
    if rc != 0:
        return f"remove failed: {(err or out).strip()[:120]}"
    # Confirm it is really gone: a unit file left behind re-arms on reboot and
    # the watch quietly starts sampling again days later.
    left = watch.t.shell(shlex.quote(f"ls {REMOTE_BIN} 2>/dev/null"),
                         timeout=15)[1].strip()
    if left:
        return "the sampler is still on the watch after removal"
    log.info("%s: wanze removed%s", watch.serial,
             " (trace deleted)" if clear_trace else " (trace kept)")
    return None


def harvest(watch, clear: bool = False) -> dict:
    """Pull the trace and say what it means. `clear` truncates the on-watch
    buffer, which is only safe once the rows are actually in hand."""
    # Take one closing sample BEFORE reading. Without it the trace ends at the
    # last opportunistic sample and every second the watch slept after that is
    # invisible — catfish's first real run lost ~41 minutes of standby that way,
    # under-reporting the very thing the probe exists to measure.
    watch.t.shell(f"systemctl start wanze.service", timeout=30)
    rc, out, _ = watch.t.shell(f"cat {REMOTE_LOG}", timeout=40)
    host_epoch = time.time()
    if rc != 0 or not out.strip():
        return {"ok": False, "error": "no wanze trace on this watch"}
    rows = parse(out)
    if not rows:
        return {"ok": False, "error": "wanze trace is empty or unreadable"}
    if clear:
        watch.t.shell(shlex.quote(f"rm -f {REMOTE_LOG}"), timeout=20)
    return {**analyse(rows, host_epoch), "first": rows[0], "last": rows[-1]}


# --- presence and run state -----------------------------------------------
# Two different questions, deliberately kept apart:
#   "is wanze on this watch?"   — cheap, cached, and remembered while it is away
#   "is a measurement running?" — persisted, because forgetting it ruins the run

_present: "dict[str, tuple[float, bool]]" = {}     # serial -> (checked_ts, present)
_PRESENT_TTL = 600
_known: "dict[str, bool] | None" = None            # lazily mirrored from the registry


def _load_known() -> "dict[str, bool]":
    """What the registry remembers about wanze, mirrored once into memory.

    Read once rather than per row: the status document is rebuilt on every
    refresh and every port would otherwise deep-copy a registry record."""
    global _known
    if _known is None:
        from .registry import registry
        _known = {}
        for rec in registry.all():
            val = (rec.get("fields") or {}).get("wanze")
            if val is not None:
                _known[rec["serial"]] = bool(val)
    return _known


def detect(serial: "str | None", force: bool = False) -> "bool | None":
    """Is wanze installed on this watch? Takes a SERIAL, not a Watch, because
    the caller is the status pass, which has serials and no Watch objects.

    Cached hard. This runs while the status document is rebuilt, and an adb
    round trip per watch per refresh would be a real cost for a fact that
    changes about twice a year. On an unreachable watch the previous answer
    stands rather than being downgraded to "no" — absence of evidence here is
    a dropped link, not a removed probe.

    Records the answer in the registry so it outlives the watch leaving the
    dock, which is the point: the row has to say "that one is carrying a
    probe" while the watch is in a drawer.
    """
    from .adb import adb_shell
    if not serial:
        return None
    hit = _present.get(serial)
    if hit and not force and time.time() - hit[0] < _PRESENT_TTL:
        return hit[1]
    rc, out, _ = adb_shell(serial, f'"test -x {REMOTE_BIN} && echo yes"')
    if rc != 0:
        return hit[1] if hit else None
    found = out.strip() == "yes"
    _present[serial] = (time.time(), found)
    if _load_known().get(serial) != found:
        _load_known()[serial] = found
        from .registry import registry
        registry.note(serial, source="wanze", wanze=found)
    return found


def known(serial: "str | None") -> bool:
    """Was wanze ever seen on this watch? Answers for an ABSENT watch, from the
    registry rather than from a live read."""
    return bool(serial and _load_known().get(serial))


# A wanze run is a long operation that must not be disturbed, which is exactly
# what oplock exists for — so it IS an oplock, taken with kind="wanze". Keeping
# a second marker meant a wanze run was invisible to the housekeeping that
# oplock holds off, and two spellings of "leave this watch alone" is one too
# many. The reads below still answer in wanze's own terms so callers and the UI
# do not have to care.
PROBING_KIND = "wanze"
# A run spans hours or days and its whole point is to be left alone across
# them, so it outlives oplock's default. It still expires: a marker with no end
# would exempt a watch from housekeeping forever if a run were abandoned.
PROBING_TTL_SEC = 14 * 24 * 3600


def probing(cfg: dict, serial: "str | None") -> "dict | None":
    """The marker for a measurement run in progress on this watch, or None.

    Persisted rather than held in memory like the drain task: a wanze run spans
    hours and a service restart mid-run must not quietly drop the one indicator
    that says 'do not touch this watch'."""
    from . import oplock
    lock = oplock.held(cfg, serial)
    if not lock or lock.get("kind") != PROBING_KIND:
        return None
    return lock


def probing_set(serial: str, on: bool, note: str = "") -> dict:
    """Start or clear the run marker."""
    from . import oplock
    if on:
        lock = oplock.hold(serial, PROBING_KIND, note, PROBING_TTL_SEC)
        if not lock.get("ok"):
            # Held for something else (a dump, a flash). Do not overwrite it.
            return {"ok": False, "probing": False,
                    "error": f"watch is held for '{lock.get('kind')}'"}
    else:
        oplock.release(serial)
    log.info("%s: wanze probing %s", serial, "started" if on else "cleared")
    return {"ok": True, "probing": on}
