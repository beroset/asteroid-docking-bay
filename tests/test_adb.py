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
