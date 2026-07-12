# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Bottle app factory, SSE bridge, status cache.

The web server is a thin frontend: routes parse HTTP and dispatch through a
caller (in-process LocalCaller, or an RpcClient in --backend split mode) to
the op table in rpcops. It touches no hardware itself; in monolithic mode it
starts the ops machinery (resume + warmer) that the split backend otherwise
owns."""

import base64
import json
import sys
import threading
import time
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

from .util import log
from .config import load_config
from .usb import _sysfs_switch_mode
from .ops import _background_warmer, _resume_persisted_tasks
from .webtemplate import _WEB_TEMPLATE


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

    @app.get("/api/status")
    def api_status():
        resp.content_type = "application/json"
        with status_lock:
            now = time.monotonic()
            if now - status_cache["ts"] > 2.0:
                status_cache["body"] = json.dumps(_call("status.get"))
                status_cache["ts"] = now
            return status_cache["body"]

    @app.get("/api/watch/<serial>")
    def api_watch(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.cc", {"serial": serial}))

    @app.post("/api/watch/<serial>/toggle/<tech>/<state>")
    def api_watch_toggle(serial, tech, state):
        resp.content_type = "application/json"
        d = _call("watch.toggle", {"serial": serial, "tech": tech, "on": state == "on"})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/watch/<serial>/settime")
    def api_watch_settime(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.settime", {"serial": serial}))

    @app.post("/api/watch/<serial>/notify")
    def api_watch_notify(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.notify", {"serial": serial}))

    @app.get("/api/watch/<serial>/screenshot.jpg")
    def api_watch_screenshot(serial):
        d = _call("watch.screenshot", {"serial": serial})
        if not d.get("ok"):
            resp.status = 502
            resp.content_type = "text/plain"
            return d.get("error", "screenshot failed")
        resp.content_type = "image/jpeg"
        return base64.b64decode(d["jpeg_b64"])

    @app.post("/api/watch/<serial>/buzz")
    def api_watch_buzz(serial):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.buzz", {"serial": serial}))

    @app.post("/api/watch/<serial>/screen/<state>")
    def api_watch_screen(serial, state):
        resp.content_type = "application/json"
        return json.dumps(_call("watch.screen", {"serial": serial, "on": state == "on"}))

    @app.post("/api/on/<loc>/<port:int>")
    def api_on(loc, port):
        resp.content_type = "application/json"
        d = _call("port.set", {"loc": loc, "port": port, "on": True})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/off/<loc>/<port:int>")
    def api_off(loc, port):
        resp.content_type = "application/json"
        d = _call("port.set", {"loc": loc, "port": port, "on": False})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/poweroff/<loc>/<port:int>")
    def api_poweroff(loc, port):
        resp.content_type = "application/json"
        d = _call("port.poweroff", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/reboot/<loc>/<port:int>")
    def api_reboot(loc, port):
        resp.content_type = "application/json"
        return json.dumps(_call("port.reboot", {"loc": loc, "port": port}))

    @app.post("/api/bootloader/<loc>/<port:int>")
    def api_bootloader(loc, port):
        resp.content_type = "application/json"
        return json.dumps(_call("port.bootloader", {"loc": loc, "port": port}))

    @app.post("/api/cycle/<loc>/<port:int>")
    def api_cycle(loc, port):
        resp.content_type = "application/json"
        d = _call("port.cycle", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/hide/<loc>/<port:int>")
    def api_hide(loc, port):
        resp.content_type = "application/json"
        d = _call("port.hide", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/hide-hub/<loc>")
    def api_hide_hub(loc):
        resp.content_type = "application/json"
        d = _call("hub.hide", {"loc": loc})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/charge/<loc>/<port:int>")
    def api_charge(loc, port):
        resp.content_type = "application/json"
        d = _call("charge.start", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/charge/stop/<loc>/<port:int>")
    def api_charge_stop(loc, port):
        resp.content_type = "application/json"
        d = _call("charge.stop", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/workbench/<loc>/<port:int>")
    def api_workbench(loc, port):
        resp.content_type = "application/json"
        d = _call("workbench.start", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/workbench/stop/<loc>/<port:int>")
    def api_workbench_stop(loc, port):
        resp.content_type = "application/json"
        d = _call("workbench.stop", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.get("/api/drain/history")
    def api_drain_history():
        resp.content_type = "application/json"
        return json.dumps(_call("drain.history"))

    @app.post("/api/drain/<loc>/<port:int>")
    def api_drain(loc, port):
        resp.content_type = "application/json"
        d = _call("drain.start", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    @app.post("/api/drain/stop/<loc>/<port:int>")
    def api_drain_stop(loc, port):
        resp.content_type = "application/json"
        d = _call("drain.stop", {"loc": loc, "port": port})
        _bust_status_cache()
        return json.dumps(d)

    def _event_stream_headers():
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"

    @app.get("/api/flash/<loc>/<port:int>")
    def api_flash(loc, port):
        _event_stream_headers()
        return _sse("flash.start", {"loc": loc, "port": port})

    @app.get("/api/remap/<loc>/<port:int>")
    def api_remap(loc, port):
        _event_stream_headers()
        return _sse("onboard.start", {"loc": loc, "port": port})

    class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    # The operations and their caches live wherever the ops actually run:
    # in-process for the monolith, in the backend container for the split.
    if not split:
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


