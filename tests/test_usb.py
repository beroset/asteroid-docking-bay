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


def test_hub_vendors_skips_superspeed_and_non_hubs(tmp_path, monkeypatch):
    """hub_vendors names physical boxes off the USB2 side only. It must skip the
    SuperSpeed companion (else every A16 counts twice) and any non-hub device."""
    from asteroid_docking_bay import usb as u
    monkeypatch.setattr(u, "_SYSFS_USB", tmp_path)

    def mk(name, cls, speed, vid):
        d = tmp_path / name; d.mkdir()
        (d / "bDeviceClass").write_text(cls + "\n")
        (d / "speed").write_text(speed + "\n")
        (d / "idVendor").write_text(vid + "\n")

    mk("1-2", "09", "480", "0bda")      # USB2 A16 — kept
    mk("3-2", "09", "5000", "0bda")     # USB3 companion — skipped
    mk("1-9", "09", "480", "17ef")      # USB2 dock — kept
    mk("1-2.1", "00", "480", "18d1")    # a watch (not a hub) — skipped
    (tmp_path / "1-2:1.0").mkdir()      # an interface dir — skipped

    locs = {h["location"] for h in u.hub_vendors()}
    assert locs == {"1-2", "1-9"}


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


def test_topology_fingerprint_tracks_devices_ignores_interfaces(monkeypatch):
    """The status cache busts on this fingerprint, so it must change exactly
    when a DEVICE appears or vanishes. Interface dirs (colon entries) are
    excluded — they churn on driver binds without any enumeration change.
    Planted-bug: drop the ':' filter and the interface assertion fails."""
    import asteroid_docking_bay.usb as usb
    base = ["1-3", "1-3.2", "usb1"]
    monkeypatch.setattr(usb.os, "listdir", lambda p: list(base))
    fp0 = usb.usb_topology_fingerprint()
    monkeypatch.setattr(usb.os, "listdir", lambda p: base + ["1-3.2:1.0"])
    assert usb.usb_topology_fingerprint() == fp0      # interfaces don't count
    monkeypatch.setattr(usb.os, "listdir", lambda p: base + ["1-3.3"])
    assert usb.usb_topology_fingerprint() != fp0      # a device does


def test_the_bus_scan_finds_a_watch_whose_bootloader_is_not_google(tmp_path, monkeypatch):
    """A vendor-ID filter cannot be complete, so it must not be the only way in.

    The AsteroidOS gadget and Wear OS adb both enumerate as Google's 18d1, so
    for a RUNNING watch one ID covers the fleet. Bootloaders do not follow that
    rule — they are the vendor's own code with the vendor's own ID. sparrow
    runs as 18d1:0a02 and sits in fastboot as 0b05:7771 (ASUSTek), so a scan
    filtered on Google alone reported "no watches on the bus" while sparrow was
    plugged in and waiting to be flashed.

    Two ways in now: a known vendor ID, or a path the caller already knows
    holds a watch (what fastboot or adb reported). The second is what keeps an
    unlisted bootloader — Mobvoi and Fossil have not been observed here — from
    being invisible."""
    from asteroid_docking_bay import usb as u

    root = tmp_path / "devices"
    root.mkdir()
    def dev(name, vid, pid, product, serial, cls="00"):
        d = root / name
        d.mkdir()
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
        (d / "product").write_text(product + "\n")
        (d / "serial").write_text(serial + "\n")
        (d / "bDeviceClass").write_text(cls + "\n")
    dev("1-1", "0b05", "7771", "Android", "H1NZCJ010087020")       # ASUS bootloader
    dev("1-2", "18d1", "d001", "beluga", "100c0a32")               # running watch
    dev("1-3", "1234", "5678", "Mystery Watch", "MYSTERY1")        # unlisted vendor
    dev("1-4", "0bda", "5411", "USB2.1 Hub", "", cls="09")         # a hub
    monkeypatch.setattr(u, "_SYSFS_USB", root)

    by_vendor = {d["path"] for d in u.watch_devices_on_bus()}
    assert by_vendor == {"1-1", "1-2"}, (
        f"the ASUS bootloader or the running watch was missed: {by_vendor}")
    assert "1-4" not in by_vendor, "a hub was reported as a watch"

    # fastboot named the unlisted one -> it must be included whatever its ID is
    with_known = {d["path"] for d in u.watch_devices_on_bus({"1-3"})}
    assert with_known == {"1-1", "1-2", "1-3"}, (
        "a watch fastboot can name was still filtered out by its vendor ID")

    asus = next(d for d in u.watch_devices_on_bus() if d["path"] == "1-1")
    assert asus["vendor"] == "0b05", "the vendor id is not reported for triage"


def test_gadget_composition_reads_interfaces_not_idproduct(tmp_path, monkeypatch):
    """What a watch offers must be read from its INTERFACE list.

    idProduct cannot answer it. `0afe` is used by BOTH the initramfs gadget,
    which carries adb, and usb-moded's charging-only fallback, which carries
    nothing useful -- so the same product ID means "fine" or "dead" depending
    on interfaces alone. It is also not a boot-state signal: the porting
    session read 0afe as "stuck in initramfs" and diagnosed a boot hang while
    the watch sat in LPM with a working launcher.

    The dead composition matters operationally: a port power CYCLE cannot fix
    it, because the problem is the gadget composition rather than the
    enumeration, so cycling re-enumerates the same broken gadget. Only a reboot
    recovers it.
    """
    import asteroid_docking_bay.usb as usb

    def mkiface(path, name, cls, sub, proto, driver=None):
        d = tmp_path / f"{path}:{name}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bInterfaceClass").write_text(cls + "\n")
        (d / "bInterfaceSubClass").write_text(sub + "\n")
        (d / "bInterfaceProtocol").write_text(proto + "\n")
        if driver:
            drv = tmp_path / "drivers" / driver
            drv.mkdir(parents=True, exist_ok=True)
            (d / "driver").symlink_to(drv)
        return d

    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)

    # sol as measured on the rig: adb + NCM control + CDC data
    mkiface("1-3.4.3", "1.0", "ff", "42", "01", "usbfs")
    mkiface("1-3.4.3", "1.1", "02", "0d", "00", "cdc_ncm")
    mkiface("1-3.4.3", "1.2", "0a", "00", "01", "cdc_ncm")
    both = usb.gadget_composition("1-3.4.3")
    assert both["adb"] and both["ncm"] and not both["mass_storage_only"]

    # aurora as measured: adb only
    mkiface("1-3.4.2", "1.0", "ff", "42", "01", "usbfs")
    only_adb = usb.gadget_composition("1-3.4.2")
    assert only_adb["adb"] and not only_adb["ncm"]
    assert not only_adb["mass_storage_only"], (
        "a healthy adb-only watch was reported as the dead composition")

    # The initramfs gadget carries adb ALONGSIDE mass storage. Storage present
    # is therefore not the signal -- storage WITHOUT adb or network is. Missing
    # this distinction would recommend a reboot to a perfectly healthy watch.
    mkiface("1-3.4.5", "1.0", "ff", "42", "01", "usbfs")
    mkiface("1-3.4.5", "1.1", "08", "06", "50")
    with_storage = usb.gadget_composition("1-3.4.5")
    assert with_storage["adb"] and not with_storage["mass_storage_only"], (
        "a watch offering adb alongside mass storage was called dead")

    # usb-moded's charging-only fallback: mass storage and nothing else
    mkiface("1-3.4.4", "1.0", "08", "06", "50")
    dead = usb.gadget_composition("1-3.4.4")
    assert dead["mass_storage_only"] and not dead["adb"] and not dead["ncm"], (
        "the charging-only fallback was not recognised -- a-d-b would offer a "
        "port cycle, which cannot fix a composition problem")

    assert usb.gadget_composition("9-9") == {
        "adb": False, "ncm": False, "mass_storage_only": False, "interfaces": []}


def test_netdev_is_resolved_by_topology_not_by_name(monkeypatch):
    """Interface names are generated from the USB path (enp0s20u3u4u3i1), differ
    per port and per host, and adjacent watches differ by ONE character. The
    porting session lost an evening to an ssh attempt against a neighbour's
    name. Resolve through /sys/class/net/*/device instead.

    The neighbouring path is the trap this pins: `1-3.4.3` must not match a
    device sitting at `1-3.4.30`, which a naive substring test accepts.
    """
    import asteroid_docking_bay.usb as usb
    # The longer sibling comes FIRST on purpose: a substring match would return
    # it for "1-3.4.3" and the test would pass on luck otherwise.
    tree = {
        "/sys/class/net/enp0s20u3u4u9i1":
            "/sys/devices/pci0000:00/usb1/1-3/1-3.4/1-3.4.30/1-3.4.30:1.1",
        "/sys/class/net/enp0s20u3u4u3i1":
            "/sys/devices/pci0000:00/usb1/1-3/1-3.4/1-3.4.3/1-3.4.3:1.1",
        "/sys/class/net/enp0s25": "/sys/devices/pci0000:00/0000:00:19.0",
    }
    monkeypatch.setattr(usb.glob, "glob", lambda pat: list(tree))
    monkeypatch.setattr(usb.os.path, "realpath", lambda p: tree[p.rsplit("/device", 1)[0]])

    assert usb.usb_netdev_for("1-3.4.3") == "enp0s20u3u4u3i1"
    assert usb.usb_netdev_for("1-3.4.30") == "enp0s20u3u4u9i1", (
        "a longer sibling path was shadowed by its prefix")
    assert usb.usb_netdev_for("1-3.4.2") is None
