# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Bottle app factory, SSE streams, status cache, background warmer."""

import copy
import json
import logging
import queue
import sys
import threading
import time
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from .util import _run, log
from .adb import _adb_state, adb_devices, get_watch_codename
from .config import (_config_lock, charge_config, find_codename_for_loc_port,
                     flash_config,
                     find_serial_for_loc_port, load_config, save_config)
from . import usb, fastboot
from .usb import (_sysfs_path_to_serial_map, _sysfs_switch_mode,
                  test_port_power_switching, uhubctl_cycle, uhubctl_set_power)
from .events import _DRAIN_FLOOR_PCT, _DRAIN_RESULTS_DIR
from .tasks import (_adb_lock, _charge_tasks, _drain_tasks, _flash_tasks,
                    _remap_tasks, _workbench_tasks)
from .watchctl import Watch
from .ops import (ChargeOp, DrainOp, WorkbenchOp, _end_port,
                  _flash_one_watch, _resume_persisted_tasks)
from .webstatus import _web_status_data
from .webtemplate import _WEB_TEMPLATE


def _background_warmer() -> None:
    """Background daemon feeding the two slow caches so the status path never
    blocks: usb's port-power cache (a `disable` read is a slow, variable USB
    query) and fastboot's device list (a multi-second scan). Lives here — the
    only place it is started — because it needs both usb and fastboot, which
    must not import each other. Sequential and gently paced: parallel USB
    reads are what wedges the bus."""
    while True:
        try:
            if time.time() - fastboot._fb_list_cache["ts"] > 60:
                fastboot._fastboot_poll()
            cfg = load_config()
            for h in cfg.get("hubs", []):
                loc = h["location"]
                for iface in usb._SYSFS_USB.glob(f"{loc}:*"):
                    for pd in sorted(iface.glob(f"{loc}-port*")):
                        try:
                            n = int(pd.name.rsplit("port", 1)[1])
                        except ValueError:
                            continue
                        if (usb._SYSFS_USB / f"{loc}.{n}").exists():
                            continue            # occupied → known powered
                        if usb.power_cache.get((loc, n)) is not None:
                            continue            # still fresh, skip the slow read
                        v = usb._sysfs_get_power(loc, n)
                        if v is not None:
                            usb.power_cache.put((loc, n), v)
                        time.sleep(0.25)        # gentle on the bus
        except Exception as e:
            log.debug("cache warmer: %s", e)
        time.sleep(5)


class _QueueHandler(logging.Handler):
    """Routes log records from one specific thread into a Queue for SSE streaming."""

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


def _sse_flash_gen(codename: str, slot: str, cfg: dict):
    """
    Generator yielding SSE-formatted strings for a flash operation.
    Runs the flash in a daemon thread; log records from that thread are
    captured by a _QueueHandler and forwarded as SSE data events.
    """
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
            # would mark a still-running flash as finished the moment the
            # browser disconnects from the event stream.
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
            yield ": heartbeat\n\n"
            continue
        if msg is None:
            break
        for line in (msg.splitlines() or [""]):
            yield f"data: {line}\n"
        yield "\n"
    yield "event: done\ndata: complete\n\n"


def _sse_remap_gen(loc: str, port: int):
    """
    SSE generator for per-port remap triggered from the web UI.

    Steps:
      1. Power the port on.
      2. Wait up to 10 s for an ADB device to appear at this port's sysfs path.
      3. Identify codename (config lookup → ADB shell → serial fallback).
      4. Update config under _config_lock:
           • record serial → codename in cfg["serials"]
           • remove old mapping for this codename from every other port
           • set this port's mapping
      5. Run PPPS test and save result.
      If no device appears: remove any stale mapping for this port, power off.
    """
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
                        cur = adb_devices()
                        path_map = _sysfs_path_to_serial_map(set(cur.keys()))
                        s = path_map.get(sysfs_path)
                        if s and _adb_state(cur, s) == "device":
                            return s
                        el = time.monotonic() - st
                        if el >= nxt:
                            _emit(f"…waiting ({int(el)} / {secs} s)")
                            nxt += 15
                        time.sleep(1.0)
                    return None

                _emit(f"Waiting for the watch to boot and expose ADB "
                      f"(up to {wait_each} s)…")
                found_serial: str | None = _wait_for_watch(wait_each)
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
                        hub_entry = next((h for h in cfg.get("hubs", [])
                                          if h["location"] == loc), None)
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
                    try:
                        uhubctl_set_power(loc, port, False)
                    except Exception:
                        pass

            except Exception as exc:
                _emit(f"ERROR: {exc}")
            finally:
                _remap_tasks[slot]["done"] = True
                q.put(None)

    _remap_tasks[slot] = {"done": False}
    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # The worker thread's finally owns the done flag — setting it here too
    # would mark a still-running remap as finished on client disconnect.
    while True:
        try:
            msg = q.get(timeout=15)
        except queue.Empty:
            yield ": heartbeat\n\n"
            continue
        if msg is None:
            break
        for line in (msg.splitlines() or [""]):
            yield f"data: {line}\n"
        yield "\n"
    yield "event: done\ndata: complete\n\n"


def serve(args, cfg: dict):
    """Start the web UI. Requires the bottle package."""
    try:
        from bottle import Bottle, response as resp
    except ImportError:
        log.error("bottle is required for 'serve'.\n"
                  "  Arch:    sudo pacman -S python-bottle\n"
                  "  Debian:  sudo apt install python3-bottle\n"
                  "  Other:   pip install bottle")
        sys.exit(1)

    app = Bottle()

    @app.get("/")
    def index():
        return _WEB_TEMPLATE

    # Status responses are cached briefly and rebuilt under a lock so
    # parallel tabs share one uhubctl/adb scan instead of racing their own.
    status_cache = {"ts": 0.0, "body": ""}
    status_lock = threading.Lock()

    def _bust_status_cache():
        """Call after any state-changing action so the next status reflects
        it — otherwise a post-action refresh can serve the pre-action state
        for up to the cache TTL, showing rows that never matched reality."""
        status_cache["ts"] = 0.0

    @app.get("/api/status")
    def api_status():
        resp.content_type = "application/json"
        with status_lock:
            now = time.monotonic()
            if now - status_cache["ts"] > 2.0:
                c_cfg = load_config()
                charge_cfg = charge_config(c_cfg)
                status_cache["body"] = json.dumps({
                    "hubs": _web_status_data(c_cfg),
                    "thresholds": {
                        "low":  charge_cfg.low_threshold,
                        "high": charge_cfg.high_threshold,
                    },
                    "drain_floor": _DRAIN_FLOOR_PCT,
                    "wearable_min_hours": c_cfg.get("wearable_min_hours", 24),
                })
                status_cache["ts"] = now
            return status_cache["body"]

    # ── Control Center: per-watch stats + hardware toggles ────────────────────
    @app.get("/api/watch/<serial>")
    def api_watch(serial):
        """Stats + toggle states for the Control Center overlay."""
        resp.content_type = "application/json"
        return json.dumps(Watch(serial).cc_data())

    @app.post("/api/watch/<serial>/toggle/<tech>/<state>")
    def api_watch_toggle(serial, tech, state):
        resp.content_type = "application/json"
        if tech not in ("wifi", "bluetooth"):
            return json.dumps({"ok": False, "error": f"unknown toggle {tech}"})
        ok = Watch(serial).toggle(tech, state == "on")
        _bust_status_cache()
        return json.dumps({"ok": ok})

    @app.post("/api/watch/<serial>/settime")
    def api_watch_settime(serial):
        """Sync the watch clock + timezone from the host."""
        resp.content_type = "application/json"
        tz = Watch(serial).set_time_from_host()
        return json.dumps({"ok": True, "timezone": tz})

    @app.post("/api/watch/<serial>/notify")
    def api_watch_notify(serial):
        """Send a test notification to the watch."""
        resp.content_type = "application/json"
        return json.dumps({"ok": Watch(serial).notify()})

    @app.get("/api/watch/<serial>/screenshot.jpg")
    def api_watch_screenshot(serial):
        """Capture and return the watch screen as a JPEG."""
        local = Watch(serial).screenshot()
        if not local:
            resp.status = 502
            resp.content_type = "text/plain"
            return "screenshot failed"
        resp.content_type = "image/jpeg"
        return local.read_bytes()

    @app.post("/api/watch/<serial>/buzz")
    def api_watch_buzz(serial):
        """Vibrate the watch briefly — locate/identify it in a full dock."""
        resp.content_type = "application/json"
        return json.dumps({"ok": Watch(serial).buzz()})

    @app.post("/api/watch/<serial>/screen/<state>")
    def api_watch_screen(serial, state):
        """Force the screen on (demo mode) or release it."""
        resp.content_type = "application/json"
        return json.dumps({"ok": Watch(serial).screen(state == "on")})

    @app.post("/api/on/<loc>/<port:int>")
    def api_on(loc, port):
        resp.content_type = "application/json"
        try:
            confirmed = uhubctl_set_power(loc, port, True)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        _bust_status_cache()
        return json.dumps({"ok": True, "confirmed": confirmed})

    @app.post("/api/off/<loc>/<port:int>")
    def api_off(loc, port):
        resp.content_type = "application/json"
        try:
            confirmed = uhubctl_set_power(loc, port, False)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        _bust_status_cache()
        return json.dumps({"ok": True, "confirmed": confirmed})

    @app.post("/api/poweroff/<loc>/<port:int>")
    def api_poweroff(loc, port):
        """Graceful OS shutdown: adb shell poweroff, then cut USB power.

        The port is cut immediately after the shutdown command returns —
        adb shell is synchronous, so the command is already delivered, and
        the watch finishes shutting down on battery.  Any delay here races
        the halt: if the watch reaches its power-off state while VBUS is
        still live, watches without offmode charging boot right back up.
        """
        resp.content_type = "application/json"
        c_cfg = load_config()
        serial = find_serial_for_loc_port(c_cfg, loc, port)
        adb_ok = False
        if serial:
            rc, _, err = _run(f"adb -s {serial} shell poweroff",
                              check=False, timeout=10)
            adb_ok = (rc == 0)
            if not adb_ok:
                log.warning("poweroff %s:%s (%s): adb shutdown failed: %s",
                            loc, port, serial, err.strip() or f"rc={rc}")
        else:
            log.warning("poweroff %s:%s: no serial known for port — cutting "
                        "power only", loc, port)
        confirmed = False
        try:
            confirmed = uhubctl_set_power(loc, port, False)
        except RuntimeError as e:
            return json.dumps({"ok": False, "error": str(e),
                               "adb_shutdown": adb_ok})
        _bust_status_cache()
        return json.dumps({"ok": True, "adb_shutdown": adb_ok,
                           "confirmed": confirmed})

    @app.post("/api/reboot/<loc>/<port:int>")
    def api_reboot(loc, port):
        resp.content_type = "application/json"
        c_cfg = load_config()
        serial = find_serial_for_loc_port(c_cfg, loc, port)
        if not serial:
            return json.dumps({"error": "no serial found for port"})
        rc, _, err = _run(f"adb -s {serial} reboot", check=False)
        if rc != 0:
            return json.dumps({"error": err or "adb reboot failed"})
        return json.dumps({"ok": True})

    @app.post("/api/bootloader/<loc>/<port:int>")
    def api_bootloader(loc, port):
        resp.content_type = "application/json"
        c_cfg = load_config()
        serial = find_serial_for_loc_port(c_cfg, loc, port)
        if not serial:
            return json.dumps({"error": "no serial found for port"})
        rc, _, err = _run(f"adb -s {serial} reboot bootloader", check=False)
        if rc != 0:
            return json.dumps({"error": err or "adb reboot bootloader failed"})
        return json.dumps({"ok": True})

    @app.post("/api/cycle/<loc>/<port:int>")
    def api_cycle(loc, port):
        resp.content_type = "application/json"
        try:
            uhubctl_set_power(loc, port, False)
            time.sleep(5)
            uhubctl_set_power(loc, port, True)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        _bust_status_cache()
        return json.dumps({"ok": True})

    @app.post("/api/hide/<loc>/<port:int>")
    def api_hide(loc, port):
        """Toggle a port's avoid/hidden flag (user-set). Stored in the hub's
        'exclude' map, same as auto-detected excludes."""
        resp.content_type = "application/json"
        with _config_lock:
            c_cfg = load_config()
            hub = next((h for h in c_cfg.get("hubs", []) if h["location"] == loc), None)
            if hub is None:
                return json.dumps({"ok": False, "error": "hub not found"})
            excl = hub.setdefault("exclude", {})
            ps = str(port)
            if ps in excl:
                del excl[ps]
                state = False
            else:
                excl[ps] = "hidden by user"
                state = True
            save_config(c_cfg)
        _bust_status_cache()
        return json.dumps({"ok": True, "hidden": state})

    @app.post("/api/hide-hub/<loc>")
    def api_hide_hub(loc):
        """Toggle a whole hub's hidden flag (for silly/unused sub-hubs)."""
        resp.content_type = "application/json"
        with _config_lock:
            c_cfg = load_config()
            hub = next((h for h in c_cfg.get("hubs", []) if h["location"] == loc), None)
            if hub is None:
                return json.dumps({"ok": False, "error": "hub not found"})
            hub["hidden"] = not hub.get("hidden", False)
            state = hub["hidden"]
            save_config(c_cfg)
        _bust_status_cache()
        return json.dumps({"ok": True, "hidden": state})

    @app.post("/api/charge/<loc>/<port:int>")
    def api_charge(loc, port):
        resp.content_type = "application/json"
        slot = f"{loc}:{port}"
        if ChargeOp.is_active(slot):
            return json.dumps({"ok": False, "error": "charge already running",
                               "charge_end_ts":
                                   _charge_tasks[slot].get("charge_end_ts", 0)})
        c_cfg = load_config()
        err = ChargeOp.start(loc, port, c_cfg)
        if err:
            return json.dumps({"ok": False, "error": err})
        _bust_status_cache()
        return json.dumps({"ok": True, "duration_seconds":
                           charge_config(c_cfg).charge_duration_minutes * 60})

    @app.post("/api/charge/stop/<loc>/<port:int>")
    def api_charge_stop(loc, port):
        resp.content_type = "application/json"
        if ChargeOp.stop(loc, port):
            _bust_status_cache()
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "no charge running"})

    @app.post("/api/workbench/<loc>/<port:int>")
    def api_workbench(loc, port):
        resp.content_type = "application/json"
        err = WorkbenchOp.start(loc, port, load_config())
        if err:
            return json.dumps({"ok": False, "error": err})
        _bust_status_cache()
        return json.dumps({"ok": True})

    @app.post("/api/workbench/stop/<loc>/<port:int>")
    def api_workbench_stop(loc, port):
        resp.content_type = "application/json"
        if WorkbenchOp.stop(loc, port):
            _bust_status_cache()
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "no workbench active"})

    @app.get("/api/drain/history")
    def api_drain_history():
        """All recorded drain test results, newest first."""
        resp.content_type = "application/json"
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
        c_cfg = load_config()
        return json.dumps({"tests": tests,
                           "wearable_min_hours": c_cfg.get("wearable_min_hours", 24)})

    @app.post("/api/drain/<loc>/<port:int>")
    def api_drain(loc, port):
        resp.content_type = "application/json"
        err = DrainOp.start(loc, port, load_config())
        if err:
            return json.dumps({"ok": False, "error": err})
        _bust_status_cache()
        return json.dumps({"ok": True})

    @app.post("/api/drain/stop/<loc>/<port:int>")
    def api_drain_stop(loc, port):
        resp.content_type = "application/json"
        if DrainOp.stop(loc, port):
            _bust_status_cache()
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "no drain test running"})

    @app.get("/api/flash/<loc>/<port:int>")
    def api_flash(loc, port):
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        slot = f"{loc}:{port}"
        if slot in _flash_tasks and not _flash_tasks[slot].get("done", True):
            def _busy():
                yield "data: flash already in progress\n\nevent: done\ndata: \n\n"
            return _busy()
        c_cfg = load_config()
        codename = find_codename_for_loc_port(c_cfg, loc, port)
        if not codename:
            def _no_codename():
                yield "data: ERROR: port not mapped to any codename\n\nevent: done\ndata: \n\n"
            return _no_codename()
        return _sse_flash_gen(codename, slot, c_cfg)

    @app.get("/api/remap/<loc>/<port:int>")
    def api_remap(loc, port):
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        slot = f"{loc}:{port}"
        if slot in _remap_tasks and not _remap_tasks[slot].get("done", True):
            def _busy():
                yield "data: remap already in progress\n\nevent: done\ndata: \n\n"
            return _busy()
        return _sse_remap_gen(loc, port)

    class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    _resume_persisted_tasks()
    threading.Thread(target=_background_warmer, daemon=True).start()
    log.info("Port switching: %s", _sysfs_switch_mode(cfg))

    host, port = args.host, args.port
    log.info("Web UI starting on http://%s:%d/", host, port)
    print(f"asteroid-docking-bay web UI → http://{host}:{port}/")
    try:
        httpd = make_server(
            host, port, app,
            server_class=_ThreadingWSGIServer,
            handler_class=WSGIRequestHandler,
        )
    except OSError as e:
        if e.errno == 98:   # EADDRINUSE: common on 8080; guide, don't traceback
            log.error(
                "port %d is already in use by another program.\n"
                "  Pick a different port:  asteroid-docking-bay serve --port 8090\n"
                "  For the systemd unit:   systemctl --user edit "
                "asteroid-docking-bay-web.service\n"
                "    [Service]\n"
                "    ExecStart=\n"
                "    ExecStart=%%h/.local/bin/asteroid-docking-bay serve "
                "--host 0.0.0.0 --port 8090", port)
            sys.exit(1)
        raise
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Web server stopped.")


