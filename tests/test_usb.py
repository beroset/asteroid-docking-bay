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
