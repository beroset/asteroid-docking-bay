# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Bottle app factory, SSE bridge, status cache.

The web server is a thin frontend: routes parse HTTP and dispatch through a
caller (in-process LocalCaller, or an RpcClient in --backend split mode) to
the op table in rpcops. It touches no hardware itself; in monolithic mode it
starts the ops machinery (resume + warmer) that the split backend otherwise
owns."""

from __future__ import annotations

import base64
import json
import signal
import sys
import threading
import time
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from .util import log
from .config import _config_lock, load_config, save_config, seed_hub_names
from .usb import _sysfs_switch_mode, hub_vendors, usb_topology_fingerprint
from .ops import _background_warmer, _resume_persisted_tasks
from .webtemplate import _WEB_TEMPLATE


# The uniform JSON route⇄op contract as data: every entry becomes a route
# whose URL parameters flow into the op's args by name, merged with the
# static args. `bust` invalidates the status cache after state changes.
# Non-uniform routes (page, status cache, screenshot binary, on/off state
# words, SSE streams) stay explicit in serve() below. Tests import this
# table — it is the contract, not just wiring.
_JSON_ROUTES = [
    # method, path,                                op,                static args,    bust
    ("GET",  "/api/watch/<serial>",                "watch.cc",        {},             False),
    ("GET",  "/api/watch/<serial>/stale",          "watch.cc",        {"stale": True}, False),
    ("GET",  "/api/watch/<serial>/timeline",       "watch.timeline",  {},             False),
    ("GET",  "/api/watch/<serial>/bootchart",      "watch.bootchart", {},             False),
    ("GET",  "/api/watch/<serial>/diag",           "watch.diag",      {},             False),
    ("POST", "/api/watch/<serial>/wake/<kind>/<value>", "watch.wake_set", {},         False),
    ("POST", "/api/watch/<serial>/locale/<locale>",    "watch.locale_set", {},        False),
    ("POST", "/api/watch/<serial>/bench/app/<action>", "bench.app",      {},             False),
    ("GET",  "/api/watch/<serial>/settings",       "watch.settings_read", {},         False),
    ("GET",  "/api/watch/<serial>/hands",          "watch.hands",     {},             False),
    ("GET",  "/api/weather",                       "weather.get",     {},             False),
    ("POST", "/api/watch/<serial>/weather-sync",   "watch.weather_sync", {},          False),
    ("GET",  "/api/watch/<serial>/weather-on-watch", "watch.weather_read", {},         False),
    ("POST", "/api/watch/<serial>/settime",        "watch.settime",   {},             False),
    ("POST", "/api/watch/<serial>/notify",         "watch.notify",    {},             False),
    ("POST", "/api/watch/<serial>/buzz",           "watch.buzz",      {},             False),
    ("POST", "/api/on/<loc>/<port:int>",           "port.set",        {"on": True},   True),
    ("POST", "/api/off/<loc>/<port:int>",          "port.set",        {"on": False},  True),
    ("POST", "/api/poweroff/<loc>/<port:int>",     "port.poweroff",   {},             True),
    ("POST", "/api/reboot/<loc>/<port:int>",       "port.reboot",     {},             False),
    ("POST", "/api/bootloader/<loc>/<port:int>",   "port.bootloader", {},             False),
    ("POST", "/api/recovery/<loc>/<port:int>",     "port.recovery",   {},             False),
    ("POST", "/api/continue/<loc>/<port:int>",     "port.continue",   {},             False),
    ("POST", "/api/cycle/<loc>/<port:int>",        "port.cycle",      {},             True),
    ("POST", "/api/hide/<loc>/<port:int>",         "port.hide",       {},             True),
    ("POST", "/api/hide-hub/<loc>",                "hub.hide",        {},             True),
    ("POST", "/api/charge/<loc>/<port:int>",       "charge.start",    {},             True),
    ("POST", "/api/charge/stop/<loc>/<port:int>",  "charge.stop",     {},             True),
    ("POST", "/api/workbench/<loc>/<port:int>",    "workbench.start", {},             True),
    ("POST", "/api/workbench/stop/<loc>/<port:int>", "workbench.stop", {},            True),
    ("POST", "/api/wear/on/<loc>/<port:int>",       "wear.set", {"on": True},  True),
    ("POST", "/api/wear/off/<loc>/<port:int>",      "wear.set", {"on": False}, True),
    ("POST", "/api/drain/<loc>/<port:int>",        "drain.start",     {},             True),
    ("POST", "/api/drain/stop/<loc>/<port:int>",   "drain.stop",      {},             True),
    ("POST", "/api/backup/<loc>/<port:int>",       "watch.backup",    {},             False),
    ("POST", "/api/restore/<loc>/<port:int>",      "watch.restore",   {},             False),
    ("POST", "/api/diagnostics/<loc>/<port:int>",  "watch.diagnostics", {},           False),
    ("POST", "/api/fbreport/<loc>/<port:int>",     "watch.fbreport",  {},             False),
    ("POST", "/api/switch-adb",                    "ssh.switch_adb",  {},             True),
    ("POST", "/api/switch-adb/<serial>",          "ssh.switch_adb",  {},             True),
    ("POST", "/api/switch-ssh/<serial>",          "watch.switch_ssh", {},            True),
    ("POST", "/api/usb-preference/<mode>",         "prefs.set_usb_mode", {},          True),
    ("POST", "/api/screen/release-all",            "screen.release_all", {},          True),
    ("GET",  "/api/drain/history",                 "drain.history",   {},             False),
]


def serve(args, cfg: dict):
    """Start the web UI. Requires the bottle package."""
    try:
        from bottle import Bottle, request, response as resp
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

    # udev tells us about a docking watch in milliseconds; polling can sit a
    # whole interval behind it. The monitor only busts this cache — it never
    # drives an operation, so a noisy bus cannot turn into a storm of actions.
    from .usbevents import UsbEventMonitor
    _usb_monitor = UsbEventMonitor(_bust_status_cache)
    _usb_monitor.start()

    from .rpc import LocalCaller, RpcClient, RpcError, load_token
    from . import rpcops
    # Split mode (--backend host:port) proxies every op to a remote backend;
    # otherwise the ops run in-process. The routes are identical either way.
    split = bool(getattr(args, "backend", None))
    if split:
        host, _, port = args.backend.rpartition(":")
        caller = RpcClient(host or "127.0.0.1", int(port),
                           load_token(getattr(args, "token_file", None)))
        log.info("frontend proxying to backend at %s", args.backend)
    else:
        caller = LocalCaller(rpcops.DISPATCH)

    def _call(op, args=None):
        try:
            return caller.call(op, args or {})
        except RpcError as e:
            return {"ok": False, "error": str(e)}

    def _sse(op, args):
        """Bridge a backend stream op onto the browser's SSE channel: a raw
        message becomes data event line(s), an empty string a keep-alive
        heartbeat. Works identically for the in-process and remote callers."""
        try:
            for msg in caller.stream(op, args):
                if msg == "":
                    yield ": heartbeat\n\n"
                else:
                    for line in (msg.splitlines() or [""]):
                        yield f"data: {line}\n"
                    yield "\n"
        except RpcError as e:
            yield f"data: ERROR: {e}\n\n"
        yield "event: done\ndata: complete\n\n"

    def _register(method, path, op, static, bust):
        def handler(**url_args):
            resp.content_type = "application/json"
            result = _call(op, {**url_args, **static})
            if bust:
                _bust_status_cache()
            return json.dumps(result)
        app.route(path, method=method, callback=handler)

    for spec in _JSON_ROUTES:
        _register(*spec)

    @app.get("/api/status")
    def api_status():
        resp.content_type = "application/json"
        with status_lock:
            now = time.monotonic()
            # The TTL caps the cost of the expensive per-watch reads; the
            # topology fingerprint (a ~1ms listing) overrides it the moment a
            # device appears or vanishes, so enumeration changes never wait
            # out the cache — they show on the very next poll.
            # CAUTION (mo, 2026-07-26): this + the 3s client poll raised the
            # rebuild rate ~5x over the uhubctl-era tuning (up to one adb/
            # sysfs sweep per ~3s, and every enumeration event triggers one
            # immediately — a churn storm means back-to-back rebuilds). If
            # adb-server crashes or hub wedges (re)appear, suspect THIS
            # pressure first: slow the poll (webtemplate setInterval) and/or
            # drop the fingerprint bust to test before deeper debugging.
            fp = usb_topology_fingerprint()
            if now - status_cache["ts"] > 2.0 or fp != status_cache.get("fp"):
                status_cache["body"] = json.dumps(_call("status.get"))
                status_cache["ts"] = now
                status_cache["fp"] = fp
            return status_cache["body"]

    # Hub rename: the name is free text (spaces, empty to clear), so it rides the
    # query string rather than the path; the prefix is the physical box address.
    @app.post("/api/rename-hub/<prefix>")
    def api_rename_hub(prefix):
        resp.content_type = "application/json"
        d = _call("hub.rename", {"prefix": prefix, "name": request.query.get("name", "")})
        _bust_status_cache()
        return json.dumps(d)

    # Set a port's physical socket number (blank clears); value rides the query.
    @app.post("/api/socket/<loc>/<port:int>")
    def api_set_socket(loc, port):
        resp.content_type = "application/json"
        d = _call("socket.set", {"loc": loc, "port": port, "n": request.query.get("n", "")})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/watch/<serial>/toggle/<tech>/<state>")
    def api_watch_toggle(serial, tech, state):
        resp.content_type = "application/json"
        d = _call("watch.toggle", {"serial": serial, "tech": tech, "on": state == "on"})
        _bust_status_cache()
        return json.dumps(d)

    # The dconf key carries slashes, so it is the trailing <key:path>; the value
    # is the on/off segment before it (only booleans are writable).
    @app.post("/api/watch/<serial>/setting/<state>/<key:path>")
    def api_watch_setting(serial, state, key):
        resp.content_type = "application/json"
        d = _call("watch.settings_write",
                  {"serial": serial, "key": "/" + key, "value": state == "on"})
        return json.dumps(d)

    # <when> is a URL-encoded 'YYYY-MM-DD HH:MM:SS' (bottle decodes it); the op
    # revalidates the format before it reaches the shell.
    @app.post("/api/watch/<serial>/datetime/<when>")
    def api_watch_datetime(serial, when):
        resp.content_type = "application/json"
        d = _call("watch.set_datetime", {"serial": serial, "when": when})
        return json.dumps(d)

    # Move a hands watch's physical hands to <when> (URL-encoded YYYY-MM-DD HH:MM:SS).
    @app.post("/api/watch/<serial>/set-hands/<when>")
    def api_watch_set_hands(serial, when):
        resp.content_type = "application/json"
        d = _call("watch.set_hands", {"serial": serial, "when": when})
        return json.dumps(d)

    # Drive a hands watch's motors to absolute positions (minute, hour; 0..179).
    @app.post("/api/watch/<serial>/hands-move/<m>/<h>")
    def api_watch_hands_move(serial, m, h):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.hands_move",
                                {"serial": serial, "m": m, "h": h}))

    # Persist a hands watch's per-hand motor-zero calibration (degrees).
    @app.post("/api/watch/<serial>/hands-cal/<min_deg>/<hr_deg>")
    def api_watch_hands_cal(serial, min_deg, hr_deg):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.set_hands_cal",
                                {"serial": serial, "min_deg": min_deg,
                                 "hr_deg": hr_deg}))

    @app.get("/api/watch/<serial>/av")
    def api_watch_av(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.av_read", {"serial": serial}))

    @app.post("/api/watch/<serial>/brightness/<pct>")
    def api_watch_brightness(serial, pct):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.set_brightness", {"serial": serial, "pct": pct}))

    @app.post("/api/watch/<serial>/volume/<pct>")
    def api_watch_volume(serial, pct):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.set_volume", {"serial": serial, "pct": pct}))

    @app.post("/api/watch/<serial>/mute/<state>")
    def api_watch_mute(serial, state):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.set_mute", {"serial": serial, "on": state == "on"}))

    @app.post("/api/watch/<serial>/record/<seconds>")
    def api_watch_record(serial, seconds):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.record_audio", {"serial": serial, "seconds": seconds}))

    @app.get("/api/watch/<serial>/recording.wav")
    def api_watch_recording(serial):
        import tempfile
        from pathlib import Path
        p = Path(tempfile.gettempdir()) / f"dockingbay_rec_{serial}.wav"
        if not p.exists():
            resp.status = 404
            resp.content_type = "text/plain"
            return "no recording"
        resp.content_type = "audio/wav"
        resp.set_header("Cache-Control", "no-cache")
        return p.read_bytes()

    @app.post("/api/watch/<serial>/quickpanel/<tid>/<state>")
    def api_watch_quickpanel(serial, tid, state):
        resp.content_type = "application/json"
        d = _call("watch.quickpanel_set",
                  {"serial": serial, "id": tid, "on": state == "on"})
        return json.dumps(d)

    # <city> is a URL-encoded place name (bottle decodes it); geocoded server-side.
    @app.post("/api/weather/location/<city>")
    def api_weather_location(city):
        resp.content_type = "application/json"
        d = _call("weather.set_location", {"city": city})
        return json.dumps(d)

    # Physical-hands art for a hands watch (narwhal): the hands-removed base
    # photo + the hour/minute hand SVGs, locally placed in the watch-image cache
    # (like the -trans.png cutouts). Overlaid + rotated by the live view.
    @app.get("/api/watch-hand/<codename>/<part>")
    def api_watch_hand(codename, part):
        from .watchimg import _CACHE_DIR
        names = {"base": (f"{codename}-trans-dot.png", "image/png"),
                 "hour": (f"{codename}-hour.svg", "image/svg+xml"),
                 "minute": (f"{codename}-minute.svg", "image/svg+xml")}
        entry = names.get(part)
        path = (_CACHE_DIR / entry[0]) if entry else None
        if not path or not path.exists():
            resp.status = 404
            resp.content_type = "text/plain"
            return "no such hand asset"
        resp.content_type = entry[1]
        resp.headers["Cache-Control"] = "max-age=3600"
        return path.read_bytes()

    @app.get("/api/registry")
    def api_registry():
        resp.content_type = "application/json"
        return json.dumps(_call("registry.get"))

    @app.post("/api/bt/scan/<seconds>")
    def api_bt_scan(seconds):
        resp.content_type = "application/json"
        return json.dumps(_call("bt.scan", {"seconds": seconds}))

    @app.post("/api/bt/pair/<mac>")
    def api_bt_pair(mac):
        resp.content_type = "application/json"
        return json.dumps(_call("bt.pair", {"mac": mac}))

    @app.post("/api/orbit/launch/<ip>")
    def api_orbit_launch(ip):
        resp.content_type = "application/json"
        return json.dumps(_call("orbit.launch", {"ip": ip}))

    @app.post("/api/orbit/deorbit/<serial>")
    def api_orbit_deorbit(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("orbit.deorbit", {"serial": serial}))

    @app.get("/api/watch/<serial>/screenshot.jpg")
    def api_watch_screenshot(serial):
        d = _call("watch.screenshot", {"serial": serial})
        if not d.get("ok"):
            resp.status = 502
            resp.content_type = "text/plain"
            return d.get("error", "screenshot failed")
        resp.content_type = "image/jpeg"
        if d.get("stale"):
            resp.set_header("X-Screenshot-Stale", "1")
        if d.get("captured_ts"):
            resp.set_header("X-Screenshot-Ts", str(int(d["captured_ts"])))
        return base64.b64decode(d["jpeg_b64"])

    @app.get("/api/diagnostics/download/<name>")
    def api_diag_download(name):
        # Serve a diagnostics bundle for download. Basename-only + a .tar.gz
        # gate keeps this scoped to the diagnostics dir (no path traversal).
        from pathlib import Path
        from .watchctl import DIAG_ROOT
        safe = Path(name).name
        f = DIAG_ROOT / safe
        if not (safe.endswith((".tar.gz", ".txt")) and f.is_file()):
            resp.status = 404
            resp.content_type = "text/plain"
            return b""
        resp.content_type = ("text/plain" if safe.endswith(".txt")
                             else "application/gzip")
        resp.set_header("Content-Disposition", f'attachment; filename="{safe}"')
        return f.read_bytes()

    @app.get("/api/watch-image/<codename>")
    def api_watch_image(codename):
        d = _call("watch.image", {"codename": codename})
        if not (isinstance(d, dict) and d.get("ok")):
            resp.status = 404
            resp.content_type = "text/plain"
            return b""
        resp.content_type = "image/png"
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return base64.b64decode(d["png_b64"])

    @app.post("/api/watch/<serial>/screen/<state>")
    def api_watch_screen(serial, state):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.screen", {"serial": serial, "on": state == "on"}))

    def _event_stream_headers():
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"

    @app.get("/api/flash/<loc>/<port:int>")
    def api_flash(loc, port):
        _event_stream_headers()
        args = {"loc": loc, "port": port}
        channel = request.query.get("channel")   # e.g. "2.1"; absent = nightly
        if channel:
            args["channel"] = channel
        return _sse("flash.start", args)

    @app.get("/api/remap/<loc>/<port:int>")
    def api_remap(loc, port):
        _event_stream_headers()
        return _sse("onboard.start", {"loc": loc, "port": port})

    # Onboard sweep: POST prepare (power every socket down), then GET run (SSE
    # streams onboarding each equipped socket one at a time).
    @app.post("/api/onboard-sweep/skip")
    def api_sweep_skip():
        resp.content_type = "application/json"
        return json.dumps(_call("onboard.sweep_skip"))

    @app.post("/api/onboard-sweep/prepare")
    def api_sweep_prepare():
        resp.content_type = "application/json"
        d = _call("onboard.sweep_prepare")
        _bust_status_cache()
        return json.dumps(d)

    @app.get("/api/onboard-sweep/run")
    def api_sweep_run():
        _event_stream_headers()
        return _sse("onboard.sweep_run", {})

    class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    # The operations and their caches live wherever the ops actually run:
    # in-process for the monolith, in the backend container for the split.
    if not split:
        _resume_persisted_tasks()
        threading.Thread(target=_background_warmer, daemon=True).start()
        log.info("Port switching: %s", _sysfs_switch_mode(cfg))
        # First run on this host: auto-name the physical hubs so the UI rows say
        # which box (A16 #1, the dock, …) a port sits on. Editable in the UI.
        with _config_lock:
            fresh = load_config()
            if seed_hub_names(fresh, hub_vendors()):
                save_config(fresh)
                log.info("auto-named hubs: %s", fresh["hub_names"])

    # PID-1 duty in the frontend container: exit on SIGTERM (see backend).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

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


