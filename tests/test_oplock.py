# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""The 'leave this watch alone' marker, and the housekeeping that must obey it.

The failure this prevents is silent: a-d-b switched a watch's USB mode 45
seconds before a 3.9 GB dump started, and the dump produced a 0-byte file that
looks perfectly fine in a directory listing.
"""

import time

from asteroid_docking_bay import oplock


def test_a_lock_is_live_until_it_expires():
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "since": now, "until": now + 60}}}
    assert oplock.held(cfg, "S1")["kind"] == "dump"
    assert oplock.held(cfg, "OTHER") is None
    assert oplock.held(cfg, None) is None
    assert oplock.held({}, "S1") is None


def test_an_expired_lock_stops_holding_without_anyone_sweeping_it():
    """A crashed holder never releases. If expiry needed a periodic sweeper to
    run, one dead process would exempt a watch from all housekeeping forever —
    and it would fail silently, which is the worst way for a guard to fail."""
    old = time.time() - 10_000
    cfg = {"op_locks": {"S1": {"kind": "dump", "since": old, "until": old + 60}}}
    assert oplock.held(cfg, "S1") is None
    assert oplock.all_held(cfg) == {}, "the UI would show a hold that has lapsed"


def test_all_held_reports_only_live_locks():
    now = time.time()
    cfg = {"op_locks": {
        "LIVE": {"kind": "flash", "since": now, "until": now + 600},
        "DEAD": {"kind": "dump", "since": now - 9999, "until": now - 9000},
    }}
    assert list(oplock.all_held(cfg)) == ["LIVE"]


def test_describe_says_how_long_it_has_been_held():
    """A lock sitting for hours is usually a crashed holder. Saying so beats
    making a human work it out from a timestamp."""
    lock = {"kind": "dump", "note": "run 1 of 2", "since": time.time() - 3600}
    text = oplock.describe(lock)
    assert "dump" in text and "60 min" in text and "run 1 of 2" in text
    assert oplock.describe(None) == ""


# ── the three housekeeping paths that must obey it ───────────────────────────

def test_the_stray_peeler_leaves_a_held_watch_alone(monkeypatch):
    """THE REGRESSION, 2026-08-03: this peeler switched nemo to adb while a
    3.9 GB SSH dump was starting. The dump wrote 0 bytes."""
    from asteroid_docking_bay import ops
    switched = []
    monkeypatch.setattr(ops, "rndis_links", lambda: [{"serial": "S1", "iface": "e0"}])
    monkeypatch.setattr(ops, "_stray_ssh_to_realign", lambda *a: "S1")
    monkeypatch.setattr(ops, "_route_winner_iface", lambda: "e0")
    monkeypatch.setattr(ops, "_detect_rndis", lambda ip: True)
    monkeypatch.setattr(ops, "_switch_ssh_to_adb",
                        lambda *a, **k: switched.append(1) or {"ok": True})
    ops._last_ssh_realign = 0.0
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "since": now, "until": now + 600}}}
    ops._maybe_realign_stray_ssh(cfg)
    assert switched == [], "peeled a watch that was mid-operation"

    # Without the lock it still peels — the guard must not disable the feature.
    ops._last_ssh_realign = 0.0
    ops._maybe_realign_stray_ssh({})
    assert switched == [1]


def test_the_mode_aligner_leaves_a_held_watch_alone(monkeypatch):
    from asteroid_docking_bay import webstatus as ws
    spawned = []

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            spawned.append(getattr(target, "__name__", target))

        def start(self):
            pass

    monkeypatch.setattr(ws.threading, "Thread", _T)
    ws._ssh_align_attempt.clear()
    now = time.time()
    cfg = {"ssh_ips": {}, "usb_mode_preference": "adb",
           "op_locks": {"S1": {"kind": "flash", "since": now, "until": now + 600}}}
    ws._maybe_align_usb_mode("S1", "ssh", cfg)
    assert spawned == [], "aligned a watch that was mid-operation"


def test_the_fake_power_self_heal_leaves_a_held_watch_alone(monkeypatch):
    """Cutting power to a held watch is worse than the wedge it would fix.

    Every other precondition is satisfied on purpose — self-heal enabled, the
    grace period already elapsed, no backoff — so the ONLY thing standing
    between this call and a power cycle is the lock. Without that setup the
    test passed even with the guard removed, because the call returned early
    on the disabled setting instead.
    """
    import types
    from asteroid_docking_bay import webstatus as ws
    spawned = []

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            spawned.append(args)

        def start(self):
            pass

    monkeypatch.setattr(ws.threading, "Thread", _T)
    monkeypatch.setattr(ws, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(ws, "charge_config",
                        lambda c: types.SimpleNamespace(fake_power_self_heal=True))
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "since": now, "until": now + 600}}}
    ws._fake_power_since.clear(); ws._fake_power_cycled.clear()
    ws._fake_power_cycles.clear()
    ws._fake_power_since["1-3:1"] = now - 10_000        # grace long since elapsed

    ws._maybe_self_heal_fake_power("1-3:1", "1-3", 1, wedged=True, busy=False, cfg=cfg)
    assert spawned == [], "power-cycled a watch that was mid-operation"

    # Same call with no lock DOES cycle — the guard must not disable the feature.
    ws._fake_power_since["1-3:1"] = now - 10_000
    ws._maybe_self_heal_fake_power("1-3:1", "1-3", 1, wedged=True, busy=False, cfg={})
    assert spawned == [("1-3", 1)], spawned


def test_ops_refuse_while_a_watch_is_held(monkeypatch):
    """The guard also faces the human: a person clicking Reboot mid-dump makes
    the same collision, just slower."""
    from asteroid_docking_bay import rpcops
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "note": "run 2",
                               "since": now, "until": now + 600}}}
    monkeypatch.setattr(rpcops, "load_config", lambda: cfg)
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    res = rpcops._refuse_if_busy("1-3", 1)
    assert res and res["ok"] is False and res["busy"] == "dump"
    assert "run 2" in res["error"]

    # An unheld port is not refused by this guard.
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "OTHER")
    monkeypatch.setattr(rpcops, "_op_owning", lambda l, p: None)
    assert rpcops._refuse_if_busy("1-3", 1) is None
