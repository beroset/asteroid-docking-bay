# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""RPC transport for the container split (see docs/CONTAINERS.md).

Newline-delimited JSON over TCP. A request is
``{"token", "id", "op", "args"}``; a data reply is
``{"id", "ok": true, "data": …}`` (or ``ok: false, "error": …``); a
streaming op replies with ``{"id", "stream": …}`` frames ended by
``{"id", "ok": true, "done": true}``.

This module is pure transport + protocol — it knows nothing about ports,
watches or config. The op table lives in rpcops.py; a Dispatcher maps op
names to handlers, and both the in-process (monolithic) and remote (split)
callers go through the same table, so the contract has one implementation.
"""

import hmac
import json
import os
import socket
import threading
import time
from pathlib import Path

from .util import log


def load_token(token_file: "str | None") -> str:
    """The shared secret, from a file (a podman secret is a file) or the
    ADB_RPC_TOKEN environment variable. Empty string if neither is set."""
    if token_file:
        return Path(token_file).read_text().strip()
    return os.environ.get("ADB_RPC_TOKEN", "").strip()


class RpcError(Exception):
    """A handler raising this returns ``ok: false`` with its message; the
    client re-raises it. Distinct from an unexpected exception, which the
    server reports as an internal error without leaking the traceback."""


# ── framing ────────────────────────────────────────────────────────────────

def send_frame(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode())


def read_frames(sock: socket.socket):
    """Yield decoded JSON objects from a newline-delimited byte stream until
    the peer closes. Non-JSON lines are dropped (JSON never contains a raw
    newline, so the framing cannot desync)."""
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                log.warning("rpc: dropping non-JSON line (%d bytes)", len(line))


# ── token gate ─────────────────────────────────────────────────────────────

class TokenGate:
    """Constant-time token check with per-peer abuse response. A mismatch is
    never answered on the wire (no oracle for a probe); it is logged and
    counted per peer, escalating from rate-limit to a listener shutdown so a
    box under attack surfaces as a failed unit rather than staying quietly up.
    The one legitimate peer holds the token and never trips this."""

    def __init__(self, token: str, rate_limit_after: int = 5,
                 shutdown_after: int = 40):
        if not token:
            # An empty token would make compare_digest("", "") succeed —
            # i.e. no gate at all. Refuse loudly instead.
            raise ValueError("TokenGate requires a non-empty token")
        self._token = token.encode() if isinstance(token, str) else bytes(token)
        self.rate_limit_after = rate_limit_after
        self.shutdown_after = shutdown_after
        self._fails: dict[str, int] = {}
        self._lock = threading.Lock()

    def verify(self, presented, peer: str) -> str:
        """Return 'ok', 'reject', or 'shutdown'. Resets the peer's count on
        success; logs and counts on failure."""
        if isinstance(presented, str) and hmac.compare_digest(
                self._token, presented.encode()):
            with self._lock:
                self._fails.pop(peer, None)
            return "ok"
        with self._lock:
            n = self._fails[peer] = self._fails.get(peer, 0) + 1
        log.warning("rpc: invalid token from %s (failure %d)", peer, n)
        return "shutdown" if n >= self.shutdown_after else "reject"

    def backoff(self, peer: str) -> float:
        """Seconds to stall a rejected peer, growing past rate_limit_after."""
        with self._lock:
            n = self._fails.get(peer, 0)
        if n <= self.rate_limit_after:
            return 0.0
        return min(2.0, 0.25 * (n - self.rate_limit_after))


# ── dispatch table ─────────────────────────────────────────────────────────

class Dispatcher:
    """Allow-list of op name → handler. Handlers take one args dict; data
    handlers return a JSON-able value, stream handlers return an iterator of
    strings. An unknown op is a normal ``ok: false``, not a crash — adding a
    capability means registering a named op in a reviewable diff."""

    def __init__(self):
        self._data: dict = {}
        self._stream: dict = {}

    def op(self, name: str):
        def deco(fn):
            self._data[name] = fn
            return fn
        return deco

    def stream_op(self, name: str):
        def deco(fn):
            self._stream[name] = fn
            return fn
        return deco

    def dispatch(self, op, args: dict):
        """Return ('data', result) or ('stream', iterator). Raises RpcError
        for an unknown op."""
        if op in self._data:
            return "data", self._data[op](args or {})
        if op in self._stream:
            return "stream", self._stream[op](args or {})
        raise RpcError(f"unknown op: {op!r}")


# ── server ─────────────────────────────────────────────────────────────────

class RpcServer:
    """Threaded NDJSON server. One thread per connection reads requests,
    checks the token, dispatches, and writes replies. Streaming handlers get
    their frames relayed as they yield."""

    def __init__(self, host: str, port: int, token: str,
                 dispatcher: Dispatcher, gate: "TokenGate | None" = None):
        self.host, self.port = host, port
        self.dispatcher = dispatcher
        self.gate = gate or TokenGate(token)
        self._sock: "socket.socket | None" = None
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind((self.host, self.port))
        except OSError as e:
            if e.errno == 98:   # EADDRINUSE: explain, like serve does
                log.error("rpc port %d is already in use by another program "
                          "— pick a different one with --port", self.port)
                raise SystemExit(1)
            raise
        self._sock.listen(16)
        log.info("rpc backend listening on %s:%d", self.host, self.port)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr[0]),
                             daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle(self, conn: socket.socket, peer: str) -> None:
        with conn:
            for req in read_frames(conn):
                verdict = self.gate.verify(req.get("token"), peer)
                if verdict == "shutdown":
                    log.error("rpc: too many invalid tokens from %s — "
                              "shutting the listener down", peer)
                    self.shutdown()
                    return
                if verdict == "reject":
                    bo = self.gate.backoff(peer)
                    if bo:
                        time.sleep(bo)
                    continue  # no reply on the wire
                self._serve_one(conn, req)

    def _serve_one(self, conn: socket.socket, req: dict) -> None:
        rid = req.get("id")
        try:
            kind, result = self.dispatcher.dispatch(req.get("op"),
                                                    req.get("args"))
        except RpcError as e:
            send_frame(conn, {"id": rid, "ok": False, "error": str(e)})
            return
        except Exception as e:                    # handler blew up
            log.exception("rpc: op %r failed", req.get("op"))
            send_frame(conn, {"id": rid, "ok": False,
                              "error": f"internal error: {e}"})
            return
        if kind == "data":
            send_frame(conn, {"id": rid, "ok": True, "data": result})
            return
        try:
            for msg in result:
                send_frame(conn, {"id": rid, "stream": msg})
            send_frame(conn, {"id": rid, "ok": True, "done": True})
        except Exception as e:
            log.exception("rpc: stream op %r failed", req.get("op"))
            send_frame(conn, {"id": rid, "ok": False, "done": True,
                              "error": f"internal error: {e}"})


# ── callers (frontend side) ────────────────────────────────────────────────
# Both expose .call(op, args) -> data and .stream(op, args) -> iterator, so
# the web routes are identical whether the backend is in-process or remote.

class LocalCaller:
    """In-process caller for monolithic mode — dispatches straight to the
    table with no socket."""

    def __init__(self, dispatcher: Dispatcher):
        self._d = dispatcher

    def call(self, op: str, args: "dict | None" = None):
        kind, result = self._d.dispatch(op, args or {})
        if kind != "data":
            raise RpcError(f"{op} is a streaming op — use stream()")
        return result

    def stream(self, op: str, args: "dict | None" = None):
        kind, result = self._d.dispatch(op, args or {})
        if kind != "stream":
            raise RpcError(f"{op} is not a streaming op")
        return result


class RpcClient:
    """Remote caller for split mode — one short connection per request."""

    def __init__(self, host: str, port: int, token: str, timeout: float = 30):
        self.host, self.port, self.token = host, port, token
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _connect(self) -> socket.socket:
        return socket.create_connection((self.host, self.port),
                                        timeout=self.timeout)

    def call(self, op: str, args: "dict | None" = None):
        rid = self._next_id()
        try:
            with self._connect() as s:
                send_frame(s, {"token": self.token, "id": rid, "op": op,
                               "args": args or {}})
                for frame in read_frames(s):
                    if frame.get("id") != rid:
                        continue
                    if frame.get("ok"):
                        return frame.get("data")
                    raise RpcError(frame.get("error", "rpc call failed"))
        except (OSError, socket.timeout) as e:
            raise RpcError(f"backend unreachable: {e}")
        raise RpcError("no response from backend (rejected token?)")

    def stream(self, op: str, args: "dict | None" = None):
        rid = self._next_id()
        try:
            with self._connect() as s:
                send_frame(s, {"token": self.token, "id": rid, "op": op,
                               "args": args or {}})
                for frame in read_frames(s):
                    if frame.get("id") != rid:
                        continue
                    if "stream" in frame:
                        yield frame["stream"]
                    elif frame.get("done"):
                        if frame.get("ok") is False:
                            raise RpcError(frame.get("error", "stream failed"))
                        return
        except (OSError, socket.timeout) as e:
            raise RpcError(f"backend unreachable: {e}")
