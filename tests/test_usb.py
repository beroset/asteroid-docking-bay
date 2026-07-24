# SPDX-License-Identifier: GPL-3.0-only
"""Pure-logic tests for uhubctl output parsing and hub/port path math."""

from asteroid_docking_bay.usb import _parse_hub_port_path, parse_uhubctl_status

# Trimmed from a real `uhubctl` run on the rig (RTS5411 cascade + root hub).
SAMPLE = """Current status for hub 1-2.3.3 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0503 power highspeed enable connect [18d1:d001 LGE G Watch R 411KPCA0121867]
  Port 2: 0100 power
  Port 3: 0000 off
  Port 4: 0000 off
Current status for hub 1-2 [0bda:5411 Generic USB2.1 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0000 off
  Port 2: 0503 power highspeed enable connect [0bda:5411 Generic USB2.1 Hub]
"""


def test_parse_hubs_and_ports():
    hubs = parse_uhubctl_status(SAMPLE)
    assert [h["location"] for h in hubs] == ["1-2.3.3", "1-2"]
    assert hubs[0]["ports"] == [1, 2, 3, 4]
    assert hubs[0]["ppps"] is True


def test_parse_power_and_connect():
    h = parse_uhubctl_status(SAMPLE)[0]
    assert h["power"] == {1: True, 2: True, 3: False, 4: False}
    assert h["connect"] == {1: True, 2: False, 3: False, 4: False}


def test_parse_description():
    h = parse_uhubctl_status(SAMPLE)[0]
    assert "Generic USB2.1 Hub" in h["description"]


def test_parse_empty():
    assert parse_uhubctl_status("") == []
    # Port lines without a hub header are ignored, not crashed on.
    assert parse_uhubctl_status("  Port 1: 0100 power\n") == []


def test_hub_port_path():
    # A device path splits into (its hub's location, port on that hub).
    assert _parse_hub_port_path("1-6.4.1") == ("1-6.4", 1)
    assert _parse_hub_port_path("1-6.2") == ("1-6", 2)
    # Direct host ports have no hub in the path.
    assert _parse_hub_port_path("1-3") is None
    assert _parse_hub_port_path("1-6.x") is None


def test_foreign_guard_accepts_known_serial(monkeypatch, tmp_path):
    """A watch with a non-Google USB identity (hacked/vendor VID) must not be
    classified foreign when its serial is adb/fastboot-visible — found on a
    Ticwatch E that map refused to map while protecting it."""
    from asteroid_docking_bay import usb
    child = tmp_path / "9-9.1"
    child.mkdir()
    (child / "idVendor").write_text("c027\n")       # Mobvoi-style, not 18d1
    (child / "idProduct").write_text("0001\n")
    (child / "product").write_text("Ticwatch E\n")
    (child / "serial").write_text("M6600TB1Z300\n")
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)

    assert usb.port_foreign_device("9-9", 1) == "Ticwatch E"
    assert usb.port_foreign_device("9-9", 1, {"M6600TB1Z300"}) is None
    assert usb.port_foreign_device("9-9", 1, {"other"}) == "Ticwatch E"


def test_port_power_spans_both_usb2_and_usb3_sides(tmp_path, monkeypatch):
    """On a USB3-connected hub VBUS is shared between the USB2 port and its USB3
    peer; cutting only one side leaves it powered (measured on the rig 2026-07-24
    — every port read OFF while physically ON). get/set must span both: on if
    EITHER side is enabled, and a set writes BOTH sides."""
    from asteroid_docking_bay import usb as u
    a = tmp_path / "a"; b = tmp_path / "b"
    a.write_text("1"); b.write_text("1")
    monkeypatch.setattr(u, "_sysfs_disable_paths", lambda loc, port: [a, b])
    assert u._sysfs_get_power("1-2", 1) is False           # both disabled → off
    a.write_text("0")
    assert u._sysfs_get_power("1-2", 1) is True            # one side on → on
    assert u._sysfs_set_power("1-2", 1, True) is True
    assert a.read_text() == "0" and b.read_text() == "0"
    assert u._sysfs_set_power("1-2", 1, False) is True
    assert a.read_text() == "1" and b.read_text() == "1"   # BOTH cut — the fix


def test_sysfs_power_complete_only_when_all_vbus_sides_reachable(tmp_path, monkeypatch):
    """A hub behind a dock (Hub B) exposes only the USB2-side `disable` — no leaf
    `peer`, no USB3-side sysfs — so cutting via sysfs leaves the shared VBUS up.
    _sysfs_power_is_complete detects that (USB3-connected but < 2 disable paths)
    so callers fall back to companion-aware uhubctl. Root-connected hubs (2 paths)
    and true USB2-only hubs (1 path, no companion) stay on the fast sysfs path."""
    from asteroid_docking_bay import usb as u
    monkeypatch.setattr(u, "_SYSFS_USB", tmp_path)

    # Hub B leaf: cascade port 1-9.1.3-port3 has a peer, leaf port has none.
    casc = tmp_path / "1-9.1.3:1.0" / "1-9.1.3-port3"; casc.mkdir(parents=True)
    (casc / "peer").symlink_to(tmp_path)                    # peer exists → USB3-connected
    assert u._hub_has_usb3_companion("1-9.1.3.3") is True
    monkeypatch.setattr(u, "_sysfs_disable_paths", lambda loc, port: [tmp_path / "d"])
    assert u._sysfs_power_is_complete("1-9.1.3.3", 4) is False   # 1 path + companion

    # Hub A root leaf: both sides reachable (2 disable paths).
    monkeypatch.setattr(u, "_sysfs_disable_paths", lambda loc, port: [tmp_path / "d", tmp_path / "e"])
    assert u._sysfs_power_is_complete("1-2.4", 2) is True

    # True USB2-only hub: 1 path, no peer anywhere → sysfs is sufficient.
    monkeypatch.setattr(u, "_sysfs_disable_paths", lambda loc, port: [tmp_path / "d"])
    assert u._hub_has_usb3_companion("9-9.9") is False
    assert u._sysfs_power_is_complete("9-9.9", 1) is True


def test_set_power_uses_uhubctl_when_sysfs_cannot_reach_companion(tmp_path, monkeypatch):
    """When sysfs can't cut every VBUS side, uhubctl_set_power must NOT trust the
    sysfs fast path (it would half-cut and falsely confirm); it must drive the
    companion-aware uhubctl instead."""
    from asteroid_docking_bay import usb as u
    monkeypatch.setattr(u, "_sysfs_power_is_complete", lambda loc, port: False)
    sysfs_called = []
    monkeypatch.setattr(u, "_sysfs_set_power", lambda l, p, o: sysfs_called.append((l, p, o)) or True)
    calls = []
    monkeypatch.setattr(u, "_uhubctl_exec", lambda cmd: calls.append(cmd) or (0, "", ""))
    monkeypatch.setattr(u, "uhubctl_get_power", lambda l, p: False)
    monkeypatch.setattr(u.power_cache, "put", lambda *a, **k: None)

    assert u.uhubctl_set_power("1-9.1.3.3", 4, False) is True
    assert sysfs_called == []                               # fast path skipped
    assert calls and "-a off" in calls[0]                   # uhubctl drove it
