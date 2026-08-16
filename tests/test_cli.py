# SPDX-License-Identifier: GPL-3.0-only
"""_web_busy_slots: the check-charge timer's decision-level handoff to a
running web service. Parses active ops from /api/status; empty when it's down."""

import io
import json
import urllib.request

from asteroid_docking_bay.cli import _web_busy_slots

_DOC = {"hubs": [{"location": "1-2", "ports": [
    {"port": 1, "drain": {"active": True}},
    {"port": 2, "charging_active": True},
    {"port": 3},                                   # idle → not busy
    {"port": 4, "workbench": {"active": True}},
    {"port": 5, "flashing": True},
    {"port": 6, "drain": {"active": False}},        # a finished drain → not busy
    {"port": 7, "held": {"kind": "dump", "note": "full-disk dump"}},
    {"port": 8, "held": None},                      # no lock → not busy
]}]}


class _Ctx:
    def __enter__(self):
        return io.BytesIO(json.dumps(_DOC).encode())

    def __exit__(self, *a):
        return False


def test_busy_slots_parsed(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda u, timeout=0: _Ctx())
    assert _web_busy_slots() == {"1-2:1", "1-2:2", "1-2:4", "1-2:5", "1-2:7"}


def test_an_operation_lock_makes_a_slot_busy_for_the_timer(monkeypatch):
    """This timer is a SEPARATE process fired by systemd, unattended, and it
    powers ports on and cuts them off again. The web service's in-process oplock
    guards cannot see it, so without reading `held` a periodic check would cut
    VBUS underneath a running 4 GB dump — the one continuously-running way left
    to break a long transfer."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda u, timeout=0: _Ctx())
    busy = _web_busy_slots()
    assert "1-2:7" in busy, "the timer would act on a watch held for a dump"
    assert "1-2:8" not in busy, "a watch with no lock was treated as busy"


def test_busy_slots_empty_when_web_down(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert _web_busy_slots() == set()


# ── status: fastboot visibility and machine-readable output ─────────────────

def _status_cfg():
    return {"hubs": [{"location": "1-2", "port_smart": {"1": True},
                      "ports": {"1": "sturgeon"}}],
            "serials": {"S1": "sturgeon"}}


def test_status_table_shows_a_watch_in_fastboot(monkeypatch, capsys):
    """A watch in the bootloader is absent from `adb devices`, so consulting
    only adb printed "--" — indistinguishable from unplugged. During a flash
    cycle that reads as "the watch is gone" when it is actually sitting in
    fastboot waiting for the next command."""
    import argparse
    from asteroid_docking_bay import cli
    monkeypatch.setattr(cli, "adb_devices", lambda: {})
    monkeypatch.setattr(cli, "_fastboot_devices", lambda: {"S1": "fastboot"})
    monkeypatch.setattr(cli, "uhubctl_get_power", lambda loc, port: True)
    monkeypatch.setattr(cli, "get_watch_codename", lambda s: None)
    cli.cmd_status(argparse.Namespace(json=False), _status_cfg())
    out = capsys.readouterr().out
    assert "fastboot" in out, f"fastboot state not shown in the table:\n{out}"
    assert "sturgeon" in out


def test_status_json_reuses_the_web_status_document(monkeypatch, capsys):
    """--json must emit the SAME document the web UI renders, not a second
    status implementation that can drift from it."""
    import argparse
    import json as _json
    from asteroid_docking_bay import cli, rpcops
    sentinel = {"hubs": [], "version": "test", "thresholds": {}}
    monkeypatch.setitem(rpcops.DISPATCH._data, "status.get", lambda args: sentinel)
    cli.cmd_status(argparse.Namespace(json=True), _status_cfg())
    assert _json.loads(capsys.readouterr().out) == sentinel


def test_status_json_does_not_touch_hardware(monkeypatch, capsys):
    """The JSON path must not also run the table's own adb/uhubctl scan —
    that would double the hardware work and could disagree with itself."""
    import argparse
    from asteroid_docking_bay import cli, rpcops
    def _boom(*a, **k):
        raise AssertionError("table scan ran during --json")
    monkeypatch.setattr(cli, "adb_devices", _boom)
    monkeypatch.setattr(cli, "uhubctl_get_power", _boom)
    monkeypatch.setitem(rpcops.DISPATCH._data, "status.get", lambda args: {"ok": 1})
    cli.cmd_status(argparse.Namespace(json=True), _status_cfg())
    assert "ok" in capsys.readouterr().out


# ── exact-codename addressing in power commands ─────────────────────────────

def _addr_cfg():
    # Two ports share the rubyfish image; one is a real rubyfish, one a rover
    # (exact codenames recorded, so each is uniquely addressable). Two more
    # share the skipjack image and are BOTH tunnys — genuinely ambiguous by
    # any name except the serial.
    return {
        "hubs": [{"location": "1-2", "ports": {"1": "rubyfish", "2": "skipjack"},
                  "port_serials": {"1": "RUBY1", "2": "TUNNYA"},
                  "port_smart": {"1": True, "2": True}},
                 {"location": "1-2.4", "ports": {"1": "rubyfish", "2": "skipjack"},
                  "port_serials": {"1": "ROVER1", "2": "TUNNYB"},
                  "port_smart": {"1": True, "2": True}}],
        "exact_codenames": {"ROVER1": "rover", "RUBY1": "rubyfish",
                            "TUNNYA": "tunny", "TUNNYB": "tunny"},
    }


def test_on_addresses_the_exact_watch_not_the_first_match(monkeypatch):
    """`on rover` must power rover's port (1-2.4:1), not the first 'rubyfish'
    image port (1-2:1). This is the whole point — a shared image name used to
    hit an arbitrary one."""
    import argparse
    from asteroid_docking_bay import cli
    powered = []
    monkeypatch.setattr(cli, "uhubctl_set_power",
                        lambda loc, port, on: powered.append((loc, port, on)))
    monkeypatch.setattr(cli, "active_op_on_slot", lambda slot: None)
    cli.cmd_on(argparse.Namespace(codename="rover"), _addr_cfg())
    assert powered == [("1-2.4", 1, True)], powered


def test_ambiguous_target_refuses_and_names_the_serials(monkeypatch):
    """`on tunny` matches two physically distinct tunnys — same exact codename,
    different watches. Only the serial can disambiguate, so it must raise and
    name both serials rather than silently pick one."""
    import argparse, pytest
    from asteroid_docking_bay import cli
    from asteroid_docking_bay.config import AmbiguousTargetError
    touched = []
    monkeypatch.setattr(cli, "uhubctl_set_power",
                        lambda *a, **k: touched.append(a))
    monkeypatch.setattr(cli, "active_op_on_slot", lambda slot: None)
    with pytest.raises(AmbiguousTargetError) as ei:
        cli.cmd_on(argparse.Namespace(codename="tunny"), _addr_cfg())
    assert not touched, "powered a port despite ambiguity"
    assert "TUNNYA" in str(ei.value) and "TUNNYB" in str(ei.value)


def test_rubyfish_and_rover_are_each_unique_addresses(monkeypatch):
    """The two watches sharing the rubyfish image are individually addressable
    by their exact codenames — the core win over first-match."""
    import argparse
    from asteroid_docking_bay import cli
    powered = []
    monkeypatch.setattr(cli, "uhubctl_set_power",
                        lambda loc, port, on: powered.append((loc, port)))
    monkeypatch.setattr(cli, "active_op_on_slot", lambda slot: None)
    cli.cmd_on(argparse.Namespace(codename="rubyfish"), _addr_cfg())
    cli.cmd_on(argparse.Namespace(codename="rover"), _addr_cfg())
    assert powered == [("1-2", 1), ("1-2.4", 1)], powered


def test_unique_image_name_and_serial_both_work(monkeypatch):
    import argparse
    from asteroid_docking_bay import cli
    powered = []
    monkeypatch.setattr(cli, "uhubctl_set_power",
                        lambda loc, port, on: powered.append((loc, port)))
    monkeypatch.setattr(cli, "active_op_on_slot", lambda slot: None)
    # addressing by raw serial always works, even for the ambiguous tunnys.
    cli.cmd_on(argparse.Namespace(codename="TUNNYA"), _addr_cfg())
    cli.cmd_on(argparse.Namespace(codename="ROVER1"), _addr_cfg())
    assert powered == [("1-2", 2), ("1-2.4", 1)], powered


def test_cmd_map_registers_topology_and_never_switches_power(monkeypatch):
    """The rewritten map records hub topology + names only: it must keep the
    USB2 side, drop USB3 companions, preserve watch mappings and per-port
    verdicts already learned, auto-name the boxes — and switch no power at all
    (switchability is verified live at runtime instead)."""
    import argparse
    from asteroid_docking_bay import cli

    # discover_hubs() owns finding hubs and classifying them (its own tests
    # cover the USB3/internal/non-PPPS cases); map's job is what it does with
    # the result.
    monkeypatch.setattr(cli, "discover_hubs", lambda: [
        {"location": "1-2", "description": "USB2.1 Hub", "ppps": True,
         "ports": [1, 2, 3, 4], "internal": False},
        {"location": "1-6", "description": "Sabrent", "ppps": False,
         "ports": [1, 2, 3, 4], "internal": False},   # non-PPPS — must register
        {"location": "2-1", "description": "", "ppps": False,
         "ports": [1, 2], "internal": True},          # chipset — must not
    ])
    monkeypatch.setattr(cli, "hub_vendors",
                        lambda: [{"location": "1-2", "vendor": "0bda"}])
    saved: dict = {}
    monkeypatch.setattr(cli, "save_config", lambda cfg: saved.update(cfg=cfg))

    def _no_power(*a, **k):
        raise AssertionError("map must not switch power")
    monkeypatch.setattr(cli, "uhubctl_set_power", _no_power)
    monkeypatch.setattr(cli, "uhubctl_get_power", _no_power)

    cfg = {"hubs": [{"location": "1-2", "ports": {"1": "skipjack"},
                     "port_smart": {"1": True}}]}
    cli.cmd_map(argparse.Namespace(), cfg)

    hubs = {h["location"]: h for h in saved["cfg"]["hubs"]}
    # The non-PPPS hub registers (its watches must be reachable); the
    # chipset-internal one does not (it has no sockets).
    assert set(hubs) == {"1-2", "1-6"}
    assert hubs["1-6"]["ppps"] is False
    assert hubs["1-2"]["ports"] == {"1": "skipjack"}      # mapping preserved
    assert hubs["1-2"]["port_smart"] == {"1": True}       # verdict preserved
    assert saved["cfg"]["hub_names"] == {"1-2": "A16 #1"}  # auto-seeded


def test_cli_power_commands_refuse_a_watch_held_under_an_oplock(monkeypatch, caplog):
    """`on` / `off` / `cycle` must respect the operation lock, not only the
    charge/drain/workbench task store.

    The oplock is the CROSS-PROCESS claim — it lives in the config file
    precisely so a separate process can see it, and it is what a dump, a wanze
    run or a flash holds. `_busy_guard` consulted `active_op_on_slot`, which
    reads the task registries only, so an interactive
    `asteroid-docking-bay off <watch>` would cut VBUS underneath a running
    4 GB dump — the same failure the lock was introduced to prevent.

    The systemd charge timer is NOT the gap: it reads `held` from the live
    /api/status (see _web_busy_slots). But that only works while the web
    service is up, whereas the lock in the config is readable regardless — so
    reading it directly is strictly the stronger check, and the only one an
    interactive command has.
    """
    import time as _t
    from asteroid_docking_bay import cli as c

    cfg = {"hubs": [{"location": "1-2", "ports": {"3": "sturgeon"},
                     "port_serials": {"3": "S1"}}],
           "serials": {"S1": "sturgeon"},
           "op_locks": {"S1": {"kind": "dump", "until": _t.time() + 3600}}}
    monkeypatch.setattr(c, "load_config", lambda: cfg)
    # no charge/drain/workbench task owns the slot — the ONLY claim is the lock
    monkeypatch.setattr(c, "active_op_on_slot", lambda slot: None)

    assert c._busy_guard("sturgeon", "1-2", 3) is True, (
        "a CLI power command would cut VBUS on a watch held for a dump")

    # ...and an expired lock must not block anything
    cfg["op_locks"]["S1"]["until"] = _t.time() - 1
    assert c._busy_guard("sturgeon", "1-2", 3) is False, (
        "an expired lock still refused — locks must lapse, not become immortal")


def test_interactive_charge_respects_a_busy_or_locked_port(monkeypatch):
    """`charge` powers the port, so it needs the same claim check as on/off/
    cycle. It had NONE — neither the operation lock nor the task store.

    Two failures that allowed. During a dump it cut the transfer. During a
    drain test it powered the port mid-run, recharging the watch while the
    test kept sampling — which does not fail loudly, it invents a result.
    That second one is the exact scenario _busy_guard was written for, and
    the charge path was still walking around it.

    The systemd timer is a different entry point (cmd_check_charge) and is
    guarded from the other side by _web_busy_slots, so it is deliberately not
    what this covers."""
    import time as _t
    from asteroid_docking_bay import cli as c

    cfg = {"hubs": [{"location": "1-2", "ports": {"3": "sturgeon"},
                     "port_serials": {"3": "S1"}}],
           "serials": {"S1": "sturgeon"},
           "op_locks": {"S1": {"kind": "dump", "until": _t.time() + 3600}}}
    switched = []
    monkeypatch.setattr(c, "load_config", lambda: cfg)
    monkeypatch.setattr(c, "find_port_for_codename", lambda cf, cn: ("1-2", 3))
    monkeypatch.setattr(c, "uhubctl_set_power",
                        lambda l, p, on: switched.append(on) or True)
    monkeypatch.setattr(c, "active_op_on_slot", lambda slot: None)
    # A tripwire on the first call AFTER the guard. Asserting only on
    # "no power was switched" is not enough: with the guard removed the
    # function runs its real charge loop, and the test hangs instead of
    # failing. This makes the planted bug fail in milliseconds and say why.
    def _past_the_guard(*a, **k):
        raise AssertionError("reached the charge body — the guard did not stop it")
    monkeypatch.setattr(c, "is_port_smart", _past_the_guard)

    ok = c._charge_one("sturgeon", cfg, c.ChargeConfig())
    assert ok is False, "charge proceeded on a watch held for a dump"
    assert switched == [], "charge switched port power on a locked watch"

    # ...and a drain test owning the slot must stop it too, lock or no lock
    cfg["op_locks"] = {}
    monkeypatch.setattr(c, "active_op_on_slot", lambda slot: "drain")
    assert c._charge_one("sturgeon", cfg, c.ChargeConfig()) is False, (
        "charge powered a port mid drain test — the run would keep sampling "
        "a watch that is being recharged and report the result as real")
    assert switched == [], "charge switched power during a drain test"
