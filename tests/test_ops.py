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


def test_workbench_end_powers_down_gracefully(monkeypatch):
    """Audit F8: ending a workbench must _end_port (graceful poweroff then
    cut), not a raw power cut that leaves the watch running on battery."""
    import threading
    from asteroid_docking_bay import ops
    from asteroid_docking_bay.tasks import _workbench_stop, _workbench_tasks

    slot = "8-8:1"
    _workbench_tasks[slot] = {"done": False}
    stop = _workbench_stop[slot] = threading.Event()
    stop.set()                       # make run() exit its loop immediately

    ended = {}
    monkeypatch.setattr(ops, "_end_port",
                        lambda loc, port, serial, cfg, reason: ended.update(
                            loc=loc, port=port, reason=reason))
    monkeypatch.setattr(ops, "uhubctl_set_power", lambda *a, **k: True)
    monkeypatch.setattr(ops, "wait_serial_online", lambda *a, **k: True)
    monkeypatch.setattr(ops, "get_battery_level", lambda s: 60)
    monkeypatch.setattr(ops, "find_serial_for_loc_port", lambda c, l, p: "SER")
    monkeypatch.setattr(ops, "find_codename_for_loc_port", lambda c, l, p: "skipjack")
    monkeypatch.setattr(ops, "_ensure_port_powered", lambda *a, **k: None)

    ops.WorkbenchOp(slot, "8-8", 1, {"charge": {}}).run()

    assert ended.get("loc") == "8-8" and ended.get("reason") == "workbench ended"
    assert _workbench_tasks.pop(slot)["done"] is True


# ── charge_to_target: explicit target (drain-recharge-to-rest) ────────────────

import asteroid_docking_bay.ops as opsmod
from asteroid_docking_bay.config import ChargeConfig


def test_charge_to_target_honours_explicit_target(monkeypatch):
    # At 60% with an explicit 50% target, there is nothing to do — it must not
    # keep charging to the default high_threshold (80%).
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 60)
    cc = ChargeConfig()
    got = opsmod.charge_to_target("skipjack", "SER", cc, target=50)
    assert got == 60


def test_charge_to_target_defaults_to_high_threshold(monkeypatch):
    # No target given → the old behaviour: charge toward high_threshold. At 90%
    # (already above 80) there's nothing to do.
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 90)
    got = opsmod.charge_to_target("skipjack", "SER", ChargeConfig())
    assert got == 90


# ── drain reading: fast poll to minimise the charge bump ──────────────────────

def test_drain_read_uses_fast_poll_same_budget(monkeypatch):
    import threading
    from asteroid_docking_bay.config import ChargeConfig
    captured = {}
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda *a: None)
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 55)

    def fake_wait(serial, wait_secs, retries, *a, **k):
        captured["wait"], captured["retries"] = wait_secs, retries
        return True
    monkeypatch.setattr(opsmod, "wait_serial_online", fake_wait)

    cc = ChargeConfig()   # adb_wait_seconds=15, adb_wait_retries=8 -> 120s budget
    got = opsmod._adb_read_battery("1-2", 1, "SER", cc, threading.Event())
    assert got == 55
    # short poll (small charge window) but the same total wall-clock budget.
    assert captured["wait"] <= 3
    budget = cc.adb_wait_seconds * cc.adb_wait_retries
    assert 0.8 * budget <= captured["wait"] * captured["retries"] <= budget


# ── drain blind-read guard (rubyfish incident, 2026-07-14) ──────────────────
#
# The drain loop deliberately discharges a watch. When rubyfish stopped
# enumerating mid-test the reads returned None, the loop logged and continued
# forever, the displayed value froze at 71%, and the watch discharged past the
# 15% floor to 0% / 3.18V unseen. The floor check only ever runs on a SUCCESSFUL
# read, so losing the reading disabled the only safety stop.

def _drain_env(monkeypatch, reads):
    """Run DrainOp's worker against a scripted sequence of battery reads.

    DrainOp reads its task and stop-event out of the module registries, so the
    slot has to be seeded there rather than passed in."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    seq = list(reads)
    power = {}
    slot = "1-2:2"
    task = {}
    monkeypatch.setitem(opsmod._drain_tasks, slot, task)
    monkeypatch.setitem(opsmod._drain_stop, slot, threading.Event())

    monkeypatch.setattr(opsmod, "_DRAIN_POLL_SEC", 0)

    calls = {"n": 0}

    def _read(*a, **k):
        # Hard stop so a missing guard FAILS loudly instead of spinning
        # forever — an unbounded retry loop would otherwise hang the suite,
        # which is a far worse signal than an assertion.
        calls["n"] += 1
        if calls["n"] > 25:
            raise AssertionError(
                "drain loop polled 25+ times without stopping — it is "
                "discharging blind (the rubyfish failure)")
        return seq.pop(0) if seq else None

    monkeypatch.setattr(opsmod, "_adb_read_battery", _read)
    monkeypatch.setattr(opsmod, "find_serial_for_loc_port", lambda *a, **k: "S1")
    monkeypatch.setattr(opsmod, "load_config", lambda: {})
    monkeypatch.setattr(opsmod, "uhubctl_set_power",
                        lambda loc, port, on: power.__setitem__("on", on))
    monkeypatch.setattr(opsmod, "_ensure_port_powered", lambda *a, **k: None)
    monkeypatch.setattr(opsmod, "_end_port",
                        lambda *a, **k: power.__setitem__("on", False))
    monkeypatch.setattr(opsmod, "_save_drain_results", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.task_store, "persist", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.task_store, "unpersist", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.event_log, "log", lambda *a, **k: None)
    return opsmod, power, slot, task


def test_drain_aborts_after_consecutive_blind_reads(monkeypatch):
    """Unbounded retries let a watch discharge invisibly. After the cap the
    test must stop rather than keep draining something it cannot see."""
    opsmod, power, slot, task = _drain_env(monkeypatch, [80] + [None] * 10)
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert task.get("blind_abort") is True, (
        "drain kept polling blind instead of aborting — the rubyfish failure")


def test_blind_abort_leaves_the_port_powered(monkeypatch):
    """The watch is low and unreadable: the end-of-test power-off would strand
    it off charge, which is exactly the deep-discharge path to avoid. Power
    must be restored instead."""
    opsmod, power, slot, _ = _drain_env(monkeypatch, [80] + [None] * 10)
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert power.get("on") is True, (
        f"port left unpowered after a blind abort: {power}")


def test_unreadable_initial_battery_does_not_shelve(monkeypatch):
    """A drain whose very first battery read fails (brownout / not
    enumerating) must not fall through to the graceful poweroff — that stamps
    safe_off_ts and renders the watch "shelved", a false "deliberately, safely
    halted" claim for a drain that never started. It takes the unreadable-abort
    path: port left powered (as docked), no graceful shelve."""
    opsmod, power, slot, task = _drain_env(monkeypatch, [None])
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert task.get("blind_abort") is True, (
        "drain that could not read its start battery shelved the watch")
    assert power.get("on") is True, (
        f"unreadable drain-start powered the watch off (shelved): {power}")


def test_resumed_drain_cuts_vbus_before_polling(monkeypatch):
    """A fresh drain powers the port off only as a side effect of its initial
    battery read. The resume path (restored after a restart) skips that read,
    so without an explicit cut the watch sits CHARGING on a powered port until
    the first 30-min poll — the opposite of a drain. The cut must be explicit."""
    opsmod, power, slot, task = _drain_env(monkeypatch, [14])  # first poll hits floor
    calls = []
    monkeypatch.setattr(opsmod, "uhubctl_set_power",
                        lambda loc, port, on: calls.append(on))
    # Look like a task restored from disk (has readings -> resuming=True).
    task.update({"readings": [{"ts": 1.0, "pct": 100}], "start_ts": 1.0,
                 "start_pct": 100, "last_pct": 100, "last_ts": 1.0})
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert False in calls, (
        "resumed drain never cut VBUS — the watch charges instead of draining")


def test_a_single_failed_read_does_not_abort(monkeypatch):
    """One transient miss is normal; aborting on it would make drain tests
    useless. The guard must tolerate misses below the cap."""
    opsmod, _, slot, task = _drain_env(monkeypatch, [80, None, 70, 60, 20, 10])
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert not task.get("blind_abort"), "aborted on a single recoverable miss"
    assert task.get("last_pct") == 10, task


# ── who owns a port, across processes ───────────────────────────────────────

def test_active_op_on_slot_sees_in_memory_ops(monkeypatch, tmp_path):
    """Inside the web service the registries are authoritative."""
    from asteroid_docking_bay import tasks
    monkeypatch.setattr(tasks.task_store, "dir", tmp_path)   # no real state
    monkeypatch.setitem(tasks._drain_tasks, "1-2:1", {"done": False})
    assert tasks.active_op_on_slot("1-2:1") == "drain"
    monkeypatch.setitem(tasks._drain_tasks, "1-2:1", {"done": True})
    assert tasks.active_op_on_slot("1-2:1") is None


def test_active_op_on_slot_sees_ops_from_another_process(monkeypatch, tmp_path):
    """A CLI process has EMPTY registries — the ops run in the web service —
    so it must fall through to the durable store. Without this a CLI
    `on`/`off`/`cycle` silently corrupts a running measurement, which is
    exactly the failure the web guard was added for."""
    import json
    from asteroid_docking_bay import tasks
    monkeypatch.setattr(tasks.task_store, "dir", tmp_path)
    for name in ("_charge_tasks", "_drain_tasks", "_workbench_tasks"):
        monkeypatch.setattr(tasks, name, {})
    (tmp_path / "drain_1-2_1.json").write_text(json.dumps(
        {"kind": "drain", "slot": "1-2:1", "loc": "1-2", "port": 1,
         "task": {"done": False}}))
    assert tasks.active_op_on_slot("1-2:1") == "drain"
    assert tasks.active_op_on_slot("1-2:2") is None, "matched the wrong slot"


def test_finished_persisted_op_does_not_block(monkeypatch, tmp_path):
    """A completed op left on disk must not refuse forever."""
    import json
    from asteroid_docking_bay import tasks
    monkeypatch.setattr(tasks.task_store, "dir", tmp_path)
    for name in ("_charge_tasks", "_drain_tasks", "_workbench_tasks"):
        monkeypatch.setattr(tasks, name, {})
    (tmp_path / "drain_1-2_1.json").write_text(json.dumps(
        {"kind": "drain", "slot": "1-2:1", "task": {"done": True}}))
    assert tasks.active_op_on_slot("1-2:1") is None


def test_workbench_records_who_claimed_the_watch(monkeypatch):
    """On a rig several sessions share, "workbench active" does not tell you
    whether to wait or take over. The claim must name its holder."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    monkeypatch.setattr(opsmod, "is_slot_smart", lambda cfg, loc, port: True)
    monkeypatch.setattr(opsmod.task_store, "persist", lambda *a, **k: None)
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(opsmod, "_workbench_tasks", {})
    monkeypatch.setattr(opsmod, "_workbench_stop", {})
    monkeypatch.setattr(opsmod.WorkbenchOp, "tasks", opsmod._workbench_tasks)
    monkeypatch.setattr(opsmod.WorkbenchOp, "stops", opsmod._workbench_stop)

    assert opsmod.WorkbenchOp.start("1-2", 1, {}, owner="ui-track session") is None
    assert opsmod.WorkbenchOp.tasks["1-2:1"]["owner"] == "ui-track session"


def test_workbench_without_an_owner_still_works(monkeypatch):
    """The web UI's existing checkout passes no owner; it must not break."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    monkeypatch.setattr(opsmod, "is_slot_smart", lambda cfg, loc, port: True)
    monkeypatch.setattr(opsmod.task_store, "persist", lambda *a, **k: None)
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    monkeypatch.setattr(opsmod, "_workbench_tasks", {})
    monkeypatch.setattr(opsmod, "_workbench_stop", {})
    monkeypatch.setattr(opsmod.WorkbenchOp, "tasks", opsmod._workbench_tasks)
    monkeypatch.setattr(opsmod.WorkbenchOp, "stops", opsmod._workbench_stop)

    assert opsmod.WorkbenchOp.start("1-2", 1, {}) is None
    assert opsmod.WorkbenchOp.tasks["1-2:1"]["owner"] is None
