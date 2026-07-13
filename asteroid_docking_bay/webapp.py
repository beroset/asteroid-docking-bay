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
from .usb import _sysfs_switch_mode
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
    ("POST", "/api/watch/<serial>/settime",        "watch.settime",   {},             False),
    ("POST", "/api/watch/<serial>/notify",         "watch.notify",    {},             False),
    ("POST", "/api/watch/<serial>/buzz",           "watch.buzz",      {},             False),
    ("POST", "/api/on/<loc>/<port:int>",           "port.set",        {"on": True},   True),
    ("POST", "/api/off/<loc>/<port:int>",          "port.set",        {"on": False},  True),
    ("POST", "/api/poweroff/<loc>/<port:int>",     "port.poweroff",   {},             True),
    ("POST", "/api/reboot/<loc>/<port:int>",       "port.reboot",     {},             False),
    ("POST", "/api/bootloader/<loc>/<port:int>",   "port.bootloader", {},             False),
    ("POST", "/api/cycle/<loc>/<port:int>",        "port.cycle",      {},             True),
    ("POST", "/api/hide/<loc>/<port:int>",         "port.hide",       {},             True),
    ("POST", "/api/hide-hub/<loc>",                "hub.hide",        {},             True),
    ("POST", "/api/charge/<loc>/<port:int>",       "charge.start",    {},             True),
    ("POST", "/api/charge/stop/<loc>/<port:int>",  "charge.stop",     {},             True),
    ("POST", "/api/workbench/<loc>/<port:int>",    "workbench.start", {},             True),
    ("POST", "/api/workbench/stop/<loc>/<port:int>", "workbench.stop", {},            True),
    ("POST", "/api/drain/<loc>/<port:int>",        "drain.start",     {},             True),
    ("POST", "/api/drain/stop/<loc>/<port:int>",   "drain.stop",      {},             True),
    ("GET",  "/api/drain/history",                 "drain.history",   {},             False),
]


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
            if now - status_cache["ts"] > 2.0:
                status_cache["body"] = json.dumps(_call("status.get"))
                status_cache["ts"] = now
            return status_cache["body"]

    @app.post("/api/watch/<serial>/toggle/<tech>/<state>")
    def api_watch_toggle(serial, tech, state):
        resp.content_type = "application/json"
        d = _call("watch.toggle", {"serial": serial, "tech": tech, "on": state == "on"})
        _bust_status_cache()
        return json.dumps(d)

    @app.get("/api/watch/<serial>/screenshot.jpg")
    def api_watch_screenshot(serial):
        d = _call("watch.screenshot", {"serial": serial})
        if not d.get("ok"):
            resp.status = 502
            resp.content_type = "text/plain"
            return d.get("error", "screenshot failed")
        resp.content_type = "image/jpeg"
        return base64.b64decode(d["jpeg_b64"])

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


