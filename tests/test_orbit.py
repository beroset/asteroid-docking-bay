# SPDX-License-Identifier: GPL-3.0-only
"""The Orbit port — onboarding a watch reachable over the air (WiFi-SSH).

The transport is injected so these run offline. What must hold: probe() keys the
member on ro.serialno (the fleet serial, so orbit == docked identity), refuses
anything that does not answer as a watch, and the launch/de-orbit ops round-trip
through config without duplicating a re-launched watch."""

import json
import asteroid_docking_bay.orbit as orbit
import asteroid_docking_bay.rpcops as rpcops
import asteroid_docking_bay.webstatus as ws
from asteroid_docking_bay.config import orbit_add, orbit_forget, orbit_members


class _FakeTransport:
    """Stand-in for SshTransport: canned shell output, no network."""

    def __init__(self, out, rc=0):
        self._out, self._rc = out, rc

    def shell(self, cmd, timeout=None):
        return self._rc, self._out, ""


def _patch_probe(monkeypatch, out, rc=0, geo=None):
    monkeypatch.setattr(orbit, "SshTransport",
                        lambda ip: _FakeTransport(out, rc))
    monkeypatch.setattr(orbit, "Watch",
                        lambda serial, transport=None: type(
                            "W", (), {"geometry": lambda self: geo})())


# ── probe ────────────────────────────────────────────────────────────────────

def test_probe_reads_serial_mac_and_geometry(monkeypatch):
    _patch_probe(monkeypatch, "720EX8C130737\n---\n30:95:e3:2d:64:71\n",
                 geo={"machine": "catfish", "resolution": "400x400"})
    m = orbit.probe("192.168.176.97")
    assert m["serial"] == "720EX8C130737"        # ro.serialno = the fleet serial
    assert m["wlanmac"] == "30:95:e3:2d:64:71"
    assert m["codename"] == "catfish" and m["resolution"] == "400x400"
    assert m["ip"] == "192.168.176.97" and isinstance(m["added"], int)


def test_probe_strips_ip_whitespace(monkeypatch):
    _patch_probe(monkeypatch, "S\n---\naa:bb\n", geo={"machine": "x"})
    assert orbit.probe("  10.0.0.5  ")["ip"] == "10.0.0.5"


def test_probe_none_on_empty_ip():
    assert orbit.probe("") is None and orbit.probe(None) is None
    assert orbit.probe("   ") is None


def test_probe_none_when_ssh_fails(monkeypatch):
    # rc != 0 → nothing answered / not reachable → no member, never a half one.
    _patch_probe(monkeypatch, "", rc=255)
    assert orbit.probe("192.168.176.97") is None


def test_probe_none_when_no_serial(monkeypatch):
    # Answered, but no ro.serialno (not an AsteroidOS watch) → refused.
    _patch_probe(monkeypatch, "\n---\n30:95:e3:2d:64:71\n", geo={"machine": "x"})
    assert orbit.probe("192.168.176.97") is None


def test_probe_tolerates_missing_mac(monkeypatch):
    # A watch whose wlan0 read came back empty still onboards; MAC is optional.
    _patch_probe(monkeypatch, "S123\n---\n\n", geo={"machine": "pike"})
    m = orbit.probe("10.0.0.9")
    assert m["serial"] == "S123" and m["wlanmac"] is None


def test_probe_survives_geometry_none(monkeypatch):
    # geometry() returning None must not crash the launch — codename just unknown.
    _patch_probe(monkeypatch, "S1\n---\naa:bb\n", geo=None)
    m = orbit.probe("10.0.0.1")
    assert m["serial"] == "S1" and m["codename"] is None


# ── reachability gate ────────────────────────────────────────────────────────

def test_reachable_true_when_port_open(monkeypatch):
    opened = {}

    class _Sock:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_connect(addr, timeout=None):
        opened["addr"], opened["timeout"] = addr, timeout
        return _Sock()

    monkeypatch.setattr(orbit.socket, "create_connection", fake_connect)
    assert orbit.reachable("10.0.0.5", timeout=2) is True
    assert opened["addr"] == ("10.0.0.5", 22) and opened["timeout"] == 2


def test_reachable_false_when_refused_or_timeout(monkeypatch):
    def boom(addr, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(orbit.socket, "create_connection", boom)
    assert orbit.reachable("10.0.0.5") is False


def test_reachable_false_on_empty_ip():
    assert orbit.reachable("") is False and orbit.reachable(None) is False


# ── per-watch transport routing (the Orbit integration seam) ─────────────────

def test_reachable_transport_routes_to_orbit_wifi(monkeypatch):
    # Not on adb, no rndis SSH IP, but an orbiting member with a live WiFi IP →
    # the op should reach it over WiFi. This is what makes CC/weather/etc. work.
    monkeypatch.setattr(rpcops, "adb_devices", lambda: {})
    monkeypatch.setattr(rpcops, "_adb_state", lambda devs, s: None)
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"orbit": {"S1": {"serial": "S1", "ip": "10.0.0.9"}}})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda cfg, s: None)
    monkeypatch.setattr(rpcops.orbit, "reachable", lambda ip, **k: True)
    monkeypatch.setattr(rpcops, "SshTransport", lambda ip: ("ssh", ip))
    assert rpcops._reachable_transport("S1") == ("ssh", "10.0.0.9")


def test_reachable_transport_none_when_orbit_unreachable(monkeypatch):
    # An orbiting member that is off WiFi → None (default AdbTransport / stale),
    # never a transport that would block on a dead address.
    monkeypatch.setattr(rpcops, "adb_devices", lambda: {})
    monkeypatch.setattr(rpcops, "_adb_state", lambda devs, s: None)
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"orbit": {"S1": {"serial": "S1", "ip": "10.0.0.9"}}})
    monkeypatch.setattr(rpcops, "ssh_ip_for_serial", lambda cfg, s: None)
    monkeypatch.setattr(rpcops.orbit, "reachable", lambda ip, **k: False)
    assert rpcops._reachable_transport("S1") is None


def test_reachable_transport_adb_wins_over_orbit(monkeypatch):
    # A watch that is BOTH docked and an orbit member must use adb (docked wins),
    # not its stale WiFi link.
    monkeypatch.setattr(rpcops, "adb_devices", lambda: {"S1": "device"})
    monkeypatch.setattr(rpcops, "_adb_state", lambda devs, s: "device")
    assert rpcops._reachable_transport("S1") is None


# ── config helpers ───────────────────────────────────────────────────────────

def test_orbit_add_get_and_forget_round_trip():
    cfg = {}
    orbit_add(cfg, {"serial": "S1", "ip": "10.0.0.1", "codename": "pike"})
    assert orbit_members(cfg) == {"S1": {"serial": "S1", "ip": "10.0.0.1",
                                         "codename": "pike"}}
    assert orbit_forget(cfg, "S1") is True
    assert orbit_members(cfg) == {}


def test_relaunch_same_serial_overwrites_not_duplicates():
    cfg = {}
    orbit_add(cfg, {"serial": "S1", "ip": "10.0.0.1"})
    orbit_add(cfg, {"serial": "S1", "ip": "10.0.0.9"})   # same watch, new IP
    assert list(orbit_members(cfg)) == ["S1"]
    assert orbit_members(cfg)["S1"]["ip"] == "10.0.0.9"


def test_forget_absent_or_no_serial_is_false_noop():
    cfg = {"orbit": {"S1": {"serial": "S1"}}}
    assert orbit_forget(cfg, "S9") is False              # not present
    assert orbit_forget(cfg, None) is False              # no serial given
    assert orbit_members(cfg) == {"S1": {"serial": "S1"}}  # untouched


def test_members_empty_when_never_used():
    assert orbit_members({}) == {}


# ── reachability cache ───────────────────────────────────────────────────────

def test_warmer_imports_orbit_members_directly():
    # The background warmer iterates orbit members; it must import orbit_members
    # at module load (a direct, loud import) rather than via a runtime attribute
    # lookup whose failure the warmer's broad except would swallow to debug.
    import asteroid_docking_bay.ops as ops
    assert ops.orbit_members is orbit_members


def test_reach_cache_round_trip():
    orbit.note_reachable("S1", True)
    orbit.note_reachable("S2", False)
    assert orbit.is_reachable_cached("S1") is True
    assert orbit.is_reachable_cached("S2") is False
    assert orbit.is_reachable_cached("never-probed") is False   # unknown → False


# ── handoff (rig ↔ orbit) ────────────────────────────────────────────────────

def test_handoff_only_when_undocked_and_reachable_in_orbit():
    orbit_map = {"S1": {"serial": "S1", "ip": "x"}}
    up = lambda s: True
    down = lambda s: False
    # Left the cradle, still reachable in orbit → hands off.
    assert ws._port_handed_off("S1", None, orbit_map, up) is True
    # Still on a wire here → never hands off, whatever orbit says.
    assert ws._port_handed_off("S1", "device", orbit_map, up) is False
    assert ws._port_handed_off("S1", "ssh", orbit_map, up) is False
    assert ws._port_handed_off("S1", "fastboot", orbit_map, up) is False
    # Undocked but NOT reachable in orbit → stays offline on its port, no handoff.
    assert ws._port_handed_off("S1", None, orbit_map, down) is False
    # Undocked, reachable, but never launched into orbit → no handoff.
    assert ws._port_handed_off("S1", None, {}, up) is False
    # No serial resolved → no handoff.
    assert ws._port_handed_off(None, None, orbit_map, up) is False


# ── status hub-view ──────────────────────────────────────────────────────────

def test_orbit_hub_view_builds_row_for_undocked_reachable(monkeypatch):
    monkeypatch.setattr(ws.orbit, "is_reachable_cached", lambda s: True)
    monkeypatch.setattr(ws.last_seen, "get",
                        lambda s: {"battery": 50, "last_live_ts": 1.0})
    cfg = {"orbit": {"S1": {"serial": "S1", "ip": "10.0.0.9",
                            "codename": "catfish", "resolution": "400x400"}}}
    v = ws._orbit_hub_view(cfg, set())
    assert v["location"] == "orbit" and v["virtual"] is True
    row = v["ports"][0]
    assert row["serial"] == "S1" and row["orbit"] is True
    assert row["adb"] == "ssh" and row["reachable"] is True     # reachable = live SSH
    assert row["battery_cached"] == 50 and row["ip"] == "10.0.0.9"
    assert row["machine"] == "catfish"


def test_orbit_hub_view_skips_docked_serial(monkeypatch):
    monkeypatch.setattr(ws.orbit, "is_reachable_cached", lambda s: True)
    monkeypatch.setattr(ws.last_seen, "get", lambda s: {})
    cfg = {"orbit": {"S1": {"serial": "S1", "ip": "x"}}}
    assert ws._orbit_hub_view(cfg, {"S1"})["ports"] == []       # docked → dock wins


def test_orbit_hub_view_unreachable_keeps_last_known(monkeypatch):
    monkeypatch.setattr(ws.orbit, "is_reachable_cached", lambda s: False)
    monkeypatch.setattr(ws.last_seen, "get",
                        lambda s: {"battery": 42, "last_live_ts": 9.0})
    cfg = {"orbit": {"S1": {"serial": "S1", "ip": "x", "codename": "pike"}}}
    row = ws._orbit_hub_view(cfg, set())["ports"][0]
    assert row["adb"] is None and row["reachable"] is False     # no live link
    assert row["battery_cached"] == 42                          # but last-known shown


def test_orbit_hub_view_renders_empty():
    """The Orbit hub is emitted even with NO members: its header carries the
    launch-by-IP input, so hiding the empty section hid the only way to
    (re)populate it — after the 0.9 rig-test data reset the whole feature
    looked deleted (found 2026-07-26). Planted-bug: restore the old
    `return None` on empty rows and this fails."""
    view = ws._orbit_hub_view({}, set())
    assert view["virtual"] is True and view["location"] == "orbit"
    assert view["ports"] == []


# ── ops ──────────────────────────────────────────────────────────────────────

def test_launch_op_probes_and_persists(monkeypatch):
    saved = {}
    monkeypatch.setattr(rpcops.orbit, "probe",
                        lambda ip: {"serial": "S1", "ip": ip, "codename": "pike"})
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "save_config", lambda cfg: saved.update(cfg))
    d = rpcops.DISPATCH._data["orbit.launch"]({"ip": "10.0.0.1"})
    assert d["ok"] is True and d["member"]["serial"] == "S1"
    assert saved["orbit"]["S1"]["ip"] == "10.0.0.1"


def test_launch_op_fails_cleanly_when_unreachable(monkeypatch):
    calls = {"saved": 0}
    monkeypatch.setattr(rpcops.orbit, "probe", lambda ip: None)
    monkeypatch.setattr(rpcops, "load_config", lambda: {})
    monkeypatch.setattr(rpcops, "save_config",
                        lambda cfg: calls.__setitem__("saved", calls["saved"] + 1))
    d = rpcops.DISPATCH._data["orbit.launch"]({"ip": "1.2.3.4"})
    assert d["ok"] is False and "reachable" in d["error"]
    assert calls["saved"] == 0                           # nothing written on failure


def test_deorbit_op_removes_and_saves(monkeypatch):
    saved = {}
    monkeypatch.setattr(rpcops, "load_config",
                        lambda: {"orbit": {"S1": {"serial": "S1"}}})
    monkeypatch.setattr(rpcops, "save_config", lambda cfg: saved.update(cfg))
    d = rpcops.DISPATCH._data["orbit.deorbit"]({"serial": "S1"})
    assert d["ok"] is True and saved["orbit"] == {}


def test_deorbit_op_noop_skips_write(monkeypatch):
    calls = {"saved": 0}
    monkeypatch.setattr(rpcops, "load_config", lambda: {"orbit": {}})
    monkeypatch.setattr(rpcops, "save_config",
                        lambda cfg: calls.__setitem__("saved", calls["saved"] + 1))
    d = rpcops.DISPATCH._data["orbit.deorbit"]({"serial": "S9"})
    assert d["ok"] is False and calls["saved"] == 0


# ── Direct USB: watches on a port no hub owns ────────────────────────────────

def test_direct_view_shows_a_watch_no_mapped_port_owns(monkeypatch):
    """The first thing a new user does: one watch, no smart hub, plugged
    straight into the machine. Rows are built by walking the CONFIGURED hubs,
    so without this view a-d-b talks to the watch over ADB while showing an
    empty table.

    Three cases that must sort correctly:
      - "1-1"     a root port. It has no dotted parent, so it can NEVER belong
                  to a hub -- the bare-laptop-socket case.
      - "1-6.2"   a hub that exists on the bus but nobody has mapped. Same
                  situation from a-d-b's side: connected, unowned.
      - "1-3.2"   a MAPPED hub port. Already has a real row with power and
                  charge cells; showing it twice would be a lie about where
                  the watch is.
    """
    cfg = {"hubs": [{"location": "1-3", "ports": {"2": "catfish"}}],
           "serials": {"SER-ROOT": "sturgeon"}}
    monkeypatch.setattr(ws, "_adb_state", lambda devices, serial: "device")
    view = ws._direct_hub_view(
        cfg,
        devices={"SER-ROOT": "device", "SER-UNMAPPED": "device", "SER-MAPPED": "device"},
        adb_paths={"SER-ROOT": "1-1", "SER-UNMAPPED": "1-6.2", "SER-MAPPED": "1-3.2"},
        fb_by_path={})
    assert view is not None
    serials = {r["serial"] for r in view["ports"]}
    assert serials == {"SER-ROOT", "SER-UNMAPPED"}, (
        "a watch on a mapped hub port was duplicated into the direct view, or "
        "a watch on an unmapped port was dropped from it")

    rows = {r["serial"]: r for r in view["ports"]}
    # A bare port cannot switch power. Saying so once, as a property of the
    # port, is what keeps the UI from drawing controls that cannot work.
    assert rows["SER-ROOT"]["smart"] is False
    assert rows["SER-ROOT"]["power"] is None
    # Identity comes from config when known and falls back to the serial, so an
    # unknown watch is still addressable rather than blank.
    assert rows["SER-ROOT"]["machine"] == "sturgeon" and rows["SER-ROOT"]["named"]
    assert rows["SER-UNMAPPED"]["machine"] is None
    assert rows["SER-UNMAPPED"]["codename"] == "SER-UNMAPPED"
    assert not rows["SER-UNMAPPED"]["named"]


def test_direct_view_reads_no_hardware_and_stays_quiet_when_empty(monkeypatch):
    """Two contracts.

    It must not touch the bus: this runs on every status refresh, and a watch
    that is slow to answer would stall the whole page. Identity comes from
    config and the last-seen cache only.

    And it returns None with nothing to show. Orbit is emitted empty because
    its header carries the launch-by-IP box; this section has no control of
    its own, so an always-present empty one is pure noise.
    """
    def boom(*a, **k):
        raise AssertionError("the direct view probed hardware during a refresh")
    for name in ("get_watch_codename", "adb_devices"):
        if hasattr(ws, name):
            monkeypatch.setattr(ws, name, boom)

    assert ws._direct_hub_view({"hubs": []}, {}, {}, {}) is None
    # every watch accounted for by a mapped hub -> still nothing to add
    cfg = {"hubs": [{"location": "1-3", "ports": {"2": "catfish"}}]}
    monkeypatch.setattr(ws, "_adb_state", lambda devices, serial: "device")
    assert ws._direct_hub_view(cfg, {"S": "device"}, {"S": "1-3.2"}, {}) is None


def test_direct_view_sees_a_watch_in_fastboot(monkeypatch):
    """A watch in its bootloader on a bare port is exactly when a user needs to
    see it -- that is the flashing case, which works without a hub. ADB cannot
    see it, so the fastboot path must feed the view too."""
    monkeypatch.setattr(ws, "_adb_state", lambda devices, serial: None)
    view = ws._direct_hub_view({"hubs": []}, devices={},
                               adb_paths={}, fb_by_path={"1-1": "FBSER"})
    assert view and len(view["ports"]) == 1
    assert view["ports"][0]["adb"] == "fastboot", (
        "a watch in fastboot on an unmapped port must not render as offline")


def test_identify_writes_a_fresh_config_and_keeps_the_rest(monkeypatch, tmp_path):
    """The onboarding write for a portless watch, and the guard against
    repeating 2026-08-16: a helper that persisted the cfg dict it was HANDED
    saved a caller's near-empty copy over a full config and erased 12 hubs.

    So this op must load the config fresh at write time and save that. The test
    plants a rich config on disk AFTER the op is entered, which a
    load-at-entry implementation would already have missed.
    """
    import asteroid_docking_bay.rpcops as ro
    import asteroid_docking_bay.config as cfgmod

    disk = {"hubs": [{"location": "1-3", "ports": {"1": "catfish"}}],
            "serials": {"OLD": "sol"}, "orbit": {"keep": "me"}}
    saved = {}
    monkeypatch.setattr(ro, "load_config", lambda: json.loads(json.dumps(disk)))
    monkeypatch.setattr(ro, "save_config", lambda c: saved.update(c))
    monkeypatch.setattr(ro, "get_watch_codename", lambda s: "sturgeon")
    monkeypatch.setattr(ro.registry, "note", lambda *a, **k: None)

    d = ro.DISPATCH._data["onboard.identify"]({"serial": "NEWSER"})
    assert d["ok"] and d["codename"] == "sturgeon"
    assert saved["serials"]["NEWSER"] == "sturgeon"
    assert saved["serials"]["OLD"] == "sol", "an existing serial was dropped"
    assert saved["hubs"] == disk["hubs"], (
        "the hub map did not survive naming one watch -- this is the config-wipe "
        "shape: writing a dict that was not loaded fresh at write time")
    assert saved["orbit"] == {"keep": "me"}


def test_identify_refuses_what_it_cannot_name(monkeypatch):
    """Two refusals, both of which would otherwise write junk into the config
    that every later sighting is matched against.

    A watch that does not answer must NOT be stored under a guessed name, and
    a serial that is not a serial (adb's own error strings arrive here) must
    never become a config key."""
    import asteroid_docking_bay.rpcops as ro
    wrote = []
    monkeypatch.setattr(ro, "save_config", lambda c: wrote.append(c))
    monkeypatch.setattr(ro, "get_watch_codename", lambda s: None)
    d = ro.DISPATCH._data["onboard.identify"]({"serial": "H1NZCJ010087020"})
    assert not d["ok"] and "did not answer" in d["error"]

    monkeypatch.setattr(ro, "get_watch_codename", lambda s: "sturgeon")
    bad = ro.DISPATCH._data["onboard.identify"]({"serial": "no permissions"})
    assert not bad["ok"]
    assert not wrote, "a refused identify still wrote to the config"
