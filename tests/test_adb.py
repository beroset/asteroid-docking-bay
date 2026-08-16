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
    shell, _ = _fake_shell(0, "83\n---SCR---\nBlank inhibit:      stay-on\n---CHG---\nCharging")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (83, True, "Charging")


def test_battery_and_screen_not_forced(monkeypatch):
    shell, _ = _fake_shell(0, "83\n---SCR---\nBlank inhibit:      disabled\n---CHG---\nFull")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (83, False, "Full")


def test_battery_and_screen_no_mce(monkeypatch):
    # Watches without mce (or where the line is absent) read as not-forced.
    shell, _ = _fake_shell(0, "50\n---SCR---\n---CHG---\n")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (50, False, None)


def test_battery_and_screen_prefers_definite_charge_status(monkeypatch):
    # Two supplies report; the definite battery verdict beats a USB "Unknown".
    shell, _ = _fake_shell(0, "70\n---SCR---\n---CHG---\nUnknown\nDischarging")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (70, False, "Discharging")


def test_battery_and_screen_no_battery(monkeypatch):
    shell, _ = _fake_shell(0, "\n---SCR---\nBlank inhibit: stay-on\n---CHG---\nCharging")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (None, True, "Charging")


def test_battery_and_screen_rc_fail(monkeypatch):
    shell, _ = _fake_shell(1, "")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert battery_and_screen("S") == (None, False, None)


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


def test_get_battery_level_wraps_command_for_device(monkeypatch):
    # The read command carries a glob (nanohub_fuelgauge-*) and a pipe. It must
    # reach the device as a single quoted arg; unquoted, the host shell expands
    # the glob against the HOST's /sys (BAT0) and the watch gets a path it lacks,
    # so the read returns None and every drain/charge that relies on it silently
    # fails. This regression shipped with the fuel-gauge glob (921b713) and was
    # caught only on hardware — the quoting guard pins it, like the status path's.
    from asteroid_docking_bay.adb import get_battery_level
    shell, captured = _fake_shell(0, "100")
    monkeypatch.setattr(adbmod, "adb_shell", shell)
    assert get_battery_level("SERIAL") == 100
    cmd = captured["cmd"]
    assert cmd.startswith('"') and cmd.endswith('"'), cmd
    assert "*" in cmd and "|" in cmd   # glob + pipe must stay device-side


def test_battery_paths_prefer_fuel_gauge():
    # The named hardware fuel gauge must be read before the generic `battery`
    # node — on some watches `battery` is a separate, miscalibrated source.
    from asteroid_docking_bay.adb import _BATTERY_SYSFS_PATHS
    paths = list(_BATTERY_SYSFS_PATHS)
    fg  = next(i for i, p in enumerate(paths) if "nanohub_fuelgauge" in p)
    bat = next(i for i, p in enumerate(paths) if p.endswith("/battery/capacity"))
    assert fg < bat


def test_maybe_heal_wedged_adb_restarts_only_on_a_persistent_wedge(monkeypatch):
    """The adb server can list nothing while watches are enumerated in adb mode
    (seen live 2026-07-24). maybe_heal_wedged_adb must restart it — but only once
    the wedge PERSISTS across two checks (so a just-plugged watch's enumeration
    race is not a false restart) and at most once per cooldown, since a restart
    briefly drops every watch (audit A2)."""
    import asteroid_docking_bay.adb as adbmod
    from asteroid_docking_bay import config as cfgmod
    from asteroid_docking_bay import usb as usbmod
    ran = []
    monkeypatch.setattr(adbmod, "_run", lambda cmd, **k: (ran.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(adbmod, "adb_devices_checked", lambda: {})       # server blind
    monkeypatch.setattr(usbmod, "_sysfs_adb_serials", lambda: {"S1"})    # S1 on the bus
    # No locks: the heal consults them now, and reading the host's real config
    # would make this test depend on whether the rig has a dump running.
    monkeypatch.setattr(cfgmod, "load_config", lambda: {})
    monkeypatch.setattr(adbmod, "_prev_adb_missing", set())
    monkeypatch.setattr(adbmod, "_last_adb_heal", 0.0)
    # first check: wedge seen but not yet persistent → no restart
    assert adbmod.maybe_heal_wedged_adb() is False and ran == []
    # second check: still wedged → persistent → restart
    assert adbmod.maybe_heal_wedged_adb() is True
    assert any("kill-server" in c for c in ran) and any("start-server" in c for c in ran)
    # third check within cooldown → no repeat restart
    ran.clear()
    assert adbmod.maybe_heal_wedged_adb() is False and ran == []
    # server now sees S1 → not a wedge
    monkeypatch.setattr(adbmod, "adb_devices_checked", lambda: {"S1": "device"})
    assert adbmod.maybe_heal_wedged_adb() is False


def test_the_adb_heal_will_not_restart_the_server_under_a_held_watch(monkeypatch):
    """A dump streams through the local adb server (`adb -s X exec-out dd`), so
    kill-server severs it mid-copy. The wedge that triggers this heal can be
    spurious — one watch enumerating oddly hides others from the server for two
    checks — and killing a 4 GB read to fix a maybe-wedge is the wrong trade.
    Defer while a watch is held; the TTL bounds the wait."""
    import time as _t

    import asteroid_docking_bay.adb as adbmod
    from asteroid_docking_bay import config as cfgmod
    from asteroid_docking_bay import usb as usbmod
    now = _t.time()
    ran = []
    monkeypatch.setattr(adbmod, "_run", lambda cmd, **k: (ran.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(adbmod, "adb_devices_checked", lambda: {})
    monkeypatch.setattr(usbmod, "_sysfs_adb_serials", lambda: {"S1"})
    monkeypatch.setattr(cfgmod, "load_config", lambda: {"op_locks": {
        "S1": {"kind": "dump", "since": now, "until": now + 600}}})
    monkeypatch.setattr(adbmod, "_prev_adb_missing", set())
    monkeypatch.setattr(adbmod, "_last_adb_heal", 0.0)

    adbmod.maybe_heal_wedged_adb()                      # arm persistence
    assert adbmod.maybe_heal_wedged_adb() is False
    assert not any("kill-server" in c for c in ran), \
        "restarted the adb server under a running dump — the transfer dies"

    # Once the lock is gone the heal works again: the guard defers, never disables.
    monkeypatch.setattr(cfgmod, "load_config", lambda: {})
    monkeypatch.setattr(adbmod, "_last_adb_heal", 0.0)
    monkeypatch.setattr(adbmod, "_prev_adb_missing", {"S1"})
    assert adbmod.maybe_heal_wedged_adb() is True
    assert any("kill-server" in c for c in ran)


def test_wait_serial_online_skips_recovery_cycle_when_another_watch_is_seated(monkeypatch):
    """The recovery power-cycle targets recover_loc_port, fixed at op start. If
    the watch moved and a DIFFERENT watch now sits at that seat, cycling it
    would bounce the innocent one (audit A4). Skip the cycle unless the seat is
    empty or holds our own serial."""
    import asteroid_docking_bay.adb as adbmod
    from asteroid_docking_bay import usb as usbmod
    monkeypatch.setattr(adbmod, "adb_devices_checked", lambda: {})   # target never online
    cycled = []
    monkeypatch.setattr(usbmod, "uhubctl_cycle", lambda l, p: cycled.append((l, p)))
    # a different watch is seated at the recovery seat → no cycle
    monkeypatch.setattr(usbmod, "_sysfs_serial_at", lambda l, p: "OTHER")
    assert adbmod.wait_serial_online("TARGET", 0, 1, recover_loc_port=("1-2", 1)) is False
    assert cycled == [], "cycled a port holding a different watch"
    # empty seat → cycle is allowed
    monkeypatch.setattr(usbmod, "_sysfs_serial_at", lambda l, p: None)
    adbmod.wait_serial_online("TARGET", 0, 1, recover_loc_port=("1-2", 1))
    assert cycled == [("1-2", 1)], "did not cycle an empty seat"


def test_a_non_answer_is_not_accepted_as_an_identity(monkeypatch):
    """`hostname` prints "(none)" when none is set. That was taken as a codename
    and persisted, so a half-ported watch was named "(none)" in the config.
    Only `localhost` and the empty string were rejected before."""
    import asteroid_docking_bay.adb as adbmod
    assert adbmod.is_a_codename("sol") and adbmod.is_a_codename("nemo")
    for junk in ("(none)", "none", "localhost", "", "  ", "UNKNOWN", "(None)"):
        assert not adbmod.is_a_codename(junk), f"{junk!r} accepted as a codename"

    # And the resolver must not return one from its hostname fallback.
    monkeypatch.setattr(adbmod, "adb_shell",
                        lambda s, cmd, **k: (0, "(none)", "") if cmd == "hostname" else (1, "", ""))
    assert adbmod.get_watch_codename("S1") is None, \
        "the resolver handed back a non-answer as the watch's name"


def test_the_device_listing_cannot_hang_forever(monkeypatch):
    """`adb devices -l` runs on the status path, and _run defaults to no
    timeout at all — so a hung adb server would block the thread for ever
    rather than failing. The listing must be bounded.

    The bound is safe only because the failure is already handled correctly:
    a timeout comes back rc != 0, which adb_devices_checked reads as "the call
    failed" (None), NOT as "no devices". That distinction is what stops a
    stuck adb server from reading as a fleet that vanished — which would in
    turn drive recovery cycles that switch VBUS."""
    from asteroid_docking_bay import adb as a

    seen = {}

    def fake_run(cmd, check=True, timeout=None):
        seen["cmd"], seen["timeout"] = cmd, timeout
        return 0, "List of devices attached\n", ""
    monkeypatch.setattr(a, "_run", fake_run)
    a.adb_devices_checked()
    assert "adb devices" in seen["cmd"]
    assert seen["timeout"], "the device listing is unbounded — a hung adb server wedges the caller"

    # a timeout must read as "call failed", never as "no devices"
    monkeypatch.setattr(a, "_run", lambda *x, **k: (1, "", "timeout"))
    assert a.adb_devices_checked() is None, (
        "a timed-out listing was reported as a successful empty result — every "
        "watch would read as gone")
    assert a.adb_devices() == {}, "the {}-on-failure contract changed"
