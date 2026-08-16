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


def test_drain_captures_features_while_the_port_is_still_powered(monkeypatch):
    """The standby consumers (WiFi/BT/AoD) must be read WHILE the watch is on
    the bus. The old code read them after _adb_read_battery had cut VBUS, so
    the watch was already dropping off adb and every run logged
    None/defaulted features — useless for per-feature attribution."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    from asteroid_docking_bay.config import charge_config
    power = {"on": None}
    seen = {}
    monkeypatch.setattr(opsmod, "uhubctl_set_power",
                        lambda loc, port, on: power.__setitem__("on", on))
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: True)
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 100)
    monkeypatch.setattr(opsmod, "battery_and_screen", lambda s: (100, False, "Full"))

    class _W:
        def __init__(self, s): pass
        def standby_features(self):
            seen["powered_when_read"] = power["on"]     # must be True here
            return {"wifi": True, "bt": False, "aod": False}
        def screen(self, on): pass
    monkeypatch.setattr(opsmod, "Watch", _W)

    pct, feats = opsmod._adb_read_battery(
        "1-2", 1, "S", charge_config({}), threading.Event(), with_features=True)
    assert pct == 100
    assert {k: feats[k] for k in ("wifi", "bt", "aod")} == {"wifi": True, "bt": False, "aod": False}
    assert seen["powered_when_read"] is True, "features read after VBUS was cut"
    assert power["on"] is False, "port must still be cut on the way out"


def test_drain_start_releases_a_forced_screen(monkeypatch):
    """A forced-on display (a leftover `mcetool -D on`) would drain the watch at
    full-panel rate and be recorded as standby (audit B5). The drain-start read
    must detect it, release it (screen(False)), and record the starting state in
    the features so a contaminated run is at least self-documenting."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    from asteroid_docking_bay.config import charge_config
    released = {"n": 0}
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda l, p, on: None)
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: True)
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 100)

    class _W:
        def __init__(self, s): pass
        def standby_features(self): return {"wifi": False, "bt": False, "aod": False}
        def screen(self, on): released["on"] = on; released["n"] += 1; return True
    monkeypatch.setattr(opsmod, "Watch", _W)

    # forced ON → released, recorded True
    monkeypatch.setattr(opsmod, "battery_and_screen", lambda s: (100, True, "Full"))
    _, feats = opsmod._adb_read_battery(
        "1-2", 1, "S", charge_config({}), threading.Event(), with_features=True)
    assert released["n"] == 1 and released["on"] is False, "did not release a forced screen"
    assert feats["screen_forced_at_start"] is True

    # not forced → no release, recorded False
    monkeypatch.setattr(opsmod, "battery_and_screen", lambda s: (100, False, "Full"))
    released["n"] = 0
    _, feats = opsmod._adb_read_battery(
        "1-2", 1, "S", charge_config({}), threading.Event(), with_features=True)
    assert released["n"] == 0 and feats["screen_forced_at_start"] is False


def test_drain_start_reads_battery_after_the_feature_window(monkeypatch):
    """start_pct must be the battery at the moment the drain BEGINS — read after
    the feature/screen round-trips (the port charges through them), right before
    VBUS is cut, so the rate anchors on the true start, not a pre-charge value
    taken seconds earlier (audit B7)."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    from asteroid_docking_bay.config import charge_config
    order = []
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda *a, **k: None)
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: True)
    monkeypatch.setattr(opsmod, "battery_and_screen", lambda s: (100, False, "Full"))
    monkeypatch.setattr(opsmod, "get_battery_level",
                        lambda s: (order.append("battery"), 88)[1])

    class _W:
        def __init__(self, s): pass
        def standby_features(self):
            order.append("features")
            return {"wifi": False, "bt": False, "aod": False}
        def screen(self, on): pass
    monkeypatch.setattr(opsmod, "Watch", _W)
    pct, _ = opsmod._adb_read_battery(
        "1-2", 1, "S", charge_config({}), threading.Event(), with_features=True)
    assert pct == 88
    assert order == ["features", "battery"], \
        f"battery read before the feature window inflates the anchor: {order}"


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
        pct = seq.pop(0) if seq else None
        # The initial read asks for features (returns a tuple); polls don't.
        return (pct, k.get("_features")) if k.get("with_features") else pct

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


def test_drain_stops_if_the_port_serial_changes_mid_run(monkeypatch):
    """If the port's mapped watch changes mid-drain (config edit / physical
    swap), reading on attributes a different watch's battery to this run's
    serial — the read is on the re-resolved serial but logged under the start
    serial (audit B6). The loop must stop rather than mis-attribute."""
    opsmod, power, slot, task = _drain_env(monkeypatch, [80, 70, 60, 20])
    serials = iter(["S1", "S2", "S2", "S2"])   # start on S1, then the port remaps
    monkeypatch.setattr(opsmod, "find_serial_for_loc_port",
                        lambda *a, **k: next(serials, "S2"))
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    # the loop broke on the first poll's serial change → only the start reading
    assert len(task["readings"]) == 1, \
        f"kept reading after the serial changed: {task['readings']}"


def test_a_single_failed_read_does_not_abort(monkeypatch):
    """One transient miss is normal; aborting on it would make drain tests
    useless. The guard must tolerate misses below the cap."""
    opsmod, _, slot, task = _drain_env(monkeypatch, [80, None, 70, 60, 20, 10])
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert not task.get("blind_abort"), "aborted on a single recoverable miss"
    assert task.get("last_pct") == 10, task


def test_drain_measures_a_known_discharge_rate_end_to_end(monkeypatch):
    """Over-arching guard the unit suite lacked (audit D2): drive a FULL
    DrainOp.run against a synthetic battery discharging at a KNOWN rate and
    assert the saved rate matches. Catches 'the whole mechanic measures
    garbage' — a rising battery from a concurrent charge, a broken divisor,
    wrong anchoring — rather than any single component. A synthetic clock
    advances one poll interval per loop wait; the battery is a pure linear
    discharge (no charge-bump), so the recovered rate must equal ground truth."""
    import asteroid_docking_bay.ops as opsmod
    import asteroid_docking_bay.tasks as tasksmod
    import time as _time

    KNOWN = 6.0                       # %/h — 30-min polls land on integers
    clock = {"t": 1_000_000.0}
    T0 = clock["t"]
    monkeypatch.setattr(_time, "time", lambda: clock["t"])

    def battery():
        return max(0.0, 100.0 - KNOWN * (clock["t"] - T0) / 3600.0)

    def read(loc, port, serial, cc, stop, with_features=False):
        pct = int(round(battery()))
        return (pct, {"wifi": False, "bt": False, "aod": False}) if with_features else pct
    monkeypatch.setattr(opsmod, "_adb_read_battery", read)

    class FakeStop:
        _set = False
        def wait(self, timeout=None):
            clock["t"] += opsmod._DRAIN_POLL_SEC     # advance one poll interval
            return self._set
        def is_set(self): return self._set
        def set(self): type(self)._set = True

    slot = "1-2:1"
    monkeypatch.setitem(tasksmod._drain_tasks, slot, {})
    monkeypatch.setitem(tasksmod._drain_stop, slot, FakeStop())
    monkeypatch.setattr(opsmod, "find_serial_for_loc_port", lambda *a, **k: "S1")
    monkeypatch.setattr(opsmod, "load_config", lambda: {})
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda *a, **k: None)
    monkeypatch.setattr(opsmod, "_end_port", lambda *a, **k: None)
    monkeypatch.setattr(opsmod, "_save_drain_results", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.task_store, "persist", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.task_store, "unpersist", lambda *a, **k: None)
    monkeypatch.setattr(opsmod.event_log, "log", lambda *a, **k: None)

    opsmod.DrainOp(slot, "1-2", 1, {}).run()
    task = tasksmod._drain_tasks[slot]
    assert len(task["readings"]) > 20, \
        f"mechanic did not run end-to-end: {len(task['readings'])} readings"
    assert task["last_pct"] <= opsmod._DRAIN_FLOOR_PCT, "did not reach the floor"
    assert abs(task["drain_rate"] - KNOWN) < 0.3, \
        f"recovered rate {task['drain_rate']} != known {KNOWN}"


def test_adb_read_battery_marks_the_bus_busy_for_the_warmer(monkeypatch):
    """The warmer defers its USB scan while a drain poll is on the bus, so its
    libusb fastboot sweep can't race the enumeration and wedge it (audit B9).
    _adb_read_battery must raise the busy counter for the read and reset it."""
    import threading
    import asteroid_docking_bay.ops as opsmod
    from asteroid_docking_bay.config import charge_config
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda *a, **k: None)
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: True)
    seen = {}

    def gbl(s):
        seen["busy_during"] = opsmod._bus_read_active
        return 50
    monkeypatch.setattr(opsmod, "get_battery_level", gbl)
    opsmod._bus_read_active = 0
    opsmod._adb_read_battery("1-2", 1, "S", charge_config({}), threading.Event())
    assert seen["busy_during"] == 1, "did not mark the bus busy during the read"
    assert opsmod._bus_read_active == 0, "did not reset the busy counter after"


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


def test_start_refuses_when_another_op_owns_the_slot(monkeypatch):
    """One long-running op per slot, enforced symmetrically in Operation.start.
    A charge starting on a draining slot would power the port on and recharge
    the watch mid-measurement (audit B1) — every pair must refuse, not just the
    workbench case that used to be checked. is_slot_smart is forced False so a
    missed cross-op check would fall through to 'non-smart', not the op name."""
    import asteroid_docking_bay.ops as opsmod
    import asteroid_docking_bay.tasks as tasksmod
    monkeypatch.setattr(opsmod, "is_slot_smart", lambda *a, **k: False)
    slot = "1-2:1"
    # drain active → charge and workbench both refused, naming the drain
    monkeypatch.setitem(tasksmod._drain_tasks, slot, {"done": False})
    assert "drain" in (opsmod.ChargeOp.start("1-2", 1, {}) or ""), "charge ran on a draining slot"
    assert "drain" in (opsmod.WorkbenchOp.start("1-2", 1, {}) or "")
    monkeypatch.delitem(tasksmod._drain_tasks, slot)
    # charge active → drain refused, naming the charge (the B1 direction)
    monkeypatch.setitem(tasksmod._charge_tasks, slot, {"done": False})
    assert "charge" in (opsmod.DrainOp.start("1-2", 1, {}) or ""), "drain ran on a charging slot"
    monkeypatch.delitem(tasksmod._charge_tasks, slot)
    # flash active → drain refused (drain never checked flash before)
    monkeypatch.setitem(tasksmod._flash_tasks, slot, {"done": False})
    assert "flash" in (opsmod.DrainOp.start("1-2", 1, {}) or "")


def test_end_port_marks_safe_off_only_when_the_watch_is_reachable(monkeypatch):
    """_end_port powers a post-drain watch up to deliver the poweroff. If it
    never comes online the shutdown isn't delivered — stamping safe_off_ts
    would render it "down" while it runs on battery, invisible (audit B3). The
    marker (and the power_off event) must be gated on reachability."""
    import asteroid_docking_bay.ops as opsmod
    from asteroid_docking_bay.config import charge_config
    marks = []
    monkeypatch.setattr(opsmod, "adb_devices_checked", lambda: {})   # adb healthy
    monkeypatch.setattr(opsmod, "uhubctl_get_power", lambda l, p: True)
    monkeypatch.setattr(opsmod, "uhubctl_set_power", lambda l, p, on: True)
    monkeypatch.setattr(opsmod, "_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(opsmod, "get_battery_level", lambda s: 50)
    monkeypatch.setattr(opsmod.last_seen, "mark", lambda s, **k: marks.append(k))
    monkeypatch.setattr(opsmod.event_log, "log", lambda *a, **k: None)
    cc = charge_config({})
    # unreachable → NO safe-off marker
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: False)
    opsmod._end_port("1-2", 1, "S", cc, "drain ended")
    assert not any("safe_off_ts" in m for m in marks), \
        f"marked safe-off for an unreachable watch: {marks}"
    # reachable → safe-off IS stamped
    marks.clear()
    monkeypatch.setattr(opsmod, "wait_serial_online", lambda *a, **k: True)
    opsmod._end_port("1-2", 1, "S", cc, "drain ended")
    assert any("safe_off_ts" in m for m in marks), \
        "did not mark safe-off for a reachable watch"


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


def test_warm_port_power_reads_only_empty_ports(monkeypatch, tmp_path):
    """The power warmer caches the register state of EMPTY configured ports
    only — a present child already proves power, and reading an occupied
    port's register would be pure extra bus traffic. Planted-bug: drop the
    child-present skip and the no-probe assertion fails."""
    import asteroid_docking_bay.ops as ops
    # fake sysfs: hub 9-9 with ports 1..3
    iface = tmp_path / "9-9:1.0"
    for p in (1, 2, 3):
        (iface / f"9-9-port{p}").mkdir(parents=True)
    monkeypatch.setattr(ops, "_SYSFS_USB", tmp_path)
    monkeypatch.setattr(ops, "_port_device_present",
                        lambda loc, p: p == 2)          # port 2 occupied
    probed, cached = [], {}
    monkeypatch.setattr(ops, "_sysfs_get_power",
                        lambda loc, p: probed.append(p) or (p != 3))
    monkeypatch.setattr(ops.power_cache, "put",
                        lambda k, v: cached.update({k: v}))
    ops._warm_port_power({"hubs": [{"location": "9-9"}]})
    assert sorted(probed) == [1, 3]                     # occupied port skipped
    assert cached == {("9-9", 1): True, ("9-9", 3): False}


# --- the no-poll drain window ---------------------------------------------

def test_a_no_poll_window_takes_exactly_two_readings(monkeypatch):
    """Every mid-run read POWERS THE PORT to reach adb, and on a watch slow to
    enumerate that window runs to minutes — putting in more charge than standby
    takes out. catfish went 97% -> 98% across nine hours that way, the
    instrument reporting the exact opposite of the truth.

    So a no-poll window must call the battery read EXACTLY twice: once before
    the cut, once after the window. Counting the calls is the assertion that
    discriminates — asserting on the stored readings does not, because a
    polling run can end up with the same two values."""
    import threading
    opsmod, power, slot, task = _drain_env(monkeypatch, [100, 96, 95, 94, 93])
    opsmod._drain_tasks[slot]["no_poll"] = True

    inner = opsmod._adb_read_battery
    calls = []

    def _counted(*a, **k):
        calls.append(1)
        return inner(*a, **k)

    monkeypatch.setattr(opsmod, "_adb_read_battery", _counted)

    # End the window shortly after it opens, the way a user's stop would.
    threading.Timer(0.05, opsmod._drain_stop[slot].set).start()
    opsmod.DrainOp(slot, "1-2", 2, {}).run()

    assert len(calls) == 2, (
        f"a no-poll window read the battery {len(calls)} times; it must read "
        "only before the cut and after the window, because every read powers "
        "the port and charges the watch it is measuring")
    assert task.get("drain_rate") is not None, "no rate from the closing read"


def test_the_closing_read_happens_before_the_watch_is_shut_down(monkeypatch):
    """_end_port shuts the watch down at the end of a drain. A closing read
    placed after it finds a powered-off watch, so the window ends with no
    second point — the one number the whole run exists to produce.

    This shipped briefly and the reading-count test did not catch it, because
    with everything mocked a read "succeeds" whether or not the watch is up.
    Ordering is the thing to assert."""
    import threading
    opsmod, power, slot, task = _drain_env(monkeypatch, [100, 96])
    opsmod._drain_tasks[slot]["no_poll"] = True

    order = []
    inner_read = opsmod._adb_read_battery
    monkeypatch.setattr(opsmod, "_adb_read_battery",
                        lambda *a, **k: (order.append("read"), inner_read(*a, **k))[1])
    monkeypatch.setattr(opsmod, "_end_port",
                        lambda *a, **k: order.append("shutdown"))

    threading.Timer(0.05, opsmod._drain_stop[slot].set).start()
    opsmod.DrainOp(slot, "1-2", 2, {}).run()

    assert "shutdown" in order, "the watch was never returned to rest"
    # The LAST read is the closing one. Using the first would pass trivially,
    # because the run always opens with an initial read before the cut.
    last_read = len(order) - 1 - order[::-1].index("read")
    assert last_read < order.index("shutdown"), \
        f"the closing read ran AFTER the watch was shut down (order={order})"
    # And the last read must not inherit the caller's already-set stop event,
    # or it aborts instantly on the ordinary user-stop path.
    assert task.get("last_pct") == 96, "the closing read did not complete"


def test_a_normal_drain_still_polls(monkeypatch):
    """The guard must be conditional. Fixing the no-poll mode by breaking the
    curve every other drain test depends on would be a bad trade."""
    opsmod, power, slot, task = _drain_env(monkeypatch, [90, 80, 70, 14])
    opsmod.DrainOp(slot, "1-2", 2, {}).run()
    assert len(task.get("readings", [])) > 2, \
        "a normal drain test stopped polling"


def _peel_env(monkeypatch, pref, switch_ok=True):
    """Drive the real peeler with one stray found and a controllable switch."""
    from asteroid_docking_bay import ops
    spawned = []

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            spawned.append((getattr(target, "__name__", target), args))

        def start(self):
            pass

    monkeypatch.setattr(ops.threading, "Thread", _T)
    monkeypatch.setattr(ops, "rndis_links", lambda: [{"serial": "S1", "iface": "e0"}])
    monkeypatch.setattr(ops, "_stray_ssh_to_realign", lambda *a: "S1")
    monkeypatch.setattr(ops, "_route_winner_iface", lambda: "e0")
    monkeypatch.setattr(ops, "_detect_rndis", lambda ip: True)
    monkeypatch.setattr(ops, "_switch_ssh_to_adb", lambda *a, **k: {"ok": switch_ok})
    monkeypatch.setattr(ops, "usb_mode_preference", lambda cfg: pref)
    ops._last_ssh_realign = 0.0
    return ops, spawned


def test_peeler_finishes_the_relocation_under_an_ssh_preference(monkeypatch):
    """The peel is only step ONE. Two paths act on strays and this is the one
    that can actually reach a shadowed watch, so when it stopped after the
    switch to adb an 'ssh' fleet preference was silently never honoured — both
    dory and sturgeon ended up on adb under an ssh preference. It must hand off
    to the same completion step the poll-side aligner uses."""
    ops, spawned = _peel_env(monkeypatch, "ssh")
    ops._maybe_realign_stray_ssh({})
    assert spawned == [("finish_ssh_relocation", ("S1",))], \
        "the ssh preference was left unhonoured after the peel"


def test_peeler_stops_at_adb_when_that_is_the_preference(monkeypatch):
    """Under an 'adb' preference the peel IS the whole job — sending the watch
    back out to SSH would undo the thing that was just asked for."""
    ops, spawned = _peel_env(monkeypatch, "adb")
    ops._maybe_realign_stray_ssh({})
    assert spawned == [], "relocated a watch back to SSH under an adb preference"


def test_peeler_does_not_relocate_a_watch_it_never_switched(monkeypatch):
    """If the switch to adb failed, the watch is not on adb — handing it to a
    step that waits for it there would just burn a thread and log a lie."""
    ops, spawned = _peel_env(monkeypatch, "ssh", switch_ok=False)
    ops._maybe_realign_stray_ssh({})
    assert spawned == [], "chased a relocation after the switch had failed"


def test_a_watch_with_no_hub_seat_can_still_be_flashed(monkeypatch):
    """Flashing must not require a hub seat, because the no-hub user has none.

    _flash_one_watch refused outright with "not mapped" when a codename had no
    port, and "non-smart port" when the port could not switch power. That is
    exactly the situation a-d-b's own 1.0 goals describe: someone with no smart
    hub plugs a watch into a laptop port. It also blocked sparrow, which only
    enumerates in fastboot on a direct chassis port.

    Without a seat there is no VBUS to switch, so the power steps are skipped
    and the watch simply has to be present already. Nothing else about the
    sequence changes."""
    import asteroid_docking_bay.ops as o
    from pathlib import Path
    switched, flashed = [], []

    monkeypatch.setattr(o, "find_port_for_codename", lambda c, cn: (None, None))
    monkeypatch.setattr(o, "is_port_smart", lambda c, cn: None)
    monkeypatch.setattr(o, "uhubctl_set_power",
                        lambda l, p, on: switched.append((l, p, on)))
    monkeypatch.setattr(o, "_download_nightly",
                        lambda cn, d, u, force=False: (Path("/tmp/b.img"), Path("/tmp/i.ext4")))
    # already sitting in the bootloader — the no-hub user's normal state
    monkeypatch.setattr(o, "fastboot_serial_for_codename", lambda cn, want=None: "FB1")
    monkeypatch.setattr(o, "_flash_watch",
                        lambda b, i, fb, dry_run=False: flashed.append(fb))
    monkeypatch.setattr(o, "_clear_ssh_known_hosts", lambda: None)

    from asteroid_docking_bay.config import FlashConfig
    res = o._flash_one_watch("sparrow", {}, FlashConfig())

    assert res == "ok", f"an unseated watch was refused: {res}"
    assert flashed == ["FB1"], "the flash never reached the device"
    assert switched == [], "tried to switch VBUS on a watch with no hub port"


def test_a_watch_already_in_fastboot_is_not_waited_for(monkeypatch):
    """_wait_for_fastboot waits for a serial that is NEW since its snapshot, so
    a watch ALREADY in the bootloader could never satisfy it — the flash timed
    out with the device sitting right there. Recognise it first instead.

    This is the common case for a no-hub user, who puts the watch into fastboot
    by hand before asking for a flash."""
    import asteroid_docking_bay.ops as o
    from pathlib import Path
    from asteroid_docking_bay.config import FlashConfig
    waited, rebooted = [], []

    monkeypatch.setattr(o, "find_port_for_codename", lambda c, cn: ("1-2", 1))
    monkeypatch.setattr(o, "is_port_smart", lambda c, cn: True)
    monkeypatch.setattr(o, "uhubctl_set_power", lambda l, p, on: None)
    monkeypatch.setattr(o, "_download_nightly",
                        lambda cn, d, u, force=False: (Path("/tmp/b.img"), Path("/tmp/i.ext4")))
    monkeypatch.setattr(o, "fastboot_serial_for_codename", lambda cn, want=None: "FB1")
    monkeypatch.setattr(o, "_wait_for_fastboot",
                        lambda before, timeout=30: waited.append(1))
    monkeypatch.setattr(o, "_run", lambda cmd, check=True: rebooted.append(cmd) or (0, "", ""))
    monkeypatch.setattr(o, "_flash_watch", lambda b, i, fb, dry_run=False: None)
    monkeypatch.setattr(o, "_clear_ssh_known_hosts", lambda: None)

    assert o._flash_one_watch("sparrow", {}, FlashConfig()) == "ok"
    assert waited == [], "waited for a 'new' fastboot device that was already present"
    assert not any("reboot bootloader" in c for c in rebooted), \
        "rebooted a watch that was already in the bootloader"


def test_the_warmer_leaves_a_watch_alone_during_onboarding(monkeypatch):
    """The background warmer peels a stray SSH watch back to ADB to free the
    shared address. During onboarding that is wrong, and it is not a
    hypothetical: on 2026-08-16 a watch deliberately put into SSH mode to test
    the guided setup was switched back mid-experiment, and the log said so --
    "stray SSH watch ... switching it to adb".

    The status path had already been gated. THIS path, its sibling, had not.
    That is the same shape as the pre-1.0 audit's F1/F8 pair: a guard added at
    one call site and assumed at the others. So the gate is asserted here
    specifically, not only where it was first written.
    """
    import asteroid_docking_bay.ops as ops
    from asteroid_docking_bay import util

    switched = []
    monkeypatch.setattr(ops, "rndis_links", lambda: [{"iface": "usb0"}])
    monkeypatch.setattr(ops, "_stray_ssh_to_realign",
                        lambda links, ips, iface, det: "S9")
    monkeypatch.setattr(ops, "_switch_ssh_to_adb",
                        lambda *a, **k: switched.append(a) or {"ok": True})
    monkeypatch.setattr(ops, "_detect_rndis", lambda: True)
    monkeypatch.setattr(ops, "_route_winner_iface", lambda: "usb0")
    monkeypatch.setattr(ops, "_last_ssh_realign", 0.0, raising=False)

    util.note_onboarding_activity()
    ops._maybe_realign_stray_ssh({"ssh_ips": {}, "usb_mode_preference": "adb"})
    assert switched == [], (
        "the warmer switched a watch out of SSH while onboarding was on "
        "screen -- the user's deliberate choice undone with no explanation")

    util.release_onboarding()
    monkeypatch.setattr(ops, "_last_ssh_realign", 0.0, raising=False)
    ops._maybe_realign_stray_ssh({"ssh_ips": {}, "usb_mode_preference": "adb"})
    assert switched, "the warmer never resumed after onboarding closed"


def test_no_fleet_corrections_in_the_first_seconds_after_a_restart(monkeypatch):
    """A restart must not become a licence to act.

    The quiet window is in memory, so restarting clears it -- and the warmer's
    first pass runs about a second into startup, before an open guide's next
    heartbeat can re-arm it. Observed on 2026-08-16: the gate refused twice,
    a deploy restarted the service, and one second later the watch was
    switched anyway, mid-experiment.

    Starting armed also matches what a-d-b knows at that moment, which is
    nothing: it has not yet seen the bus and cannot tell a stray from a watch
    somebody is holding.
    """
    import importlib
    from asteroid_docking_bay import util
    fresh = importlib.reload(util)
    try:
        assert fresh.onboarding_active(), (
            "a freshly started process is willing to correct the fleet "
            "immediately -- a restart mid-onboarding undoes the user's watch")
    finally:
        importlib.reload(util)


def test_a_watch_on_adb_is_never_treated_as_an_ssh_stray(monkeypatch):
    """Newer watches run CDC-NCM, where ADB and the network link are live at
    the SAME time. Every USB-mode path here was written for RNDIS, where a
    watch is in one mode or the other.

    The peeler exists because watches sharing the default SSH address shadow
    each other and become unreachable; a watch answering ADB is reachable, so
    there is nothing stuck to fix. "Correcting" one re-runs an RNDIS mode
    change its kernel does not support and leaves it in neither state -- which
    is how a working NCM setup on aurora was destroyed on 2026-08-16.
    """
    import asteroid_docking_bay.ops as ops
    from asteroid_docking_bay import util

    switched = []
    monkeypatch.setattr(ops, "rndis_links", lambda: [
        {"iface": "usb0", "usb_path": "1-3.4.2", "serial": "NCM"},   # also on adb
        {"iface": "usb1", "usb_path": "1-3.4.3", "serial": "STRAY"}, # genuinely stuck
    ])
    monkeypatch.setattr(ops, "adb_devices",
                        lambda: {"NCM": {"status": "device", "usb": "1-3.4.2"}})
    seen = {}
    def picker(links, ips, iface, probe):
        seen["serials"] = [l["serial"] for l in links]
        return links[0]["serial"] if links else None
    monkeypatch.setattr(ops, "_stray_ssh_to_realign", picker)
    monkeypatch.setattr(ops, "_switch_ssh_to_adb",
                        lambda *a, **k: switched.append(a) or {"ok": True})
    monkeypatch.setattr(ops, "_detect_rndis", lambda: True)
    monkeypatch.setattr(ops, "_route_winner_iface", lambda: "usb1")
    monkeypatch.setattr(ops, "_last_ssh_realign", 0.0, raising=False)
    util.release_onboarding()

    ops._maybe_realign_stray_ssh({"ssh_ips": {}, "usb_mode_preference": "adb"})

    assert seen["serials"] == ["STRAY"], (
        "the watch answering ADB was offered to the peeler -- on an NCM gadget "
        "that is a working setup, not a stray")
    assert switched, "the genuinely stuck watch was not peeled"
