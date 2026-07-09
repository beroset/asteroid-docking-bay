# SPDX-License-Identifier: GPL-3.0-only
"""ChargeDropDetector — the losing-power alarm state machine."""

from asteroid_docking_bay.ops import ChargeDropDetector


def run(seq):
    d = ChargeDropDetector(seq[0])
    events = [v for p in seq[1:] if (v := d.feed(p))]
    return d, events


def test_normal_charge_never_alarms():
    d, events = run([50, 52, 54, 56])
    assert not d.alarmed and events == []


def test_consecutive_drops_alarm_once():
    d, events = run([50, 49, 48, 47])
    assert d.alarmed and events == ["alarm"]   # raised once, not re-raised


def test_single_dip_recovers_silently():
    d, events = run([50, 49, 50])
    assert not d.alarmed and events == []      # below threshold: no alarm


def test_plateau_is_not_a_drop():
    d, events = run([79, 79, 79])
    assert not d.alarmed and events == []


def test_recovery_after_alarm():
    d, events = run([50, 49, 48, 49, 50])
    assert not d.alarmed and events == ["alarm", "recovered"]


def test_plateau_holds_an_active_alarm():
    # Equal readings neither clear nor re-raise; only a gain clears.
    d, events = run([50, 49, 48, 48, 48])
    assert d.alarmed and events == ["alarm"]


def test_second_episode_alarms_again():
    d, events = run([50, 49, 48, 49, 48, 47])
    assert events == ["alarm", "recovered", "alarm"]


def test_resumed_charge_sheds_stale_blind_countdown(monkeypatch):
    """A charge resumed from a blind-mode run carries charge_end_ts from the
    previous attempt; entering target mode must drop it, or the UI receives
    a countdown already in the past and refresh-loops (beroset's 30 req/s)."""
    import threading
    import time as _time
    from asteroid_docking_bay import ops
    from asteroid_docking_bay.tasks import _charge_stop, _charge_tasks

    slot = "9-9:1"
    _charge_tasks[slot] = {"done": False,
                           "charge_end_ts": _time.time() - 3600}
    _charge_stop[slot] = threading.Event()

    persists: list[dict] = []
    monkeypatch.setattr(ops, "task_store", type("Rec", (), {
        "persist": staticmethod(
            lambda kind, s, loc, port, task: persists.append(dict(task))),
        "unpersist": staticmethod(lambda kind, s: None)})())
    monkeypatch.setattr(ops, "event_log", type("Log", (), {
        "log": staticmethod(lambda *a, **k: None)})())
    levels = iter([50, 85])                      # start below, then reach it
    monkeypatch.setattr(ops, "get_battery_level", lambda s: next(levels))
    monkeypatch.setattr(ops, "uhubctl_set_power", lambda *a, **k: True)
    monkeypatch.setattr(ops, "uhubctl_get_power", lambda *a, **k: True)
    monkeypatch.setattr(ops, "wait_serial_online", lambda *a, **k: True)
    monkeypatch.setattr(ops, "find_serial_for_loc_port", lambda *a: "SER")
    monkeypatch.setattr(ops, "find_codename_for_loc_port", lambda *a: "catfish")
    monkeypatch.setattr(ops, "_end_port", lambda *a, **k: None)
    monkeypatch.setattr(ops, "_CHARGE_POLL_SEC", 0.01)

    ops.ChargeOp(slot, "9-9", 1, {"charge": {}}).run()

    assert persists, "target mode must persist its state"
    assert all("charge_end_ts" not in p for p in persists)
    assert _charge_tasks.pop(slot)["done"] is True
