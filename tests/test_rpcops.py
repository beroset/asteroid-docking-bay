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
        "watch.cc", "watch.timeline", "watch.settings_read", "watch.settings_write",
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
    monkeypatch.setattr(ro, "_mark_booting", lambda s: None)
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

    # Not on adb, but reachable over SSH at its assigned IP → SshTransport there.
    monkeypatch.setattr(ro, "_adb_state", lambda devs, s: None)
    monkeypatch.setattr(ro, "load_config", lambda: {"ssh_ips": {"S1": "192.168.13.37"}})
    monkeypatch.setattr(ro, "_detect_rndis", lambda ip: ip == "192.168.13.37")
    t = ro._reachable_transport("S1")
    assert isinstance(t, SshTransport) and t.ip == "192.168.13.37", t

    # Neither adb nor reachable SSH → default (offline handled downstream).
    monkeypatch.setattr(ro, "_detect_rndis", lambda ip: False)
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
    monkeypatch.setattr(ro, "ssh_ip_for_serial", lambda c, s: "192.168.13.37")
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
