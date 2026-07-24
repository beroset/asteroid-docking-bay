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
]}]}


class _Ctx:
    def __enter__(self):
        return io.BytesIO(json.dumps(_DOC).encode())

    def __exit__(self, *a):
        return False


def test_busy_slots_parsed(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda u, timeout=0: _Ctx())
    assert _web_busy_slots() == {"1-2:1", "1-2:2", "1-2:4", "1-2:5"}


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

    monkeypatch.setattr(cli, "uhubctl_list", lambda: [
        {"location": "1-2", "description": "USB2.1 Hub", "ppps": True,
         "ports": [1, 2, 3, 4]},
        {"location": "3-2", "description": "USB2.1 Hub, USB 3.0", "ppps": True,
         "ports": [1, 2]},                         # USB3 companion — must drop
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
    assert set(hubs) == {"1-2"}                     # USB3 companion filtered out
    assert hubs["1-2"]["ports"] == {"1": "skipjack"}      # mapping preserved
    assert hubs["1-2"]["port_smart"] == {"1": True}       # verdict preserved
    assert saved["cfg"]["hub_names"] == {"1-2": "A16 #1"}  # auto-seeded
