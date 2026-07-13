# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""The backend op table (see docs/CONTAINERS.md).

Every host-touching operation the web API offers is a named op here. This is
the single implementation of that logic: the monolithic web server dispatches
to it in-process via LocalCaller, and the split backend serves the same table
over RPC. Adding a capability means registering a named op in a reviewable
diff — there is deliberately no generic "run a command" op.

Op names mirror the former /api/* routes. Data handlers take one args dict
and return a JSON-able value (the app-level response, distinct from the RPC
envelope's ok/error). Streaming handlers (flash, onboard) yield raw message
strings — an empty string is a keep-alive heartbeat — which the frontend
turns into SSE frames.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import queue
import threading
import time

from .util import _run, log
from .adb import _adb_state, adb_devices, get_watch_codename
from .config import (_config_lock, charge_config, find_codename_for_loc_port,
                     find_serial_for_loc_port, flash_config, load_config,
                     save_config)
from .usb import (_sysfs_path_to_serial_map, test_port_power_switching,
                  uhubctl_cycle, uhubctl_set_power)
from .watchctl import Watch
from .ops import ChargeOp, DrainOp, WorkbenchOp, _flash_one_watch
from .events import _DRAIN_FLOOR_PCT, _DRAIN_RESULTS_DIR
from .webstatus import _web_status_data
from .tasks import _adb_lock, _charge_tasks, _flash_tasks, _remap_tasks
from .rpc import Dispatcher
from . import __version__

DISPATCH = Dispatcher()


# ── status ──────────────────────────────────────────────────────────────────

@DISPATCH.op("status.get")
def _status_get(args):
    cfg = load_config()
    cc = charge_config(cfg)
    return {
        "hubs": _web_status_data(cfg),
        "thresholds": {"low": cc.low_threshold, "high": cc.high_threshold},
        "drain_floor": _DRAIN_FLOOR_PCT,
        "wearable_min_hours": cfg.get("wearable_min_hours", 24),
        # The version of the process running the ops — in split mode the
        # backend's, which is what an upgrade check cares about.
        "version": __version__,
    }


# ── per-watch (Control Center) ──────────────────────────────────────────────

@DISPATCH.op("watch.cc")
def _watch_cc(args):
    return Watch(args["serial"]).cc_data()


@DISPATCH.op("watch.toggle")
def _watch_toggle(args):
    tech = args["tech"]
    if tech not in ("wifi", "bluetooth"):
        return {"ok": False, "error": f"unknown toggle {tech}"}
    return {"ok": Watch(args["serial"]).toggle(tech, bool(args["on"]))}


@DISPATCH.op("watch.settime")
def _watch_settime(args):
    return {"ok": True, "timezone": Watch(args["serial"]).set_time_from_host()}


@DISPATCH.op("watch.notify")
def _watch_notify(args):
    return {"ok": Watch(args["serial"]).notify()}


@DISPATCH.op("watch.buzz")
def _watch_buzz(args):
    return {"ok": Watch(args["serial"]).buzz()}


@DISPATCH.op("watch.screen")
def _watch_screen(args):
    return {"ok": Watch(args["serial"]).screen(bool(args["on"]))}


@DISPATCH.op("watch.screenshot")
def _watch_screenshot(args):
    """JPEG as base64 in the response — keeps the protocol single-channel
    (a screenshot is ~60 KB, the overhead is irrelevant)."""
    local = Watch(args["serial"]).screenshot()
    if not local:
        return {"ok": False, "error": "screenshot failed"}
    return {"ok": True, "jpeg_b64": base64.b64encode(local.read_bytes()).decode()}


# ── port power ──────────────────────────────────────────────────────────────

@DISPATCH.op("port.set")
def _port_set(args):
    try:
        confirmed = uhubctl_set_power(args["loc"], args["port"], bool(args["on"]))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "confirmed": confirmed}


@DISPATCH.op("port.cycle")
def _port_cycle(args):
    loc, port = args["loc"], args["port"]
    try:
        uhubctl_set_power(loc, port, False)
        time.sleep(5)
        uhubctl_set_power(loc, port, True)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@DISPATCH.op("port.poweroff")
def _port_poweroff(args):
    """Graceful OS shutdown then cut VBUS immediately — adb shell is
    synchronous, so the command is delivered before power is cut and the
    watch finishes halting on battery. Any delay here races the halt: cutting
    while the watch is still up lets a watch without offmode charging bounce
    back on."""
    loc, port = args["loc"], args["port"]
    serial = find_serial_for_loc_port(load_config(), loc, port)
    adb_ok = False
    if serial:
        rc, _, err = _run(f"adb -s {serial} shell poweroff", check=False,
                          timeout=10)
        adb_ok = (rc == 0)
        if not adb_ok:
            log.warning("poweroff %s:%s (%s): adb shutdown failed: %s",
                        loc, port, serial, err.strip() or f"rc={rc}")
    else:
        log.warning("poweroff %s:%s: no serial known — cutting power only",
                    loc, port)
    try:
        confirmed = uhubctl_set_power(loc, port, False)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "adb_shutdown": adb_ok}
    return {"ok": True, "adb_shutdown": adb_ok, "confirmed": confirmed}


def _adb_action(loc, port, cmd, fail_msg):
    serial = find_serial_for_loc_port(load_config(), loc, port)
    if not serial:
        return {"ok": False, "error": "no serial found for port"}
    rc, _, err = _run(f"adb -s {serial} {cmd}", check=False)
    if rc != 0:
        return {"ok": False, "error": err or fail_msg}
    return {"ok": True}


@DISPATCH.op("port.reboot")
def _port_reboot(args):
    return _adb_action(args["loc"], args["port"], "reboot",
                       "adb reboot failed")


@DISPATCH.op("port.bootloader")
def _port_bootloader(args):
    return _adb_action(args["loc"], args["port"], "reboot bootloader",
                       "adb reboot bootloader failed")


# ── config visibility ───────────────────────────────────────────────────────

@DISPATCH.op("port.hide")
def _port_hide(args):
    """Toggle a port's user avoid/hidden flag, stored in the hub's exclude
    map alongside auto-detected excludes."""
    loc, port = args["loc"], args["port"]
    with _config_lock:
        cfg = load_config()
        hub = next((hub for hub in cfg.get("hubs", []) if hub["location"] == loc), None)
        if hub is None:
            return {"ok": False, "error": "hub not found"}
        excl = hub.setdefault("exclude", {})
        port_str = str(port)
        if port_str in excl:
            del excl[port_str]
            state = False
        else:
            excl[port_str] = "hidden by user"
            state = True
        save_config(cfg)
    return {"ok": True, "hidden": state}


@DISPATCH.op("hub.hide")
def _hub_hide(args):
    """Toggle a whole hub's hidden flag (for unused sub-hubs)."""
    loc = args["loc"]
    with _config_lock:
        cfg = load_config()
        hub = next((hub for hub in cfg.get("hubs", []) if hub["location"] == loc), None)
        if hub is None:
            return {"ok": False, "error": "hub not found"}
        hub["hidden"] = not hub.get("hidden", False)
        state = hub["hidden"]
        save_config(cfg)
    return {"ok": True, "hidden": state}


# ── operations (charge / drain / workbench) ─────────────────────────────────

@DISPATCH.op("charge.start")
def _charge_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if ChargeOp.is_active(slot):
        return {"ok": False, "error": "charge already running",
                "charge_end_ts": _charge_tasks[slot].get("charge_end_ts", 0)}
    cfg = load_config()
    err = ChargeOp.start(loc, port, cfg)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True,
            "duration_seconds": charge_config(cfg).charge_duration_minutes * 60}


def _register_lifecycle(op_cls, name, stop_error):
    """start/stop ops share one shape per Operation subclass; charge.start
    stays hand-written above for its already-running special case."""
    if name != "charge":
        @DISPATCH.op(f"{name}.start")
        def _start(args):
            err = op_cls.start(args["loc"], args["port"], load_config())
            return {"ok": False, "error": err} if err else {"ok": True}

    @DISPATCH.op(f"{name}.stop")
    def _stop(args):
        if op_cls.stop(args["loc"], args["port"]):
            return {"ok": True}
        return {"ok": False, "error": stop_error}


_register_lifecycle(ChargeOp, "charge", "no charge running")
_register_lifecycle(WorkbenchOp, "workbench", "no workbench active")
_register_lifecycle(DrainOp, "drain", "no drain test running")


@DISPATCH.op("drain.history")
def _drain_history(args):
    """All recorded drain results, newest first."""
    tests = []
    for f in _DRAIN_RESULTS_DIR.glob("*.json"):
        try:
            with f.open() as fh:
                d = json.load(fh)
        except Exception:
            continue
        readings = d.get("readings") or []
        tests.append({
            "codename":  d.get("codename"),
            "slot":      d.get("slot"),
            "start_ts":  d.get("start_ts"),
            "end_ts":    readings[-1].get("ts") if readings else d.get("start_ts"),
            "start_pct": d.get("start_pct"),
            "end_pct":   d.get("end_pct"),
            "rate":      d.get("drain_rate_pct_per_hour"),
            "stopped":   d.get("stopped_by_user", False),
            "samples":   len(readings),
        })
    tests.sort(key=lambda t: t.get("start_ts") or 0, reverse=True)
    return {"tests": tests,
            "wearable_min_hours": load_config().get("wearable_min_hours", 24)}


# ── streaming ops ────────────────────────────────────────────────────────────
# Stream handlers yield raw message strings; an empty string is a keep-alive
# heartbeat sentinel. The frontend turns each into an SSE frame — the backend
# knows nothing about SSE. Task state (_flash_tasks/_remap_tasks) lives here,
# with the backend, which the status builder reads.

class _QueueHandler(logging.Handler):
    """Routes log records from one specific thread into a Queue, so a worker
    thread's log output can be streamed to the client."""

    def __init__(self, q: "queue.Queue[str | None]", thread_id: int):
        super().__init__()
        self.q = q
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord):
        if record.thread == self.thread_id:
            try:
                self.q.put_nowait(self.format(record))
            except Exception:
                self.handleError(record)


def _flash_stream(codename: str, slot: str, cfg: dict):
    """Run a flash in a daemon thread and yield its log lines as they happen.
    Empty string = heartbeat."""
    q: "queue.Queue[str | None]" = queue.Queue()
    flash_cfg = flash_config(cfg)
    cfg_copy = copy.deepcopy(cfg)

    def _run_flash():
        tid = threading.get_ident()
        h = _QueueHandler(q, tid)
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.root.addHandler(h)
        try:
            q.put("INFO: waiting for ADB bus (another operation in progress)…")
            with _adb_lock:
                _flash_one_watch(codename, cfg_copy, flash_cfg)
        except Exception as exc:
            try:
                q.put(f"ERROR: {exc}")
            except Exception:
                pass
        finally:
            logging.root.removeHandler(h)
            # The worker owns the done flag: setting it from the generator
            # would mark a still-running flash finished the moment the client
            # disconnects from the stream.
            _flash_tasks[slot]["done"] = True
            q.put(None)

    _flash_tasks[slot] = {"done": False}
    t = threading.Thread(target=_run_flash, daemon=True)
    t.start()
    _flash_tasks[slot]["thread"] = t

    while True:
        try:
            msg = q.get(timeout=25)
        except queue.Empty:
            yield ""                       # heartbeat
            continue
        if msg is None:
            return
        yield msg


@DISPATCH.stream_op("flash.start")
def _flash_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if slot in _flash_tasks and not _flash_tasks[slot].get("done", True):
        yield "flash already in progress"
        return
    cfg = load_config()
    codename = find_codename_for_loc_port(cfg, loc, port)
    if not codename:
        yield "ERROR: port not mapped to any codename"
        return
    yield from _flash_stream(codename, slot, cfg)


def _onboard_stream(loc: str, port: int):
    """Per-port onboarding (remap): power on, wait for a watch to enumerate on
    this port, identify it, update the mapping, PPPS-test. Yields progress
    lines; empty string = heartbeat."""
    slot = f"{loc}:{port}"
    sysfs_path = f"{loc}.{port}"
    q: "queue.Queue[str | None]" = queue.Queue()

    def _emit(msg: str) -> None:
        q.put(msg)

    def _run() -> None:
        _emit("Waiting for ADB bus…")
        with _adb_lock:
            try:
                _emit(f"Powering on {loc} p{port}…")
                try:
                    uhubctl_set_power(loc, port, True)
                except RuntimeError as e:
                    _emit(f"WARNING: {e}")

                # A watch attached powered-off has to cold-boot before it
                # exposes ADB. And on this hardware a watch often fails to
                # enumerate on its first boot (stale-node / enumeration
                # hiccup), only appearing after a power cycle. So wait a boot
                # window, and if nothing shows, cycle the port once and wait
                # again — this is what made the manual "Refresh twice" work.
                wait_each = charge_config(load_config()).onboard_wait_seconds

                def _wait_for_watch(secs: int) -> "str | None":
                    st = time.monotonic()
                    nxt = 15
                    while time.monotonic() - st < secs:
                        devices = adb_devices()
                        path_map = _sysfs_path_to_serial_map(set(devices.keys()))
                        s = path_map.get(sysfs_path)
                        if s and _adb_state(devices, s) == "device":
                            return s
                        el = time.monotonic() - st
                        if el >= nxt:
                            _emit(f"…waiting ({int(el)} / {secs} s)")
                            nxt += 15
                        time.sleep(1.0)
                    return None

                _emit(f"Waiting for the watch to boot and expose ADB "
                      f"(up to {wait_each} s)…")
                found_serial: "str | None" = _wait_for_watch(wait_each)
                if not found_serial:
                    _emit("No ADB yet — power-cycling the port to retry "
                          "enumeration…")
                    uhubctl_cycle(loc, port)
                    _emit(f"Waiting again after the cycle (up to {wait_each} s)…")
                    found_serial = _wait_for_watch(wait_each)

                if found_serial:
                    _emit(f"ADB: {found_serial}")
                    with _config_lock:
                        cfg = load_config()
                        codename = cfg.get("serials", {}).get(found_serial)

                    if not codename:
                        _emit("Reading codename from watch…")
                        codename = get_watch_codename(found_serial) or found_serial
                    _emit(f"Watch: {codename}")

                    with _config_lock:
                        cfg = load_config()
                        cfg.setdefault("serials", {})[found_serial] = codename
                        # Remove old mapping for this watch from every other
                        # port: exact serial binding first, codename fallback.
                        for hub in cfg.get("hubs", []):
                            hub_ports   = hub.get("ports", {})
                            hub_serials = hub.get("port_serials", {})
                            stale = [k for k, s in hub_serials.items()
                                     if s == found_serial
                                     and not (hub["location"] == loc and k == str(port))]
                            stale += [k for k, v in hub_ports.items()
                                      if v == codename and k not in hub_serials
                                      and not (hub["location"] == loc and k == str(port))]
                            for k in stale:
                                hub_ports.pop(k, None)
                                hub_serials.pop(k, None)
                                _emit(f"Removed stale mapping: {hub['location']}:p{k} → {codename}")
                        # Add/update this port.
                        hub_entry = next((hub for hub in cfg.get("hubs", [])
                                          if hub["location"] == loc), None)
                        if hub_entry is not None:
                            hub_entry.setdefault("ports", {})[str(port)] = codename
                            hub_entry.setdefault("port_serials", {})[str(port)] = found_serial
                        save_config(cfg)

                    _emit(f"Mapped {loc}:p{port} → {codename}")

                    _emit("Testing port switching (PPPS, up to ~30 s)…")
                    try:
                        smart, msg = test_port_power_switching(loc, port, found_serial)
                        with _config_lock:
                            cfg = load_config()
                            for hub in cfg.get("hubs", []):
                                if hub["location"] == loc:
                                    hub.setdefault("port_smart", {})[str(port)] = smart
                                    break
                            save_config(cfg)
                        verdict = ("SMART ✓" if smart
                                   else "NOT SMART" if smart is False else "UNVERIFIED")
                        _emit(f"Port: {verdict} — {msg}")
                    except RuntimeError as e:
                        _emit(f"PPPS test error: {e}")

                else:
                    _emit("No watch detected.")
                    with _config_lock:
                        cfg = load_config()
                        was_mapped = False
                        for hub in cfg.get("hubs", []):
                            if hub["location"] == loc:
                                if str(port) in hub.get("ports", {}):
                                    was_mapped = True
                                    del hub["ports"][str(port)]
                                hub.get("port_serials", {}).pop(str(port), None)
                        if was_mapped:
                            save_config(cfg)
                            _emit("Cleared stale port mapping.")
                    # A deeply discharged watch can't boot inside any window —
                    # it needs VBUS to pre-charge first. Leave the port powered
                    # and say so; cutting power here strands exactly the watches
                    # that need charge the most.
                    _emit("Port left POWERED: if a watch with a flat battery "
                          "is docked here, let it pre-charge 30-60 min and "
                          "onboard again. Bootlooping watch? Hold it in "
                          "fastboot to charge. Empty port? Toggle it off.")

            except Exception as exc:
                _emit(f"ERROR: {exc}")
            finally:
                _remap_tasks[slot]["done"] = True
                q.put(None)

    _remap_tasks[slot] = {"done": False}
    threading.Thread(target=_run, daemon=True).start()

    while True:
        try:
            msg = q.get(timeout=15)
        except queue.Empty:
            yield ""                       # heartbeat
            continue
        if msg is None:
            return
        yield msg


@DISPATCH.stream_op("onboard.start")
def _onboard_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if slot in _remap_tasks and not _remap_tasks[slot].get("done", True):
        yield "onboard already in progress"
        return
    yield from _onboard_stream(loc, port)
