# SPDX-License-Identifier: GPL-3.0-only
"""RPC transport: framing, token gate, dispatch, and a live loopback round-trip."""

import socket
import threading
import time

import pytest

from asteroid_docking_bay.rpc import (Dispatcher, LocalCaller, RpcClient,
                                      RpcError, RpcServer, TokenGate,
                                      read_frames, send_frame)


# ── framing ─────────────────────────────────────────────────────────────────

def socketpair():
    a, b = socket.socketpair()
    return a, b


def test_frame_roundtrip():
    a, b = socketpair()
    send_frame(a, {"hello": 1, "x": [1, 2, 3]})
    a.close()
    assert list(read_frames(b)) == [{"hello": 1, "x": [1, 2, 3]}]


def test_read_frames_skips_garbage_and_blanks():
    a, b = socketpair()
    a.sendall(b'\n{"ok":1}\nnot json\n{"ok":2}\n')
    a.close()
    assert list(read_frames(b)) == [{"ok": 1}, {"ok": 2}]


# ── token gate ──────────────────────────────────────────────────────────────

def test_token_accept_and_reset():
    g = TokenGate("s3cret")
    assert g.verify("s3cret", "peerA") == "ok"
    # a wrong try then a right one clears the count
    assert g.verify("wrong", "peerA") == "reject"
    assert g.verify("s3cret", "peerA") == "ok"
    assert g.backoff("peerA") == 0.0


def test_token_reject_types():
    g = TokenGate("s3cret")
    assert g.verify(None, "p") == "reject"
    assert g.verify(12345, "p") == "reject"
    assert g.verify("", "p") == "reject"


def test_token_escalation():
    g = TokenGate("s3cret", rate_limit_after=3, shutdown_after=6)
    peer = "attacker"
    for _ in range(3):
        assert g.verify("no", peer) == "reject"
    assert g.backoff(peer) == 0.0            # not yet past the rate-limit point
    assert g.verify("no", peer) == "reject"  # 4th
    assert g.backoff(peer) > 0.0             # now backing off
    for _ in range(2):
        g.verify("no", peer)                 # 5th, 6th
    assert g.verify("no", peer) == "shutdown"


def test_token_per_peer_isolation():
    g = TokenGate("s3cret", shutdown_after=2)
    assert g.verify("no", "a") == "reject"
    assert g.verify("no", "b") == "reject"   # b unaffected by a's failure
    assert g.verify("s3cret", "b") == "ok"


# ── dispatch ────────────────────────────────────────────────────────────────

def test_dispatch_data_and_stream():
    d = Dispatcher()

    @d.op("add")
    def _add(args):
        return {"sum": args["a"] + args["b"]}

    @d.stream_op("count")
    def _count(args):
        return (str(i) for i in range(args["n"]))

    assert d.dispatch("add", {"a": 2, "b": 3}) == ("data", {"sum": 5})
    kind, it = d.dispatch("count", {"n": 3})
    assert kind == "stream" and list(it) == ["0", "1", "2"]


def test_dispatch_unknown_op_raises():
    with pytest.raises(RpcError):
        Dispatcher().dispatch("nope", {})


def test_local_caller_enforces_op_kind():
    d = Dispatcher()
    d.op("x")(lambda a: 1)
    d.stream_op("s")(lambda a: iter(["a"]))
    lc = LocalCaller(d)
    assert lc.call("x") == 1
    assert list(lc.stream("s")) == ["a"]
    with pytest.raises(RpcError):
        lc.call("s")               # streaming op via call()
    with pytest.raises(RpcError):
        list(lc.stream("x"))       # data op via stream()


# ── live client ↔ server ─────────────────────────────────────────────────────

@pytest.fixture
def server():
    d = Dispatcher()

    @d.op("echo")
    def _echo(args):
        return args

    @d.op("boom")
    def _boom(args):
        raise RuntimeError("kaboom")

    @d.op("denied")
    def _denied(args):
        raise RpcError("not allowed")

    @d.stream_op("emit")
    def _emit(args):
        for i in range(args.get("n", 3)):
            yield f"line {i}"

    srv = RpcServer("127.0.0.1", 0, "tok", d)
    srv._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv._sock.bind(("127.0.0.1", 0))
    srv._sock.listen(16)
    port = srv._sock.getsockname()[1]
    t = threading.Thread(target=lambda: _accept_loop(srv), daemon=True)
    t.start()
    yield port, srv
    srv.shutdown()


def _accept_loop(srv):
    while not srv._stop.is_set():
        try:
            conn, addr = srv._sock.accept()
        except OSError:
            return
        threading.Thread(target=srv._handle, args=(conn, addr[0]),
                         daemon=True).start()


def test_client_data_call(server):
    port, _ = server
    c = RpcClient("127.0.0.1", port, "tok", timeout=5)
    assert c.call("echo", {"hi": "there"}) == {"hi": "there"}


def test_client_handler_error_maps_to_rpcerror(server):
    port, _ = server
    c = RpcClient("127.0.0.1", port, "tok", timeout=5)
    with pytest.raises(RpcError, match="not allowed"):
        c.call("denied")
    with pytest.raises(RpcError, match="internal error"):
        c.call("boom")           # unexpected exception, no traceback leak


def test_client_streaming(server):
    port, _ = server
    c = RpcClient("127.0.0.1", port, "tok", timeout=5)
    assert list(c.stream("emit", {"n": 4})) == [f"line {i}" for i in range(4)]


def test_bad_token_gets_no_reply(server):
    port, _ = server
    c = RpcClient("127.0.0.1", port, "WRONG", timeout=2)
    with pytest.raises(RpcError):     # times out into "no response / unreachable"
        c.call("echo", {"x": 1})


# ── edge cases (the ones a good tester imagines) ─────────────────────────────

def test_empty_token_is_refused():
    # compare_digest("", "") is True — an empty configured token would mean
    # no gate at all. The constructor must refuse it.
    with pytest.raises(ValueError):
        TokenGate("")


def test_frame_split_across_tcp_chunks():
    # A JSON line arriving in arbitrary TCP fragments must reassemble.
    a, b = socket.socketpair()
    payload = b'{"id": 1, "op": "x", "args": {"blob": "' + b"A" * 200000 + b'"}}\n'
    half = len(payload) // 2
    a.sendall(payload[:half])
    got = []
    t = threading.Thread(target=lambda: got.extend(read_frames(b)))
    t.start()
    time.sleep(0.05)
    a.sendall(payload[half:])
    a.close()
    t.join(timeout=5)
    assert len(got) == 1 and len(got[0]["args"]["blob"]) == 200000


def test_unicode_token_roundtrip():
    g = TokenGate("s3crét-⚙")
    assert g.verify("s3crét-⚙", "p") == "ok"
    assert g.verify("s3cret-x", "p") == "reject"


def test_detector_threshold_one():
    from asteroid_docking_bay.ops import ChargeDropDetector
    d = ChargeDropDetector(50, threshold=1)
    assert d.feed(49) == "alarm"          # a single drop alarms immediately
    assert d.feed(50) == "recovered"


def test_parse_adb_duplicate_serial_last_wins():
    from asteroid_docking_bay.adb import parse_adb_devices
    out = ("List of devices attached\n"
           "S1 offline usb:1-1\n"
           "S1 device usb:1-2\n")
    devices = parse_adb_devices(out)
    assert devices["S1"]["status"] == "device"
    assert devices["S1"]["usb"] == "1-2"


def test_parse_adb_token_with_extra_colons():
    from asteroid_docking_bay.adb import parse_adb_devices
    out = "List of devices attached\nS1 device weird:a:b:c\n"
    assert parse_adb_devices(out)["S1"]["weird"] == "a:b:c"


def test_configmanager_corrupt_file_raises(tmp_path):
    # Pinned CURRENT behavior: a corrupt config crashes loudly rather than
    # silently starting with defaults (which would wipe the mapping on the
    # next save). If this ever changes, it must be a conscious decision —
    # see the 0.6 queue.
    import json as _json
    from asteroid_docking_bay.config import ConfigManager
    f = tmp_path / "config.json"
    f.write_text('{"hubs": [BROKEN')
    with pytest.raises(_json.JSONDecodeError):
        ConfigManager(f).load()


def test_next_due_boundary_headroom_zero(tmp_path):
    # A watch sitting exactly at low+margin is due NOW (not in the future).
    import json as _json
    import time as _time
    from asteroid_docking_bay.events import EventLog
    el = EventLog(tmp_path / "ev")
    now = _time.time()
    evs = [{"event": "check_reading", "ts": now - 7200, "pct": 51},
           {"event": "check_reading", "ts": now - 3600, "pct": 50}]
    (tmp_path / "ev").mkdir()
    (tmp_path / "ev" / "S.jsonl").write_text(
        "".join(_json.dumps(e) + "\n" for e in evs))
    cfg = {"charge": {"low_threshold": 40, "adaptive_margin_pct": 10}}
    due = el.next_due_ts("S", None, cfg)
    assert due == evs[-1]["ts"]           # due at the last reading, i.e. now
