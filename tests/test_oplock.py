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


def _dict_config(monkeypatch):
    """Back hold()/release() with an in-memory store shaped like the real
    config file: every load() is an independent copy, save() persists it — so
    the check-and-write atomicity inside _config_lock is actually exercised."""
    import copy
    from asteroid_docking_bay import config
    store = {"op_locks": {}}
    monkeypatch.setattr(config, "load_config", lambda: copy.deepcopy(store))
    monkeypatch.setattr(config, "save_config",
                        lambda c: (store.clear(), store.update(copy.deepcopy(c))))
    return store


def test_hold_will_not_overwrite_a_live_lock_of_another_kind(monkeypatch):
    """THE bug behind the wanze/dump collision: hold() overwrote whatever was
    there. A dump taken on a watch mid-wanze-run replaced the wanze lock and
    then deleted it on release — the run left unprotected and, because probing()
    keys on kind, invisible. A marker the next operation may stamp over is no
    marker at all."""
    from asteroid_docking_bay import oplock, wanze
    store = _dict_config(monkeypatch)

    assert oplock.hold("S1", wanze.PROBING_KIND, "arm A", ttl=600)["ok"]
    r = oplock.hold("S1", "dump", "full-disk dump", ttl=600)
    assert r["ok"] is False and r.get("conflict") and r["kind"] == wanze.PROBING_KIND
    assert oplock.held(store, "S1")["kind"] == wanze.PROBING_KIND, \
        "the wanze run lost its lock to the dump"
    assert oplock.held(store, "S1")["note"] == "arm A", "even the note was replaced"


def test_hold_renews_its_own_kind_and_can_take_an_expired_slot(monkeypatch):
    """Re-holding the same kind is a caller extending its own claim, not a
    collision — it must refresh the expiry. And an EXPIRED lock blocks nobody,
    or a crashed holder would wedge the slot until someone noticed by hand."""
    from asteroid_docking_bay import oplock
    store = _dict_config(monkeypatch)

    assert oplock.hold("S1", "wanze", "arm A", ttl=600)["ok"]
    assert oplock.hold("S1", "wanze", "arm B", ttl=900)["ok"], "a renewal was refused"
    assert oplock.held(store, "S1")["note"] == "arm B"

    store["op_locks"]["S1"]["until"] = time.time() - 1      # expire it
    assert oplock.hold("S1", "dump", "later", ttl=600)["ok"], \
        "an expired lock still blocked a new holder"
    assert oplock.held(store, "S1")["kind"] == "dump"


def test_a_dump_refuses_to_start_over_a_held_watch(monkeypatch):
    """The overwrite bug seen from the dump op: it must decline, not clobber."""
    from asteroid_docking_bay import rpcops, wanze, stockrom
    store = _dict_config(monkeypatch)
    assert wanze.probing_set("S1", True, "arm A")["ok"]     # a run is live

    class _W:
        t = object()
    monkeypatch.setattr(rpcops, "_watch", lambda s: _W())
    monkeypatch.setattr(stockrom, "disk_bytes", lambda w: (7634944 * 512, None))
    r = rpcops._watch_dump({"serial": "S1"})
    assert r["ok"] is False and "held" in r["error"] and "wanze" in r["error"]
    assert "S1" not in rpcops._dump_runs, "a run record was created for a refused dump"


def test_a_wanze_run_refuses_to_start_over_a_held_watch(monkeypatch):
    """And the symmetric case: a wanze run must not stamp over a live dump."""
    from asteroid_docking_bay import oplock, wanze
    _dict_config(monkeypatch)
    assert oplock.hold("S1", "dump", "full-disk dump", ttl=600)["ok"]
    r = wanze.probing_set("S1", True, "arm A")
    assert r["ok"] is False and "dump" in r["error"]


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


def test_charge_drain_and_workbench_refuse_a_held_watch(monkeypatch):
    """Every long op actuates the port — charge powers it on, drain cuts VBUS
    and poweroffs at the end, workbench cycles it continuously — so starting one
    over a running dump or wanze run breaks it. The guard was inconsistent at the
    same API: port.set refused a held watch while a charge, which does strictly
    more, started happily."""
    from asteroid_docking_bay import ops
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "note": "full-disk dump",
                               "since": now, "until": now + 600}},
           "hubs": [{"location": "1-2", "port_serials": {"1": "S1"}}]}
    monkeypatch.setattr(ops, "active_op_on_slot", lambda slot: None)
    monkeypatch.setattr(ops, "is_slot_smart", lambda c, l, p: True)
    started = []
    monkeypatch.setattr(ops.threading, "Thread",
                        lambda *a, **k: type("T", (), {
                            "start": lambda self: started.append(1)})())

    for op_cls in (ops.ChargeOp, ops.DrainOp, ops.WorkbenchOp):
        op_cls.tasks.pop("1-2:1", None)
        err = op_cls.start("1-2", 1, cfg)
        assert err and "held" in err, \
            f"{op_cls.kind} started on a watch held for a dump: {err!r}"
    assert not started, "an op thread was launched for a held watch"

    # And the guard must not disable the ops: an unheld watch still starts.
    ops.ChargeOp.tasks.pop("1-2:1", None)
    assert ops.ChargeOp.start("1-2", 1, {"hubs": cfg["hubs"]}) is None
    ops.ChargeOp.tasks.pop("1-2:1", None)


def test_flash_refuses_a_held_watch_and_a_busy_port(monkeypatch):
    """A flash reboots the watch to the bootloader and rewrites it — the most
    destructive thing done to a watch something else may be mid-read of. It
    checked only its own task table, so it would start over a live dump. The
    asymmetry was the tell: Operation.start refuses while a flash runs, and
    flash refused for nothing."""
    from asteroid_docking_bay import rpcops
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "note": "full-disk dump",
                               "since": now, "until": now + 600}}}
    monkeypatch.setattr(rpcops, "load_config", lambda: cfg)
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "_op_owning", lambda l, p: None)
    monkeypatch.setattr(rpcops, "find_codename_for_loc_port", lambda c, l, p: "nemo")
    reached = []
    monkeypatch.setattr(rpcops, "_flash_stream",
                        lambda *a, **k: reached.append(1) or iter(()))

    out = list(rpcops.DISPATCH._stream["flash.start"](
        {"loc": "1-2", "port": 1, "channel": None}))
    assert any("held" in line for line in out), \
        f"a flash started on a watch held for a dump: {out}"
    assert not reached, "the flash stream ran for a held watch"

    # An op owning the port is refused by the same guard (no lock this time, so
    # the refusal can only come from the op-ownership half)...
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "_op_owning", lambda l, p: "drain")
    out = list(rpcops.DISPATCH._stream["flash.start"](
        {"loc": "1-2", "port": 1, "channel": None}))
    assert any("drain" in line for line in out), f"flashed a draining port: {out}"

    # ...and a free, unheld port still flashes (the guard must not disable it).
    monkeypatch.setattr(rpcops, "_op_owning", lambda l, p: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    list(rpcops.DISPATCH._stream["flash.start"](
        {"loc": "1-2", "port": 1, "channel": None}))
    assert reached, "the guard blocked a flash on a free port"


def test_the_manual_mode_switch_ops_refuse_a_held_watch(monkeypatch):
    """Switching USB mode under a running transfer IS the 2026-08-03 regression
    that produced a 0-byte dump, and the oplock docstring names a shell script
    calling exactly these ops as the threat model. The automatic peeler and
    aligner were guarded; the manual entry points were not.

    Guarding watch.switch_ssh also closes the aligner's own gap:
    finish_ssh_relocation calls it up to a minute after the pass that checked
    the lock, so a hold taken in between was invisible until it had fired."""
    from asteroid_docking_bay import rpcops
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "dump", "note": "full-disk dump",
                               "since": now, "until": now + 600}}}
    monkeypatch.setattr(rpcops, "load_config", lambda: cfg)
    switched = []
    monkeypatch.setattr(rpcops, "_switch_ssh_to_adb",
                        lambda ip: switched.append(ip) or {"ok": True})
    monkeypatch.setattr(rpcops, "_run",
                        lambda *a, **k: switched.append(a) or (0, "", ""))

    r = rpcops.DISPATCH._data["ssh.switch_adb"]({"serial": "S1"})
    assert r["ok"] is False and "held" in r["error"]
    r = rpcops.DISPATCH._data["watch.switch_ssh"]({"serial": "S1"})
    assert r["ok"] is False and "held" in r["error"]
    assert switched == [], "a held watch had its USB mode switched"


def test_wear_set_refuses_a_held_watch(monkeypatch):
    """Both branches actuate the port — on powers it up, off can cut VBUS on a
    watch that has left — so it carries the same hazard port.set is guarded
    against, and it was the odd one out."""
    from asteroid_docking_bay import rpcops
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": "wanze", "note": "arm A",
                               "since": now, "until": now + 600}}}
    monkeypatch.setattr(rpcops, "load_config", lambda: cfg)
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "_op_owning", lambda l, p: None)
    powered = []
    monkeypatch.setattr(rpcops, "uhubctl_set_power",
                        lambda l, p, on: powered.append((l, p, on)))

    r = rpcops.DISPATCH._data["wear.set"]({"loc": "1-2", "port": 1, "on": True})
    assert r["ok"] is False and "held" in r["error"]
    assert powered == [], "powered the port of a watch mid-wanze-run"


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


# ── wanze's run marker IS an operation lock ──────────────────────────────────

def test_a_wanze_run_is_an_operation_lock(monkeypatch):
    """A wanze run is a long operation that must not be disturbed, which is
    what oplock exists for. Keeping a second marker meant a wanze run was
    INVISIBLE to the very housekeeping oplock holds off — a run could be
    ended by the stray peeler that this whole mechanism exists to stop."""
    from asteroid_docking_bay import wanze, oplock
    now = time.time()
    cfg = {"op_locks": {"S1": {"kind": wanze.PROBING_KIND, "note": "arm A",
                               "since": now, "until": now + 600}}}
    assert wanze.probing(cfg, "S1")["note"] == "arm A"
    # And the housekeeping now sees it, which it could not before.
    assert oplock.held(cfg, "S1")


def test_probing_reports_only_wanze_runs_not_every_lock():
    """A watch held for a dump is not 'wanze probing'. Reporting it as one
    would put a false claim in the UI about what the watch is doing."""
    from asteroid_docking_bay import wanze
    now = time.time()
    cfg = {"op_locks": {"S2": {"kind": "dump", "since": now, "until": now + 600}}}
    assert wanze.probing(cfg, "S2") is None
    assert wanze.probing(cfg, "MISSING") is None
    assert wanze.probing({}, "S2") is None
    assert wanze.probing(cfg, None) is None


def test_a_wanze_run_still_expires_eventually():
    """Its TTL is long because a run spans days and being left alone is the
    point — but an abandoned run must not exempt a watch from housekeeping
    forever, which a marker with no end would."""
    from asteroid_docking_bay import wanze
    assert wanze.PROBING_TTL_SEC > 24 * 3600, "too short for a real run"
    # An ABSOLUTE upper bound, not one derived from the TTL itself: a test that
    # computes its stale timestamp from PROBING_TTL_SEC passes for any value,
    # including an effectively infinite one, which is the bug it exists to
    # catch. A marker that cannot lapse within a month is not expiring.
    assert wanze.PROBING_TTL_SEC <= 30 * 24 * 3600, \
        "an abandoned run would exempt this watch from housekeeping ~forever"
    stale = time.time() - wanze.PROBING_TTL_SEC - 60
    cfg = {"op_locks": {"S1": {"kind": wanze.PROBING_KIND, "since": stale,
                               "until": stale + wanze.PROBING_TTL_SEC}}}
    assert wanze.probing(cfg, "S1") is None
