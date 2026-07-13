# SPDX-License-Identifier: GPL-3.0-only
"""Pure-logic tests for adb output parsing and per-serial state lookup."""

from asteroid_docking_bay.adb import _adb_state, parse_adb_devices

# Real `adb devices -l` output shapes, incl. beroset's -l extras (#1).
SAMPLE = """List of devices attached
411KPCA0121867         device usb:1-2.3.3.1 product:lenok model:G_Watch_R device:lenok transport_id:7
0393ed6402a24539       device usb:1-2.3.3.2 product:dory transport_id:9
TKQ7N17406001852       offline usb:1-2.3.3.3 transport_id:11
510KPWQ0314577         unauthorized
"""


def test_parse_full_line():
    devices = parse_adb_devices(SAMPLE)
    lenok = devices["411KPCA0121867"]
    assert lenok["status"] == "device"
    assert lenok["usb"] == "1-2.3.3.1"
    assert lenok["product"] == "lenok"
    assert lenok["model"] == "G_Watch_R"


def test_parse_states():
    devices = parse_adb_devices(SAMPLE)
    assert devices["TKQ7N17406001852"]["status"] == "offline"
    assert devices["510KPWQ0314577"]["status"] == "unauthorized"


def test_parse_empty_list():
    assert parse_adb_devices("List of devices attached\n") == {}
    assert parse_adb_devices("") == {}


def test_parse_ignores_noise():
    # Daemon-restart notices precede the header and must not become devices —
    # the pre-0.4 parser turned them into bogus "*" and "List" entries.
    noisy = ("* daemon not running; starting now at tcp:5037\n"
             "* daemon started successfully\n"
             "List of devices attached\n"
             "S1 device usb:1-2\n")
    devices = parse_adb_devices(noisy)
    assert list(devices) == ["S1"]
    # Stray short lines after the header are skipped, not crashed on.
    assert parse_adb_devices("List of devices attached\nX\n") == {}


def test_adb_state_present():
    devices = parse_adb_devices(SAMPLE)
    assert _adb_state(devices, "411KPCA0121867") == "device"
    assert _adb_state(devices, "TKQ7N17406001852") == "offline"


def test_adb_state_absent_is_none():
    # The normal "watch not present yet / went offline" case: must be
    # None-safe, never raise (this crashed once as devices.get(x)['status']).
    assert _adb_state({}, "nope") is None
    assert _adb_state(parse_adb_devices(SAMPLE), "unknown-serial") is None
    assert _adb_state({}, None) is None


def test_adb_state_plain_string_defensive():
    assert _adb_state({"s": "device"}, "s") == "device"


# ── battery_and_screen: parsing + the shell-quoting regression guard ──────────
#
# battery_and_screen packs battery + mce demo-mode state into one round-trip.
# The pipeline (`... | head; echo; mcetool | grep`) MUST run on the watch, not
# the host: _run uses shell=True, so an unquoted command has the host shell
# parse the pipes/semicolons and run `mcetool` on the *host* (where it doesn't
# exist), silently returning (None, False) — a stuck screen that reads "fine".
# This shipped once and was caught only on hardware; the quoting test pins it.

import asteroid_docking_bay.adb as adbmod
from asteroid_docking_bay.adb import battery_and_screen


def _fake_shell(rc, out):
    captured = {}

    def shell(serial, cmd, timeout=8):
        captured["cmd"] = cmd
        return rc, out, ""

    return shell, captured


def test_battery_and_screen_wraps_whole_pipeline_for_device(monkeypatch):
    shell, captured = _fake_shell(0, "100\n---SCR---\nBlank inhibit: stay-on")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    battery_and_screen("SERIAL")
    cmd = captured["cmd"]
    # The remote command is passed as a single quoted arg so the host shell
    # hands the entire pipeline to the device rather than running mcetool locally.
    assert cmd.startswith('"') and cmd.endswith('"'), cmd
    assert "mcetool" in cmd and "|" in cmd


def test_battery_and_screen_forced(monkeypatch):
    shell, _ = _fake_shell(0, "83\n---SCR---\nBlank inhibit:      stay-on")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (83, True)


def test_battery_and_screen_not_forced(monkeypatch):
    shell, _ = _fake_shell(0, "83\n---SCR---\nBlank inhibit:      disabled")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (83, False)


def test_battery_and_screen_no_mce(monkeypatch):
    # Watches without mce (or where the line is absent) read as not-forced.
    shell, _ = _fake_shell(0, "50\n---SCR---\n")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (50, False)


def test_battery_and_screen_no_battery(monkeypatch):
    shell, _ = _fake_shell(0, "\n---SCR---\nBlank inhibit: stay-on")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (None, True)


def test_battery_and_screen_rc_fail(monkeypatch):
    shell, _ = _fake_shell(1, "")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (None, False)


# ── _resolve_conn_state: the row connection-state priority (fastboot/ssh) ──────

from asteroid_docking_bay.adb import _resolve_conn_state


def test_conn_state_adb_wins():
    # A live adb status short-circuits everything, and the ssh probe (a sysfs
    # read) must not run for an already-on-adb port.
    calls = []
    assert _resolve_conn_state("device", True, lambda: calls.append(1) or True) == "device"
    assert calls == []


def test_conn_state_offline_is_a_status():
    # 'offline' is a real adb status, not "nothing there" — it wins over fastboot.
    assert _resolve_conn_state("offline", True, lambda: True) == "offline"


def test_conn_state_fastboot():
    assert _resolve_conn_state(None, True, lambda: False) == "fastboot"


def test_conn_state_fastboot_beats_ssh_probe():
    # In the bootloader we never fall through to the ssh probe.
    calls = []
    assert _resolve_conn_state(None, True, lambda: calls.append(1) or True) == "fastboot"
    assert calls == []


def test_conn_state_ssh():
    assert _resolve_conn_state(None, False, lambda: True) == "ssh"


def test_conn_state_nothing():
    assert _resolve_conn_state(None, False, lambda: False) is None
