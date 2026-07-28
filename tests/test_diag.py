# SPDX-License-Identifier: GPL-3.0-only
"""The a-d-b-doctor parser: raw one-round-trip capture -> structured diag."""

from asteroid_docking_bay.diag import parse_diag

SAMPLE = """\
---wakeup---
name\tactive_count\tevent_count\twakeup_count\texpire_count\tactive_since\ttotal_time\tmax_time\tlast_change\tprevent_suspend_time
mce_input_handler \t1\t1\t0\t0\t0\t0\t0\t33195\t0
alarmtimer \t4\t4\t0\t0\t0\t8123\t4000\t99\t7500
---suspend---
0
0
---freq---
800000 100
1094400 900
---emmc---
0x07 0x09
01
---failed---
swclock-offset-boot.service loaded failed failed
---errors---
Jul 27 kernel: something bad
---boots---
2
---psi---
---batfull---
300000
400000
---irq---
           CPU0       CPU1
 18:     100      50       GIC  20  arch_t
 20:       5       5       GIC  39  arch_m
"""


def test_parse_diag_full_sample():
    """Every section lands structured; wakeup sources sort blockers first
    (prevent_suspend_time), eMMC hex maps to JEDEC decile bands. Planted-bug:
    shift the JEDEC band mapping by one and the 60-70% assertion fails."""
    d = parse_diag(SAMPLE)
    assert d["wakeup_sources"][0]["name"] == "alarmtimer"      # blocker first
    assert d["wakeup_sources"][0]["prevent_ms"] == 7500
    assert d["suspend"] == {"success": 0, "fail": 0}
    assert d["freq_residency"] == [{"mhz": 800, "pct": 10.0},
                                   {"mhz": 1094, "pct": 90.0}]
    assert d["emmc_life"] == ["60-70% used", "80-90% used"]
    assert d["emmc_pre_eol"] == "normal"
    assert d["failed_units"] == ["swclock-offset-boot.service"]
    assert d["boots"] == 1
    assert d["bat_capacity_pct"] == 75.0
    assert d["top_irqs"][0]["irq"] == "18" and d["top_irqs"][0]["count"] == 150
    assert "psi" not in d


def test_parse_diag_degrades_to_empty():
    d = parse_diag("---wakeup---\n")
    assert d["wakeup_sources"] == [] and d["failed_units"] == []


# ── benchymark driving ──────────────────────────────────────────────────────

def test_installer_clears_a_stale_assets_tree():
    """The withdrawn nutty-benchy watchface used to be installed as assets/
    beside the package. An upgrade must clear it, or an install tree keeps
    carrying files nothing reads. Planted-bug: drop the rm line and this
    fails."""
    from pathlib import Path
    sh = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    assert 'rm -rf "${LIB_DIR}/assets"' in sh
    assert 'cp -r assets' not in sh


def test_newest_ipk_ignores_sibling_packages(tmp_path, monkeypatch):
    """The build emits benchymark-src/-dev/-dbg beside the real package, and
    installing one of those puts no app on the watch. Planted-bug: widen the
    glob to benchymark* and this picks -src."""
    from asteroid_docking_bay import bench
    d = tmp_path / "armv7vehf-neon"
    d.mkdir()
    real = d / "benchymark_1.0-r0_armv7vehf-neon.ipk"
    real.write_text("real")
    for sib in ("benchymark-src", "benchymark-dev", "benchymark-dbg"):
        (d / f"{sib}_1.0-r0_armv7vehf-neon.ipk").write_text("nope")
    monkeypatch.setattr(bench, "IPK_DIR", tmp_path)
    assert bench.newest_ipk() == str(real)


def test_newest_ipk_returns_none_when_nothing_built(tmp_path, monkeypatch):
    """A missing build directory must read as 'nothing built' rather than
    raising — the UI button carries no arguments and the op turns this into a
    readable error."""
    from asteroid_docking_bay import bench
    monkeypatch.setattr(bench, "IPK_DIR", tmp_path / "does-not-exist")
    assert bench.newest_ipk() is None


# ── onboard task hygiene ────────────────────────────────────────────────────

def test_task_active_ignores_a_task_past_its_deadline():
    """A worker that dies without clearing `done` used to dim its row for the
    life of the service and refuse every later onboard on that port — one
    stuck onboard made the whole rig look full. A deadline lets the UI recover
    on its own. Planted-bug: drop the deadline branch from task_active and
    this fails."""
    import time
    from asteroid_docking_bay.tasks import task_active
    tasks = {"1-2:3": {"done": False, "deadline": time.monotonic() - 1}}
    assert task_active(tasks, "1-2:3") is False


def test_task_active_still_reports_a_live_task():
    """The deadline must not make a genuinely running task look finished —
    that would let two onboards power ports at once, which is the flood the
    serialization exists to prevent."""
    import time
    from asteroid_docking_bay.tasks import task_active
    assert task_active({"s": {"done": False,
                              "deadline": time.monotonic() + 300}}, "s") is True
    assert task_active({"s": {"done": False}}, "s") is True      # no deadline
    assert task_active({"s": {"done": True,
                              "deadline": time.monotonic() + 300}}, "s") is False
    assert task_active({}, "s") is False


def test_onboard_does_not_hold_the_adb_lock_across_its_waits():
    """Onboarding must stay serial (it owns port power) without blocking every
    other ADB operation for the length of a cold-boot window. So the flow is
    guarded by _onboard_lock, and _adb_lock is taken only around the power
    changes. Planted-bug: put the whole flow back under _adb_lock and this
    fails."""
    import inspect
    from asteroid_docking_bay import rpcops
    src = inspect.getsource(rpcops._onboard_stream)
    assert "with _onboard_lock:" in src
    # _adb_lock appears only in the two short power-change blocks
    assert src.count("with _adb_lock:") == 2
    assert "uhubctl_set_power" in src and "uhubctl_cycle" in src


# ── hub discovery (non-PPPS hubs must register too) ─────────────────────────

def _fake_hub(root, loc, vendor, cls="09", speed="480", ports=4, product=""):
    dev = root / loc
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "bDeviceClass").write_text(cls + "\n")
    (dev / "speed").write_text(speed + "\n")
    (dev / "idVendor").write_text(vendor + "\n")
    if product:
        (dev / "product").write_text(product + "\n")
    iface = root / f"{loc}:1.0"
    iface.mkdir(parents=True, exist_ok=True)
    for n in range(1, ports + 1):
        (iface / f"{loc}-port{n}").mkdir(exist_ok=True)


def test_discover_hubs_registers_hubs_uhubctl_cannot_see(tmp_path, monkeypatch):
    """uhubctl reports ONLY power-switchable hubs, so the Sabrent's Genesys
    chips were invisible to map and could not be registered at all — with
    watches sitting on them. sysfs must find them and mark them non-PPPS.
    Planted-bug: build the list from uhubctl_list() again and the Sabrent
    chips vanish."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)
    _fake_hub(tmp_path, "1-3", "0bda", product="Realtek A16")     # switchable
    _fake_hub(tmp_path, "1-6", "05e3", product="USB2.1 Hub")      # Sabrent
    monkeypatch.setattr(usb, "uhubctl_list",
                        lambda: [{"location": "1-3", "ppps": True,
                                  "description": "0bda:5411 Generic USB2.1 Hub"}])
    by = {h["location"]: h for h in usb.discover_hubs()}
    assert set(by) == {"1-3", "1-6"}
    assert by["1-3"]["ppps"] is True
    assert by["1-6"]["ppps"] is False          # honest: nothing can cut its VBUS
    assert by["1-6"]["ports"] == [1, 2, 3, 4]
    assert by["1-6"]["description"] == "USB2.1 Hub"     # from sysfs `product`


def test_discover_hubs_skips_usb3_companions_and_non_hubs(tmp_path, monkeypatch):
    """A USB 3.x companion mirrors the same physical box, so counting it would
    double every hub; a non-hub device is not a hub at all."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)
    monkeypatch.setattr(usb, "uhubctl_list", lambda: [])
    _fake_hub(tmp_path, "1-3", "0bda")                            # keep
    _fake_hub(tmp_path, "3-5", "0bda", speed="5000")              # USB3 — skip
    _fake_hub(tmp_path, "1-4", "0bda", cls="00")                  # not a hub
    _fake_hub(tmp_path, "1-5", "0bda", ports=0)                   # no ports
    assert [h["location"] for h in usb.discover_hubs()] == ["1-3"]


def test_discover_hubs_flags_chipset_internal_hubs(tmp_path, monkeypatch):
    """Intel's 8087:8000 rate-matching hub is soldered between the controller
    and the real ports — it has no sockets, so map must not grow rows for it.
    Flagged rather than dropped, so map can report what it passed over."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)
    monkeypatch.setattr(usb, "uhubctl_list", lambda: [])
    _fake_hub(tmp_path, "2-1", "8087", ports=8)
    _fake_hub(tmp_path, "1-3", "0bda")
    by = {h["location"]: h for h in usb.discover_hubs()}
    assert by["2-1"]["internal"] is True
    assert by["1-3"]["internal"] is False


def test_discover_hubs_survives_uhubctl_being_absent(tmp_path, monkeypatch):
    """No uhubctl, or no permission: every hub registers as non-PPPS, which is
    honest — nothing can switch power without it. It must not raise, or map
    dies on a rig where uhubctl is not installed."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)

    def _boom():
        raise RuntimeError("uhubctl not found")
    monkeypatch.setattr(usb, "uhubctl_list", _boom)
    _fake_hub(tmp_path, "1-3", "0bda")
    hubs = usb.discover_hubs()
    assert len(hubs) == 1 and hubs[0]["ppps"] is False


# ── sysfs-first port identification ─────────────────────────────────────────

def _fake_dev(root, loc, port, vid="18d1", pid="d001", serial="ABC123",
              ifaces=(("ff", "42", "01"),), cls="00", configured=True):
    dev = root / f"{loc}.{port}"
    dev.mkdir(parents=True, exist_ok=True)
    (dev / "idVendor").write_text(vid + "\n")
    (dev / "idProduct").write_text(pid + "\n")
    (dev / "bDeviceClass").write_text(cls + "\n")
    (dev / "bConfigurationValue").write_text(("1" if configured else "0") + "\n")
    if serial:
        (dev / "serial").write_text(serial + "\n")
    for n, (c, sc, pr) in enumerate(ifaces):
        i = root / f"{loc}.{port}" / f"{loc}.{port}:1.{n}"
        i.mkdir(parents=True, exist_ok=True)
        (i / "bInterfaceClass").write_text(c + "\n")
        (i / "bInterfaceSubClass").write_text(sc + "\n")
        (i / "bInterfaceProtocol").write_text(pr + "\n")


def _patch_sysfs(monkeypatch, root):
    """port_device_info builds its path from a module-level string, so the
    whole prefix has to be redirected for a test."""
    from asteroid_docking_bay import usb
    real = usb.Path

    class _P(type(real())):
        pass
    monkeypatch.setattr(usb, "Path", lambda p: real(str(p).replace(
        "/sys/bus/usb/devices", str(root))))


def test_port_device_info_names_each_link_type(tmp_path, monkeypatch):
    """A row built only from `adb devices` shows EMPTY for a watch sitting
    there in fastboot or storage mode. Each descriptor must be named.
    Planted-bug: drop the fastboot signature from _LINK_BY_IFACE and the
    fastboot watch reads as 'unknown'."""
    from asteroid_docking_bay import usb
    _patch_sysfs(monkeypatch, tmp_path)
    _fake_dev(tmp_path, "1-3", 1, ifaces=(("ff", "42", "01"),))
    _fake_dev(tmp_path, "1-3", 2, ifaces=(("ff", "42", "03"),))
    _fake_dev(tmp_path, "1-3", 3, ifaces=(("e0", "01", "03"),))
    _fake_dev(tmp_path, "1-3", 4, ifaces=(("08", "06", "50"),))
    assert usb.port_device_info("1-3", 1)["link"] == "adb"
    assert usb.port_device_info("1-3", 2)["link"] == "fastboot"
    assert usb.port_device_info("1-3", 3)["link"] == "rndis"
    assert usb.port_device_info("1-3", 4)["link"] == "storage"


def test_port_device_info_reports_an_unfamiliar_vendor(tmp_path, monkeypatch):
    """The ASUS builds present vendor 0afe. Identification must not be gated
    on a vendor allow-list, or those watches stay invisible."""
    from asteroid_docking_bay import usb
    _patch_sysfs(monkeypatch, tmp_path)
    _fake_dev(tmp_path, "1-3", 1, vid="0afe", pid="dead", ifaces=(("ff", "ff", "ff"),))
    d = usb.port_device_info("1-3", 1)
    assert d["link"] == "unknown" and d["vid"] == "0afe"
    assert d["serial"] == "ABC123"


def test_port_device_info_flags_the_unconfigured_state(tmp_path, monkeypatch):
    """xHCI slot exhaustion enumerates a device and never configures it: on
    the bus and unusable, which reads identically to a broken watch unless it
    is named. Planted-bug: treat "0" as configured and this fails."""
    from asteroid_docking_bay import usb
    _patch_sysfs(monkeypatch, tmp_path)
    _fake_dev(tmp_path, "1-3", 1, configured=False)
    _fake_dev(tmp_path, "1-3", 2, configured=True)
    assert usb.port_device_info("1-3", 1)["configured"] is False
    assert usb.port_device_info("1-3", 2)["configured"] is True


def test_port_device_info_preserves_serial_case(tmp_path, monkeypatch):
    """Serials are case-sensitive identifiers and must match what adb reports.
    Planted-bug: lowercase in _read_attr and 720EX8C130737 stops matching."""
    from asteroid_docking_bay import usb
    _patch_sysfs(monkeypatch, tmp_path)
    _fake_dev(tmp_path, "1-3", 1, serial="720EX8C130737")
    assert usb.port_device_info("1-3", 1)["serial"] == "720EX8C130737"


def test_port_device_info_ignores_hub_chips_and_empty_ports(tmp_path, monkeypatch):
    """A cascade port holds a hub chip, not a watch; reporting it as a device
    would put a phantom entry on every cascade row."""
    from asteroid_docking_bay import usb
    _patch_sysfs(monkeypatch, tmp_path)
    _fake_dev(tmp_path, "1-3", 1, vid="0bda", pid="5411", cls="09", serial="")
    assert usb.port_device_info("1-3", 1) is None
    assert usb.port_device_info("1-3", 9) is None          # nothing there


# ── xHCI slot budget and the powered-port governor ──────────────────────────

def test_xhci_slots_counts_only_xhci_buses(tmp_path, monkeypatch):
    """EHCI buses draw from a different pool, so counting them would make the
    budget look tighter than it is. Planted-bug: count every bus and the EHCI
    device inflates `used`."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)
    monkeypatch.setattr(usb, "xhci_buses", lambda: ["1", "3"])
    for name in ("1-1", "1-2", "1-2.1", "3-1", "2-1", "2-1.4"):
        (tmp_path / name).mkdir()
    (tmp_path / "1-1:1.0").mkdir()                 # interface dir, not a device
    s = usb.xhci_slots()
    assert s["used"] == 4                          # the two bus-2 devices excluded
    assert s["max"] == 32


def test_xhci_slots_max_is_overridable(tmp_path, monkeypatch):
    """The real figure lives in a register readable only as root, so it is a
    per-controller constant — a different controller must be configurable
    rather than silently wrong."""
    from asteroid_docking_bay import usb
    monkeypatch.setattr(usb, "_SYSFS_USB", tmp_path)
    monkeypatch.setattr(usb, "xhci_buses", lambda: [])
    assert usb.xhci_slots(64)["max"] == 64
    assert usb.xhci_slots(None)["max"] == usb.XHCI_DEFAULT_MAX_SLOTS


def test_powered_port_count_ignores_non_ppps_hubs(monkeypatch):
    """A non-PPPS hub's ports are permanently live and cannot be switched off,
    so charging them to the budget would put the rig over any sane cap before
    a single switchable port came on. Planted-bug: drop the ppps filter and
    the Sabrent's four powered ports get counted."""
    from asteroid_docking_bay import rpcops
    cfg = {"hubs": [{"location": "1-3", "ppps": True},
                    {"location": "1-6", "ppps": False}]}
    monkeypatch.setattr(rpcops, "_sysfs_hub_scan", lambda c: [
        {"location": "1-3", "power": {1: True, 2: True, 3: False, 4: None}},
        {"location": "1-6", "power": {1: True, 2: True, 3: True, 4: True}},
    ])
    assert rpcops._powered_port_count(cfg) == 2


def test_power_on_refused_when_the_slot_pool_is_exhausted(monkeypatch):
    """Past the slot limit the controller enumerates a device and never
    configures it, so powering another port on cannot work — it manufactures a
    watch that looks present and broken. Refuse with the reason instead."""
    from asteroid_docking_bay import rpcops
    monkeypatch.setattr(rpcops, "xhci_slots",
                        lambda m=None: {"used": 32, "max": 32, "buses": ["1"]})
    out = rpcops._refuse_if_bus_full({})
    assert out and out["ok"] is False and "device slots" in out["error"]


def test_power_on_allowed_with_slots_free_and_no_cap(monkeypatch):
    """Default config must not change behaviour: no cap set, slots available,
    the power-on proceeds."""
    from asteroid_docking_bay import rpcops
    monkeypatch.setattr(rpcops, "xhci_slots",
                        lambda m=None: {"used": 21, "max": 32, "buses": ["1"]})
    assert rpcops._refuse_if_bus_full({}) is None


def test_max_powered_ports_policy_refuses_at_the_cap(monkeypatch):
    """The soft, opt-in half: a user policy keeping the powered set near the
    handful actually being worked on."""
    from asteroid_docking_bay import rpcops
    monkeypatch.setattr(rpcops, "xhci_slots",
                        lambda m=None: {"used": 10, "max": 32, "buses": ["1"]})
    monkeypatch.setattr(rpcops, "_powered_port_count", lambda cfg: 6)
    assert rpcops._refuse_if_bus_full({"max_powered_ports": 6})["ok"] is False
    assert rpcops._refuse_if_bus_full({"max_powered_ports": 8}) is None


# ── udev event-driven discovery ─────────────────────────────────────────────

def test_parse_event_reads_device_events_and_ignores_interfaces():
    """One physical plug emits the device plus one event per interface.
    Interface events must report dev=None so callers can drop the duplicates
    instead of running a full scan for each. Planted-bug: let the devpath
    regex match a trailing ':1.0' and every plug refreshes several times."""
    from asteroid_docking_bay.usbevents import parse_event
    ev = parse_event("UDEV  [123.4] add      /devices/pci0000:00/usb1/1-3/1-3.2 (usb)")
    assert ev == {"action": "add", "subsystem": "usb", "dev": "1-3.2",
                  "devpath": "/devices/pci0000:00/usb1/1-3/1-3.2"}
    iface = parse_event("UDEV  [123.5] add      /devices/pci0000:00/usb1/1-3/1-3.2/1-3.2:1.0 (usb)")
    assert iface["dev"] is None
    assert parse_event("KERNEL[1.0] add /devices/x (usb)") is None   # kernel half
    assert parse_event("") is None
    assert parse_event("garbage") is None


def test_split_dev_maps_a_device_to_its_hub_port():
    """A device path names the port it sits on, which is what every a-d-b
    lookup is keyed by."""
    from asteroid_docking_bay.usbevents import split_dev
    assert split_dev("1-3.2") == ("1-3", 2)
    assert split_dev("1-6.1.4") == ("1-6.1", 4)
    assert split_dev("1-3") is None            # root device, not on a hub port
    assert split_dev("1-3.x") is None


def test_monitor_collapses_an_event_burst_into_one_refresh(monkeypatch):
    """A plug emits the device plus one event per interface; refreshing on
    each would cost several full bus scans for one physical action.

    Drives the REAL reader over a fake udevadm stream rather than poking the
    debounce flag directly — an earlier version of this test set _pending by
    hand, never executed _read, and so passed happily against a monitor that
    called on_change() on every single line.

    Planted-bug: call on_change directly from the reader and this sees 4."""
    import threading, time
    from asteroid_docking_bay import usbevents
    from asteroid_docking_bay.usbevents import UsbEventMonitor

    burst = [
        "UDEV  [1.0] add      /devices/pci0000:00/usb1/1-3/1-3.2 (usb)\n",
        "UDEV  [1.1] add      /devices/pci0000:00/usb1/1-3/1-3.2/1-3.2:1.0 (usb)\n",
        "UDEV  [1.2] bind     /devices/pci0000:00/usb1/1-3/1-3.2 (usb)\n",
        "UDEV  [1.3] add      /devices/pci0000:00/usb1/1-3/1-3.2/1-3.2:1.1 (usb)\n",
    ]

    class _FakeProc:
        stdout = iter(burst)

        def terminate(self):
            pass

    calls = []
    mon = UsbEventMonitor(lambda: calls.append(1), debounce_s=0.05)
    mon._proc = _FakeProc()
    # never let the unconfigured probe touch real sysfs
    monkeypatch.setattr(usbevents, "port_device_info", lambda loc, port: None)
    threading.Thread(target=mon._flush, daemon=True).start()
    mon._read()
    time.sleep(0.4)
    mon.stop()
    assert len(calls) == 1


def test_monitor_start_degrades_when_udevadm_is_missing(monkeypatch):
    """No udevadm is a degradation, not a failure — polling still covers
    everything, so the web service must come up regardless."""
    import subprocess
    from asteroid_docking_bay.usbevents import UsbEventMonitor

    def _boom(*a, **k):
        raise FileNotFoundError("udevadm")
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert UsbEventMonitor(lambda: None).start() is False


# ── WiFi provisioning from a backup ─────────────────────────────────────────

_SETTINGS = """[wifi_9828a6ec99db_667269747a677562697372617468_managed_psk]
Name=fritzgubisrath
SSID=667269747a677562697372617468
Frequency=2437
Favorite=true
AutoConnect=true
Modified=2026-07-13T18:13:50Z
Passphrase=hunter2
IPv4.method=dhcp
IPv4.DHCP.LastAddress=192.168.176.127
IPv6.method=auto
"""


def test_parse_service_dir_and_ssid_decode():
    """connman names a service wifi_<mac>_<ssid-hex>_<mode>_<security>. The
    tail is kept verbatim rather than assumed to be managed_psk — an open
    network is managed_none."""
    from asteroid_docking_bay import wifi
    assert wifi.parse_service_dir(
        "wifi_9828a6ec99db_667269747a677562697372617468_managed_psk") == (
        "9828a6ec99db", "667269747a677562697372617468", "managed_psk")
    assert wifi.parse_service_dir("wifi_aabbccddeeff_6162_managed_none")[2] == "managed_none"
    assert wifi.parse_service_dir("ethernet_aabb_cable") is None
    assert wifi.ssid_from_hex("667269747a677562697372617468") == "fritzgubisrath"
    # An SSID is arbitrary bytes; an undecodable one must stay selectable.
    assert wifi.ssid_from_hex("ffff") == "ffff"


def test_adapt_settings_rekeys_to_the_target_watch():
    """THE feature. A connman service is keyed on the MAC of the interface
    that saved it, in the directory name AND the section header. Copied
    verbatim to another watch, connman never matches it to the scanned
    network. Planted-bug: keep the original header and this fails."""
    from asteroid_docking_bay import wifi
    new_id = wifi.service_id("00:90:4c:11:22:33",
                             "667269747a677562697372617468", "managed_psk")
    assert new_id == "wifi_00904c112233_667269747a677562697372617468_managed_psk"
    out = wifi.adapt_settings(_SETTINGS, new_id)
    assert out.splitlines()[0] == f"[{new_id}]"
    assert "9828a6ec99db" not in out              # no trace of the source watch
    kv = wifi.parse_settings(out)
    assert kv["Passphrase"] == "hunter2"          # the credential travels
    assert kv["Name"] == "fritzgubisrath"
    assert kv["AutoConnect"] == "true" and kv["Favorite"] == "true"


def test_adapt_settings_drops_the_source_watchs_dhcp_lease():
    """LastAddress is another watch's lease. Handing it to a second watch
    invites a duplicate-address attempt on a network where the first may still
    be up. Planted-bug: drop the _DROP_KEYS filter and this fails."""
    from asteroid_docking_bay import wifi
    out = wifi.adapt_settings(_SETTINGS, "wifi_00904c112233_6162_managed_psk")
    assert "IPv4.DHCP.LastAddress" not in out
    assert "192.168.176.127" not in out
    assert wifi.parse_settings(out)["IPv4.method"] == "dhcp"   # method stays


def test_adapt_settings_forces_autoconnect_when_absent():
    """A provisioned watch should join by itself; Favorite is what makes
    connman treat it as a known network at all."""
    from asteroid_docking_bay import wifi
    kv = wifi.parse_settings(wifi.adapt_settings(
        "[old]\nName=x\nPassphrase=p\n", "wifi_aa_6162_managed_psk"))
    assert kv["AutoConnect"] == "true" and kv["Favorite"] == "true"


def test_find_aps_skips_service_dirs_without_settings(tmp_path):
    """The rig's own backup carries a second, locally-administered MAC (the
    P2P/WiFi-Direct interface) holding only connman's binary cache and no
    credential. Offering it would produce a button that cannot work.

    Note on validation: removing the explicit `settings.is_file()` guard does
    NOT fail this — the read below raises OSError and skips the entry anyway,
    so the two paths are equivalent and the guard is documentation rather than
    load-bearing. What this test does pin is the OUTCOME: a bare service dir
    never reaches the UI, whichever path excludes it."""
    from asteroid_docking_bay import wifi
    cm = tmp_path / "skipjack" / "connman"
    good = cm / "wifi_9828a6ec99db_667269747a677562697372617468_managed_psk"
    good.mkdir(parents=True)
    (good / "settings").write_text(_SETTINGS)
    bare = cm / "wifi_9a28a6ec99db_667269747a677562697372617468_managed_psk"
    bare.mkdir(parents=True)
    (bare / "data").write_bytes(b"\x00" * 16)
    aps = wifi.find_aps(tmp_path)
    assert len(aps) == 1
    assert aps[0]["ssid"] == "fritzgubisrath" and aps[0]["source"] == "skipjack"
    assert aps[0]["secured"] is True


def test_find_aps_offers_a_network_once_across_watches(tmp_path):
    """Several watches on one network hold the same credential. The UI should
    offer the NETWORK once, not one entry per watch that knows it."""
    from asteroid_docking_bay import wifi
    for who in ("skipjack", "catfish"):
        d = (tmp_path / who / "connman"
             / "wifi_aabbccddeeff_667269747a677562697372617468_managed_psk")
        d.mkdir(parents=True)
        (d / "settings").write_text(_SETTINGS)
    assert len(wifi.find_aps(tmp_path)) == 1


def test_find_aps_on_a_rig_with_no_backups(tmp_path):
    """No backups is the normal state before the first one is taken — it must
    read as 'nothing to offer', not raise."""
    from asteroid_docking_bay import wifi
    assert wifi.find_aps(tmp_path / "nope") == []
    assert wifi.find_aps(tmp_path) == []
