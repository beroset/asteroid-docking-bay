# SPDX-License-Identifier: GPL-3.0-only
"""fake-power self-heal: only cycle a wedged, idle, opt-in port — and only
once per episode. Guards a hardware-actuating path, so it's worth pinning."""

import asteroid_docking_bay.webstatus as ws
from asteroid_docking_bay.lastseen import LastSeen


class _SyncThread:
    """Run the daemon cycle inline so the test can observe it."""
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _setup(monkeypatch, enabled=True):
    calls = []
    monkeypatch.setattr(ws, "uhubctl_cycle", lambda loc, port: calls.append((loc, port)))
    monkeypatch.setattr(ws.threading, "Thread", _SyncThread)
    ws._fake_power_since.clear()
    ws._fake_power_cycled.clear()
    ws._fake_power_cycles.clear()
    cfg = {"charge": {"fake_power_self_heal": enabled}}
    return calls, cfg


def test_heals_after_grace(monkeypatch):
    calls, cfg = _setup(monkeypatch)
    ws._fake_power_since["1-2:1"] = 0        # wedged since the epoch → past grace
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=False, cfg=cfg)
    assert calls == [("1-2", 1)]


def test_no_heal_within_grace(monkeypatch):
    calls, cfg = _setup(monkeypatch)
    # First sighting starts the clock; nothing fires yet.
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=False, cfg=cfg)
    assert calls == []


def test_no_heal_when_busy(monkeypatch):
    calls, cfg = _setup(monkeypatch)
    ws._fake_power_since["1-2:1"] = 0
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=True, cfg=cfg)
    assert calls == [] and "1-2:1" not in ws._fake_power_since


def test_no_heal_when_disabled(monkeypatch):
    calls, cfg = _setup(monkeypatch, enabled=False)
    ws._fake_power_since["1-2:1"] = 0
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=False, cfg=cfg)
    assert calls == []


def test_backoff_prevents_repeat(monkeypatch):
    import time
    calls, cfg = _setup(monkeypatch)
    ws._fake_power_since["1-2:1"] = 0
    ws._fake_power_cycled["1-2:1"] = time.time()   # just cycled
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=False, cfg=cfg)
    assert calls == []


def test_stops_cycling_a_persistent_wedge_it_cannot_fix(monkeypatch):
    """A sysfs register cycle can't restore a VBUS cut by the hub's physical
    per-port button; after a couple of futile cycles the self-heal must STOP
    actuating and name the physical cause, not keep implying an impossible
    recovery (audit A3)."""
    calls, cfg = _setup(monkeypatch)
    ws._fake_power_since["1-2:1"] = 0        # wedged past grace

    def heal():
        ws._fake_power_cycled["1-2:1"] = 0   # clear the backoff so it can act
        ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=True, busy=False, cfg=cfg)
    for _ in range(4):
        heal()
    assert len(calls) == ws._FAKE_POWER_MAX_CYCLES, \
        f"kept cycling a wedge a register cycle can't fix: {calls}"
    # a clean episode (un-wedge) resets the count so a genuine future wedge heals
    ws._maybe_self_heal_fake_power("1-2:1", "1-2", 1, wedged=False, busy=False, cfg=cfg)
    assert "1-2:1" not in ws._fake_power_cycles


# ── stale-value fallback (_battery_view) ─────────────────────────────────────

def test_battery_view_records_live_and_offers_no_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "last_seen", LastSeen(tmp_path / "ls.json"))
    # A live watch stores its reading and returns no stale fallback — the
    # caller's live `battery` must not be shadowed by a cached one.
    assert ws._battery_view("device", "S1", 55, False, "aos") == (None, None)
    assert ws.last_seen.get("S1")["battery"] == 55


def test_battery_view_falls_back_when_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "last_seen", LastSeen(tmp_path / "ls.json"))
    ws._battery_view("device", "S1", 55, False, "aos")     # seed while live
    battery_cached, last_live_ts = ws._battery_view(None, "S1", None, False, None)
    assert battery_cached == 55 and last_live_ts > 0


def test_battery_view_blank_without_a_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "last_seen", LastSeen(tmp_path / "ls.json"))
    assert ws._battery_view(None, "S1", None, False, None) == (None, None)


# ── geometry: probe once when live, then serve from cache ────────────────────

def test_geometry_view_probes_once_then_caches(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "last_seen", LastSeen(tmp_path / "ls.json"))
    calls = []

    class _W:
        def __init__(self, serial):
            pass
        def geometry(self):
            calls.append(1)
            return {"round": True, "resolution": "360x360"}

    monkeypatch.setattr(ws, "Watch", _W)
    g1 = ws._geometry_view("device", "S1")
    g2 = ws._geometry_view("device", "S1")     # cached → must not re-probe
    assert g1["round"] is True and g2 == g1 and len(calls) == 1


def test_geometry_view_offline_without_cache_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "last_seen", LastSeen(tmp_path / "ls.json"))
    assert ws._geometry_view(None, "S1") is None


def test_geometry_view_offline_reads_cache_without_probing(monkeypatch, tmp_path):
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(ws, "last_seen", ls)
    ls.record("S1", geometry={"round": True, "resolution": "400x400"})
    # If it tried to probe an offline watch this would blow up.
    monkeypatch.setattr(ws, "Watch",
                        lambda s: (_ for _ in ()).throw(AssertionError("probed!")))
    assert ws._geometry_view(None, "S1")["resolution"] == "400x400"


def test_geometry_cache_refreshes_when_the_probe_gained_a_field(monkeypatch, tmp_path):
    """A watch cached before a new probe field existed must re-probe, not serve
    the stale shape forever. This bit for real: the bootloader codename
    detector shipped and every already-cached watch kept reporting no
    bootloader, because the cache was 'probe once, keep forever'."""
    from asteroid_docking_bay import webstatus as ws
    from asteroid_docking_bay.lastseen import LastSeen
    from asteroid_docking_bay.watchctl import GEOMETRY_PROBE_VERSION

    ls = LastSeen(tmp_path / "ls.json")
    ls.record("S1", geometry={"round": True, "resolution": "454x454"})  # no probe_v
    monkeypatch.setattr(ws, "last_seen", ls)

    class _W:
        def __init__(self, serial): pass
        def geometry(self):
            # The probe reports what it read; the cache layer stamps probe_v.
            return {"round": True, "resolution": "454x454",
                    "bootloader": "rover-03.02.39.03.16"}
    monkeypatch.setattr(ws, "Watch", _W)

    got = ws._geometry_view("device", "S1")
    assert got.get("bootloader") == "rover-03.02.39.03.16", (
        f"stale cache served instead of re-probing: {got}")

    # A current cache must NOT re-probe (the probe costs three adb round trips).
    def _boom(serial):
        raise AssertionError("re-probed a cache that was already current")
    monkeypatch.setattr(ws, "Watch", _boom)
    assert ws._geometry_view("device", "S1")["bootloader"] == "rover-03.02.39.03.16"


def test_fb_draining_flags_a_watch_left_in_the_bootloader(monkeypatch, tmp_path):
    """A watch that vanished from an unpowered port while it was in fastboot is
    still running on battery: LK does not shut down when USB goes away
    (measured 2026-07-18 — sturgeon reappeared 4s after power returned, where a
    cold boot takes ~20s). With the port off there is nothing left to read, so
    the only way to warn is to remember the state it vanished in. This is the
    failure that deep-discharged sturgeon to 0%."""
    from asteroid_docking_bay import webstatus as ws
    from asteroid_docking_bay.lastseen import LastSeen

    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(ws, "last_seen", ls)

    ls.record("S1", last_conn_state="fastboot")
    assert (ls.get("S1") or {}).get("last_conn_state") == "fastboot"

    # A watch last seen booted must NOT raise the warning — only fastboot
    # keeps running through a VBUS cut in the way this flag describes.
    ls.record("S2", last_conn_state="device")
    assert (ls.get("S2") or {}).get("last_conn_state") == "device"


def test_last_conn_state_is_not_erased_by_an_offline_poll(tmp_path):
    """record() ignores None fields, so a poll that sees nothing must not wipe
    the remembered state — that is precisely when the warning is needed."""
    from asteroid_docking_bay.lastseen import LastSeen
    ls = LastSeen(tmp_path / "ls.json")
    ls.record("S1", last_conn_state="fastboot")
    ls.record("S1", last_conn_state=None, battery=None)
    assert (ls.get("S1") or {}).get("last_conn_state") == "fastboot", (
        "an offline poll erased the state the warning depends on")


def test_lifecycle_down_only_after_a_graceful_shutdown(monkeypatch):
    """"down" is the one power-state we assert: a confirmed graceful shutdown
    (safe_off_ts) with the watch not seen live since and its port off. A raw
    port cut never stamps safe_off_ts, so it stays unmarked — absence is "no
    claim", never a false "definitely off"."""
    from asteroid_docking_bay import webstatus as ws
    store = {}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))

    # Gracefully powered off: safe_off stamped at/after it was last seen live
    # (the real poweroff records both at the same "now").
    store["S1"] = {"last_live_ts": 1000.0, "safe_off_ts": 1000.0}
    assert ws._lifecycle("S1", present=False, power=False) == "down"
    # Its port is powered again (booting) -> not "down" anymore.
    assert ws._lifecycle("S1", present=False, power=True) is None
    # It is back on the bus -> not "down".
    assert ws._lifecycle("S1", present=True, power=True) is None
    # A watch cut raw (seen live, no safe_off) is NOT claimed down.
    store["S2"] = {"last_live_ts": 1000.0}
    assert ws._lifecycle("S2", present=False, power=False) is None


def test_lifecycle_booting_after_leaving_fastboot(monkeypatch):
    """A powered watch last seen in fastboot that has dropped off the bus is
    almost certainly booting (a flash / fastboot reboot) — not the bare no-link
    it showed before (mo). Bounded: past the fail cap we stop claiming, and an
    unpowered watch is drain-warning territory, not booting."""
    import time
    from asteroid_docking_bay import webstatus as ws
    store = {}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))
    now = time.time()
    store["S1"] = {"last_conn_state": "fastboot", "last_live_ts": now - 10}
    assert ws._lifecycle("S1", present=False, power=True) == "booting"
    assert ws._lifecycle("S1", present=True, power=True) is None      # booted, seen
    store["S1"]["last_live_ts"] = now - (ws.BOOT_FAIL_CAP + 10)       # gave up
    assert ws._lifecycle("S1", present=False, power=True) is None
    store["S2"] = {"last_conn_state": "fastboot", "last_live_ts": now - 10}
    assert ws._lifecycle("S2", present=False, power=False) is None    # unpowered


def test_lifecycle_self_clears_when_seen_live_again(monkeypatch):
    """After the watch is seen live again, last_live_ts advances past
    safe_off_ts and the claim drops with no explicit clear."""
    from asteroid_docking_bay import webstatus as ws
    store = {"S1": {"last_live_ts": 1000.0, "safe_off_ts": 1000.0}}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))
    assert ws._lifecycle("S1", False, False) == "down"
    store["S1"]["last_live_ts"] = 3000.0   # seen live again
    assert ws._lifecycle("S1", False, False) is None


def test_lifecycle_tracks_a_triggered_boot_through_its_window(monkeypatch):
    """A deliberate (re)boot stamps booting_since. With the port powered and no
    OS sighting yet, the connection column shows "booting" inside the definite-
    boot window and a hedged "bootfail" past it — up to the cap, after which no
    claim is made. A real live sighting (last_live_ts past the stamp) ends it."""
    from asteroid_docking_bay import webstatus as ws
    now = 10_000.0
    monkeypatch.setattr(ws.time, "time", lambda: now)
    # A cold boot: the watch was gracefully shelved (safe_off marker) before we
    # powered it on, so it really boots.
    store = {"S1": {"booting_since": now, "last_live_ts": 500.0, "safe_off_ts": 600.0}}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))

    # Just triggered, port powered, not up yet -> booting.
    assert ws._lifecycle("S1", present=False, power=True) == "booting"
    # Past the window but under the cap -> hedged failure question.
    now = 10_000.0 + ws.BOOT_WINDOW + 1
    assert ws._lifecycle("S1", present=False, power=True) == "bootfail"
    # Past the cap -> stop claiming; fall through to plain state (None here).
    now = 10_000.0 + ws.BOOT_FAIL_CAP + 1
    assert ws._lifecycle("S1", present=False, power=True) is None
    # A real OS sighting since the stamp -> boot succeeded, no claim.
    store["S1"]["last_live_ts"] = 10_000.0 + 5
    now = 10_000.0 + 10
    assert ws._lifecycle("S1", present=False, power=True) is None
    # Port off again on this shelved watch: nothing is booting — it reads "down".
    store["S1"]["last_live_ts"] = 500.0
    now = 10_000.0 + 5
    assert ws._lifecycle("S1", present=False, power=False) == "down"


def test_powering_a_running_watch_reads_reconnecting_not_booting(monkeypatch):
    """Toggling a running watch's port off then on does not reboot it — it keeps
    running on battery and only re-enumerates. With no graceful-shutdown marker
    (it was live, not shelved), the window reads "reconnecting", and past the
    window it makes no claim rather than a false "boot failed?"."""
    from asteroid_docking_bay import webstatus as ws
    now = 10_000.0
    monkeypatch.setattr(ws.time, "time", lambda: now)
    # Live seconds before the power-on, no safe_off marker -> a warm re-enumerate.
    store = {"S1": {"booting_since": now, "last_live_ts": now - 8}}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))

    assert ws._lifecycle("S1", present=False, power=True) == "reconnecting"
    now = 10_000.0 + ws.BOOT_WINDOW + 1
    assert ws._lifecycle("S1", present=False, power=True) is None, \
        "a warm reconnect must not escalate to boot failed"


def test_align_usb_mode_only_touches_a_stray_and_backs_off(monkeypatch):
    """A watch WITH an allocated SSH IP was switched deliberately — never
    disturbed. Only a stray (SSH mode, no allocation, hence on the shared
    default IP) is aligned, once, with the current preference, and a second poll
    inside the backoff does not re-fire the in-flight round-trip."""
    from asteroid_docking_bay import webstatus as ws
    spawned = []

    class _T:
        def __init__(self, target=None, args=(), daemon=None):
            spawned.append(args)

        def start(self):
            pass

    monkeypatch.setattr(ws.threading, "Thread", _T)
    ws._ssh_align_attempt.clear()
    cfg = {"ssh_ips": {"S1": "192.168.13.40"}, "usb_mode_preference": "adb"}

    ws._maybe_align_usb_mode("S1", "ssh", cfg)          # allocated -> deliberate
    assert spawned == [], "a watch with its own IP was disturbed"
    ws._maybe_align_usb_mode("S2", "device", cfg)       # not in SSH -> nothing
    assert spawned == []
    ws._maybe_align_usb_mode("S2", "ssh", cfg)          # stray -> align to pref
    assert spawned == [("S2", "adb")], spawned
    ws._maybe_align_usb_mode("S2", "ssh", cfg)          # backoff -> no re-fire
    assert spawned == [("S2", "adb")], "re-fired inside the backoff window"


def test_align_worker_switches_to_adb_or_relocates_by_preference(monkeypatch):
    """The worker reuses the two proven ops: it always gets the stray off the
    shared IP onto adb; under an SSH preference it then hands it a unique IP via
    the adb-side switch_ssh op. Under adb it stops after the switch."""
    from asteroid_docking_bay import webstatus as ws
    import asteroid_docking_bay.fastboot as fb
    import asteroid_docking_bay.rpcops as ro
    calls = {"to_adb": 0, "switch_ssh": None}
    monkeypatch.setattr(fb, "_switch_ssh_to_adb",
                        lambda ip="x": calls.__setitem__("to_adb", calls["to_adb"] + 1) or {"ok": True})
    monkeypatch.setattr(ws, "adb_devices", lambda: {"S1": {}})
    monkeypatch.setattr(ws.time, "sleep", lambda *a: None)
    monkeypatch.setitem(ro.DISPATCH._data, "watch.switch_ssh",
                        lambda args: calls.__setitem__("switch_ssh", args) or {"ok": True, "ip": "x"})

    ws._align_usb_mode_worker("S1", "adb")
    assert calls["to_adb"] == 1 and calls["switch_ssh"] is None, "adb-pref relocated"

    ws._align_usb_mode_worker("S1", "ssh")
    assert calls["switch_ssh"] == {"serial": "S1"}, "ssh-pref did not relocate"


def test_align_worker_gives_up_when_the_stray_is_unreachable(monkeypatch):
    """If the watch cannot be reached on the shared IP, the worker logs and
    stops — it never proceeds to a relocation it cannot complete."""
    from asteroid_docking_bay import webstatus as ws
    import asteroid_docking_bay.fastboot as fb
    import asteroid_docking_bay.rpcops as ro
    hit = {"switch_ssh": False}
    monkeypatch.setattr(fb, "_switch_ssh_to_adb", lambda ip="x": {"ok": False, "error": "unreachable"})
    monkeypatch.setitem(ro.DISPATCH._data, "watch.switch_ssh",
                        lambda args: hit.__setitem__("switch_ssh", True) or {"ok": True})
    ws._align_usb_mode_worker("S1", "ssh")
    assert hit["switch_ssh"] is False, "relocated despite never reaching the watch"


def test_wear_makes_a_departed_watch_worn_not_down(monkeypatch):
    """A wear-held watch that has left the bus is 'worn' (off-rig), overriding
    any 'down' — and while still docked it shows no pill (the button carries
    the armed state)."""
    from asteroid_docking_bay import webstatus as ws
    store = {"S1": {"wear": True, "safe_off_ts": 5000.0, "last_live_ts": 4000.0}}
    monkeypatch.setattr(ws.last_seen, "get", lambda s: store.get(s))
    assert ws._lifecycle("S1", present=False, power=True) == "worn"  # port held, gone
    assert ws._lifecycle("S1", present=True, power=True) is None     # docked, topping up


# ── battery over SSH (a watch on SSH must show a live reading) ────────────────

def test_battery_and_screen_reads_over_a_given_shell():
    """battery_and_screen runs its read through an injected shell, so the same
    read works over SSH — the fix for a watch on SSH freezing at its last ADB %."""
    from asteroid_docking_bay.adb import battery_and_screen
    calls = []

    def fake_shell(cmd):
        calls.append(cmd)
        return (0, "88\n---SCR---\ndisabled\n---CHG---\nCharging\n", "")

    bat, forced, chg = battery_and_screen("S1", shell=fake_shell)
    assert bat == 88 and forced is False and chg == "Charging"
    assert calls, "the injected shell was not used"


def test_battery_view_records_an_ssh_reading_as_live(monkeypatch, tmp_path):
    """An SSH reading is live, not stale — _battery_view records it (so the row
    shows it fresh) rather than dropping to the cached value."""
    from asteroid_docking_bay import webstatus as ws
    from asteroid_docking_bay.lastseen import LastSeen
    ls = LastSeen(tmp_path / "ls.json")
    monkeypatch.setattr(ws, "last_seen", ls)
    cached, lts = ws._battery_view("ssh", "S1", 88, False, None)
    assert cached is None and lts is None                 # the live contract
    assert ls.get("S1")["battery"] == 88, "SSH reading was not recorded"


def test_ssh_battery_reads_over_the_ssh_link(monkeypatch):
    from asteroid_docking_bay import webstatus as ws
    monkeypatch.setattr(ws, "ssh_ip_for_serial", lambda cfg, s: "192.168.13.40")
    monkeypatch.setattr(ws, "_detect_rndis", lambda ip: True)
    seen = {}

    class FakeTr:
        def __init__(self, ip):
            seen["ip"] = ip

        def shell(self, cmd, timeout=8, check=False):
            return (0, "77\n---SCR---\n\n---CHG---\nFull\n", "")

    monkeypatch.setattr(ws, "SshTransport", FakeTr)
    bat, forced, chg = ws._ssh_battery({}, "S1")
    assert bat == 77 and chg == "Full" and seen["ip"] == "192.168.13.40"


def test_ssh_battery_none_without_an_ip(monkeypatch):
    from asteroid_docking_bay import webstatus as ws
    monkeypatch.setattr(ws, "ssh_ip_for_serial", lambda cfg, s: None)
    assert ws._ssh_battery({}, "S1") == (None, False, None)


def test_enum_stuck_flags_a_powered_port_that_never_enumerates():
    """A mapped, powered port with no adb link and no device node is a docked
    watch that never came up (audit A1). The old inline guard required a
    `connect` bit that, on the sysfs rig, equalled node-existence — so
    `connect and not present` was always False and 'not enumerating' never
    fired; the row misreported 'no watch docked / dead cable'. It must now fire
    after the boot grace, independent of connect, and self-clear."""
    ws._enum_stuck_since.clear()
    t0 = 1000.0
    g = ws._ENUM_STUCK_GRACE_SEC
    # powered, no adb, no device node -> arms now, still within grace
    assert ws._enum_stuck("1-2:1", power=True, adb_state=None, present=False, now=t0) is False
    # past the grace -> fires (this is the case that never fired before)
    assert ws._enum_stuck("1-2:1", True, None, False, t0 + g + 1) is True
    # a device node appears -> clears
    assert ws._enum_stuck("1-2:1", True, None, present=True, now=t0 + g + 2) is False
    assert "1-2:1" not in ws._enum_stuck_since
    # an unpowered port never arms; an on-adb port is not stuck
    assert ws._enum_stuck("1-2:2", power=False, adb_state=None, present=False, now=t0 + 999) is False
    assert ws._enum_stuck("1-2:3", True, "device", False, t0) is False
