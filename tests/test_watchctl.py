# SPDX-License-Identifier: GPL-3.0-only
"""wait_for_adb targeting: an explicit serial must never resolve to a
different unit of the same codename.

This exists because the flash path identified its target only by codename.
With two watches sharing a codename (e.g. two "skipjack"), the codename scan
could pick — and flash — the wrong physical watch. wait_for_adb(serial=…) now
pins the exact unit; this test fails against the old codename-only match.
"""

import asteroid_docking_bay.watchctl as wc
import asteroid_docking_bay.transport as tp
from asteroid_docking_bay.watchctl import wait_for_adb


class _CC:
    adb_wait_seconds = 0      # no real sleeping in the test
    adb_wait_retries = 1


def test_explicit_serial_does_not_match_a_sibling(monkeypatch):
    # Only the SIBLING (same codename, different serial) is online.
    monkeypatch.setattr(wc, "adb_devices",
                        lambda: {"SIBLING": {"status": "device"}})
    monkeypatch.setattr(wc, "get_watch_codename", lambda s: "skipjack")
    # Asking for WANTED must not return SIBLING — the wrong-watch bug.
    assert wait_for_adb("skipjack", {}, _CC(), serial="WANTED") is None


def test_explicit_serial_matches_itself(monkeypatch):
    monkeypatch.setattr(wc, "adb_devices",
                        lambda: {"WANTED": {"status": "device"}})
    assert wait_for_adb("skipjack", {}, _CC(), serial="WANTED") == "WANTED"


def test_no_serial_still_matches_by_codename(monkeypatch):
    # Backward compatible: without an explicit serial, the codename scan runs.
    monkeypatch.setattr(wc, "adb_devices",
                        lambda: {"SOMEUNIT": {"status": "device"}})
    monkeypatch.setattr(wc, "get_watch_codename", lambda s: "skipjack")
    monkeypatch.setattr(wc, "save_config", lambda cfg: None)
    assert wait_for_adb("skipjack", {}, _CC()) == "SOMEUNIT"


# ── geometry: shape from machine.conf, resolution from fb0/modes ─────────────

def _geo_run(conf, modes):
    def fake(cmd, check=True, timeout=None):
        if "machine.conf" in cmd:
            return (0, conf, "")
        if "fb0/modes" in cmd:
            return (0, modes, "")
        return (1, "", "")
    return fake


def test_geometry_round_watch(monkeypatch):
    monkeypatch.setattr(tp, "_run", _geo_run(
        "[Display]\nROUND = true\n\n[Identity]\nMACHINE = skipjack\n",
        "U:360x360p-2640\n"))
    geo = wc.Watch("S1").geometry()
    assert geo["round"] is True and geo["machine"] == "skipjack"
    # Resolution must come from modes, not the double-buffered virtual_size.
    assert geo["width"] == 360 and geo["height"] == 360
    assert geo["resolution"] == "360x360"


def test_geometry_square_watch(monkeypatch):
    monkeypatch.setattr(tp, "_run", _geo_run(
        "[Display]\nROUND = false\nFLAT_TIRE = 0\n", "U:320x320p-100\n"))
    geo = wc.Watch("S1").geometry()
    assert geo["round"] is False and geo["flat_tire"] == 0 and geo["width"] == 320


def test_watch_routes_through_an_injected_ssh_transport(monkeypatch):
    from asteroid_docking_bay.transport import SshTransport
    calls = []
    monkeypatch.setattr(tp, "_run",
                        lambda cmd, check=True, timeout=None:
                        calls.append(cmd) or (0, "", ""))
    wc.Watch("S", SshTransport("1.2.3.4")).toggle("wifi", True)
    assert calls and calls[0].startswith("ssh ")
    assert "root@1.2.3.4 connmanctl enable wifi" in calls[0]


def test_geometry_empty_when_unreachable(monkeypatch):
    monkeypatch.setattr(tp, "_run", lambda *a, **k: (1, "", ""))
    assert wc.Watch("S1").geometry() == {}


def test_every_battery_reader_uses_the_preferred_gauge():
    """Two independent battery readers exist — the status path in adb.py and
    the Control Center's on-watch script — and BOTH must prefer the named
    fuel gauge. catfish carries a generic `battery` node frozen at 50% next to
    a nanohub gauge reading the truth (8% while the watch was panicking,
    2026-07-28); only adb.py knew, so the Control Center, the fleet registry
    and the battery history all showed a fabricated 50% for weeks. Planted-bug:
    hardcode /sys/class/power_supply/battery/capacity back into the script and
    this fails."""
    from asteroid_docking_bay.watchctl import _CC_SCRIPT
    from asteroid_docking_bay.adb import BATTERY_DIRS, _BATTERY_SYSFS_PATHS
    assert "/sys/class/power_supply/battery/capacity" not in _CC_SCRIPT
    assert "$BATD/capacity" in _CC_SCRIPT
    assert "nanohub_fuelgauge" in _CC_SCRIPT      # resolved from BATTERY_DIRS
    # the two readers derive from one list, so they cannot drift apart
    assert _BATTERY_SYSFS_PATHS == tuple(f"{d}/capacity" for d in BATTERY_DIRS)
    assert BATTERY_DIRS[0].endswith("nanohub_fuelgauge-*")
