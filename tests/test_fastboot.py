# SPDX-License-Identifier: GPL-3.0-only
"""The USB-SSH reachability helpers: sysfs link discovery, address picking,
and the stray-.15 peel decision (the 2026-07-25 shared-address collision)."""

from asteroid_docking_bay import fastboot
from asteroid_docking_bay.fastboot import (_pick_ssh_ip, _stray_ssh_to_realign,
                                           rndis_links)
from asteroid_docking_bay.transport import USB_SSH_IP


def _fake_sysfs(tmp_path, devices):
    """Build a fake /sys/bus/usb/devices: devices = {path: (serial, iface)}."""
    for path, (serial, iface) in devices.items():
        dev = tmp_path / path
        dev.mkdir()
        if serial is not None:
            (dev / "serial").write_text(serial + "\n")
        ifdir = tmp_path / f"{path}:1.0" / "net" / iface
        ifdir.mkdir(parents=True)
    return str(tmp_path)


def test_rndis_links_parses_sysfs_tree(tmp_path):
    base = _fake_sysfs(tmp_path, {
        "1-6.3.2": ("604KPMZ003491", "enp0s20u6u3u2"),
        "1-3.2":   ("720EX91190639", "enp0s20u3u2"),
    })
    links = sorted(rndis_links(base), key=lambda l: l["usb_path"])
    assert links == [
        {"iface": "enp0s20u3u2", "usb_path": "1-3.2", "serial": "720EX91190639"},
        {"iface": "enp0s20u6u3u2", "usb_path": "1-6.3.2", "serial": "604KPMZ003491"},
    ]


LINKS = [
    {"iface": "ifA", "usb_path": "1-3.2", "serial": "A"},
    {"iface": "ifB", "usb_path": "1-6.3.2", "serial": "B"},
]


def test_pick_ssh_ip_orders_allocated_then_default_winner():
    up = {"10.0.0.5", USB_SSH_IP}
    # allocated answers → allocated wins
    assert _pick_ssh_ip("A", LINKS, "10.0.0.5", "ifB", up.__contains__) == "10.0.0.5"
    # allocated dead, this link wins the route, default answers → default
    assert _pick_ssh_ip("A", LINKS, "10.9.9.9", "ifA", up.__contains__) == USB_SSH_IP
    # allocated dead and another link wins the route → shadowed, unreachable
    assert _pick_ssh_ip("A", LINKS, "10.9.9.9", "ifB", up.__contains__) is None


def test_pick_ssh_ip_costs_nothing_without_a_link():
    """A dead allocated address must cost nothing when the watch has no live
    RNDIS link — this probe-free early-out is what removed the constant 4.25s
    status render (2s ping per dead address, 2026-07-25)."""
    probed = []
    def probe(ip):
        probed.append(ip)
        return True
    assert _pick_ssh_ip("GONE", LINKS, "10.0.0.5", "ifA", probe) is None
    assert probed == []


def test_stray_realign_picks_route_winner_and_skips_aligned():
    """Peel decision: only strays (not answering on their allocated address)
    are candidates, and only the current route winner is actionable — the
    others are shadowed behind it. Planted-bug validated: dropping the
    aligned-skip makes the third case return the aligned watch's serial."""
    links = LINKS + [{"iface": "ifC", "usb_path": "1-6.1", "serial": "C"}]
    ssh_ips = {"A": "10.0.0.5", "B": "10.0.0.6"}         # C never allocated
    up = {"10.0.0.5", USB_SSH_IP}                        # A aligned; B, C stray
    # B wins the route → peel B (C is shadowed, A is aligned).
    assert _stray_ssh_to_realign(links, ssh_ips, "ifB", up.__contains__) == "B"
    # The default not answering → nothing actionable this cycle.
    assert _stray_ssh_to_realign(links, ssh_ips, "ifB",
                                 {"10.0.0.5"}.__contains__) is None
    # The ALIGNED watch wins the route → nothing to peel: the strays are
    # shadowed and A must not be touched.
    assert _stray_ssh_to_realign(links, ssh_ips, "ifA", up.__contains__) is None
    # No strays at all → None.
    assert _stray_ssh_to_realign(
        [LINKS[0]], {"A": "10.0.0.5"}, "ifA", up.__contains__) is None


# ── bootloader unlock state: the capability that gates a clean dump ──────────

_NEMO_GETVAR = """product: nemo
serialno: 603KPVH000855
unlocked: no
secure: yes
version-bootloader: LGE
partition-size:boot: 0x1600000
"""


def test_parse_getvar_reads_the_dump_and_lets_later_lines_win():
    from asteroid_docking_bay.fastboot import parse_getvar
    kv = parse_getvar(_NEMO_GETVAR)
    assert kv["product"] == "nemo" and kv["unlocked"] == "no"
    assert kv["partition-size:boot"] == "0x1600000", "a key containing ':' was split wrong"
    # fastboot repeats some keys; the settled value is the last one printed.
    assert parse_getvar("unlocked: no\nunlocked: yes")["unlocked"] == "yes"
    assert parse_getvar("") == {} and parse_getvar(None) == {}
    assert parse_getvar("no colon here") == {}


def test_bootloader_unlocked_decides_whether_a_clean_dump_is_even_possible():
    """A locked bootloader refuses `fastboot boot` outright — verified on nemo
    603KPVH000855 on 2026-08-03, which is where the whole debug-ramdisk dump
    plan died. Knowing this per watch answers up front what otherwise costs an
    hour of setup and a refusal."""
    from asteroid_docking_bay.fastboot import bootloader_unlocked
    assert bootloader_unlocked(_NEMO_GETVAR) is False
    assert bootloader_unlocked("unlocked: yes") is True
    assert bootloader_unlocked("device-unlocked: true") is True
    # secure-boot reads the other way round.
    assert bootloader_unlocked("secure-boot: yes") is False
    assert bootloader_unlocked("secure-boot: no") is True


def test_unknown_unlock_state_is_None_not_False():
    """Absence of the field is not evidence of a lock. Reporting False would
    quietly write off watches that are in fact dumpable."""
    from asteroid_docking_bay.fastboot import bootloader_unlocked
    assert bootloader_unlocked("product: nemo") is None
    assert bootloader_unlocked("") is None
    assert bootloader_unlocked("unlocked: maybe") is None


# ── how quickly a watch entering the bootloader is noticed ───────────────────

def test_a_bus_event_brings_the_fastboot_scan_forward(monkeypatch):
    """A watch entering the bootloader re-enumerates in 1-2s, but the pill took
    over a minute to appear (sawfish, 2026-08-08). The status cache was busted
    promptly by udev and then simply re-read a fastboot device list that is
    cached separately on a 60s ceiling.

    udev already knows. An event marks the list due so the warmer's next tick
    re-reads it, instead of waiting out a ceiling that exists for a quiet bus."""
    from asteroid_docking_bay import fastboot as fb
    now = 1000.0
    monkeypatch.setitem(fb._fb_list_cache, "ts", now)      # just scanned
    monkeypatch.setitem(fb._fb_list_cache, "due", False)

    assert fb._fb_list_due(now + 1) is False, "scanning again immediately"
    # Nothing happened on the bus: it waits out the full ceiling.
    assert fb._fb_list_due(now + 30) is False
    assert fb._fb_list_due(now + fb.FB_LIST_MAX_AGE) is True

    # The bus changed — the watch just entered fastboot.
    fb.invalidate_list_cache()
    assert fb._fb_list_due(now + fb.FB_LIST_MIN_INTERVAL) is True, \
        "an event did not bring the scan forward — the pill waits out the TTL"
    assert fb._fb_list_due(now + 30) is True


def test_an_event_cannot_make_the_scan_run_continuously(monkeypatch):
    """`fastboot devices` is a libusb sweep that races enumeration and can wedge
    these hubs (audit B9), which is why it is not on a short timer. An event may
    bring it forward but must never remove the floor: a flapping port or a power
    cycle emits a burst, and a scan per event is exactly the storm to avoid."""
    from asteroid_docking_bay import fastboot as fb
    now = 1000.0
    monkeypatch.setitem(fb._fb_list_cache, "ts", now)
    monkeypatch.setitem(fb._fb_list_cache, "due", False)

    for _ in range(20):                       # a burst of events
        fb.invalidate_list_cache()
    assert fb._fb_list_due(now + 1) is False, \
        "a burst of bus events could trigger back-to-back libusb sweeps"
    assert fb._fb_list_due(now + fb.FB_LIST_MIN_INTERVAL - 0.1) is False
    assert fb.FB_LIST_MIN_INTERVAL >= 5, "the floor is too low to protect the bus"


def test_a_scan_clears_the_due_flag(monkeypatch):
    """Otherwise the list would re-scan every tick forever after one event."""
    from asteroid_docking_bay import fastboot as fb
    monkeypatch.setattr(fb, "_run", lambda *a, **k: (0, "", ""))
    fb.invalidate_list_cache()
    fb._fastboot_poll()
    assert fb._fb_list_cache["due"] is False
    assert fb._fb_list_due(fb._fb_list_cache["ts"] + 1) is False


def test_the_usb_monitor_actually_invalidates_the_fastboot_list():
    """The mechanism only helps if the udev callback is wired to it. Busting the
    status cache alone was the original bug: the rebuilt status re-read the same
    stale fastboot list, so the pill still waited out the ceiling."""
    import inspect

    from asteroid_docking_bay import webapp
    src = inspect.getsource(webapp.serve)
    assert "invalidate_list_cache()" in src, \
        "a bus event no longer marks the fastboot list due — a watch entering " \
        "the bootloader goes unrecognised until the 60s ceiling lapses"
    assert "UsbEventMonitor(_on_usb_change)" in src, \
        "the monitor is not wired to the combined callback"


def test_release_image_names_come_from_the_release_not_from_an_assumption(tmp_path):
    """Image filenames differ per channel, and hardcoding one pair broke the
    others silently — as a 404 at flash time, on a watch already in fastboot.

    Observed on release.asteroidos.org:

        1.0        zImage-dtb-<cn>.fastboot   asteroid-image-<cn>.ext4
        2.0        zImage-dtb-<cn>.fastboot   asteroid-image-<cn>.rootfs.ext4
        2.1        BOTH boot names            asteroid-image-<cn>.rootfs.ext4
        nightlies  asteroid-<cn>-boot.img     asteroid-image-<cn>.rootfs.ext4

    a-d-b asked for the nightly pair everywhere, so the channel selector
    offered 1.0 and 2.0 while being unable to fetch either.

    The boot preference is zImage FIRST on purpose: the new name applies from
    2.2 on, and 2.1 only published it early beside the old one. zImage is what
    every released channel carries, so preferring it picks the intended image
    on 1.0/2.0/2.1 and falls through on the nightlies, where it is absent."""
    from asteroid_docking_bay import fastboot as fb

    def sums(*names):
        f = tmp_path / f"SHA512SUMS-{'-'.join(names)[:40]}"
        f.write_text("".join(f"{'0'*128}  {n}\n" for n in names))
        return f

    # 1.0
    assert fb._release_filenames("sparrow", sums(
        "zImage-dtb-sparrow.fastboot", "asteroid-image-sparrow.ext4")) == (
        "zImage-dtb-sparrow.fastboot", "asteroid-image-sparrow.ext4")
    # 2.0
    assert fb._release_filenames("sparrow", sums(
        "zImage-dtb-sparrow.fastboot", "asteroid-image-sparrow.rootfs.ext4")) == (
        "zImage-dtb-sparrow.fastboot", "asteroid-image-sparrow.rootfs.ext4")
    # 2.1 ships both boot names — the released one must win
    assert fb._release_filenames("sparrow", sums(
        "asteroid-sparrow-boot.img", "zImage-dtb-sparrow.fastboot",
        "asteroid-image-sparrow.rootfs.ext4"))[0] == "zImage-dtb-sparrow.fastboot"
    # nightlies carry only the new name
    assert fb._release_filenames("sparrow", sums(
        "asteroid-sparrow-boot.img", "asteroid-image-sparrow.rootfs.ext4")) == (
        "asteroid-sparrow-boot.img", "asteroid-image-sparrow.rootfs.ext4")

    # A release listing no usable boot image must SAY so, not fetch a 404 and
    # discover it while the watch sits in fastboot.
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="no boot image"):
        fb._release_filenames("sparrow", sums("README"))

    # No manifest at all (offline / empty file) keeps the previous behaviour.
    empty = tmp_path / "none"
    assert fb._release_filenames("sparrow", empty)[0] == "zImage-dtb-sparrow.fastboot"
