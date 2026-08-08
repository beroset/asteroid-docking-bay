# SPDX-License-Identifier: GPL-3.0-only
"""Integrity of the op table and the frontend's use of it.

The web routes and the op table are two sides of one contract; these tests
pin them together so a renamed op or a route calling a nonexistent one
fails here instead of returning ok:false to a browser."""

import re
from pathlib import Path

import pytest

from asteroid_docking_bay import rpcops
from asteroid_docking_bay.rpc import RpcError
from asteroid_docking_bay.lastseen import LastSeen

WEBAPP_SRC = (Path(__file__).resolve().parent.parent
              / "asteroid_docking_bay" / "webapp.py").read_text()

REGISTERED = set(rpcops.DISPATCH._data) | set(rpcops.DISPATCH._stream)


def test_every_frontend_op_is_registered():
    from asteroid_docking_bay.webapp import _JSON_ROUTES
    called = {spec[2] for spec in _JSON_ROUTES}
    called |= set(re.findall(r'_call\("([\w.]+)"', WEBAPP_SRC))
    called |= set(re.findall(r'_sse\("([\w.]+)"', WEBAPP_SRC))
    missing = sorted(called - REGISTERED)
    assert not missing, (
        f"webapp dispatches op(s) the table doesn't register: {missing}")


def test_no_op_is_both_data_and_stream():
    both = set(rpcops.DISPATCH._data) & set(rpcops.DISPATCH._stream)
    assert not both, f"op(s) registered as both kinds: {sorted(both)}"


def test_hub_rename_round_trips_and_clears(monkeypatch):
    store: dict = {"hub_names": {}}
    monkeypatch.setattr(rpcops, "load_config", lambda: store)
    monkeypatch.setattr(rpcops, "save_config", lambda cfg: store.update(cfg))
    d = rpcops.DISPATCH._data["hub.rename"]({"prefix": "1-9.1", "name": "A16 #2"})
    assert d == {"ok": True, "name": "A16 #2"}
    assert store["hub_names"]["1-9.1"] == "A16 #2"
    d = rpcops.DISPATCH._data["hub.rename"]({"prefix": "1-9.1", "name": ""})
    assert d == {"ok": True, "name": None}
    assert "1-9.1" not in store["hub_names"]


def test_socket_set_stores_int_rejects_nonnumber_and_clears(monkeypatch):
    store = {"hubs": [{"location": "1-2", "sockets": {}}]}
    monkeypatch.setattr(rpcops, "load_config", lambda: store)
    monkeypatch.setattr(rpcops, "save_config", lambda cfg: store.update(cfg))
    d = rpcops.DISPATCH._data["socket.set"]({"loc": "1-2", "port": 3, "n": "7"})
    assert d == {"ok": True, "socket": 7}
    assert store["hubs"][0]["sockets"]["3"] == 7           # stored as int → sorts
    # non-numeric is rejected, leaves the value untouched
    assert rpcops.DISPATCH._data["socket.set"](
        {"loc": "1-2", "port": 3, "n": "x"})["ok"] is False
    assert store["hubs"][0]["sockets"]["3"] == 7
    # blank clears
    d = rpcops.DISPATCH._data["socket.set"]({"loc": "1-2", "port": 3, "n": ""})
    assert d == {"ok": True, "socket": None}
    assert "3" not in store["hubs"][0]["sockets"]
    # unknown hub errors, not crashes
    assert rpcops.DISPATCH._data["socket.set"](
        {"loc": "9-9", "port": 1, "n": "1"})["ok"] is False


def test_sweep_map_to_port_maps_and_clears_stale_seat(monkeypatch):
    """The sweep's map step maps the watch to its port and clears any stale seat
    the same serial held elsewhere. (The fleet-registry write with full CC data
    is done by the caller — validated on the rig: 14 watches registered.)"""
    store = {"hubs": [{"location": "1-2", "ports": {"1": "skipjack"},
                       "port_serials": {"1": "S1"}},
                      {"location": "1-3", "ports": {}, "port_serials": {}}],
             "serials": {"S1": "skipjack"}, "ssh_ips": {}}
    monkeypatch.setattr(rpcops, "load_config", lambda: store)
    monkeypatch.setattr(rpcops, "save_config", lambda c: store.update(c))
    # S1 (skipjack) re-appears on a different port/hub → map there, clear old seat.
    rpcops._sweep_map_to_port("1-3", 2, "S1", "skipjack", None, lambda m: None)
    assert store["hubs"][1]["ports"]["2"] == "skipjack"       # new seat
    assert store["hubs"][1]["port_serials"]["2"] == "S1"
    assert "1" not in store["hubs"][0]["ports"]               # old seat cleared


def test_registered_ops_are_the_documented_contract():
    """The allow-list IS the security boundary: adding an op must be a
    conscious, reviewed act. If this fails because you added one, update it
    here and in docs/CONTAINERS.md — that is the point."""
    assert REGISTERED == {
        "status.get",
        "watch.cc", "watch.timeline", "watch.bootchart", "watch.diag", "watch.wake_set", "watch.locale_set",
        "bench.app", "wanze.probe", "oplock.set", "watch.dump", "aod.check", "wifi.aps", "wifi.provision", "watch.session_restart", "watch.drainlog",
        "watch.settings_read", "watch.settings_write",
        "watch.quickpanel_set",
        "watch.toggle", "watch.settime", "watch.set_datetime", "watch.notify",
        "watch.hands", "watch.set_hands", "watch.hands_move", "watch.set_hands_cal",
        "watch.av_read", "watch.set_brightness", "watch.set_volume", "watch.set_mute",
        "watch.record_audio",
        "weather.get", "weather.set_location", "watch.weather_sync",
        "watch.weather_read",
        "orbit.launch", "orbit.deorbit", "registry.get",
        "bt.scan", "bt.pair",
        "watch.buzz", "watch.screen", "watch.screenshot", "screen.release_all",
        "watch.backup", "watch.restore", "watch.diagnostics", "watch.fbreport",
        "watch.image", "ssh.switch_adb", "watch.switch_ssh",
        "port.set", "port.cycle", "port.poweroff", "port.reboot",
        "port.bootloader", "port.recovery", "port.continue",
        "port.hide", "hub.hide", "hub.rename", "socket.set",
        "charge.start", "charge.stop", "prefs.set_usb_mode",
        "workbench.start", "workbench.stop", "wear.set",
        "drain.start", "drain.stop", "drain.history",
        "flash.start", "onboard.start", "onboard.sweep_prepare", "onboard.sweep_run",
        "onboard.sweep_skip",
    }


# ── handler behavior with mocked hardware ────────────────────────────────────

def test_port_set_maps_runtime_error(monkeypatch):
    def boom(loc, port, on):
        raise RuntimeError("hub said no")
    monkeypatch.setattr(rpcops, "uhubctl_set_power", boom)
    d = rpcops.DISPATCH._data["port.set"]({"loc": "1-1", "port": 1, "on": True})
    assert d == {"ok": False, "error": "hub said no"}


def test_port_set_ok(monkeypatch):
    monkeypatch.setattr(rpcops, "uhubctl_set_power", lambda l, p, o: True)
    d = rpcops.DISPATCH._data["port.set"]({"loc": "1-1", "port": 1, "on": True})
    assert d == {"ok": True, "confirmed": True}


def test_port_cycle_records_smart_verdict(monkeypatch):
    saved, marked = {}, {}
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "test_port_power_switching",
                        lambda l, p, s: (True, "VBUS cut confirmed"))
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"hubs": [{"location": "1-2", "port_smart": {}}]})
    monkeypatch.setattr(rpcops, "save_config", lambda cfg: saved.update(cfg=cfg))
    monkeypatch.setattr(rpcops.last_seen, "mark", lambda s, **k: marked.update(k))
    d = rpcops.DISPATCH._data["port.cycle"]({"loc": "1-2", "port": 2})
    assert d["ok"] is True and d["smart"] is True
    assert saved["cfg"]["hubs"][0]["port_smart"]["2"] is True
    # A cycle stamps the boot marker and clears safe_off so it reads
    # "reconnecting" (a re-power), not "booting up".
    assert marked.get("booting_since") and marked.get("safe_off_ts") == 0, marked


def test_port_cycle_inconclusive_does_not_save(monkeypatch):
    calls = {}
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: None)
    monkeypatch.setattr(rpcops, "test_port_power_switching",
                        lambda l, p, s: (None, "unverified"))
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"hubs": [{"location": "1-2", "port_smart": {}}]})
    monkeypatch.setattr(rpcops, "save_config",
                        lambda cfg: calls.setdefault("saved", True))
    d = rpcops.DISPATCH._data["port.cycle"]({"loc": "1-2", "port": 2})
    assert d["ok"] is True and d["smart"] is None and "saved" not in calls


def test_watch_toggle_rejects_unknown_tech():
    d = rpcops.DISPATCH._data["watch.toggle"](
        {"serial": "S", "tech": "nfc", "on": True})
    assert d["ok"] is False and "unknown toggle" in d["error"]


def test_poweroff_without_serial_still_cuts(monkeypatch):
    cut = {}
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "uhubctl_set_power",
                        lambda l, p, o: cut.setdefault("done", True))
    d = rpcops.DISPATCH._data["port.poweroff"]({"loc": "1-1", "port": 2})
    assert d["ok"] is True and d["adb_shutdown"] is False and cut["done"]


def test_charge_start_reports_running(monkeypatch):
    monkeypatch.setattr(rpcops.ChargeOp, "is_active",
                        classmethod(lambda cls, slot: True))
    monkeypatch.setattr(rpcops, "_charge_tasks",
                        {"1-1:1": {"charge_end_ts": 42}})
    d = rpcops.DISPATCH._data["charge.start"]({"loc": "1-1", "port": 1})
    assert d["ok"] is False and d["charge_end_ts"] == 42


def test_hide_on_unknown_hub(monkeypatch):
    monkeypatch.setattr(rpcops, "load_config", lambda: {"hubs": []})
    d = rpcops.DISPATCH._data["port.hide"]({"loc": "9-9", "port": 1})
    assert d == {"ok": False, "error": "hub not found"}


class _FakeWatch:
    def __init__(self, serial, data):
        self._data = data
    def cc_data(self):
        return self._data


def test_watch_cc_live_returns_and_caches(monkeypatch, tmp_path):
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(rpcops, "last_seen", ls)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch",
                        lambda s, transport=None: _FakeWatch(s, {"kernel": "x", "serial": s}))
    d = rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"})
    assert d["kernel"] == "x" and "stale" not in d
    assert ls.get("S1")["cc"]["kernel"] == "x"


def test_watch_cc_offline_serves_stale(monkeypatch, tmp_path):
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(rpcops, "last_seen", ls)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch", lambda s, transport=None: _FakeWatch(s, {"kernel": "x"}))
    rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"})       # seed while live
    monkeypatch.setattr(rpcops, "Watch", lambda s, transport=None: _FakeWatch(s, {}))  # offline
    d = rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"})
    assert d["kernel"] == "x" and d["stale"] is True and d["last_live_ts"] > 0


def test_watch_cc_offline_uncached_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(rpcops, "last_seen", LastSeen(tmp_path / "ls.json"))
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch", lambda s, transport=None: _FakeWatch(s, {}))
    assert rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"}) == {}


def test_watch_cc_feeds_the_registry(monkeypatch, tmp_path):
    # A live CC read must fold the watch into the Fleet Registry: identity +
    # versions from the blob, codename/resolution from cached geometry, with
    # btmac_self mapped to the registry's btmac. (registry is tmp-isolated by
    # the autouse conftest fixture.)
    from asteroid_docking_bay.registry import registry
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(rpcops, "last_seen", ls)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    ls.record("S1", geometry={"machine": "skipjack", "resolution": "360x360"})
    cc = {"kernel": "3.18.24", "qt": "6.11.2", "soc": "APQ8009W",
          "wlanmac": "aa:bb", "btmac_self": "cc:dd", "bat_cap": 100}
    monkeypatch.setattr(rpcops, "Watch",
                        lambda s, transport=None: _FakeWatch(s, cc))
    rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"})
    rec = registry.get("S1")
    assert rec["fields"]["kernel"] == "3.18.24" and rec["fields"]["qt"] == "6.11.2"
    assert rec["fields"]["codename"] == "skipjack"
    assert rec["fields"]["resolution"] == "360x360"
    assert rec["fields"]["btmac"] == "cc:dd"        # btmac_self → registry btmac
    assert rec["last_source"] == "adb"


def test_fbreport_writes_downloadable_text(monkeypatch, tmp_path):
    monkeypatch.setattr(rpcops, "DIAG_ROOT", tmp_path)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "find_codename_for_loc_port", lambda c, l, p: "sturgeon")
    monkeypatch.setattr(rpcops, "fastboot_getvar_all",
                        lambda s: "product:sturgeon\nbattery-voltage:3668mV")
    d = rpcops.DISPATCH._data["watch.fbreport"]({"loc": "1-2", "port": 1})
    assert d["ok"] and d["name"].startswith("sturgeon-")
    assert d["name"].endswith("-fastboot.txt") and d["lines"] == 2
    assert "battery-voltage:3668mV" in (tmp_path / d["name"]).read_text()


def test_fbreport_needs_a_fastboot_device(monkeypatch, tmp_path):
    monkeypatch.setattr(rpcops, "DIAG_ROOT", tmp_path)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "fastboot_getvar_all", lambda s: "")
    d = rpcops.DISPATCH._data["watch.fbreport"]({"loc": "1-2", "port": 1})
    assert d["ok"] is False and "bootloader" in d["error"]


def test_watch_timeline_returns_battery_points(monkeypatch):
    class _EL:
        def read(self, serial, codename=None):
            return [
                {"event": "check_reading", "ts": 100, "pct": 80},
                {"event": "charge_start", "ts": 150},
                {"event": "drain_reading", "ts": 200, "pct": 70},
                {"event": "flash", "ts": 250},          # no pct → excluded
            ]
        def standby_loss_rate(self, serial, codename, evs):
            return 1.5
    monkeypatch.setattr(rpcops, "event_log", _EL())
    d = rpcops.DISPATCH._data["watch.timeline"]({"serial": "S1"})
    assert d["rate"] == 1.5
    assert d["points"] == [{"ts": 100, "pct": 80}, {"ts": 200, "pct": 70}]


def test_watch_cc_attaches_cached_resolution(monkeypatch, tmp_path):
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(rpcops, "last_seen", ls)
    ls.record("S1", geometry={"round": True, "resolution": "360x360"})
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch", lambda s, transport=None: _FakeWatch(s, {"kernel": "x"}))
    d = rpcops.DISPATCH._data["watch.cc"]({"serial": "S1"})
    assert d["resolution"] == "360x360" and d["geometry"]["round"] is True


def _fake_watch_cls(shot_return, last_path):
    class _W:
        def __init__(self, serial):
            pass
        def screenshot(self):
            return shot_return
        def last_screenshot_path(self):
            return last_path
    return _W


def test_watch_screenshot_stale_fallback(monkeypatch, tmp_path):
    shot = tmp_path / "s.jpg"; shot.write_bytes(b"\xff\xd8jpg")
    # Fresh capture fails (offline) but a last pull exists → serve it stale.
    monkeypatch.setattr(rpcops, "Watch", _fake_watch_cls(None, shot))
    d = rpcops.DISPATCH._data["watch.screenshot"]({"serial": "S1"})
    assert d["ok"] and d["stale"] is True and d["captured_ts"] > 0


def test_watch_screenshot_fresh_is_not_stale(monkeypatch, tmp_path):
    shot = tmp_path / "s.jpg"; shot.write_bytes(b"\xff\xd8jpg")
    monkeypatch.setattr(rpcops, "Watch", _fake_watch_cls(shot, shot))
    d = rpcops.DISPATCH._data["watch.screenshot"]({"serial": "S1"})
    assert d["ok"] and d["stale"] is False


def test_watch_screenshot_fails_when_never_captured(monkeypatch, tmp_path):
    monkeypatch.setattr(rpcops, "Watch",
                        _fake_watch_cls(None, tmp_path / "nope.jpg"))
    d = rpcops.DISPATCH._data["watch.screenshot"]({"serial": "S1"})
    assert d["ok"] is False


def test_flash_start_unmapped_port_streams_error(monkeypatch):
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"hubs": [], "serials": {}})
    monkeypatch.setattr(rpcops, "find_codename_for_loc_port",
                        lambda c, l, p: None)
    frames = list(rpcops.DISPATCH._stream["flash.start"](
        {"loc": "9-9", "port": 9}))
    assert frames == ["ERROR: port not mapped to any codename"]


# ── fastboot-aware power actions ────────────────────────────────────────────

def _cap_cmd(monkeypatch, in_fastboot):
    """Capture the command a power op would run, with the port's watch either
    in fastboot or on adb."""
    import asteroid_docking_bay.rpcops as ro
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(ro, "_run", fake_run)
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda *a, **k: "S1")
    monkeypatch.setattr(ro, "load_config", lambda: {"hubs": []})
    # {fastboot_serial: usb_path}; the port is 1-2:1 → path "1-2.1". The op
    # resolves fastboot by path, so the key here is the port's path, not a name.
    monkeypatch.setattr(ro, "_fastboot_list",
                        lambda: ({"S1": "1-2.1"} if in_fastboot else {}))
    # "not in fastboot" no longer implies "on adb" — a watch can also be in
    # SSH mode, which is the case the routing fix exists for. These cases are
    # the booted-on-adb ones, so state that rather than leaving it implied.
    monkeypatch.setattr(ro, "adb_devices", lambda: {})
    monkeypatch.setattr(ro, "_adb_state", lambda d, s: "device")
    return ro, seen


def test_power_actions_use_fastboot_when_watch_is_in_bootloader(monkeypatch):
    """A watch in the bootloader speaks fastboot, not adb. Sending it an adb
    command is a silent no-op that leaves the UI claiming success, which is
    why the menu was previously hidden entirely in fastboot."""
    ro, seen = _cap_cmd(monkeypatch, in_fastboot=True)
    ro.DISPATCH._data["port.reboot"]({"loc": "1-2", "port": 1})
    assert seen["cmd"].startswith("fastboot -s S1 "), seen["cmd"]
    assert "adb" not in seen["cmd"]

    ro.DISPATCH._data["port.bootloader"]({"loc": "1-2", "port": 1})
    assert seen["cmd"] == "fastboot -s S1 reboot bootloader", seen["cmd"]

    ro.DISPATCH._data["port.recovery"]({"loc": "1-2", "port": 1})
    assert seen["cmd"] == "fastboot -s S1 reboot recovery", seen["cmd"]


def test_power_actions_use_adb_when_watch_is_booted(monkeypatch):
    ro, seen = _cap_cmd(monkeypatch, in_fastboot=False)
    ro.DISPATCH._data["port.reboot"]({"loc": "1-2", "port": 1})
    assert seen["cmd"] == "adb -s S1 reboot", seen["cmd"]

    ro.DISPATCH._data["port.recovery"]({"loc": "1-2", "port": 1})
    assert seen["cmd"] == "adb -s S1 reboot recovery", seen["cmd"]


def test_power_action_targets_the_fastboot_serial_not_the_mapped_serial(monkeypatch):
    """A watch's fastboot serial differs from its adb serial, so the port's
    MAPPED (adb) serial is not in the fastboot list. Resolving fastboot by that
    serial misses it and routes reboot/continue to a dead adb link (beluga, and
    a swapped un-onboarded watch). The action must resolve the fastboot device
    by PORT and command it with its own fastboot serial."""
    import asteroid_docking_bay.rpcops as ro
    seen = {}
    monkeypatch.setattr(ro, "_run",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), (0, "", ""))[1])
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda *a, **k: "MAPPED_ADB")
    monkeypatch.setattr(ro, "load_config", lambda: {"hubs": []})
    monkeypatch.setattr(ro, "_mark_booting", lambda s, commanded=False: None)
    # fastboot serial differs from the mapped adb serial, at this port's path
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {"FBSERIAL": "1-2.1"})
    r = ro.DISPATCH._data["port.reboot"]({"loc": "1-2", "port": 1})
    assert r["ok"] is True and r["via"] == "fastboot", r
    assert seen["cmd"] == "fastboot -s FBSERIAL reboot", seen["cmd"]
    # continue is fastboot-only — it was dead when routed over the mapped serial
    ro.DISPATCH._data["port.continue"]({"loc": "1-2", "port": 1})
    assert seen["cmd"] == "fastboot -s FBSERIAL continue", seen["cmd"]


def test_fastboot_poweroff_targets_the_fastboot_serial(monkeypatch):
    """Shelving a watch in the bootloader must oem-poweroff its FASTBOOT serial,
    not the differing / stale mapped adb serial — otherwise the halt goes to a
    dead adb link and the watch is stranded running on battery in fastboot."""
    import asteroid_docking_bay.rpcops as ro
    order = []
    monkeypatch.setattr(ro, "_run",
                        lambda cmd, **kw: (order.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda *a, **k: "MAPPED_ADB")
    monkeypatch.setattr(ro, "load_config", lambda: {"hubs": []})
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {"FBSERIAL": "1-2.1"})
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda *a, **k: order.append("VBUS_OFF") or True)
    r = ro.DISPATCH._data["port.poweroff"]({"loc": "1-2", "port": 1})
    assert r["ok"] is True, r
    assert order == ["fastboot -s FBSERIAL oem poweroff", "VBUS_OFF"], order


def test_continue_is_rejected_on_a_booted_watch(monkeypatch):
    """`fastboot continue` resumes a boot chain; a running watch has none.
    Offering it over adb would send a meaningless command and report ok."""
    ro, seen = _cap_cmd(monkeypatch, in_fastboot=False)
    r = ro.DISPATCH._data["port.continue"]({"loc": "1-2", "port": 1})
    assert r["ok"] is False and "adb" in r["error"], r
    assert "cmd" not in seen, f"ran a command anyway: {seen}"


def test_fastboot_poweroff_uses_oem_poweroff_then_cuts_vbus(monkeypatch):
    """LK cannot shut down with USB attached — it grants ~5s to disconnect.
    The rig cuts VBUS itself, so the order (command first, power second) is
    load-bearing: cutting first would strand the watch running on battery."""
    import asteroid_docking_bay.rpcops as ro
    order = []
    monkeypatch.setattr(ro, "_run",
                        lambda cmd, **kw: (order.append(cmd), (0, "", ""))[1])
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda *a, **k: "S1")
    monkeypatch.setattr(ro, "load_config", lambda: {"hubs": []})
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {"S1": "1-2.1"})
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda *a, **k: order.append("VBUS_OFF") or True)
    r = ro.DISPATCH._data["port.poweroff"]({"loc": "1-2", "port": 1})
    assert r["ok"] is True, r
    assert order == ["fastboot -s S1 oem poweroff", "VBUS_OFF"], order


def test_failed_fastboot_poweroff_does_not_cut_vbus(monkeypatch):
    """`oem poweroff` is not universal — rover's bootloader lacks it entirely.
    Cutting VBUS after a failed shutdown strands the watch running on battery
    in the bootloader, invisible to the host: the rig's worst failure mode.
    A failed shutdown must leave power ON and say so."""
    import asteroid_docking_bay.rpcops as ro
    cut = {}
    monkeypatch.setattr(ro, "_run", lambda cmd, **kw: (1, "", "unknown command"))
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda *a, **k: "S1")
    monkeypatch.setattr(ro, "load_config", lambda: {"hubs": []})
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {"S1": "1-2.1"})
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda *a, **k: cut.setdefault("done", True))
    r = ro.DISPATCH._data["port.poweroff"]({"loc": "1-2", "port": 1})
    assert r["ok"] is False, r
    assert "done" not in cut, "cut VBUS after a failed fastboot shutdown"


# ── port ops must not disturb a running operation ───────────────────────────
#
# The UI greys these controls out on a busy row, but the UI is not a safety
# boundary. On 2026-07-18 a direct `POST /api/on` to test an unrelated feature
# re-powered a port mid-drain, recharged the watch 96% -> 100%, and destroyed
# five hours of readings while the browser correctly showed the row disabled.

@pytest.mark.parametrize("op,args", [
    ("port.set",        {"on": True}),
    ("port.cycle",      {}),
    ("port.poweroff",   {}),
    ("port.reboot",     {}),
    ("port.bootloader", {}),
])
def test_port_ops_refuse_while_an_operation_owns_the_port(monkeypatch, op, args):
    import asteroid_docking_bay.rpcops as ro
    touched = {}
    monkeypatch.setattr(ro, "active_op_on_slot", lambda slot: "drain")
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda *a, **k: touched.setdefault("power", True))
    monkeypatch.setattr(ro, "uhubctl_cycle",
                        lambda *a, **k: touched.setdefault("cycle", True))
    monkeypatch.setattr(ro, "test_port_power_switching",
                        lambda *a, **k: touched.setdefault("ppps", True))
    monkeypatch.setattr(ro, "_run", lambda *a, **k: touched.setdefault("cmd", True))
    r = ro.DISPATCH._data[op]({"loc": "1-2.3", "port": 1, **args})
    assert r["ok"] is False and r.get("busy") == "drain", r
    assert not touched, f"{op} touched the hardware anyway: {touched}"


def test_port_ops_work_normally_when_no_operation_is_running(monkeypatch):
    """The guard must not break ordinary use — an idle port still switches."""
    import asteroid_docking_bay.rpcops as ro
    monkeypatch.setattr(ro, "active_op_on_slot", lambda slot: None)
    monkeypatch.setattr(ro, "uhubctl_set_power", lambda *a, **k: True)
    r = ro.DISPATCH._data["port.set"]({"loc": "1-2.3", "port": 1, "on": True})
    assert r == {"ok": True, "confirmed": True}, r


def _mock_switch_ssh_config(monkeypatch, ro, cfg=None):
    cfg = cfg if cfg is not None else {}
    monkeypatch.setattr(ro, "load_config", lambda: cfg)
    monkeypatch.setattr(ro, "save_config", lambda c: None)
    return cfg


def test_switch_ssh_assigns_a_unique_ip_then_switches(monkeypatch):
    """ADB->SSH gives the watch its own IP (so two watches never both grab the
    default 192.168.2.15) and then switches it to developer mode. Both commands
    must target the named serial, in that order."""
    import asteroid_docking_bay.rpcops as ro
    _mock_switch_ssh_config(monkeypatch, ro)
    cmds = []
    monkeypatch.setattr(ro, "_run", lambda cmd, **k: (cmds.append(cmd), (0, "", ""))[1])
    d = ro.DISPATCH._data["watch.switch_ssh"]({"serial": "S9"})
    assert d["ok"] is True and d["ip"] == "192.168.13.37", d
    assert cmds == ["adb -s S9 shell usb_moded_util -n set:ip,192.168.13.37",
                    "adb -s S9 shell usb_moded_util -s developer_mode"], cmds


def test_switch_ssh_without_serial_is_rejected(monkeypatch):
    import asteroid_docking_bay.rpcops as ro
    ran = []
    monkeypatch.setattr(ro, "_run", lambda *a, **k: ran.append(a) or (0, "", ""))
    d = ro.DISPATCH._data["watch.switch_ssh"]({})
    assert d["ok"] is False and not ran


def test_switch_ssh_reports_failure_when_usb_moded_refuses(monkeypatch):
    """A watch whose usb-moded service is down prints an error but still exits
    0, and the adb link stays up. That must surface as a failure, not a silent
    'ok' — the beluga case."""
    import asteroid_docking_bay.rpcops as ro
    _mock_switch_ssh_config(monkeypatch, ro)
    monkeypatch.setattr(ro, "_run",
                        lambda cmd, **k: (0, "Trying to set the following mode "
                                          "developer_mode\nSorry an error occured, "
                                          "your request was not processed.", ""))
    d = ro.DISPATCH._data["watch.switch_ssh"]({"serial": "S9"})
    assert d["ok"] is False and "usb-moded" in d["error"], d


def test_switch_ssh_reports_ok_when_the_link_drops(monkeypatch):
    """A switch that took re-enumerates and drops the link, so the command
    comes back with no error text — that is success."""
    import asteroid_docking_bay.rpcops as ro
    _mock_switch_ssh_config(monkeypatch, ro)
    monkeypatch.setattr(ro, "_run", lambda cmd, **k: (255, "", "closed by remote host"))
    d = ro.DISPATCH._data["watch.switch_ssh"]({"serial": "S9"})
    assert d["ok"] is True, d


def test_reachable_transport_prefers_adb_then_ssh(monkeypatch):
    """The Control Center and other watch ops must work over whichever link is
    up: adb when the watch is on adb, else SSH at its assigned address when it
    is in SSH mode there. This is what makes SSH a full adb replacement."""
    import asteroid_docking_bay.rpcops as ro
    from asteroid_docking_bay.transport import SshTransport

    # On adb → default transport (None → AdbTransport).
    monkeypatch.setattr(ro, "adb_devices", lambda: {"S1": {"status": "device"}})
    monkeypatch.setattr(ro, "_adb_state", lambda devs, s: "device")
    assert ro._reachable_transport("S1") is None

    # Not on adb, but answering over SSH somewhere → SshTransport there.
    # ssh_reach_ip owns WHERE (allocated address, or the shared default when
    # this watch's link wins the route) — here it reports the allocated one.
    monkeypatch.setattr(ro, "_adb_state", lambda devs, s: None)
    monkeypatch.setattr(ro, "load_config", lambda: {"ssh_ips": {"S1": "192.168.13.37"}})
    monkeypatch.setattr(ro, "ssh_reach_ip",
                        lambda cfg, s: "192.168.13.37" if s == "S1" else None)
    t = ro._reachable_transport("S1")
    assert isinstance(t, SshTransport) and t.ip == "192.168.13.37", t

    # Neither adb nor reachable SSH → default (offline handled downstream).
    monkeypatch.setattr(ro, "ssh_reach_ip", lambda cfg, s: None)
    assert ro._reachable_transport("S1") is None


def test_wear_arm_powers_the_port_and_flags_it(monkeypatch):
    """Arming wear tops the watch up (port on) and marks it wear-held so the
    port is kept and not auto-cycled. A wear event is logged to break the
    standby chain (the coming interval is wearing, not shelf-rest)."""
    import asteroid_docking_bay.rpcops as ro
    powered, recorded, events = [], {}, []
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda c, l, p: "S9")
    monkeypatch.setattr(ro, "load_config", lambda: {})
    monkeypatch.setattr(ro, "find_codename_for_loc_port", lambda c, l, p: "skipjack")
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda l, p, on: powered.append((l, p, on)))
    monkeypatch.setattr(ro.last_seen, "mark",
                        lambda s, **k: recorded.update(k))
    monkeypatch.setattr(ro.event_log, "log", lambda *a, **k: events.append(a))
    d = ro.DISPATCH._data["wear.set"]({"loc": "1-2", "port": 1, "on": True})
    assert d == {"ok": True, "wear": True}
    assert powered == [("1-2", 1, True)] and recorded.get("wear") is True
    assert any("wear" in a for a in events), "no wear event logged"


def test_wear_release_frees_a_gone_watch_but_not_a_present_one(monkeypatch):
    """Release frees the port when the watch is gone (worn), but must NOT raw-cut
    a re-docked present watch — that would strand it running on battery."""
    import asteroid_docking_bay.rpcops as ro
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda c, l, p: "S9")
    monkeypatch.setattr(ro, "load_config", lambda: {})
    monkeypatch.setattr(ro, "last_seen",
                        type("L", (), {"mark": staticmethod(lambda s, **k: None)}))
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {})

    # Watch gone -> free the port.
    powered = []
    monkeypatch.setattr(ro, "adb_devices", lambda: {})
    monkeypatch.setattr(ro, "_adb_state", lambda d, s: None)
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda l, p, on: powered.append((l, p, on)))
    assert ro.DISPATCH._data["wear.set"]({"loc": "1-2", "port": 1, "on": False})["ok"]
    assert powered == [("1-2", 1, False)]

    # Watch present (re-docked) -> leave it powered.
    powered2 = []
    monkeypatch.setattr(ro, "_adb_state", lambda d, s: "device")
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda l, p, on: powered2.append((l, p, on)))
    ro.DISPATCH._data["wear.set"]({"loc": "1-2", "port": 1, "on": False})
    assert powered2 == [], "release raw-cut a present watch — stranding hazard"


def test_poweroff_over_ssh_marks_down_and_does_not_strand(monkeypatch):
    """An SSH-mode watch must be powered off over SSH (not a failed adb command
    followed by a raw VBUS cut that strands it running). Delivery over ssh is
    graceful, so it stamps safe_off and the "down" pill can show."""
    import asteroid_docking_bay.rpcops as ro
    calls, marked, powered = [], {}, []
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda c, l, p: "S9")
    monkeypatch.setattr(ro, "load_config", lambda: {})
    monkeypatch.setattr(ro, "ssh_reach_ip", lambda c, s: "192.168.13.37")
    monkeypatch.setattr(ro, "_refuse_if_busy", lambda l, p: None)
    monkeypatch.setattr(ro, "_fastboot_list", lambda: {})              # not fastboot
    monkeypatch.setattr(ro, "_adb_state", lambda d, s: None)          # not on adb
    monkeypatch.setattr(ro, "adb_devices", lambda: {})
    monkeypatch.setattr(ro, "_detect_rndis", lambda ip: True)         # reachable over ssh

    class _T:
        def __init__(self, ip): self.ip = ip
        def shell(self, cmd, timeout=8): calls.append((self.ip, cmd)); return (255, "", "closed")
    monkeypatch.setattr(ro, "SshTransport", _T)
    monkeypatch.setattr(ro, "uhubctl_set_power",
                        lambda l, p, on: powered.append(on) or True)
    monkeypatch.setattr(ro.last_seen, "mark", lambda s, **k: marked.update(k))

    d = ro.DISPATCH._data["port.poweroff"]({"loc": "1-2", "port": 1})
    assert d["ok"] and d["adb_shutdown"] is True, d
    assert calls == [("192.168.13.37", "poweroff")], "did not power off over ssh"
    assert powered == [False], "port not cut after the ssh halt"
    assert marked.get("safe_off_ts"), "ssh poweroff did not stamp the down marker"


def test_set_usb_mode_preference_persists_and_validates(monkeypatch):
    """The top-bar toggle op writes the fleet USB-mode preference and rejects
    anything that is not exactly 'adb' or 'ssh' (a bad value must not become a
    third, meaningless mode)."""
    import asteroid_docking_bay.rpcops as ro
    store = {}
    monkeypatch.setattr(ro, "load_config", lambda: store)
    monkeypatch.setattr(ro, "save_config", lambda c: None)

    assert ro.DISPATCH._data["prefs.set_usb_mode"]({"mode": "ssh"}) == {"ok": True, "mode": "ssh"}
    assert store["usb_mode_preference"] == "ssh"
    assert ro.DISPATCH._data["prefs.set_usb_mode"]({"mode": "adb"})["ok"]
    assert store["usb_mode_preference"] == "adb"

    bad = ro.DISPATCH._data["prefs.set_usb_mode"]({"mode": "developer"})
    assert bad["ok"] is False and store["usb_mode_preference"] == "adb", (
        "an invalid mode changed the stored preference")


def test_status_get_reports_the_usb_mode_preference(monkeypatch):
    """status.get carries the preference so the top bar can render the toggle
    label without a second request."""
    import asteroid_docking_bay.rpcops as ro
    monkeypatch.setattr(ro, "load_config", lambda: {"usb_mode_preference": "ssh"})
    monkeypatch.setattr(ro, "_web_status_data", lambda cfg: [])
    d = ro.DISPATCH._data["status.get"]({})
    assert d["usb_mode_preference"] == "ssh"


def test_power_on_boots_and_raw_power_off_clears_the_shelved_marker(monkeypatch):
    """Powering a docked watch's port on boots it, so it stamps booting_since
    for the "booting up" pill. A raw power-off (the toggle) is NOT a graceful
    shutdown, so it stamps no boot AND clears any (possibly stale) safe_off
    marker — otherwise the watch would falsely read "shelved" after a failed
    manual boot. Only port.poweroff sets the shelved marker."""
    import asteroid_docking_bay.rpcops as ro
    marked = {}
    monkeypatch.setattr(ro, "_refuse_if_busy", lambda l, p: None)
    monkeypatch.setattr(ro, "load_config", lambda: {})
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda c, l, p: "S9")
    monkeypatch.setattr(ro, "uhubctl_set_power", lambda l, p, on: True)   # confirmed
    monkeypatch.setattr(ro.last_seen, "mark",
                        lambda s, **k: marked.update({"serial": s, **k}))

    d = ro.DISPATCH._data["port.set"]({"loc": "1-2", "port": 1, "on": True})
    assert d["ok"] and marked.get("serial") == "S9", d
    assert marked.get("booting_since"), "power-on did not stamp the boot marker"

    marked.clear()
    ro.DISPATCH._data["port.set"]({"loc": "1-2", "port": 1, "on": False})
    assert "booting_since" not in marked, "power-off must not claim a boot"
    assert marked.get("safe_off_ts") == 0, "raw power-off did not clear the shelved marker"


def test_reboot_and_continue_track_the_boot_but_bootloader_does_not(monkeypatch):
    """The actions that send the watch off to boot the OS (reboot, continue)
    stamp booting_since; the ones that land in another mode (bootloader) do
    not — a bootloader entry is not an OS boot to wait on."""
    import asteroid_docking_bay.rpcops as ro
    marks = []
    monkeypatch.setattr(ro, "_refuse_if_busy", lambda l, p: None)
    monkeypatch.setattr(ro, "load_config", lambda: {})
    monkeypatch.setattr(ro, "find_serial_for_loc_port", lambda c, l, p: "S9")
    monkeypatch.setattr(ro, "_run", lambda cmd, **k: (0, "", ""))
    monkeypatch.setattr(ro, "adb_devices", lambda: {})
    monkeypatch.setattr(ro, "_adb_state", lambda d, s: "device")   # booted on adb
    monkeypatch.setattr(ro.last_seen, "mark", lambda s, **k: marks.append((s, k)))

    monkeypatch.setattr(ro, "_fastboot_list", lambda: {})    # on adb, not fastboot
    marks.clear()
    ro.DISPATCH._data["port.reboot"]({"loc": "1-2", "port": 1})
    assert marks and marks[-1][0] == "S9" and "booting_since" in marks[-1][1]

    marks.clear()
    ro.DISPATCH._data["port.bootloader"]({"loc": "1-2", "port": 1})
    assert marks == [], "reboot-to-bootloader must not claim an OS boot"

    monkeypatch.setattr(ro, "_fastboot_list", lambda: {"S9": "1-2.1"})   # continue is fb-only (path = port 1-2:1)
    marks.clear()
    ro.DISPATCH._data["port.continue"]({"loc": "1-2", "port": 1})
    assert marks and "booting_since" in marks[-1][1]


def test_watch_cc_reports_the_transport_for_poll_pacing(monkeypatch):
    """The Control Center paces its live poll to the link: adb is fast, SSH is
    slow. So watch.cc must report which transport answered."""
    import asteroid_docking_bay.rpcops as ro
    from asteroid_docking_bay.transport import SshTransport

    class _W:
        def __init__(self, s, transport=None): pass
        def cc_data(self): return {"kernel": "x"}
    monkeypatch.setattr(ro, "Watch", _W)
    monkeypatch.setattr(ro, "last_seen",
                        type("L", (), {"record": staticmethod(lambda *a, **k: None),
                                       "get": staticmethod(lambda s: None)}))
    monkeypatch.setattr(ro.event_log, "standby_off_to_on_rate", lambda *a, **k: None)

    monkeypatch.setattr(ro, "_reachable_transport", lambda s: None)   # adb
    assert ro.DISPATCH._data["watch.cc"]({"serial": "S1"})["transport"] == "adb"
    monkeypatch.setattr(ro, "_reachable_transport", lambda s: SshTransport("1.2.3.4"))
    assert ro.DISPATCH._data["watch.cc"]({"serial": "S1"})["transport"] == "ssh"


def test_watch_cc_stale_returns_cached_without_device_io(monkeypatch):
    """The panel's instant-open path asks for the last-known values with no
    device read. stale=True must serve the cached blob (marked stale) and
    never touch the watch."""
    import asteroid_docking_bay.rpcops as ro
    monkeypatch.setattr(ro, "last_seen",
                        type("L", (), {"get": staticmethod(lambda s:
                            {"cc": {"kernel": "3.18"}, "cc_ts": 1000.0})})())
    monkeypatch.setattr(ro.event_log, "standby_off_to_on_rate", lambda *a, **k: None)
    def _boom(*a, **k):
        raise AssertionError("stale path touched the device")
    monkeypatch.setattr(ro, "_reachable_transport", _boom)
    monkeypatch.setattr(ro, "Watch", _boom)
    d = ro.DISPATCH._data["watch.cc"]({"serial": "S1", "stale": True})
    assert d["kernel"] == "3.18" and d["stale"] is True and d["last_live_ts"] == 1000.0
    monkeypatch.setattr(ro, "last_seen",
                        type("L", (), {"get": staticmethod(lambda s: None)})())
    assert ro.DISPATCH._data["watch.cc"]({"serial": "X", "stale": True}) == {}


# ── live battery readings feed the history over any transport ─────────────────

def test_timeline_includes_live_readings(monkeypatch, tmp_path):
    """A live CC read (over adb or ssh) is logged as 'live_reading' and must show
    in the battery-history points — watching a watch charge over SSH left the
    history flat before, since only charge/drain ops logged."""
    from asteroid_docking_bay.events import EventLog
    el = EventLog(tmp_path)
    el.log("S1", None, "check_reading", pct=80)
    el.log("S1", None, "live_reading", pct=73)
    monkeypatch.setattr(rpcops, "event_log", el)
    d = rpcops.DISPATCH._data["watch.timeline"]({"serial": "S1"})
    pcts = {p["pct"] for p in d["points"]}
    assert 80 in pcts and 73 in pcts, "live_reading missing from the battery history"


def test_live_readings_do_not_pollute_the_standby_rate(tmp_path):
    """Live readings carry a charge bump (the port is on for the read) and are
    logged while charging, so they must NOT count toward the honest standby rate
    — that math is check/drain readings only."""
    from asteroid_docking_bay.events import EventLog
    el = EventLog(tmp_path)
    el.log("S1", None, "live_reading", pct=90)
    el.log("S1", None, "live_reading", pct=50)
    assert el.standby_loss_rate("S1", None) is None, \
        "live_readings leaked into the standby rate"


def test_log_live_battery_throttles_and_ignores_unreadable(monkeypatch):
    logged = []
    monkeypatch.setattr(rpcops.event_log, "log", lambda *a, **k: logged.append(k))
    rpcops._live_reading_ts.clear()
    rpcops._log_live_battery("S1", 73)
    rpcops._log_live_battery("S1", 74)      # immediately after — throttled out
    assert len(logged) == 1 and logged[0]["pct"] == 73, "live reading not throttled"
    rpcops._log_live_battery("S1", None)    # unreadable — nothing logged
    assert len(logged) == 1


# ── physical hands (narwhal live-view overlay) ────────────────────────────────

def test_watch_hands_parses_the_sysfs_position():
    from asteroid_docking_bay.watchctl import Watch
    w = Watch("S1", transport=object())
    w.t = type("T", (), {"shell": lambda self, c, timeout=8: (0, "18:31\n", "")})()
    assert w.hands() == {"position": "18:31", "h": 18, "m": 31}
    w.t = type("T", (), {"shell": lambda self, c, timeout=8: (0, "", "")})()
    assert w.hands() is None        # no movement → empty sysfs → None


def test_watch_hands_op_dispatches(monkeypatch):
    class W:
        def __init__(self, *a, **k):
            pass

        def hands(self):
            return {"position": "18:31", "h": 18, "m": 31}

    monkeypatch.setattr(rpcops, "Watch", W)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    d = rpcops.DISPATCH._data["watch.hands"]({"serial": "S1"})
    assert d["ok"] is True and d["hands"]["h"] == 18 and d["hands"]["m"] == 31


def test_set_hands_op_validates_before_moving(monkeypatch):
    called = []
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch",
                        type("W", (), {"__init__": lambda self, *a, **k: None,
                                       "set_hands": lambda self, w: (called.append(w), True)[1]}))
    bad = rpcops.DISPATCH._data["watch.set_hands"]({"serial": "S1", "when": "half past two"})
    assert bad == {"ok": False, "error": "bad datetime"} and called == []
    ok = rpcops.DISPATCH._data["watch.set_hands"]({"serial": "S1", "when": "2026-07-23 02:42:00"})
    assert ok == {"ok": True} and called == ["2026-07-23 02:42:00"]


def _run_sweep_one_port(monkeypatch, halt_rc):
    """Drive _sweep_one_port with everything mocked, recording the order of
    the halt (adb poweroff), any adb polling, and the VBUS cut. Returns
    (events, marks) — marks holds last_seen.mark calls per serial."""
    import types
    events, marks = [], {}
    monkeypatch.setattr(rpcops, "charge_config",
                        lambda c: types.SimpleNamespace(onboard_wait_seconds=0))
    monkeypatch.setattr(rpcops, "load_config", lambda: {"serials": {}})
    monkeypatch.setattr(rpcops, "save_config", lambda c: None)
    monkeypatch.setattr(
        rpcops, "uhubctl_set_power",
        lambda l, p, on: events.append("cut" if not on else "power-on"))
    monkeypatch.setattr(rpcops, "_sweep_wait_adb",
                        lambda path, secs, emit: ("S1", None))
    monkeypatch.setattr(rpcops, "adb_devices",
                        lambda: (events.append("poll"), {})[1])
    monkeypatch.setattr(rpcops, "get_watch_codename", lambda s: "skipjack")
    monkeypatch.setattr(rpcops, "_sweep_map_to_port", lambda *a, **k: None)
    monkeypatch.setattr(rpcops, "test_port_power_switching",
                        lambda l, p, s: (True, "ok"))
    monkeypatch.setattr(rpcops, "_store_smart_verdict", lambda h, p, v: None)
    monkeypatch.setattr(rpcops, "last_seen", types.SimpleNamespace(
        record=lambda *a, **k: None,
        mark=lambda serial, **kw: marks.setdefault(serial, {}).update(kw)))
    monkeypatch.setattr(rpcops, "registry",
                        types.SimpleNamespace(note=lambda *a, **k: None))
    monkeypatch.setattr(
        rpcops, "_run",
        lambda cmd, **kw: (events.append("halt"), (halt_rc, "", ""))[1]
        if "poweroff" in cmd else (0, "", ""))
    monkeypatch.setattr(rpcops.time, "sleep", lambda s: None)

    class _FakeWatch:
        def __init__(self, *a, **k): pass
        def cc_data(self): return {"bat_cap": 50}
        def geometry(self): return {"machine": "skipjack"}
    monkeypatch.setattr(rpcops, "Watch", _FakeWatch)

    assert rpcops._sweep_one_port("1-3.3.3", 2, True,
                                  lambda m: None) == ("skipjack", None)
    return events, marks


def test_sweep_shelve_cuts_vbus_immediately_after_halt(monkeypatch):
    """The sweep's shelve must cut VBUS in the very next step after the
    synchronous poweroff delivery — no adb polling in between. The old
    wait-for-adb-drop raced the halt (watches bounced back up) and treated
    the drop as poweroff proof, though a REBOOT drops adb too: 14 watches
    were stamped 'shelved' while running on battery (2026-07-25, audit F4).
    Planted-bug: reinstate the wait loop between halt and cut → this fails."""
    events, marks = _run_sweep_one_port(monkeypatch, halt_rc=0)
    assert events.index("cut") == events.index("halt") + 1
    assert "safe_off_ts" in marks.get("S1", {})   # delivered halt → shelved


def test_sweep_shelve_claims_nothing_on_failed_halt(monkeypatch):
    """A failed poweroff delivery must still cut VBUS but must NOT stamp
    safe_off — a bare cut is not a shelve (the watch may run on battery)."""
    events, marks = _run_sweep_one_port(monkeypatch, halt_rc=1)
    assert "cut" in events
    assert "safe_off_ts" not in marks.get("S1", {})


def test_sweep_skip_aborts_the_boot_wait(monkeypatch):
    """onboard.sweep_skip makes the active boot-wait return early (the port is
    then handled as a no-show) and clears the event so the next port's wait
    runs normally. Planted-bug: drop the event check in _sweep_wait_adb and
    this fails on the timing assertion."""
    import time
    rpcops._sweep_skip.set()
    monkeypatch.setattr(rpcops, "adb_devices", lambda: {})
    monkeypatch.setattr(rpcops, "_sysfs_path_to_serial_map", lambda s: {})
    t0 = time.monotonic()
    assert rpcops._sweep_wait_adb("1-3.2", 30, lambda m: None) == (None, None)
    assert time.monotonic() - t0 < 5           # aborted, not timed out
    assert not rpcops._sweep_skip.is_set()     # cleared for the next port


def test_sweep_skip_requires_a_running_sweep(monkeypatch):
    """A stale skip click with no sweep running must not arm a skip that
    would silently eat the first port of a FUTURE sweep."""
    monkeypatch.setattr(rpcops, "_remap_tasks", {})
    assert rpcops.DISPATCH._data["onboard.sweep_skip"]({})["ok"] is False
    assert not rpcops._sweep_skip.is_set()
    monkeypatch.setattr(rpcops, "_remap_tasks", {"__sweep__": {"done": False}})
    assert rpcops.DISPATCH._data["onboard.sweep_skip"]({})["ok"] is True
    rpcops._sweep_skip.clear()


def test_sweep_leaf_ports_skips_hidden_hubs_and_excluded_ports(monkeypatch):
    """A hidden hub (the Lenovo dock) and user-excluded ports are not swept —
    every empty socket otherwise costs a full boot window."""
    tree = {
        "/sys/bus/usb/devices/1-3:*": ["/sys/bus/usb/devices/1-3:1.0"],
        "/sys/bus/usb/devices/1-3:1.0/1-3-port*": [
            "/sys/bus/usb/devices/1-3:1.0/1-3-port1",
            "/sys/bus/usb/devices/1-3:1.0/1-3-port2"],
        "/sys/bus/usb/devices/1-9:*": ["/sys/bus/usb/devices/1-9:1.0"],
        "/sys/bus/usb/devices/1-9:1.0/1-9-port*": [
            "/sys/bus/usb/devices/1-9:1.0/1-9-port1"],
    }
    import glob as glob_mod
    monkeypatch.setattr(glob_mod, "glob", lambda pat: tree.get(pat, []))
    cfg = {"hubs": [
        {"location": "1-3", "exclude": {"2": "hidden by user"}},
        {"location": "1-9", "hidden": True},
    ]}
    assert rpcops._sweep_leaf_ports(cfg) == [("1-3", 1)]


def test_sweep_unauthorized_watch_stays_powered_and_is_noted(monkeypatch):
    """A watch that is alive but ADB-unauthorized (the WearOS RSA prompt) is
    NOT a no-show: its port stays POWERED (a cut would strand it running on
    battery), the sighting lands in the fleet registry by USB serial, and the
    sweep reports 'unauthorized'. Planted-bug: routing it through the no-show
    branch (cut + needs-charge) fails the no-cut assertion."""
    import types
    events, noted = [], {}
    monkeypatch.setattr(rpcops, "charge_config",
                        lambda c: types.SimpleNamespace(onboard_wait_seconds=0))
    monkeypatch.setattr(rpcops, "load_config", lambda: {"serials": {}})
    monkeypatch.setattr(rpcops, "uhubctl_set_power",
                        lambda l, p, on: events.append("cut" if not on else "power-on"))
    monkeypatch.setattr(rpcops, "_sweep_wait_adb",
                        lambda path, secs, emit: (None, "unauthorized"))
    monkeypatch.setattr(rpcops, "_detect_rndis", lambda *a: False)
    monkeypatch.setattr(rpcops, "_sysfs_serial_at", lambda l, p: "WEAR123")
    monkeypatch.setattr(rpcops, "registry", types.SimpleNamespace(
        note=lambda serial, **kw: noted.update({serial: kw})))
    assert rpcops._sweep_one_port("1-6", 1, True,
                                  lambda m: None) == (None, "unauthorized")
    assert "cut" not in events                 # left powered
    assert "WEAR123" in noted                  # sighted in the fleet registry


# ── _watch_action transport routing ──────────────────────────────────────────

def _action_env(monkeypatch, *, fb=None, adb_online=False, transport=None):
    """Stand up the three transports independently so each branch is reachable."""
    import types
    calls = []
    monkeypatch.setattr(rpcops, "_refuse_if_busy", lambda l, p: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "_fastboot_serial_for_port", lambda l, p: fb)
    monkeypatch.setattr(rpcops, "adb_devices", lambda: {})
    monkeypatch.setattr(rpcops, "_adb_state",
                        lambda d, s: "device" if adb_online else None)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: transport)
    monkeypatch.setattr(rpcops, "_mark_booting", lambda *a, **k: None)
    monkeypatch.setattr(rpcops, "_run",
                        lambda cmd, **k: calls.append(cmd) or (0, "", ""))
    return calls


class _Ssh:
    kind = "ssh (usb)"

    def __init__(self, rc=255, err="Connection to 1.2.3.4 closed by remote host."):
        self.rc, self.err, self.sent = rc, err, []

    def shell(self, cmd, timeout=None):
        self.sent.append(cmd)
        return self.rc, "", self.err


def test_watch_action_prefers_fastboot_then_adb():
    """Unchanged behaviour for the two links that already worked."""
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    try:
        calls = _action_env(monkeypatch, fb="FB1")
        assert rpcops._watch_action("1-3", 1, "reboot", "reboot", "x")["via"] == "fastboot"
        assert calls and calls[0].startswith("fastboot -s FB1")
        calls2 = _action_env(monkeypatch, adb_online=True)
        assert rpcops._watch_action("1-3", 1, "reboot", "reboot", "x")["via"] == "adb"
        assert calls2 and calls2[0].startswith("adb -s S1")
    finally:
        monkeypatch.undo()


def test_watch_action_reaches_an_ssh_mode_watch(monkeypatch):
    """A watch in SSH mode is reachable — the command just has to go over the
    link that is actually up. Before this it was fired at adb, which no longer
    answers, so the caller waited on a state change that could never happen."""
    t = _Ssh()
    _action_env(monkeypatch, transport=t)
    res = rpcops._watch_action("1-3", 1, "reboot", "reboot", "x",
                               boots_os=True, ssh_cmd="reboot")
    assert res["ok"] is True and res["via"] == "ssh (usb)"
    assert t.sent == ["reboot"]


def test_bootloader_on_an_ssh_watch_refuses_out_loud(monkeypatch):
    """THE BUG mo reported: the 'boot to fastboot' button did nothing on an SSH
    watch. Rebooting into the bootloader has no portable SSH equivalent, so the
    honest answer is an actionable refusal — never a silent success, which
    leaves the caller waiting on a watch that was never told anything."""
    _action_env(monkeypatch, transport=_Ssh())
    res = rpcops._watch_action("1-3", 1, "reboot bootloader", "reboot bootloader",
                               "failed")          # no ssh_cmd
    assert res["ok"] is False, "claimed success without delivering the command"
    assert "adb" in res["error"].lower() and "ssh" in res["error"].lower()


def test_watch_action_reports_when_nothing_can_reach_the_watch(monkeypatch):
    _action_env(monkeypatch, transport=None)
    res = rpcops._watch_action("1-3", 1, "reboot", "reboot", "x", ssh_cmd="reboot")
    assert res["ok"] is False
    assert "fastboot" in res["error"] and "adb" in res["error"]


def test_ssh_delivery_distinguishes_a_dropped_link_from_never_arriving():
    """A reboot kills the link it arrived on, so ssh exits non-zero on SUCCESS.
    Only a failure to connect is a real failure — treating every non-zero exit
    as failure would report every successful reboot as broken."""
    assert rpcops._ssh_delivered(0, "")
    assert rpcops._ssh_delivered(255, "Connection to 1.2.3.4 closed by remote host.")
    for fatal in ("ssh: connect to host 1.2.3.4 port 22: Connection refused",
                  "ssh: connect to host 1.2.3.4 port 22: No route to host",
                  "ssh: connect to host 1.2.3.4 port 22: Connection timed out",
                  "Host key verification failed."):
        assert not rpcops._ssh_delivered(255, fatal), fatal


# ── a cached Control Center blob for the WRONG OS ────────────────────────────

def test_stale_cc_drops_a_blob_describing_a_different_os(monkeypatch):
    """After beluga 22979c8c was restored to Wear OS its panel went on
    reporting an AsteroidOS version, kernel and Qt build it no longer had.
    That is not stale data — it is data about another system, and no age label
    can qualify a false claim about what the watch IS."""
    from asteroid_docking_bay.watchctl import _watch_os
    monkeypatch.setitem(_watch_os, "S1", "WearOS")
    monkeypatch.setattr(rpcops.last_seen, "get", lambda s: {
        "cc": {"os": "AsteroidOS 2.2-nightly", "kernel": "4.9.112",
               "bat_cap": "100"}, "cc_ts": 1000.0})
    assert rpcops._stale_cc("S1", None) == {}, \
        "served another OS's identity as this watch's own"


def test_stale_cc_still_serves_a_blob_from_the_same_os(monkeypatch):
    """The point is wrongness, not age. A watch that is merely off the bus must
    still get its last-known values, dimmed and stamped — that behaviour is why
    the cache exists."""
    from asteroid_docking_bay.watchctl import _watch_os
    monkeypatch.setitem(_watch_os, "S1", "asteroidos")
    monkeypatch.setattr(rpcops.last_seen, "get", lambda s: {
        "cc": {"os": "AsteroidOS 2.2-nightly", "bat_cap": "88"}, "cc_ts": 1000.0})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda c, s: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    blob = rpcops._stale_cc("S1", None)
    assert blob["bat_cap"] == "88" and blob["stale"] is True
    assert blob["last_live_ts"] == 1000.0


def test_stale_cc_keeps_the_cache_when_the_os_is_not_known(monkeypatch):
    """No detection cached (the watch has been offline since a restart) means
    no evidence of a change — and absence of evidence must not throw away the
    only data we have."""
    from asteroid_docking_bay.watchctl import _watch_os
    monkeypatch.delitem(_watch_os, "S1", raising=False)
    monkeypatch.setattr(rpcops.last_seen, "get", lambda s: {
        "cc": {"os": "AsteroidOS 2.2-nightly", "bat_cap": "77"}, "cc_ts": 1.0})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda c, s: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    assert rpcops._stale_cc("S1", None)["bat_cap"] == "77"


def test_os_family_is_blunt_on_purpose():
    """It only has to notice 'this is a different system', so an unrecognised
    string must compare as unknown rather than as a mismatch — otherwise a new
    OS name would silently start discarding good caches."""
    assert rpcops._os_family("AsteroidOS 2.2-nightly") == "asteroidos"
    assert rpcops._os_family("Wear OS (Android 9)") == "android"
    assert rpcops._os_family("Android Wear (Android 7.1.1)") == "android"
    assert rpcops._os_family("") == "" and rpcops._os_family(None) == ""
    assert rpcops._os_family("SomeFutureOS 1.0") == ""


def test_fbreport_records_the_unlock_state_against_the_watch(monkeypatch, tmp_path):
    """The getvar dump is saved as a file, but one field in it is a durable
    per-watch CAPABILITY rather than a report: a locked bootloader refuses
    `fastboot boot`, so it decides whether this watch can ever be dumped by the
    clean debug-ramdisk method. Filing it away in a text file leaves that
    answer to be rediscovered by spending an hour and being refused."""
    noted = {}
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "find_serial_for_loc_port", lambda c, l, p: "S1")
    monkeypatch.setattr(rpcops, "find_codename_for_loc_port", lambda c, l, p: "nemo")
    monkeypatch.setattr(rpcops, "fastboot_getvar_all",
                        lambda s: "product: nemo\nunlocked: no\n")
    monkeypatch.setattr(rpcops, "DIAG_ROOT", tmp_path)
    monkeypatch.setattr(rpcops.registry, "note",
                        lambda serial, **kw: noted.update({serial: kw}))
    res = rpcops.DISPATCH._data["watch.fbreport"]({"loc": "1-3", "port": 1})
    assert res["ok"] is True
    assert noted["S1"]["bootloader_unlocked"] is False, \
        "the one durable capability in the dump was never recorded"


def test_op_args_take_the_body_but_never_let_it_override_the_url():
    """Ops that take a body were silently receiving their defaults: the route
    layer built args from URL params and static args only, and never read the
    request body. wanze runs recorded an empty note for months, and an
    operation lock came back labelled "operation" whatever the caller asked.
    The call succeeded every time, which is why it went unnoticed.

    Precedence is body < url < static, so a body cannot redirect a call at a
    different watch by overriding the serial in the path."""
    from asteroid_docking_bay.webapp import merge_op_args
    args = merge_op_args({"kind": "dump", "note": "run 1", "serial": "ATTACKER"},
                         {"serial": "REAL", "action": "hold"},
                         {"forced": 1})
    assert args["kind"] == "dump" and args["note"] == "run 1", \
        "the request body never reached the op"
    assert args["serial"] == "REAL", "a body overrode the serial in the URL"
    assert args["forced"] == 1

    # A static arg outranks a body trying to unset it.
    assert merge_op_args({"stale": False}, {}, {"stale": True})["stale"] is True
    # Absent or malformed body: still a usable arg dict, never a crash.
    assert merge_op_args(None, {"serial": "S"}, None) == {"serial": "S"}
    assert merge_op_args({}, {}, {}) == {}


# ── taking a dump: the two properties the feature exists for ─────────────────

def test_a_dump_holds_the_watch_before_it_starts_copying(monkeypatch):
    """THE 2026-08-03 FAILURE: a 3.9 GB read was starting over SSH when a-d-b's
    own stray peeler switched the watch to adb 45 seconds ahead of it, and the
    dump wrote 0 bytes. The lock must be taken BEFORE the copy begins, not
    after — a race the operator cannot see is the whole hazard."""
    from asteroid_docking_bay import stockrom, oplock
    order = []
    monkeypatch.setattr(stockrom, "disk_bytes", lambda w: (4096, None))
    monkeypatch.setattr(rpcops, "_watch",
                        lambda s: type("W", (), {"t": object()})())
    monkeypatch.setattr(rpcops, "load_config", lambda: {"serials": {"S1": "nemo"}})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda c, s: None)
    monkeypatch.setattr(oplock, "hold",
                        lambda *a, **k: order.append("hold") or {"ok": True})

    class _T:
        def __init__(self, *a, **k):
            pass

        def start(self):
            order.append("copy")

    monkeypatch.setattr(rpcops.threading, "Thread", _T)
    rpcops._dump_runs.clear()
    res = rpcops.DISPATCH._data["watch.dump"]({"serial": "S1", "action": "start"})
    assert res["ok"] is True
    assert order == ["hold", "copy"], f"lock not taken before the copy: {order}"


def test_a_dump_that_cannot_be_size_checked_is_refused(monkeypatch):
    """Without the watch's own disk size there is no way to tell a complete
    backup from a truncated one, and a truncated backup looks exactly like a
    file. Refuse rather than produce something unverifiable."""
    from asteroid_docking_bay import stockrom
    monkeypatch.setattr(stockrom, "disk_bytes",
                        lambda w: (None, stockrom.NO_ROOT_BLOCKER))
    monkeypatch.setattr(rpcops, "_watch",
                        lambda s: type("W", (), {"t": object()})())
    rpcops._dump_runs.clear()
    res = rpcops.DISPATCH._data["watch.dump"]({"serial": "S1", "action": "start"})
    assert res["ok"] is False
    # The reason reaches the operator verbatim, so a Wear OS watch says "needs
    # root" rather than blaming a connection that is working fine.
    assert res["error"] == stockrom.NO_ROOT_BLOCKER


def test_a_dump_targets_the_link_the_preflight_used(monkeypatch):
    """The size preflight reads over the watch's actual transport, but the copy
    command re-derived the address from cfg. An orbit/WiFi watch reaches us on
    an SshTransport with no ssh_ips allocation, so the re-derivation returned
    None and built an ADB command against a watch that is not on adb — a 0-byte
    dump after a preflight that passed. Build for the transport that answered."""
    from asteroid_docking_bay import stockrom, oplock
    from asteroid_docking_bay.transport import SshTransport
    seen = {}
    monkeypatch.setattr(stockrom, "disk_bytes", lambda w: (4096, None))
    monkeypatch.setattr(rpcops, "_watch",
                        lambda s: type("W", (), {"t": SshTransport("10.0.0.9")})())
    monkeypatch.setattr(rpcops, "load_config", lambda: {"serials": {"S1": "skipjack"}})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda c, s: None)   # no allocation
    monkeypatch.setattr(stockrom, "dump_command",
                        lambda serial, ip, dest: seen.setdefault("ip", ip) or "cmd")
    monkeypatch.setattr(oplock, "hold", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(rpcops.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    rpcops._dump_runs.clear()
    res = rpcops.DISPATCH._data["watch.dump"]({"serial": "S1", "action": "start"})
    assert res["ok"] is True
    assert seen["ip"] == "10.0.0.9", \
        f"dump built for the wrong link: ip={seen['ip']} (should be the SSH address)"


def test_a_truncated_dump_is_reported_as_failed_not_done(monkeypatch, tmp_path):
    """A short dump is the failure that hides best. original-sprat.img is
    0 bytes and sat in the backup directory looking present for months."""
    from asteroid_docking_bay import oplock
    dest = tmp_path / "short.img"
    dest.write_bytes(b"x" * 100)                       # expected 4096
    monkeypatch.setattr(oplock, "release", lambda s: None)

    import subprocess as real_sp
    monkeypatch.setattr(real_sp, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    rpcops._dump_runs["S1"] = {"state": "running"}
    rpcops._dump_worker("S1", dest, tmp_path / "m.txt", "true", 4096, "nemo")
    run = rpcops._dump_runs["S1"]
    assert run["state"] == "failed", "a truncated copy was called a backup"
    assert "truncated" in run["error"] and "100" in run["error"]

    # And the manifest says so too, since the file may outlive this process.
    assert "complete: False" in (tmp_path / "m.txt").read_text()

    # The file itself is renamed so a directory listing cannot mistake the
    # truncated image for a good one — the manifest can be lost, the name cannot.
    assert not dest.exists(), "the truncated .img was left looking complete"
    assert (tmp_path / "short.img.partial").exists()
    assert run["dest"].endswith(".partial")


def test_a_complete_dump_is_reported_done_and_releases_the_watch(monkeypatch, tmp_path):
    from asteroid_docking_bay import oplock
    released = []
    dest = tmp_path / "full.img"
    dest.write_bytes(b"x" * 4096)
    monkeypatch.setattr(oplock, "release", lambda s: released.append(s))
    import subprocess as real_sp
    monkeypatch.setattr(real_sp, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    rpcops._dump_runs["S1"] = {"state": "running"}
    rpcops._dump_worker("S1", dest, tmp_path / "m.txt", "true", 4096, "nemo")
    assert rpcops._dump_runs["S1"]["state"] == "done"
    assert released == ["S1"], "the watch stayed held after the dump finished"


def test_the_status_poll_never_waits_on_the_compile_scheduler(monkeypatch):
    """THE HANDOVER'S CENTRAL WARNING. The scheduler lives on ANOTHER machine
    and /api/status is polled continuously, so reading it inline would hand a
    dead e15 or a dropped LAN link the power to stall the whole watch fleet UI
    on a timer. The poll may only ever read the cache; refreshing happens on a
    background thread. Watches are the primary function.

    Driven through the REAL status op and timed, because the failure being
    prevented is the delay itself — asserting which helper gets called would
    pass just as happily with the blocking one wired in.
    """
    import time as _t
    import types
    from asteroid_docking_bay import icecc

    monkeypatch.setattr(icecc, "configured",
                        lambda *a, **k: {"scheduler": "10.255.255.1",
                                         "netname": "asteroid", "max_jobs": "8"})

    def _slow_query(*a, **k):
        _t.sleep(3)                      # a scheduler that is up but wedged
        return {"ok": False, "banner": "", "out": {}, "error": "timeout"}

    monkeypatch.setattr(icecc, "query", _slow_query)
    icecc._cache.update(ts=0.0, data=None)
    icecc._refreshing.clear()

    # Everything else in the status doc stubbed to nothing, so the only thing
    # this measures is the compile-cluster read.
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "_web_status_data", lambda cfg: [])
    monkeypatch.setattr(rpcops, "charge_config",
                        lambda c: types.SimpleNamespace(low_threshold=40,
                                                        high_threshold=80))
    monkeypatch.setattr(rpcops, "xhci_slots", lambda *a, **k: {})
    monkeypatch.setattr(rpcops, "_powered_port_count", lambda cfg: 0)
    monkeypatch.setattr(rpcops, "usb_mode_preference", lambda cfg: "adb")

    start = _t.monotonic()
    doc = rpcops.DISPATCH._data["status.get"]({})
    elapsed = _t.monotonic() - start
    assert "machineroom" in doc
    assert elapsed < 1.0, (
        f"/api/status waited {elapsed:.1f}s on another machine — a dead "
        f"scheduler would freeze the fleet UI on a timer")
