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
