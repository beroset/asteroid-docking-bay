# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""The backend op table (see docs/CONTAINERS.md).

Every host-touching operation the web API offers is a named op here. This is
the single implementation of that logic: the monolithic web server dispatches
to it in-process via LocalCaller, and the split backend serves the same table
over RPC. Adding a capability means registering a named op in a reviewable
diff — there is deliberately no generic "run a command" op.

Op names mirror the former /api/* routes. Data handlers take one args dict
and return a JSON-able value (the app-level response, distinct from the RPC
envelope's ok/error). Streaming handlers (flash, onboard) yield raw message
strings — an empty string is a keep-alive heartbeat — which the frontend
turns into SSE frames.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import queue
import re
import shutil
import threading
from pathlib import Path
import time

from . import bench
from . import icecc, oplock, wanze
from . import aodcheck
from .util import _run, log
from .adb import _adb_state, adb_devices, get_watch_codename
from .config import (_config_lock, _store_smart_verdict, allocate_ssh_ip,
                     charge_config, ssh_ip_for_serial, usb_mode_preference,
                     find_codename_for_loc_port, find_serial_for_loc_port,
                     flash_config, load_config, save_config,
                     orbit_add, orbit_forget, orbit_members,
                     hands_cal_for, set_hands_cal, set_hub_name)
from .usb import (_sysfs_hub_scan, _sysfs_path_to_serial_map, adb_usb_paths,
                  _sysfs_serial_at, xhci_slots,
                  test_port_power_switching, uhubctl_cycle, uhubctl_set_power)
from . import drainlog, wifi
from .watchctl import BACKUP_ROOT, DIAG_ROOT, Watch, _watch_os
from .ops import ChargeOp, DrainOp, WorkbenchOp, _flash_one_watch
from .fastboot import (_switch_ssh_to_adb, _usb_moded_switch_failed,
                       _detect_rndis, _fastboot_list, bootloader_unlocked,
                       fastboot_getvar_all,
                       ssh_reach_ip)
from .transport import SshTransport, USB_SSH_IP
from .watchimg import watch_image_bytes
from .variants import image_of
from .weather import dconf_writeset, fetch_forecast, geocode, parse_watch_weather
from . import orbit
from . import bt
from .registry import registry
from .events import _DRAIN_FLOOR_PCT, _DRAIN_RESULTS_DIR, event_log
from .webstatus import _web_status_data
from .lastseen import last_seen
from .tasks import (_adb_lock, _charge_tasks, _flash_tasks, _onboard_lock,
                    _remap_tasks, active_op_on_slot, task_active)
from .rpc import Dispatcher
from . import __version__

DISPATCH = Dispatcher()


def _reachable_transport(serial: str):
    """How to reach a watch right now: adb when it is on adb, else SSH at its
    assigned address when it is in SSH/developer mode there. Returns None to
    mean "the default AdbTransport", which is also the right fallback for an
    offline watch — the op then returns empty/stale as before.

    This is what lets SSH be a full replacement for adb: the Control Center
    and the other per-watch ops read and toggle over whichever link is up,
    with no change at the call site beyond going through _watch()."""
    if _adb_state(adb_devices(), serial) == "device":
        return None
    cfg = load_config()
    ip = ssh_reach_ip(cfg, serial)
    if ip:
        return SshTransport(ip)
    # Off the dock but in orbit: reach it over WiFi at its stored address. This
    # is the whole point of the Orbit port — every per-watch op (CC, weather,
    # settings, screenshot) routes over WiFi with no change at the call site.
    member = orbit_members(cfg).get(serial)
    if member and member.get("ip") and orbit.reachable(member["ip"]):
        return SshTransport(member["ip"])
    return None


def _watch(serial: str) -> Watch:
    """A Watch bound to whichever transport currently reaches it."""
    return Watch(serial, transport=_reachable_transport(serial))


# ── status ──────────────────────────────────────────────────────────────────

@DISPATCH.op("status.get")
def _status_get(args):
    cfg = load_config()
    cc = charge_config(cfg)
    return {
        "hubs": _web_status_data(cfg),
        "thresholds": {"low": cc.low_threshold, "high": cc.high_threshold},
        "drain_floor": _DRAIN_FLOOR_PCT,
        "wearable_min_hours": cfg.get("wearable_min_hours", 24),
        "usb_mode_preference": usb_mode_preference(cfg),
        # The resource that actually limits this rig — see the xHCI audit. Sent
        # every refresh so the header can show it filling up BEFORE devices
        # start failing to configure.
        "slots": {**xhci_slots(cfg.get("xhci_max_slots")),
                  "powered_ports": _powered_port_count(cfg),
                  "max_powered_ports": cfg.get("max_powered_ports")},
        # The compile cluster this dock is part of, when it is part of one:
        # None on any host without icecream, so the Machine Room renders
        # nothing at all rather than an empty frame. Read from cache only —
        # the scheduler is on another machine and must never stall this poll.
        "machineroom": icecc.summary_cached(),
        # The version of the process running the ops — in split mode the
        # backend's, which is what an upgrade check cares about.
        "version": __version__,
    }


@DISPATCH.op("prefs.set_usb_mode")
def _prefs_set_usb_mode(args):
    """Set the fleet USB-mode preference (adb|ssh) — the situational top-bar
    toggle. It drives how a watch that self-enumerates in the wrong mode is
    auto-corrected; see webstatus._maybe_align_usb_mode."""
    mode = args.get("mode")
    if mode not in ("adb", "ssh"):
        return {"ok": False, "error": "mode must be 'adb' or 'ssh'"}
    with _config_lock:
        cfg = load_config()
        cfg["usb_mode_preference"] = mode
        save_config(cfg)
    return {"ok": True, "mode": mode}


# ── per-watch (Control Center) ──────────────────────────────────────────────

def _os_family(text: str) -> str:
    """Which OS a Control Center blob is describing, coarsely. Used only to
    notice that a cached blob belongs to a DIFFERENT system than the watch is
    running now, so the comparison wants to be blunt, not precise."""
    low = (text or "").lower()
    if "asteroid" in low:
        return "asteroidos"
    if "wear os" in low or "android wear" in low:
        return "android"
    return ""


def _stale_cc(serial, standby):
    """The last-known Control Center blob for a watch, marked stale, or {} if
    it was never seen. No device I/O — pure last_seen read, so it is instant.

    A cached blob is dropped outright when we already know the watch is running
    a DIFFERENT OS than the blob describes. That is not stale data, it is data
    about another system: after beluga 22979c8c was restored to Wear OS its
    panel went on reporting an AsteroidOS version, kernel and Qt build it no
    longer had. Dimming that and calling it old would still be a false claim
    about what the watch IS, which no age label can qualify."""
    cached = last_seen.get(serial)
    if not (cached and cached.get("cc")):
        return {}
    # The in-memory detection cache first, then the durable one. The in-memory
    # cache is evicted for every OFFLINE watch on each status pass, so for a
    # shelved watch — precisely when this cached panel is what gets served — it
    # is always empty, and the guard below could never fire. That left the very
    # case it was written for: beluga, restored to Wear OS and then shelved,
    # still reporting the AsteroidOS version, kernel and Qt build it no longer
    # had. Neither read touches the device.
    detected = _watch_os.get(serial) or (cached.get("os_detected") or None)
    if detected:
        was = _os_family(cached["cc"].get("os", ""))
        now = _os_family(detected) or ("android" if detected in
                                       ("WearOS", "AndroidWear") else "")
        if was and now and was != now:
            log.info("%s: dropping cached %s stats — the watch runs %s now",
                     serial, was, now)
            return {}
    blob = dict(cached["cc"])
    blob["stale"] = True
    blob["last_live_ts"] = cached.get("cc_ts")
    ip = ssh_ip_for_serial(load_config(), serial)
    if ip:
        blob["ssh_ip"] = ip
    geo = cached.get("geometry")
    if geo:
        blob["geometry"] = geo
        blob["resolution"] = geo.get("resolution")
    if standby is not None:
        blob["standby_measured"] = round(standby, 2)
    return blob


# A live CC/battery read (over adb OR ssh) should feed the battery-history chart
# too, not just the live gauge — otherwise watching a watch charge over SSH left
# the history flat (mo), since only charge/drain ops logged points. Throttled per
# serial so a brisk poll can't flood the log, and logged as 'live_reading' so the
# standby-rate math (check/drain readings only) stays uncontaminated.
_LIVE_READING_GAP = 120.0
_live_reading_ts: dict = {}


def _log_live_battery(serial, bat_cap):
    try:
        pct = int(bat_cap)
    except (TypeError, ValueError):
        return
    now = time.time()
    if now - _live_reading_ts.get(serial, 0) >= _LIVE_READING_GAP:
        event_log.log(serial, None, "live_reading", pct=pct)
        _live_reading_ts[serial] = now


@DISPATCH.op("watch.cc")
def _watch_cc(args):
    """Live Control Center stats, or the last-seen ones marked stale.

    A reachable watch answers fresh and its stats are cached with the moment
    they were captured. An unreachable one gets served the cached blob (if we
    ever saw it) stamped stale + last_live_ts, so the CC shows dimmed old
    values with an age rather than a bare 'no data'."""
    serial = args["serial"]
    # Passive standby drain measured across power-off→boot (event log), honest
    # because it carries no charge-bump. Always current, so fold into either path.
    standby = event_log.standby_off_to_on_rate(serial, None)
    # Fast path (stale=True): return the last-known values with NO device I/O,
    # so a panel can paint instantly on open — amber and marked stale — while
    # its live fetch (below, and slow over SSH) follows and replaces it.
    if args.get("stale"):
        return _stale_cc(serial, standby)
    tr = _reachable_transport(serial)
    # Tell the UI which link answered, so it can pace its live-poll to match:
    # adb is a warm channel (fast), SSH pays a fresh handshake per call (slow).
    tkind = "ssh" if isinstance(tr, SshTransport) else "adb"
    data = Watch(serial, transport=tr).cc_data()
    if data:
        last_seen.record(serial, cc=data, cc_ts=time.time())
        _log_live_battery(serial, data.get("bat_cap"))
        # Screen geometry/resolution is cached separately (probed by the status
        # path); fold it in so the CC shows the real resolution + can mask the
        # screen correctly.
        geo = (last_seen.get(serial) or {}).get("geometry")
        # Fold this sighting into the Fleet Registry: identity/versions get
        # change-logged, battery/ip kept as latest. Fills the registry from the
        # read we already did, no extra device traffic.
        registry.note(serial, source=tkind,
                      codename=(geo or {}).get("machine"),
                      resolution=(geo or {}).get("resolution"),
                      kernel=data.get("kernel"), qt=data.get("qt"),
                      release=data.get("release"), soc=data.get("soc"),
                      wlanmac=data.get("wlanmac"), btmac=data.get("btmac_self"),
                      battery=data.get("bat_cap"), ip=data.get("ip"))
        # The link that answered IS the watch's USB gadget mode (ssh/developer
        # vs adb), and its assigned SSH IP lives in config — surface both so the
        # Network tab shows the truth however it was opened, not the click's
        # stale context.
        extra = {"transport": tkind}
        ip = ssh_ip_for_serial(load_config(), serial)
        if ip:
            extra["ssh_ip"] = ip
        if geo:
            extra["geometry"] = geo
            extra["resolution"] = geo.get("resolution")
        if standby is not None:
            extra["standby_measured"] = round(standby, 2)
        return {**data, **extra}
    return _stale_cc(serial, standby)


@DISPATCH.op("watch.settings_read")
def _watch_settings_read(args):
    """The mirrored watch settings (appearance/display/nightstand) with their
    current dconf values. Read-only — the write op is deliberately separate."""
    data = _watch(args["serial"]).settings_read()
    if data is None:
        return {"ok": False, "error": "watch unreachable"}
    return {"ok": True, "settings": data["settings"],
            "quickpanel": data["quickpanel"], "mce": data.get("mce"),
            "locale": data.get("locale")}


@DISPATCH.op("watch.quickpanel_set")
def _watch_quickpanel_set(args):
    """Enable/disable one quick-panel toggle (the mirror writes the whole dconf
    dict). The watch layer refuses any id outside the catalog."""
    ok = _watch(args["serial"]).quickpanel_set(args["id"], bool(args["on"]))
    return {"ok": ok}


@DISPATCH.op("watch.settings_write")
def _watch_settings_write(args):
    """Write one togglable mirrored setting over dconf. The watch layer refuses
    any key not in the writable catalog, so an unknown or display-only key is a
    no-op, never a write — the catalog is the boundary."""
    ok = _watch(args["serial"]).settings_write(args["key"], bool(args["value"]))
    return {"ok": ok}


@DISPATCH.op("watch.toggle")
def _watch_toggle(args):
    tech = args["tech"]
    if tech not in ("wifi", "bluetooth"):
        return {"ok": False, "error": f"unknown toggle {tech}"}
    return {"ok": _watch(args["serial"]).toggle(tech, bool(args["on"]))}


@DISPATCH.op("watch.settime")
def _watch_settime(args):
    return {"ok": True, "timezone": _watch(args["serial"]).set_time_from_host()}


@DISPATCH.op("watch.hands")
def _watch_hands(args):
    """Physical hand position (HH:MM) for a hands watch (narwhal), or null on a
    watch without the movement — read on demand for the live-view composite. Also
    returns the stored motor-zero calibration so the control can map a drag angle
    to a motor value."""
    serial = args["serial"]
    return {"ok": True, "hands": _watch(serial).hands(),
            "cal": hands_cal_for(load_config(), serial)}


@DISPATCH.op("watch.hands_move")
def _watch_hands_move(args):
    """Drive a hands watch's motors to absolute positions (minute, hour), each
    0..179 (180 per turn). motor_move_all is absolute and re-syncs the counter —
    Free-mode drag and the choreography ride this."""
    try:
        m = int(args.get("m"))
        h = int(args.get("h"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "m and h must be integers"}
    if not (0 <= m < 180 and 0 <= h < 180):
        return {"ok": False, "error": "m and h must be 0..179"}
    return {"ok": _watch(args["serial"]).move_hands(m, h)}


@DISPATCH.op("watch.set_hands_cal")
def _watch_set_hands_cal(args):
    """Persist a hands watch's per-hand motor-zero offset (degrees), learned by
    Calibrate mode's overlap/oppose match."""
    serial = args["serial"]
    try:
        min_deg = float(args.get("min_deg"))
        hr_deg = float(args.get("hr_deg"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "min_deg and hr_deg must be numbers"}
    with _config_lock:
        cfg = load_config()
        set_hands_cal(cfg, serial, min_deg, hr_deg)
        save_config(cfg)
    return {"ok": True, "cal": {"min_deg": min_deg, "hr_deg": hr_deg}}


# ── display & sound (MCE brightness + PulseAudio volume/mute) ────────────────

@DISPATCH.op("watch.av_read")
def _watch_av_read(args):
    """Display brightness + sound volume/mute + hasSpeaker, for the Settings tab
    Display & Sound group. Volume/mute are read only on a speaker watch."""
    return {"ok": True, **_watch(args["serial"]).av_read()}


@DISPATCH.op("watch.set_brightness")
def _watch_set_brightness(args):
    """Set display brightness (clamped 1..100) via MCE."""
    try:
        pct = int(args.get("pct"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pct must be an integer"}
    pct = max(1, min(100, pct))
    return {"ok": _watch(args["serial"]).set_brightness(pct), "pct": pct}


@DISPATCH.op("watch.set_volume")
def _watch_set_volume(args):
    """Set master volume (clamped 0..100) via PulseAudio, then play the system
    notification sound at the new level so the user hears it (mo's bonus)."""
    try:
        pct = int(args.get("pct"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "pct must be an integer"}
    pct = max(0, min(100, pct))
    w = _watch(args["serial"])
    ok = w.set_volume(pct)
    if ok and pct > 0 and args.get("blip", True):
        w.play_notification()          # the test blip, at the level just set
    return {"ok": ok, "pct": pct}


@DISPATCH.op("watch.set_mute")
def _watch_set_mute(args):
    """Mute/unmute the master sink."""
    return {"ok": _watch(args["serial"]).set_mute(bool(args.get("on")))}


@DISPATCH.op("watch.record_audio")
def _watch_record_audio(args):
    """Record mic audio on the watch (UI-gated on HAS_MIC) and pull it; the WAV
    is served at /api/watch/<serial>/recording.wav for playback/download."""
    try:
        secs = max(1, min(30, int(args.get("seconds", 5))))
    except (TypeError, ValueError):
        secs = 5
    path = _watch(args["serial"]).record_audio(secs)
    if not path:
        return {"ok": False, "error": "recording failed — no mic or gst pipeline"}
    return {"ok": True, "seconds": secs, "bytes": path.stat().st_size}


@DISPATCH.op("watch.set_hands")
def _watch_set_hands(args):
    """Move a hands watch's physical hands to an explicit YYYY-MM-DD HH:MM:SS
    (narwhal). The format is validated before it reaches the shell."""
    when = args.get("when", "")
    if not _DATETIME_RE.match(when):
        return {"ok": False, "error": "bad datetime"}
    return {"ok": _watch(args["serial"]).set_hands(when)}


# ── weather (fetch host-side, sync to a watch) ───────────────────────────────

@DISPATCH.op("weather.set_location")
def _weather_set_location(args):
    """Resolve a city name to a location (Open-Meteo geocoding) and store it
    fleet-wide — the one location the whole fleet syncs from."""
    loc = geocode(args.get("city", ""))
    if not loc or loc.get("lat") is None:
        return {"ok": False, "error": "city not found"}
    with _config_lock:
        cfg = load_config()
        cfg["weather"] = loc
        save_config(cfg)
    return {"ok": True, "location": loc}


@DISPATCH.op("weather.get")
def _weather_get(args):
    """The current forecast for the stored location — a host-side fetch, no watch
    touched, so the Control Center can show weather even for an offline watch."""
    loc = load_config().get("weather") or {}
    if loc.get("lat") is None:
        return {"ok": True, "location": None, "days": []}
    return {"ok": True, "location": loc,
            "days": fetch_forecast(loc.get("lat"), loc.get("lon"))}


@DISPATCH.op("watch.weather_read")
def _watch_weather_read(args):
    """What weather is currently STORED on the watch (parsed from its dconf), so
    the UI can show on-watch vs the incoming forecast before a sync."""
    dump = _watch(args["serial"]).weather_read()
    return {"ok": dump is not None, "weather": parse_watch_weather(dump or "")}


@DISPATCH.op("watch.weather_sync")
def _watch_weather_sync(args):
    """Fetch the forecast for the stored location and write it to a watch's
    weather dconf, so its weather app / Today screen show it."""
    loc = load_config().get("weather") or {}
    if loc.get("lat") is None:
        return {"ok": False, "error": "no location set"}
    days = fetch_forecast(loc.get("lat"), loc.get("lon"))
    if not days:
        return {"ok": False, "error": "weather fetch failed"}
    ok = _watch(args["serial"]).weather_sync(dconf_writeset(loc.get("city"), days))
    return {"ok": ok, "city": loc.get("city"), "days": days}


@DISPATCH.op("bt.scan")
def _bt_scan(args):
    """Scan for Bluetooth devices and correlate to the fleet — by BT-MAC (the
    registry's stored btmac) or advertised name (watches broadcast their
    codename). Fleet watches sort first. Blocking: a manual action."""
    secs = max(1, min(30, int(args.get("seconds", 8))))
    found = bt.scan(secs)
    by_mac, codenames = {}, set()
    for rec in registry.all():
        f = rec.get("fields", {})
        if f.get("btmac"):
            by_mac[f["btmac"].upper()] = rec
        if f.get("codename"):
            codenames.add(f["codename"].lower())
    for hub in load_config().get("hubs", []):
        for cn in hub.get("ports", {}).values():
            codenames.add(cn.lower())
    for d in found:
        rec = by_mac.get(d["mac"].upper())
        if rec:
            d["codename"] = rec.get("fields", {}).get("codename")
            d["serial"] = rec.get("serial")
            d["in_fleet"] = True
        elif d["name"].lower() in codenames:
            d["codename"], d["serial"], d["in_fleet"] = d["name"], None, True
        else:
            d["codename"], d["serial"], d["in_fleet"] = None, None, False
    found.sort(key=lambda d: (not d["in_fleet"], (d["name"] or "").lower()))
    return {"ok": True, "devices": found}


@DISPATCH.op("bt.pair")
def _bt_pair(args):
    """Pair (bond) a discovered device by MAC — the user confirms on the watch."""
    mac = args.get("mac", "")
    if not mac:
        return {"ok": False, "error": "no mac"}
    return bt.pair(mac)


@DISPATCH.op("registry.get")
def _registry_get(args):
    """The Fleet Registry — every watch the rig has ever seen, newest sighting
    first, each with identity, first/last-seen, last source, and its change Log."""
    return {"ok": True, "watches": registry.all()}


# ── Orbit port (watches reachable over the air) ─────────────────────────────

@DISPATCH.op("orbit.launch")
def _orbit_launch(args):
    """Launch a watch into orbit by IP: SSH-probe it over WiFi, read its serial +
    codename + geometry, and record it as an orbiting fleet member. Idempotent —
    re-launching the same watch just refreshes its stored IP."""
    member = orbit.probe(args.get("ip", ""))
    if not member:
        return {"ok": False, "error": "no watch reachable at that address"}
    with _config_lock:
        cfg = load_config()
        orbit_add(cfg, member)
        save_config(cfg)
    registry.note(member["serial"], source="orbit",
                  codename=member.get("codename"),
                  resolution=member.get("resolution"),
                  wlanmac=member.get("wlanmac"), ip=member.get("ip"))
    return {"ok": True, "member": member}


@DISPATCH.op("orbit.deorbit")
def _orbit_deorbit(args):
    """De-orbit a watch: drop it from the Orbit port. The watch itself is
    untouched — this only forgets how to reach it over the air."""
    with _config_lock:
        cfg = load_config()
        removed = orbit_forget(cfg, args.get("serial"))
        if removed:
            save_config(cfg)
    return {"ok": removed}


_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@DISPATCH.op("watch.set_datetime")
def _watch_set_datetime(args):
    """Set the watch clock to an explicit 'YYYY-MM-DD HH:MM:SS'. The format is
    validated here so only a well-formed moment ever reaches the shell."""
    when = args.get("when", "")
    if not _DATETIME_RE.match(when):
        return {"ok": False, "error": "bad datetime"}
    return {"ok": _watch(args["serial"]).set_datetime(when)}


@DISPATCH.op("watch.notify")
def _watch_notify(args):
    return {"ok": _watch(args["serial"]).notify()}


@DISPATCH.op("watch.buzz")
def _watch_buzz(args):
    return {"ok": _watch(args["serial"]).buzz()}


@DISPATCH.op("watch.screen")
def _watch_screen(args):
    return {"ok": _watch(args["serial"]).screen(bool(args["on"]))}


@DISPATCH.op("wear.set")
def _wear_set(args):
    """Arm or release the wear marker on a watch's port.

    On: power the port up to top the watch off, and flag it wear-held so the
    port is not auto-cycled and is kept lit even after the watch leaves — the
    LED then marks exactly where to re-dock. A wear event breaks the standby
    chain, because the coming off→bus interval is the watch being *worn*, not
    resting on the shelf. Manual release only: off clears the flag and frees
    the port so another watch can use it."""
    loc, port = args["loc"], args["port"]
    on = bool(args.get("on"))
    serial = find_serial_for_loc_port(load_config(), loc, port)
    if not serial:
        return {"ok": False, "error": "no watch mapped to this port"}
    # Both branches actuate the port — on powers it up, off can cut VBUS on a
    # watch that has left — so this is the same hazard port.set is guarded
    # against, and it was the odd one out.
    busy = _refuse_if_busy(loc, port)
    if busy:
        return busy
    if on:
        try:
            uhubctl_set_power(loc, port, True)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        last_seen.mark(serial, wear=True)
        cn = find_codename_for_loc_port(load_config(), loc, port)
        event_log.log(serial, cn, "wear")
    else:
        last_seen.mark(serial, wear=False)
        # Free the port only if the watch is actually gone (the normal worn
        # case). If it re-docked and is present, leave it powered — a raw cut
        # would strand a running watch on battery, the ambiguous-off hazard.
        present = (_adb_state(adb_devices(), serial) == "device"
                   or serial in _fastboot_list())
        if not present:
            try:
                uhubctl_set_power(loc, port, False)
            except RuntimeError:
                pass
    return {"ok": True, "wear": on}


@DISPATCH.op("screen.release_all")
def _screen_release_all(args):
    """Release every on-adb watch's forced-on screen (mcetool -D off) — the
    panic button for a demo mode left draining a watch. Harmless on watches
    that were not forced (a no-op release)."""
    released = []
    for serial, entry in adb_devices().items():
        status = entry.get("status") if isinstance(entry, dict) else entry
        if status == "device" and Watch(serial).screen(False):
            released.append(serial)
    return {"ok": True, "released": released}


@DISPATCH.op("wifi.aps")
def _wifi_aps(args):
    """The WiFi networks the rig can lend out — every credential across all
    watch backups, deduplicated by SSID. Cheap enough to send with the Control
    Center, so the button can name the network it will join."""
    return {"ok": True, "aps": wifi.find_aps(BACKUP_ROOT)}


@DISPATCH.op("wifi.provision")
def _wifi_provision(args):
    """Lend a saved WiFi credential to this watch.

    The credential comes from whichever watch first joined that network; the
    service is re-keyed to THIS watch's MAC on the way in, because connman
    identifies a saved network by the interface that saved it.
    """
    serial, ssid = args["serial"], args.get("ssid")
    aps = wifi.find_aps(BACKUP_ROOT)
    if not aps:
        return {"ok": False,
                "error": "no WiFi credentials on the rig yet — back up a watch "
                         "that is already on the network first"}
    ap = next((a for a in aps if a["ssid"] == ssid), None) if ssid else aps[0]
    if not ap:
        return {"ok": False, "error": f"no saved credential for {ssid!r}"}
    return Watch(serial).provision_wifi(ap)


@DISPATCH.op("watch.drainlog")
def _watch_drainlog(args):
    """Start / stop / fetch the on-watch battery-current sampler.

    The point is that it keeps sampling while the watch is OFF CHARGE and
    unreachable: start it, cut the port, let the watch sit, restore the port,
    fetch. Reading current over USB measures the charger, not the battery.
    """
    serial, action = args["serial"], args.get("action")
    w = _watch(serial)
    if action == "start":
        err = drainlog.start(w, int(args.get("interval") or drainlog.DEFAULT_INTERVAL))
        return {"ok": not err, **({"error": err} if err else {})}
    if action == "stop":
        drainlog.stop(w)
        return {"ok": True}
    if action == "fetch":
        return drainlog.fetch(w)
    return {"ok": False, "error": f"unknown action: {action}"}


@DISPATCH.op("watch.session_restart")
def _watch_session_restart(args):
    """Restart the ceres user session (`systemctl restart user@1000`).

    The blunt instrument, and sometimes the only one that works: the launcher
    reads an app's declared colours when it builds its grid, and QML modules
    are loaded once per session, so a freshly installed or changed app can be
    invisible to a running session however correct the files on disk are.

    Heavier than restarting the launcher alone — it takes the whole session,
    so anything running in it dies. Run as root over the transport rather than
    inside the session being killed, or the command dies with its own target.
    """
    w = _watch(args["serial"])
    rc, out, err = w.t.shell("systemctl restart user@1000", timeout=45)
    if rc != 0:
        return {"ok": False, "error": (err or out).strip()[:160] or f"rc={rc}"}
    return {"ok": True}


@DISPATCH.op("watch.backup")
def _watch_backup(args):
    """Back up one port's watch data to the host. Slot-based like flash: the
    serial is resolved from the port so the row menu needn't carry it."""
    serial = find_serial_for_loc_port(load_config(), args["loc"], int(args["port"]))
    if not serial:
        return {"ok": False, "error": "no watch mapped to this port"}
    return Watch(serial).backup()


@DISPATCH.op("watch.restore")
def _watch_restore(args):
    serial = find_serial_for_loc_port(load_config(), args["loc"], int(args["port"]))
    if not serial:
        return {"ok": False, "error": "no watch mapped to this port"}
    return Watch(serial).restore()


@DISPATCH.op("watch.image")
def _watch_image(args):
    """The watch's product photo (cached from asteroidos.org) as base64 PNG.
    ok=False means no image for this codename."""
    codename = args.get("codename")
    data = watch_image_bytes(codename)
    if not data:
        # Exact-codename detection can name a variant that has no photo of its
        # own — rover is physically a rubyfish and asteroidos.org carries one
        # image for the pair. Fall back to the image the family ships, so
        # naming a watch more precisely never costs it its picture.
        base = image_of(codename)
        if base:
            data = watch_image_bytes(base)
    if not data:
        return {"ok": False}
    return {"ok": True, "png_b64": base64.b64encode(data).decode()}


@DISPATCH.op("ssh.switch_adb")
def _ssh_switch_adb(args):
    """Switch a watch in SSH/developer USB mode back to ADB. Reaches it at its
    assigned SSH address (per-watch, so each has a unique one), falling back to
    the default 192.168.2.15 for a watch that predates IP assignment. ok=False
    means nothing was reachable there, or a broken usb-moded refused it."""
    serial = args.get("serial")
    cfg = load_config()
    # Switching USB mode under a running transfer IS the 2026-08-03 regression
    # that produced a 0-byte dump, and the oplock docstring names a shell script
    # calling exactly this op as the threat model. The automatic peeler and
    # aligner have been guarded since; the manual/scripted entry point had not.
    lock = oplock.held(cfg, serial)
    if lock:
        return {"ok": False, "busy": lock.get("kind"),
                "error": f"this watch is held: {oplock.describe(lock)} — "
                         f"switching its USB mode would break that operation"}
    # Prefer the address the watch demonstrably answers on (allocated, or the
    # shared default when its link wins the route); fall back to the assigned
    # address so an unreachable watch still yields the helper's clean error.
    ip = (ssh_reach_ip(cfg, serial)
          or (ssh_ip_for_serial(cfg, serial) if serial else None)
          or USB_SSH_IP)
    return _switch_ssh_to_adb(ip)


@DISPATCH.op("watch.switch_ssh")
def _watch_switch_ssh(args):
    """The reverse of ssh.switch_adb: put an adb watch into SSH/developer USB
    mode. usb_moded re-enumerates the gadget as rndis reachable at
    192.168.2.15, which drops the adb connection — so a non-zero return from
    the command is expected; success is the command being delivered before the
    link goes. Per serial, because only one watch can hold the fixed
    192.168.2.15, so exactly one may be in SSH mode at a time."""
    serial = args.get("serial")
    if not serial:
        return {"ok": False, "error": "no serial for this port"}
    # Same hazard as ssh.switch_adb, from the other direction. This also closes
    # the aligner's own gap: finish_ssh_relocation calls this op up to a minute
    # after the pass that checked the lock, so a hold taken in between was
    # invisible until the switch had already fired.
    lock = oplock.held(load_config(), serial)
    if lock:
        return {"ok": False, "busy": lock.get("kind"),
                "error": f"this watch is held: {oplock.describe(lock)} — "
                         f"switching its USB mode would break that operation"}
    # Give this watch its own SSH-mode IP before switching, so two watches sent
    # to SSH on the same rig never both land on the default 192.168.2.15. The
    # assignment is sticky and persisted, so the watch keeps this address.
    with _config_lock:
        cfg = load_config()
        ip = allocate_ssh_ip(cfg, serial)
        save_config(cfg)
    _run(f"adb -s {serial} shell usb_moded_util -n set:ip,{ip}",
         check=False, timeout=10)
    _, out, err = _run(f"adb -s {serial} shell usb_moded_util -s developer_mode",
                       check=False, timeout=15)
    if _usb_moded_switch_failed(out, err):
        return {"ok": False,
                "error": "usb-moded did not switch mode — its service may be "
                         "down on this watch (a known device-specific issue)"}
    return {"ok": True, "ip": ip}


@DISPATCH.op("watch.diagnostics")
def _watch_diagnostics(args):
    serial = find_serial_for_loc_port(load_config(), args["loc"], int(args["port"]))
    if not serial:
        return {"ok": False, "error": "no watch mapped to this port"}
    res = Watch(serial).collect_diagnostics()
    # Expose the bundle's basename so the browser can pull it down (it lives on
    # the host by default, out of reach of a remote operator).
    if res.get("path"):
        res["name"] = res["path"].rsplit("/", 1)[-1]
    return res


@DISPATCH.op("watch.fbreport")
def _watch_fbreport(args):
    """Save `fastboot getvar all` as a downloadable text report — the
    bootloader's ground truth (identity, boardid, BT/WLAN MACs, bootloader
    version, unlock/secure state, live battery-voltage + battery-soc-ok,
    partition table). Works on a watch too flat to boot, so it's the one
    report you can still take from a bricked or bootlooping unit."""
    loc, port = args["loc"], int(args["port"])
    cfg = load_config()
    serial = find_serial_for_loc_port(cfg, loc, port)
    if not serial:
        return {"ok": False, "error": "no watch mapped to this port"}
    text = fastboot_getvar_all(serial)
    if not text or ":" not in text:
        return {"ok": False,
                "error": "no fastboot device — put the watch in bootloader first"}
    # The one capability in this dump worth keeping rather than filing: a
    # locked bootloader refuses `fastboot boot`, so it decides whether this
    # watch can ever be dumped by the clean debug-ramdisk method. Recording it
    # per serial turns an hour of setup ending in a refusal into a question
    # answered up front.
    registry.note(serial, source="fastboot",
                  bootloader_unlocked=bootloader_unlocked(text))
    codename = find_codename_for_loc_port(cfg, loc, port) or serial
    DIAG_ROOT.mkdir(parents=True, exist_ok=True)
    name = f"{codename}-{time.strftime('%Y%m%d-%H%M%S')}-fastboot.txt"
    (DIAG_ROOT / name).write_text(text + "\n")
    return {"ok": True, "name": name, "lines": len(text.splitlines())}


@DISPATCH.op("watch.screenshot")
def _watch_screenshot(args):
    """JPEG as base64 in the response — keeps the protocol single-channel
    (a screenshot is ~60 KB, the overhead is irrelevant).

    A fresh capture needs the watch on the bus; when it fails, fall back to
    the last pulled screenshot (if any) marked stale, so the overlay shows
    the last screen instead of an empty box."""
    w = Watch(args["serial"])
    local = w.screenshot()
    stale = False
    if not local:
        last = w.last_screenshot_path()
        if last.exists() and last.stat().st_size > 0:
            local, stale = last, True
    if not local:
        return {"ok": False, "error": "screenshot failed"}
    return {"ok": True, "stale": stale, "captured_ts": local.stat().st_mtime,
            "jpeg_b64": base64.b64encode(local.read_bytes()).decode()}


# ── port power ──────────────────────────────────────────────────────────────

def _op_owning(loc, port) -> "str | None":
    """The kind of operation currently owning this port, or None.

    A running charge/drain/workbench test owns its port's power state, and
    changing it underneath silently corrupts the measurement. The UI already
    disables these controls on a busy row, but the UI is not a safety
    boundary: any direct API caller — a script, a curl, a compromised
    frontend (see docs/CONTAINERS.md) — bypasses it entirely.

    This is not hypothetical. On 2026-07-18 a direct `POST /api/on` to test an
    unrelated feature re-powered a port mid-drain-test, recharged the watch
    from 96% back to 100%, and destroyed five hours of readings. The row was
    correctly greyed out in the browser at the time."""
    return active_op_on_slot(f"{loc}:{port}")


def _refuse_if_busy(loc, port) -> "dict | None":
    cfg = load_config()
    lock = oplock.held(cfg, find_serial_for_loc_port(cfg, loc, port))
    if lock:
        return {"ok": False, "busy": lock.get("kind"),
                "error": f"this watch is held: {oplock.describe(lock)} — "
                         f"release it first, or wait for it to expire"}
    kind = _op_owning(loc, port)
    if kind is None:
        return None
    return {"ok": False, "busy": kind,
            "error": f"a {kind} operation owns this port — stop it first, "
                     f"otherwise its readings are silently corrupted"}


def _powered_port_count(cfg: dict) -> int:
    """How many SWITCHABLE ports currently read as powered.

    Only PPPS hubs count. A non-PPPS hub's ports are permanently live and
    cannot be switched off, so counting them would charge the budget for
    something no policy can ever give back — on this rig the Sabrent alone
    would sit above any sane cap before a single switchable port came on.
    Same source the UI shows, so the governor and the display cannot disagree.
    """
    switchable = {hub["location"] for hub in cfg.get("hubs", [])
                  if hub.get("ppps")}
    n = 0
    for hub in _sysfs_hub_scan(cfg):
        if hub["location"] not in switchable:
            continue
        for on in (hub.get("power") or {}).values():
            if on is True:
                n += 1
    return n


def _refuse_if_bus_full(cfg: dict) -> "dict | None":
    """Guard a power-on against the two ways this rig runs out of room.

    The xHCI slot pool is the hard one: every device on the bus takes a slot,
    hubs included, and past the limit the controller enumerates a device and
    then refuses to configure it. Powering another port on at that moment
    cannot work — it produces a watch that looks present and broken. Refusing
    with the reason beats reproducing the mystery.

    max_powered_ports is the soft one: a user policy, off by default, for
    keeping the powered set near the handful actually being worked on.
    """
    slots = xhci_slots(cfg.get("xhci_max_slots"))
    if slots["used"] >= slots["max"]:
        return {"ok": False,
                "error": (f"USB controller out of device slots "
                          f"({slots['used']}/{slots['max']}). Another device "
                          f"cannot be configured until one is freed — power a "
                          f"port off first.")}
    cap = cfg.get("max_powered_ports")
    if cap:
        powered = _powered_port_count(cfg)
        if powered >= int(cap):
            return {"ok": False,
                    "error": (f"max_powered_ports reached ({powered}/{cap}). "
                              f"Power a port off, or raise the limit in "
                              f"config.")}
    return None


@DISPATCH.op("port.set")
def _port_set(args):
    busy = _refuse_if_busy(args["loc"], args["port"])
    if busy:
        return busy
    if args["on"]:
        full = _refuse_if_bus_full(load_config())
        if full:
            return full
    try:
        confirmed = uhubctl_set_power(args["loc"], args["port"], bool(args["on"]))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if confirmed:
        serial = find_serial_for_loc_port(load_config(), args["loc"], args["port"])
        if args["on"]:
            # Powering a docked watch's port on boots it; a watch already up just
            # re-asserts and the marker self-clears on its next live sighting.
            _mark_booting(serial)
        elif serial:
            # A raw power cut via the toggle is NOT a graceful shutdown, so it
            # must not read as "shelved": clear any (possibly stale) safe_off
            # marker so the watch reads ambiguous, not down. Only port.poweroff
            # (which delivers a real shutdown) sets that marker.
            last_seen.mark(serial, safe_off_ts=0)
    return {"ok": True, "confirmed": confirmed}


@DISPATCH.op("port.cycle")
def _port_cycle(args):
    loc, port = args["loc"], args["port"]
    busy = _refuse_if_busy(loc, port)
    if busy:
        return busy
    serial = find_serial_for_loc_port(load_config(), loc, port)
    # A power-cycle IS a PPPS test — it cuts VBUS and restores it while checking
    # whether the device dropped — so use it to (re)assess and record the port's
    # smart verdict. This is the way to resolve a '?' without a full re-onboard,
    # matching the common workflow of just powering a port up rather than
    # onboarding it. test_port_power_switching restores the port's prior state.
    try:
        smart, reason = test_port_power_switching(loc, port, serial)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    if smart is not None:
        with _config_lock:
            cfg = load_config()
            for hub in cfg.get("hubs", []):
                if hub["location"] == loc:
                    _store_smart_verdict(hub, port, smart)
                    save_config(cfg)
                    break
    if serial:
        # A cycle cuts and restores VBUS: the watch re-enumerates on the bus.
        # Stamp the boot marker so the wait is shown, and clear any safe_off
        # marker so it reads as "reconnecting" (a re-power), not "booting up" —
        # that state is reserved for powering on a genuinely shelved watch.
        _mark_booting(serial)
        last_seen.mark(serial, safe_off_ts=0)
    return {"ok": True, "smart": smart, "reason": reason}


def _fastboot_serial_for_port(loc, port):
    """The fastboot serial of the device physically at this port, or None.

    A watch's fastboot (bootloader) serial differs from its adb serial, so a
    port can't be matched to a fastboot device by its mapped serial — resolve
    by USB path (loc.port), the one identity that survives the adb↔fastboot
    boundary. The connection-column pill already resolves this way; the power
    actions must too, or they route to a dead adb link when the serials differ
    (beluga), or the port's mapping is stale (a swapped, un-onboarded watch).
    (webstatus builds the same path→serial map inline for the pill.)"""
    path = f"{loc}.{port}"
    return next((s for s, p in _fastboot_list().items() if p == path), None)


@DISPATCH.op("port.poweroff")
def _port_poweroff(args):
    """Graceful shutdown then cut VBUS immediately — the shutdown command is
    synchronous, so it is delivered before power is cut and the watch
    finishes halting on battery. Any delay here races the halt: cutting while
    the watch is still up lets a watch without offmode charging bounce back
    on.

    From the bootloader the equivalent is `fastboot oem poweroff`. LK cannot
    complete a shutdown while USB is attached and instead gives ~5s to
    disconnect — which is normally a cable yank, but here the rig cuts VBUS
    programmatically well inside that window. Same order, same guarantee."""
    loc, port = args["loc"], args["port"]
    busy = _refuse_if_busy(loc, port)
    if busy:
        return busy
    serial = find_serial_for_loc_port(load_config(), loc, port)
    fb_serial = _fastboot_serial_for_port(loc, port)
    graceful = False   # was a graceful shutdown actually delivered?
    if fb_serial:
        # In the bootloader: oem-poweroff its OWN fastboot serial. That serial
        # differs from the mapped adb serial (and the port's mapping may be
        # stale after a swap), so keying off the mapped serial sent the halt to
        # a dead adb link and stranded the watch running in fastboot.
        rc, _, err = _run(f"fastboot -s {fb_serial} oem poweroff",
                          check=False, timeout=10)
        graceful = (rc == 0)
        if not graceful:
            # `oem poweroff` is NOT universal — rover's bootloader has no
            # such command. Cutting VBUS after a failed shutdown would
            # strand the watch running on battery in the bootloader,
            # invisible to the host. Leaving it powered is the safe failure.
            log.warning("poweroff %s:%s (fastboot %s): shutdown failed: %s",
                        loc, port, fb_serial, err.strip() or f"rc={rc}")
            return {"ok": False, "adb_shutdown": False,
                    "error": "this bootloader has no 'oem poweroff' — "
                             "power left on so the watch is not stranded "
                             "running on battery"}
    elif serial:
        ip = ssh_reach_ip(load_config(), serial)
        if _adb_state(adb_devices(), serial) == "device":
            rc, _, err = _run(f"adb -s {serial} shell poweroff",
                              check=False, timeout=10)
            graceful = (rc == 0)
            if not graceful:
                log.warning("poweroff %s:%s (%s): adb shutdown failed: %s",
                            loc, port, serial, err.strip() or f"rc={rc}")
        elif ip:
            # SSH/developer mode: ssh_reach_ip already proved this address
            # answers (allocated, or the shared default when this watch's link
            # wins the route). The halt drops the ssh session, so a non-zero
            # return is expected — delivery to a reachable watch is the
            # success signal, as for the mode switch.
            SshTransport(ip).shell("poweroff", timeout=12)
            graceful = True
        else:
            # Known serial but reachable on no transport (already off, or a
            # wedged/booting watch). Fall through to the raw VBUS cut, as
            # before — no graceful marker, so no "down" claim.
            log.warning("poweroff %s:%s (%s): not on adb/ssh/fastboot — "
                        "cutting power only", loc, port, serial)
    else:
        log.warning("poweroff %s:%s: no serial known — cutting power only",
                    loc, port)
    try:
        confirmed = uhubctl_set_power(loc, port, False)
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "adb_shutdown": graceful}
    # Mark a *confirmed graceful* shutdown — over adb, ssh, or fastboot — as the
    # one off-state we can vouch for: the watch was told to halt and it went, so
    # it is safely down and not draining. A raw port toggle never reaches here,
    # so its ambiguous off-state stays unmarked; the status build turns this
    # into the "down" pill.
    if serial and graceful:
        last_seen.mark(serial, safe_off_ts=time.time())
    return {"ok": True, "adb_shutdown": graceful, "confirmed": confirmed}


@DISPATCH.op("port.declare_shelved")
def _port_declare_shelved(args):
    """Record, on mo's word, that this watch is safely powered down.

    Pure bookkeeping — it actuates nothing. It exists because a-d-b can only
    vouch for an off-state it delivered itself (port.poweroff stamps the same
    marker after a confirmed halt). A watch powered off from the fastboot
    menu, by a held button, or by a pulled cradle is equally off, but the host
    saw none of it, so the row sits on a hedge — "draining in fastboot?" or a
    bare dash — that no amount of polling can resolve. This is the one input
    that can: the operator looking at the watch.

    Deliberately NOT gated by _refuse_if_busy and taking no oplock: nothing is
    actuated, and while an op owns the slot the UI does not offer this at all
    (see webstatus._declarable_off).

    Self-correcting in both directions, so no undo is needed: a wrong claim
    dies the moment the watch is next seen live, because last_live_ts advances
    past the marker and every reader compares the two; and powering the port
    on clears the marker outright and stamps the boot instead.
    """
    loc, port = args["loc"], args["port"]
    serial = find_serial_for_loc_port(load_config(), loc, port)
    if not serial:
        return {"ok": False, "error": "no watch is mapped to this port, so "
                                      "there is no identity to record it against"}
    # safe_off_ts only — bumping last_live_ts here would defeat the very
    # comparison that makes the marker mean anything (see lastseen.py).
    last_seen.mark(serial, safe_off_ts=time.time(), safe_off_declared=True)
    return {"ok": True, "serial": serial}


def _mark_booting(serial, commanded=False):
    """Stamp when we deliberately (re)boot a known watch, so the connection
    column can show "booting up" through the ~40s window and a hedged "boot
    failed?" past it. Only a real OS sighting (last_live_ts) clears it — see
    webstatus._boot_state. A None serial (empty/unmapped port) stamps nothing.

    `commanded` separates the two things that land here. Restoring VBUS to a
    port MIGHT be a boot — if the watch was shelved — or might be a mere
    re-enumeration of a watch that kept running on battery, and only the
    safe_off marker can tell those apart. A `reboot` we sent over adb is
    ALWAYS a real boot, whatever the power history says, so it declares itself
    rather than being inferred.
    """
    if serial:
        last_seen.mark(serial, booting_since=time.time(),
                       booting_commanded=bool(commanded))


_SSH_UNREACHABLE = ("connection refused", "no route to host",
                    "connection timed out", "connection closed by remote host",
                    "could not resolve", "permission denied",
                    "host key verification failed")


def _ssh_delivered(rc: int, err: str) -> bool:
    """Whether an SSH command reached the watch.

    A reboot or poweroff kills the link it arrived on, so ssh almost always
    exits non-zero on success — rc alone cannot tell "the watch obeyed" from
    "we never got there". What CAN be told apart is a failure to connect, and
    that is the only case worth reporting as a failure: anything else means the
    command was handed over before the link went.
    """
    if rc == 0:
        return True
    low = (err or "").lower()
    return not any(marker in low for marker in _SSH_UNREACHABLE)


def _watch_action(loc, port, adb_cmd, fb_cmd, fail_msg, boots_os=False,
                  ssh_cmd=None):
    """Run a power action against whichever protocol the watch is speaking.

    A docked watch is reachable over adb when it is booted and over fastboot
    when it is in the bootloader — the same intent ("reboot", "go to the
    bootloader") just needs a different command. Dispatching here keeps one
    op per concept instead of a parallel fastboot family, and lets the UI
    offer the same menu in both states. Either command may be None where the
    action has no equivalent in that protocol. boots_os marks the actions that
    send the watch off to boot the OS (reboot, continue) so the UI can track
    the boot — not the ones that land in another mode (bootloader, recovery)."""
    busy = _refuse_if_busy(loc, port)
    if busy:
        return busy
    serial = find_serial_for_loc_port(load_config(), loc, port)
    # Fresh evidence beats a cached guess. The fastboot list comes from a
    # background warmer and can be up to a warmer cycle out of date, while
    # `adb devices` is read live here — and a watch cannot be adb-online and in
    # the bootloader at once. Checking adb first means a watch that just LEFT
    # fastboot is commanded over adb immediately, instead of being sent to a
    # fastboot serial that is no longer there and hanging until the 20s timeout
    # turns a working button into a dead one. A watch genuinely in fastboot has
    # no adb, so this never steals the fastboot route from it.
    adb_live = bool(serial) and _adb_state(adb_devices(), serial) == "device"
    # Fastboot presence is resolved by PORT PATH, not by the mapped serial: a
    # watch's fastboot serial differs from its adb serial, so `serial in
    # _fastboot_list()` misses it and the action wrongly routes to a dead adb
    # link (the bootloader has no adb). Command the fastboot device bound to
    # THIS port by its own fastboot serial.
    fb_serial = None if adb_live else _fastboot_serial_for_port(loc, port)
    if fb_serial is not None:
        if fb_cmd is None:
            return {"ok": False, "error": "action not available over fastboot"}
        rc, _, err = _run(f"fastboot -s {fb_serial} {fb_cmd}",
                          check=False, timeout=20)
        if rc != 0:
            return {"ok": False, "error": err or fail_msg}
        if boots_os:
            _mark_booting(serial or fb_serial, commanded=True)
        return {"ok": True, "via": "fastboot"}

    if not serial:
        return {"ok": False, "error": "no serial found for port"}

    # Only reach for adb when adb can actually answer. Firing the command at a
    # watch that is in SSH mode used to leave the caller waiting on a state
    # change that was never coming — the button appeared to do nothing at all.
    if adb_live:
        if adb_cmd is None:
            return {"ok": False, "error": "action not available over adb"}
        rc, _, err = _run(f"adb -s {serial} {adb_cmd}", check=False, timeout=20)
        if rc != 0:
            return {"ok": False, "error": err or fail_msg}
        if boots_os:
            _mark_booting(serial, commanded=True)
        return {"ok": True, "via": "adb"}

    transport = _reachable_transport(serial)
    if transport is not None and ssh_cmd:
        rc, _, err = transport.shell(ssh_cmd, timeout=20)
        if not _ssh_delivered(rc, err):
            return {"ok": False, "error": err or fail_msg}
        if boots_os:
            _mark_booting(serial, commanded=True)
        return {"ok": True, "via": transport.kind}

    # Nothing could carry the command. Say so, and say what would fix it —
    # reporting success here is worse than any error, because the caller then
    # waits on a watch that was never told to do anything.
    if transport is not None:
        return {"ok": False,
                "error": "this watch is only reachable over SSH, and this "
                         "action has no SSH equivalent — switch it to ADB "
                         "mode first (Network Center → USB mode)"}
    # Name the adb state when there IS one. "not on adb" is false for a watch
    # sitting in `unauthorized` or `recovery` — it is on adb, just not in a
    # state that accepts this command — and sending an operator to look for a
    # connection problem that does not exist costs more time than the failure
    # itself. unauthorized in particular is fixed ON THE WATCH, by tapping the
    # RSA prompt, which the old message gave no hint of.
    state = _adb_state(adb_devices(), serial) if serial else None
    if state == "unauthorized":
        return {"ok": False,
                "error": "this watch is on adb but UNAUTHORIZED — confirm the "
                         "debugging prompt on the watch screen, then retry"}
    if state:
        return {"ok": False,
                "error": f"this watch is on adb in '{state}' state, which does "
                         f"not accept this command"}
    return {"ok": False,
            "error": "no way to reach this watch right now: not in fastboot, "
                     "not on adb, and no usable SSH address"}


@DISPATCH.op("port.reboot")
def _port_reboot(args):
    return _watch_action(args["loc"], args["port"], "reboot", "reboot",
                         "reboot failed", boots_os=True, ssh_cmd="reboot")


@DISPATCH.op("port.bootloader")
def _port_bootloader(args):
    # From adb this enters the bootloader; from fastboot it cycles it, which
    # is also how a fastboot battery reading gets re-sampled (the bootloader
    # snapshots it on entry and never refreshes within a session).
    # No ssh_cmd on purpose: rebooting into the bootloader is an Android
    # concept with no portable SSH equivalent, and inventing one would be a
    # claim about watch-side behaviour we have not tested. Without adb the
    # caller now gets an actionable refusal instead of silence.
    return _watch_action(args["loc"], args["port"], "reboot bootloader",
                         "reboot bootloader", "reboot to bootloader failed")


@DISPATCH.op("port.recovery")
def _port_recovery(args):
    return _watch_action(args["loc"], args["port"], "reboot recovery",
                         "reboot recovery", "reboot to recovery failed")


@DISPATCH.op("port.continue")
def _port_continue(args):
    """Resume the boot chain from the bootloader. Fastboot-only — a booted
    watch has nothing to continue."""
    return _watch_action(args["loc"], args["port"], None, "continue",
                         "fastboot continue failed", boots_os=True)


# ── config visibility ───────────────────────────────────────────────────────

@DISPATCH.op("port.hide")
def _port_hide(args):
    """Toggle a port's user avoid/hidden flag, stored in the hub's exclude
    map alongside auto-detected excludes."""
    loc, port = args["loc"], args["port"]
    with _config_lock:
        cfg = load_config()
        hub = next((hub for hub in cfg.get("hubs", []) if hub["location"] == loc), None)
        if hub is None:
            return {"ok": False, "error": "hub not found"}
        excl = hub.setdefault("exclude", {})
        port_str = str(port)
        if port_str in excl:
            del excl[port_str]
            state = False
        else:
            excl[port_str] = "hidden by user"
            state = True
        save_config(cfg)
    return {"ok": True, "hidden": state}


@DISPATCH.op("hub.hide")
def _hub_hide(args):
    """Toggle the hidden flag for a whole physical BOX, not one chip.

    A box registers as several cascaded hubs — the Sabrent is five entries
    (1-6 plus 1-6.1..1-6.4), the A16 is five, the dock two — and they all carry
    the same auto-assigned name. Hiding one chip at a time meant "hide the
    Sabrent" took five clicks, the first of which appeared to do nothing at all
    because the box root carries no ports: only its own header row vanished.

    So one click hides the box: the root address and every hub cascaded under
    it. The whole box takes the state of the hub that was clicked, so a
    half-hidden box resolves to fully shown or fully hidden rather than
    toggling each chip out of step.
    """
    loc = args["loc"]
    root = loc.split(".")[0]
    with _config_lock:
        cfg = load_config()
        box = [h for h in cfg.get("hubs", [])
               if h["location"] == root or h["location"].startswith(root + ".")]
        target = next((h for h in box if h["location"] == loc), None)
        if target is None:
            return {"ok": False, "error": "hub not found"}
        state = not target.get("hidden", False)
        for h in box:
            h["hidden"] = state
        save_config(cfg)
    return {"ok": True, "hidden": state, "hubs": len(box)}


@DISPATCH.op("socket.set")
def _socket_set(args):
    """Set (or clear, with a blank value) a port's physical socket number, stored
    in the hub's `sockets` map. Ports sort by socket, so this records the rig's
    physical socket order as you map it."""
    loc, port = args["loc"], int(args["port"])
    raw = str(args.get("n", "")).strip()
    with _config_lock:
        cfg = load_config()
        hub = next((h for h in cfg.get("hubs", []) if h["location"] == loc), None)
        if hub is None:
            return {"ok": False, "error": "hub not found"}
        socks = hub.setdefault("sockets", {})
        if raw == "":
            socks.pop(str(port), None)
            val = None
        else:
            try:
                val = int(raw)
            except ValueError:
                return {"ok": False, "error": "socket must be a number"}
            socks[str(port)] = val
        save_config(cfg)
    return {"ok": True, "socket": val}


@DISPATCH.op("hub.rename")
def _hub_rename(args):
    """Set (or clear, with an empty name) the friendly name for a physical hub,
    keyed by its address prefix so it covers every chip and port beneath it."""
    prefix = args["prefix"]
    name = args.get("name", "")
    with _config_lock:
        cfg = load_config()
        set_hub_name(cfg, prefix, name)
        save_config(cfg)
    return {"ok": True, "name": name.strip() or None}


# ── operations (charge / drain / workbench) ─────────────────────────────────

@DISPATCH.op("charge.start")
def _charge_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if ChargeOp.is_active(slot):
        return {"ok": False, "error": "charge already running",
                "charge_end_ts": _charge_tasks[slot].get("charge_end_ts", 0)}
    cfg = load_config()
    err = ChargeOp.start(loc, port, cfg)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True,
            "duration_seconds": charge_config(cfg).charge_duration_minutes * 60}


def _register_lifecycle(op_cls, name, stop_error):
    """start/stop ops share one shape per Operation subclass; charge.start
    stays hand-written above for its already-running special case."""
    if name != "charge":
        @DISPATCH.op(f"{name}.start")
        def _start(args):
            err = op_cls.start(args["loc"], args["port"], load_config(),
                               owner=args.get("owner"),
                               opts={"no_poll": True} if args.get("no_poll") else None)
            return {"ok": False, "error": err} if err else {"ok": True}

    @DISPATCH.op(f"{name}.stop")
    def _stop(args):
        if op_cls.stop(args["loc"], args["port"]):
            return {"ok": True}
        return {"ok": False, "error": stop_error}


_register_lifecycle(ChargeOp, "charge", "no charge running")
_register_lifecycle(WorkbenchOp, "workbench", "no workbench active")
_register_lifecycle(DrainOp, "drain", "no drain test running")


# ── benchymark: the FPS benchmark app (docs/FPS_BENCH.md) ───────────────────
# a-d-b installs and drives the app; the app measures and writes its own
# results. No watchface is pushed and no dconf key is touched, so there is
# nothing to save and restore here.


@DISPATCH.op("bench.app")
def _bench_app(argsd):
    """Install / start / stop / remove the benchymark app, and read back the
    last run it wrote. One op with an action rather than five ops: they share
    the watch lookup and the same failure shape, and the op table is the
    security boundary — fewer entries is fewer things to audit."""
    serial, action = argsd["serial"], argsd.get("action")
    w = _watch(serial)
    if action == "install":
        # With no explicit path, take the newest ipk the build produced: the
        # UI button carries no arguments, and hunting for the file by hand is
        # exactly the step that goes stale between rebuilds.
        ipk = argsd.get("ipk") or bench.newest_ipk()
        if not ipk or not Path(ipk).is_file():
            return {"ok": False,
                    "error": f"no benchymark ipk found (looked in {bench.IPK_DIR})"}
        err = bench.app_install(w, ipk)
        return {"ok": not err, **({"error": err} if err else {"installed": Path(ipk).name})}
    if action == "start":
        err = bench.app_start(w)
        return {"ok": not err, **({"error": err} if err else {})}
    if action == "stop":
        err = bench.app_stop(w)
        return {"ok": not err, **({"error": err} if err else {})}
    if action == "remove":
        err = bench.app_remove(w)
        return {"ok": not err, **({"error": err} if err else {})}
    if action == "results":
        d = bench.app_results(w)
        if not d:
            return {"ok": False, "error": "no completed run on this watch yet"}
        if bench.all_zero(d):
            # Refuse to hand back a run that measured a dark screen. Silently
            # returning it is how a campaign ends up averaging nothing.
            return {"ok": False,
                    "error": ("every phase read 0 fps with samples collected — "
                              "the panel was blanked for this run, so it "
                              "measured nothing. Wake the screen and re-run."),
                    **d}
        return {"ok": True, **d}
    return {"ok": False, "error": f"unknown action: {action}"}


# The 'before' capture per serial, for aod.check — see the op.
_aod_before: dict = {}


# ── wanze: the probe that records while the watch is away ───────────────────
# Unlike benchymark, nothing here drives a measurement — wanze is already
# running on its own timer. These actions only place it, remove it, and read
# back what it collected.


@DISPATCH.op("aod.check")
def _aod_check(argsd):
    """Capture a watch's AoD state, and diff it against an earlier capture.

    `capture` is side-effect free ON PURPOSE: the evidence for a boot is
    destroyed the moment anything opens the settings app, so this has to be
    safe to run first, before the toggle is even looked at.

    Captures are kept in memory per serial — one "before" per watch is all the
    procedure needs, and persisting them would invite comparing captures from
    different boots, which answers nothing."""
    serial, action = argsd["serial"], argsd.get("action") or "capture"
    w = _watch(serial)
    if action == "capture":
        res = aodcheck.capture(w)
        if res.get("ok"):
            _aod_before[serial] = res
        return res
    if action == "diff":
        before = _aod_before.get(serial)
        if not before:
            return {"ok": False,
                    "error": "no earlier capture for this watch — run "
                             "'capture' BEFORE the action you want to test"}
        after = aodcheck.capture(w)
        if not after.get("ok"):
            return after
        return {"ok": True, "before_uptime": before.get("uptime"),
                "after_uptime": after.get("uptime"),
                **aodcheck.diff(before, after)}
    return {"ok": False, "error": f"unknown action: {action}"}


# Dump runs are tracked here rather than in tasks.py: they are per-SERIAL (a
# watch can be dumped over WiFi with no port at all), while every task there is
# keyed by a hub port.
_dump_runs: dict = {}
# Serializes the check-and-claim in watch.dump. The web server is threaded and
# the dest filename is only second-granular, so two starts inside one second
# could both pass the "already running" check and be handed the SAME path.
_dump_claim = threading.Lock()
# Spare room demanded beyond the image itself, so a dump cannot fill the disk
# the fleet's backups, registry and logs also live on.
_DUMP_HEADROOM_BYTES = 2 * 1024 ** 3


def _dump_worker(serial, dest, manifest, cmd, expect_bytes, codename):
    import subprocess as sp
    from . import stockrom
    run = _dump_runs[serial]
    try:
        rc = sp.run(["bash", "-c", cmd], stdin=sp.DEVNULL,
                    capture_output=True, timeout=4 * 3600).returncode
        size = dest.stat().st_size if dest.exists() else 0
        # A short dump is the failure that hides best: it looks like a file.
        # Compare against what the WATCH said its disk was, asked before the
        # copy started, and refuse to call a truncated result a backup.
        complete = bool(expect_bytes) and size == expect_bytes
        if not complete and dest.exists():
            # Mark it in the NAME, not only in the sidecar manifest: a manifest
            # can be moved or lost, and a truncated .img that looks complete in
            # a directory listing is exactly the original-sprat.img failure.
            partial = dest.with_name(dest.name + ".partial")
            dest.rename(partial)
            dest = partial
            run["dest"] = str(dest)
        run.update(state="done" if complete else "failed", size=size,
                   expect=expect_bytes, rc=rc,
                   error=None if complete else
                   (f"truncated: {size} of {expect_bytes} bytes — the link "
                    f"dropped mid-copy" if expect_bytes else
                    "could not read the watch's disk size to verify this dump"))
        stockrom.write_manifest(
            manifest, codename=codename, serial=serial,
            taken=time.strftime("%Y-%m-%d %H:%M:%S"),
            method="runtime (watch booted; userdata is NOT trustworthy)",
            disk_bytes=expect_bytes, file_bytes=size,
            complete=complete,
            note=("Verify by taking a SECOND dump and comparing per partition: "
                  "the quiescent partitions will match and userdata will not."))
    except Exception as exc:                      # noqa: BLE001 - reported, not raised
        run.update(state="failed", error=str(exc)[:200])
        # An exception leaves whatever bytes landed. Mark them partial too, so a
        # crashed dump never leaves a bare .img with no provenance at all.
        try:
            if dest.exists() and not str(dest).endswith(".partial"):
                partial = dest.with_name(dest.name + ".partial")
                dest.rename(partial)
                run["dest"] = str(partial)
        except OSError:
            pass
    finally:
        oplock.release(serial)
        run["ended"] = time.time()


@DISPATCH.op("watch.dump")
def _watch_dump(args):
    """Take a full-disk dump of a watch, in the background.

    Held under an oplock for the duration, because a-d-b's own housekeeping
    would otherwise switch the watch's USB mode mid-copy — which is exactly how
    a 3.9 GB read produced a 0-byte file on 2026-08-03."""
    serial = args["serial"]
    action = args.get("action") or "start"
    if action == "status":
        return {"ok": True, "run": _dump_runs.get(serial)}
    # Every other multi-action op ends in an "unknown action" refusal; this one
    # treated ANYTHING that was not "status" as "start", so a typo — or a probe
    # aimed at some other action — silently began a four-hour dump and took the
    # watch's lock with it.
    if action != "start":
        return {"ok": False, "error": f"unknown action: {action}"}
    # Claim the slot ATOMICALLY. The check and the assignment used to be
    # separate statements on a threaded server, so two starts a moment apart
    # both passed — and the dest name is only second-granular, so a double click
    # inside one second gave both workers the SAME file. Two dd pipelines
    # interleaved into one path can still add up to the expected size and be
    # manifested complete, which is the one thing this feature must never do.
    with _dump_claim:
        run = _dump_runs.get(serial)
        # "starting" counts as taken: between the claim and the copy actually
        # launching there is a preflight that talks to the watch, and a second
        # caller arriving in that window would otherwise pass the check and be
        # handed the same second-granular filename.
        if run and run.get("state") in ("starting", "running"):
            return {"ok": False, "error": "a dump of this watch is already running"}
        _dump_runs[serial] = {"state": "starting", "started": time.time()}

    try:
        from . import stockrom
        w = _watch(serial)
        expect, blocker = stockrom.disk_bytes(w)
        if blocker:
            return {"ok": False, "error": blocker}
        # An 8 GB write onto a full disk produces a short dump. That is caught
        # and marked .partial now, but the host also shares this disk with the
        # fleet's backups, registry and logs, so filling it breaks more than the
        # dump. Refuse up front, while the number is still just a number.
        #
        # Create the directory FIRST and measure that: on a host that has never
        # taken a dump, neither it nor its parent exists yet, and disk_usage on
        # a missing path raises rather than reporting free space.
        stockrom.DUMP_ROOT.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(stockrom.DUMP_ROOT).free
        if expect and free < expect + _DUMP_HEADROOM_BYTES:
            return {"ok": False,
                    "error": (f"not enough space for this dump: it needs "
                              f"{expect / 1e9:.1f} GB plus headroom and "
                              f"{free / 1e9:.1f} GB is free")}
        cfg = load_config()
        codename = (cfg.get("serials") or {}).get(serial) or serial
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = stockrom.DUMP_ROOT / f"{codename}-{serial}-{stamp}.img"
        manifest = stockrom.DUMP_ROOT / f"{codename}-{serial}-{stamp}.manifest.txt"
    # Use the address of the transport the size preflight just succeeded over,
    # not a re-derivation from cfg. An orbit/WiFi watch reaches us on an
    # SshTransport whose ip is its WiFi address, and it often has no ssh_ips
    # allocation — re-deriving gave None and fell back to an adb command against
        # a watch that is not on adb, dumping 0 bytes after a preflight that passed.
        ip = w.t.ip if isinstance(w.t, SshTransport) else None
        cmd = stockrom.dump_command(serial, ip, str(dest))

        lock = oplock.hold(serial, "dump", f"full-disk dump to {dest.name}",
                           4 * 3600)
        if not lock.get("ok"):
            # The watch is already held for something else (a wanze run, a
            # flash). Starting a dump would have overwritten that lock and
            # deleted it on release; refuse instead of stamping over it.
            note = f" — {lock['note']}" if lock.get("note") else ""
            return {"ok": False,
                    "error": f"watch is held for '{lock.get('kind')}'{note}; "
                             "not starting a dump over it"}
        _dump_runs[serial] = {"state": "running", "started": time.time(),
                              "dest": str(dest), "expect": expect, "size": 0}
        try:
            threading.Thread(target=_dump_worker, daemon=True,
                             args=(serial, dest, manifest, cmd, expect,
                                   codename)).start()
        except RuntimeError as exc:
            # The lock is already taken at this point; without releasing it the
            # watch would sit held for four hours for a dump that never ran.
            oplock.release(serial)
            _dump_runs[serial] = {"state": "failed", "error": str(exc)[:200]}
            return {"ok": False, "error": f"could not start the copy: {exc}"}
        return {"ok": True, "started": True, "dest": dest.name,
                "expect_bytes": expect}
    finally:
        # Any path that returned without reaching "running" must give the slot
        # back, or a refused start would block every later dump of this watch.
        with _dump_claim:
            if _dump_runs.get(serial, {}).get("state") == "starting":
                _dump_runs.pop(serial, None)


@DISPATCH.op("oplock.set")
def _oplock_set(argsd):
    """Claim or release a watch for a long operation.

    Exposed as an op so a script driving a dump or a flash can take the lock
    the same way the UI does — the collision this prevents came from a shell
    script, not from a button."""
    serial, action = argsd["serial"], argsd.get("action")
    if action == "hold":
        # A caller-supplied TTL is clamped rather than trusted. The expiry is
        # the backstop that stops a crashed holder exempting a watch from
        # housekeeping forever, so an unbounded ttl would defeat the one
        # property this lock cannot do without. A non-numeric ttl falls back to
        # the default instead of raising a 500 out of the route layer.
        try:
            ttl = float(argsd.get("ttl") or oplock.DEFAULT_TTL_SEC)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"ttl must be a number of seconds, "
                                          f"got {argsd.get('ttl')!r}"}
        if ttl <= 0:
            return {"ok": False, "error": "ttl must be positive"}
        ttl = min(ttl, oplock.MAX_TTL_SEC)
        return oplock.hold(serial, argsd.get("kind") or "operation",
                           argsd.get("note") or "", ttl)
    if action == "release":
        return oplock.release(serial)
    return {"ok": False, "error": f"unknown action: {action}"}


@DISPATCH.op("wanze.probe")
def _wanze_probe(argsd):
    """Install / stop / harvest the wanze probe.

    One op with an action, for the same reason bench.app is one op: they share
    the watch lookup and the failure shape, and the op table is the security
    boundary — fewer entries is fewer things to audit.

    `harvest` deliberately does NOT clear the on-watch buffer unless asked.
    Clearing is destructive to the only copy, and a harvest that failed to
    parse would otherwise take the evidence with it."""
    serial, action = argsd["serial"], argsd.get("action")
    w = _watch(serial)
    if action == "install":
        err = wanze.install(w)
        return {"ok": not err, **({"error": err} if err else {"armed": True})}
    if action == "stop":
        wanze.stop(w)
        return {"ok": True}
    if action == "uninstall":
        # clear_trace is opt-in: the trace is the only copy of the measurement
        # until it is harvested, so tidying up the tool must not take the data
        # with it unless that is what was asked for.
        err = wanze.uninstall(w, clear_trace=bool(argsd.get("clear_trace")))
        if err:
            return {"ok": False, "error": err}
        wanze.probing_set(serial, False)
        return {"ok": True, "removed": True,
                "trace_kept": not bool(argsd.get("clear_trace"))}
    if action == "harvest":
        d = wanze.harvest(w, clear=bool(argsd.get("clear")))
        return d
    # The run marker. Persisted rather than held in memory like the drain task:
    # a wanze run spans hours, and a service restart mid-run must not drop the
    # one indicator saying "leave this watch alone".
    if action in ("probing_start", "probing_stop"):
        return wanze.probing_set(serial, action == "probing_start",
                                 argsd.get("note") or "")
    return {"ok": False, "error": f"unknown action: {action}"}


@DISPATCH.op("watch.locale_set")
def _watch_locale_set(args):
    """Set the system locale via localectl — the same localed the settings app
    drives. Gated on what the WATCH itself lists under /usr/share/locale, so a
    locale it does not carry (or anything shell-shaped) can never be set."""
    serial, loc = args["serial"], args.get("locale", "")
    w = _watch(serial)
    data = w.settings_read() or {}
    avail = (data.get("locale") or {}).get("available") or []
    if loc not in avail:
        return {"ok": False, "error": "this watch does not carry that locale"}
    rc, _, err = w.t.shell(f"localectl set-locale LANG={loc}", timeout=12)
    return {"ok": rc == 0, **({} if rc == 0 else {"error": err.strip()[:120]})}


@DISPATCH.op("watch.wake_set")
def _watch_wake_set(args):
    """Set a wake gesture over MCE: kind 'tap' (doubletap policy never/always/
    proximity) or 'tilt' (wrist gesture enabled/disabled). Values outside the
    watch's own vocabulary are refused here — this op can express nothing
    else."""
    from .watch_settings import WAKE_FLAGS
    kind, value = args.get("kind"), args.get("value")
    if kind not in WAKE_FLAGS or value not in WAKE_FLAGS[kind][1]:
        return {"ok": False, "error": "unknown wake gesture or value"}
    flag = WAKE_FLAGS[kind][0]
    rc, _, err = _watch(args["serial"]).t.shell(f"mcetool {flag}={value}",
                                                timeout=10)
    return {"ok": rc == 0, **({} if rc == 0 else {"error": err.strip()[:120]})}


@DISPATCH.op("watch.diag")
def _watch_diag(args):
    """One-shot diagnostics read for the Diag tab. Durable fleet facts (eMMC
    wear, true battery capacity where the gauge exposes it) also land in the
    registry as latest values."""
    serial = args["serial"]
    d = _watch(serial).diag()
    if not d:
        return {"ok": False, "error": "diagnostics unreadable — watch offline?"}
    registry.note(serial, source="diag",
                  emmc_life="/".join(d.get("emmc_life", [])) or None,
                  bat_capacity_pct=d.get("bat_capacity_pct"))
    return {"ok": True, **d}


@DISPATCH.op("watch.bootchart")
def _watch_bootchart(args):
    """systemd's boot accounting for this watch (summary + per-service spans),
    read live over whichever link is up. The finish time — systemd's own
    'startup finished' — lands in the fleet registry as boot_finish_s,
    complementing the host-measured boot_adb_s/boot_ui_s."""
    serial = args["serial"]
    d = _watch(serial).bootchart()
    if not d:
        return {"ok": False,
                "error": "boot accounting unreadable — watch offline?"}
    if d.get("finish_s"):
        registry.note(serial, source="bootchart",
                      boot_finish_s=round(d["finish_s"], 1))
    return {"ok": True, **d}


@DISPATCH.op("watch.timeline")
def _watch_timeline(args):
    """The watch's battery-over-time points for the row sparkline, plus its
    standby loss rate. Serial keys the per-watch event log (codename is the
    fallback key)."""
    serial = args.get("serial")
    codename = args.get("codename")
    evs = event_log.read(serial, codename)
    points = [{"ts": e["ts"], "pct": e["pct"]}
              for e in evs
              if e.get("event") in ("check_reading", "drain_reading", "live_reading")
              and e.get("pct") is not None and e.get("ts") is not None]
    # Power-off / wear events mark where the record legitimately goes dark —
    # the history chart draws them as red gap lines (mo: suggest data is
    # missing rather than silently interpolating across a shelf period).
    marks = [{"ts": e["ts"], "kind": e.get("event")}
             for e in evs
             if e.get("event") in ("power_off", "wear") and e.get("ts")]
    return {"points": points, "marks": marks,
            "rate": event_log.standby_loss_rate(serial, codename, evs)}


@DISPATCH.op("drain.history")
def _drain_history(args):
    """All recorded drain results, newest first."""
    tests = []
    for f in _DRAIN_RESULTS_DIR.glob("*.json"):
        try:
            with f.open() as fh:
                d = json.load(fh)
        except Exception:
            continue
        readings = d.get("readings") or []
        tests.append({
            "codename":  d.get("codename"),
            "slot":      d.get("slot"),
            "start_ts":  d.get("start_ts"),
            "end_ts":    readings[-1].get("ts") if readings else d.get("start_ts"),
            "start_pct": d.get("start_pct"),
            "end_pct":   d.get("end_pct"),
            "rate":      d.get("drain_rate_pct_per_hour"),
            "stopped":   d.get("stopped_by_user", False),
            "samples":   len(readings),
        })
    tests.sort(key=lambda t: t.get("start_ts") or 0, reverse=True)
    return {"tests": tests,
            "wearable_min_hours": load_config().get("wearable_min_hours", 24)}


# ── streaming ops ────────────────────────────────────────────────────────────
# Stream handlers yield raw message strings; an empty string is a keep-alive
# heartbeat sentinel. The frontend turns each into an SSE frame — the backend
# knows nothing about SSE. Task state (_flash_tasks/_remap_tasks) lives here,
# with the backend, which the status builder reads.

class _QueueHandler(logging.Handler):
    """Routes log records from one specific thread into a Queue, so a worker
    thread's log output can be streamed to the client."""

    def __init__(self, q: "queue.Queue[str | None]", thread_id: int):
        super().__init__()
        self.q = q
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord):
        if record.thread == self.thread_id:
            try:
                self.q.put_nowait(self.format(record))
            except Exception:
                self.handleError(record)


def _flash_stream(codename: str, slot: str, cfg: dict, channel: "str | None" = None,
                  target: "tuple[str, int, str | None] | None" = None):
    """Run a flash in a daemon thread and yield its log lines as they happen.
    channel selects a release (e.g. "2.1"); None flashes the nightly.
    target = (loc, port, serial) pins the exact watch. Empty string = heartbeat."""
    q: "queue.Queue[str | None]" = queue.Queue()
    flash_cfg = flash_config(cfg)
    cfg_copy = copy.deepcopy(cfg)

    def _run_flash():
        tid = threading.get_ident()
        h = _QueueHandler(q, tid)
        h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.root.addHandler(h)
        try:
            q.put(f"INFO: flashing {codename} ({channel or 'nightly'})")
            q.put("INFO: waiting for ADB bus (another operation in progress)…")
            with _adb_lock:
                _flash_one_watch(codename, cfg_copy, flash_cfg,
                                 channel=channel, target=target)
        except Exception as exc:
            try:
                q.put(f"ERROR: {exc}")
            except Exception:
                pass
        finally:
            logging.root.removeHandler(h)
            # The worker owns the done flag: setting it from the generator
            # would mark a still-running flash finished the moment the client
            # disconnects from the stream.
            _flash_tasks[slot]["done"] = True
            q.put(None)

    _flash_tasks[slot] = {"done": False}
    t = threading.Thread(target=_run_flash, daemon=True)
    t.start()
    _flash_tasks[slot]["thread"] = t

    while True:
        try:
            msg = q.get(timeout=25)
        except queue.Empty:
            yield ""                       # heartbeat
            continue
        if msg is None:
            return
        yield msg


@DISPATCH.stream_op("flash.start")
def _flash_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if slot in _flash_tasks and not _flash_tasks[slot].get("done", True):
        yield "flash already in progress"
        return
    # A flash reboots the watch to the bootloader and rewrites it — the most
    # destructive thing this rig does to a watch that something else may be
    # mid-way through reading. It checked only its OWN task table before, so it
    # would start over a running dump, a wanze probe, or a live drain. The
    # asymmetry was the tell: Operation.start refuses while a flash runs, and
    # flash refused for nothing. _refuse_if_busy covers both the operation lock
    # and an op owning the port.
    busy = _refuse_if_busy(loc, port)
    if busy:
        yield f"ERROR: {busy['error']}"
        return
    channel = args.get("channel")
    if channel and not re.fullmatch(r"[\w.-]+", channel):
        yield f"ERROR: invalid channel {channel!r}"
        return
    cfg = load_config()
    codename = find_codename_for_loc_port(cfg, loc, port)
    if not codename:
        yield "ERROR: port not mapped to any codename"
        return
    # Pin the exact watch by the slot the user clicked — never re-derive the
    # port from the codename, which flashes the wrong unit when two watches
    # share a codename.
    serial = find_serial_for_loc_port(cfg, loc, port)
    yield from _flash_stream(codename, slot, cfg, channel, target=(loc, port, serial))


def _onboard_stream(loc: str, port: int):
    """Per-port onboarding (remap): power on, wait for a watch to enumerate on
    this port, identify it, update the mapping, PPPS-test. Yields progress
    lines; empty string = heartbeat."""
    slot = f"{loc}:{port}"
    sysfs_path = f"{loc}.{port}"
    q: "queue.Queue[str | None]" = queue.Queue()

    def _emit(msg: str) -> None:
        q.put(msg)

    def _run() -> None:
        # Onboards are strictly serial (they own port power), but the waiting
        # below disturbs nothing, so _adb_lock is taken only around the power
        # changes themselves rather than held across the whole boot window.
        _emit("Waiting for the onboard queue…")
        with _onboard_lock:
            try:
                _emit(f"Powering on {loc} p{port}…")
                try:
                    with _adb_lock:
                        uhubctl_set_power(loc, port, True)
                except RuntimeError as e:
                    _emit(f"WARNING: {e}")

                # A watch attached powered-off has to cold-boot before it
                # exposes ADB. And on this hardware a watch often fails to
                # enumerate on its first boot (stale-node / enumeration
                # hiccup), only appearing after a power cycle. So wait a boot
                # window, and if nothing shows, cycle the port once and wait
                # again — this is what made the manual "Refresh twice" work.
                wait_each = charge_config(load_config()).onboard_wait_seconds

                def _wait_for_watch(secs: int) -> "str | None":
                    st = time.monotonic()
                    nxt = 15
                    while time.monotonic() - st < secs:
                        devices = adb_devices()
                        path_map = _sysfs_path_to_serial_map(
                            set(devices.keys()), adb_usb_paths(devices))
                        s = path_map.get(sysfs_path)
                        if s and _adb_state(devices, s) == "device":
                            return s
                        el = time.monotonic() - st
                        if el >= nxt:
                            _emit(f"…waiting ({int(el)} / {secs} s)")
                            nxt += 15
                        time.sleep(1.0)
                    return None

                _emit(f"Waiting for the watch to boot and expose ADB "
                      f"(up to {wait_each} s)…")
                found_serial: "str | None" = _wait_for_watch(wait_each)
                if not found_serial:
                    _emit("No ADB yet — power-cycling the port to retry "
                          "enumeration…")
                    with _adb_lock:
                        uhubctl_cycle(loc, port)
                    _emit(f"Waiting again after the cycle (up to {wait_each} s)…")
                    found_serial = _wait_for_watch(wait_each)

                if found_serial:
                    _emit(f"ADB: {found_serial}")
                    with _config_lock:
                        cfg = load_config()
                        codename = cfg.get("serials", {}).get(found_serial)

                    if not codename:
                        _emit("Reading codename from watch…")
                        codename = get_watch_codename(found_serial) or found_serial
                    _emit(f"Watch: {codename}")

                    with _config_lock:
                        cfg = load_config()
                        cfg.setdefault("serials", {})[found_serial] = codename
                        # Remove old mapping for this watch from every other
                        # port: exact serial binding first, codename fallback.
                        for hub in cfg.get("hubs", []):
                            hub_ports   = hub.get("ports", {})
                            hub_serials = hub.get("port_serials", {})
                            stale = [k for k, s in hub_serials.items()
                                     if s == found_serial
                                     and not (hub["location"] == loc and k == str(port))]
                            stale += [k for k, v in hub_ports.items()
                                      if v == codename and k not in hub_serials
                                      and not (hub["location"] == loc and k == str(port))]
                            for k in stale:
                                hub_ports.pop(k, None)
                                hub_serials.pop(k, None)
                                _emit(f"Removed stale mapping: {hub['location']}:p{k} → {codename}")
                        # Add/update this port.
                        hub_entry = next((hub for hub in cfg.get("hubs", [])
                                          if hub["location"] == loc), None)
                        if hub_entry is not None:
                            hub_entry.setdefault("ports", {})[str(port)] = codename
                            hub_entry.setdefault("port_serials", {})[str(port)] = found_serial
                        save_config(cfg)

                    _emit(f"Mapped {loc}:p{port} → {codename}")

                    _emit("Testing port switching (PPPS, up to ~30 s)…")
                    try:
                        smart, msg = test_port_power_switching(loc, port, found_serial)
                        with _config_lock:
                            cfg = load_config()
                            for hub in cfg.get("hubs", []):
                                if hub["location"] == loc:
                                    _store_smart_verdict(hub, port, smart)
                                    break
                            save_config(cfg)
                        verdict = ("SMART ✓" if smart
                                   else "NOT SMART" if smart is False else "UNVERIFIED")
                        _emit(f"Port: {verdict} — {msg}")
                    except RuntimeError as e:
                        _emit(f"PPPS test error: {e}")

                else:
                    _emit("No watch detected.")
                    with _config_lock:
                        cfg = load_config()
                        was_mapped = False
                        for hub in cfg.get("hubs", []):
                            if hub["location"] == loc:
                                if str(port) in hub.get("ports", {}):
                                    was_mapped = True
                                    del hub["ports"][str(port)]
                                hub.get("port_serials", {}).pop(str(port), None)
                        if was_mapped:
                            save_config(cfg)
                            _emit("Cleared stale port mapping.")
                    # A deeply discharged watch can't boot inside any window —
                    # it needs VBUS to pre-charge first. Leave the port powered
                    # and say so; cutting power here strands exactly the watches
                    # that need charge the most.
                    _emit("Port left POWERED: if a watch with a flat battery "
                          "is docked here, let it pre-charge 30-60 min and "
                          "onboard again. Bootlooping watch? Hold it in "
                          "fastboot to charge. Empty port? Toggle it off.")

            except Exception as exc:
                _emit(f"ERROR: {exc}")
            finally:
                _remap_tasks[slot]["done"] = True
                q.put(None)

    # Budget: two full boot windows, the cycle between them, and slack for the
    # identify/PPPS tail. Past it the task reads as finished even if the worker
    # never said so — see tasks.task_active for why that matters.
    budget = 2 * charge_config(load_config()).onboard_wait_seconds + 180
    _remap_tasks[slot] = {"done": False, "deadline": time.monotonic() + budget}
    threading.Thread(target=_run, daemon=True).start()

    while True:
        try:
            msg = q.get(timeout=15)
        except queue.Empty:
            yield ""                       # heartbeat
            continue
        if msg is None:
            return
        yield msg


@DISPATCH.stream_op("onboard.start")
def _onboard_start(args):
    loc, port = args["loc"], args["port"]
    slot = f"{loc}:{port}"
    if task_active(_remap_tasks, slot):
        yield "onboard already in progress"
        return
    yield from _onboard_stream(loc, port)


# ── Onboard sweep ────────────────────────────────────────────────────────────
# The deliberate fill-the-rig flow: map the topology, power every port DOWN, let
# the user equip every socket with a watch (on an unpowered port nothing
# enumerates, so no 24-watch ADB flood), then run this — one port at a time.
# Each port: power on → detect the watch on ADB (or SSH/RNDIS) → map + read first
# data + write it to the fleet registry → PPPS-test → clean poweroff → cut VBUS →
# next. A port whose watch never boots (drained/empty) is cut and logged, so the
# sweep never leaves more than one port powered (no brownout, no flood).

def _sweep_leaf_ports(cfg: dict) -> "list[tuple[str, int]]":
    """Every watch-bearing leaf port across the mapped hubs — cascade ports that
    feed a sub-hub are skipped (powering one cuts the whole sub-tree), as are
    hidden hubs and user-excluded ports: a box marked hidden (the Lenovo dock)
    is not a watch dock, and sweeping it burns a full boot window per empty
    socket."""
    import glob
    hub_locs = {h["location"] for h in cfg.get("hubs", [])}
    out: list[tuple[str, int]] = []
    for hub in cfg.get("hubs", []):
        if hub.get("hidden"):
            continue
        loc = hub["location"]
        excluded = set(hub.get("exclude", {}))
        for iface in sorted(glob.glob(f"/sys/bus/usb/devices/{loc}:*")):
            for pd in sorted(glob.glob(f"{iface}/{loc}-port*")):
                try:
                    port = int(pd.rsplit("port", 1)[1])
                except ValueError:
                    continue
                if f"{loc}.{port}" in hub_locs:
                    continue                       # cascade → not a watch socket
                if str(port) in excluded:
                    continue
                out.append((loc, port))
    return out


def _sweep_held_ports(cfg: dict, ports: "list[tuple[str, int]]") -> dict:
    """Of these leaf ports, the ones whose watch is under an operation lock:
    {(loc, port): (serial, lock)}.

    The sweep is the one actuator the operator explicitly asks to power
    everything down, so it does not REFUSE on a lock the way the other ops do —
    it steps around it. A watch part-way through a 4 GB dump or a 14-day wanze
    run is exactly the one that must not be cut, and the person clicking sweep
    has no way to know a background run is live. Skipping keeps the sweep
    useful (every other socket is still swept) while making the exception
    visible, which a silent skip would not.
    """
    held = {}
    for loc, port in ports:
        serial = find_serial_for_loc_port(cfg, loc, port)
        lock = oplock.held(cfg, serial)
        if lock:
            held[(loc, port)] = (serial, lock)
    return held


def _describe_held_ports(held: dict) -> str:
    """One line naming each skipped port and what holds it."""
    return "; ".join(
        f"{loc}:{port} ({serial or '?'}) — {oplock.describe(lock)}"
        for (loc, port), (serial, lock) in sorted(held.items()))


# Set by onboard.sweep_skip while a sweep runs; the active boot-wait aborts on
# its next tick and the port is treated as a no-show (cut + logged). An Event,
# not a flag: the wait loop and the skip op run on different threads.
_sweep_skip = threading.Event()


def _sweep_wait_adb(sysfs_path: str, secs: int, emit) -> "tuple[str | None, str | None]":
    """Wait up to `secs` for a watch to boot and come fully online on ADB at
    this sysfs port. Returns (serial, None) once online, or (None, blocker) on
    timeout — blocker is the last not-online adb state seen here (WearOS
    'unauthorized': present, awaiting the on-watch RSA confirmation — the hint
    gives the user the rest of the window to tap it) or None for a plain
    no-show. Returns early on onboard.sweep_skip."""
    st = time.monotonic()
    nxt = 20
    blocker = None
    while time.monotonic() - st < secs:
        if _sweep_skip.is_set():
            _sweep_skip.clear()
            emit("  port skipped by user")
            return None, blocker
        devices = adb_devices()
        s = _sysfs_path_to_serial_map(
            set(devices.keys()), adb_usb_paths(devices)).get(sysfs_path)
        state = _adb_state(devices, s) if s else None
        if state == "device":
            return s, None
        if state and state != blocker:
            blocker = state
            if state == "unauthorized":
                emit("  watch present but ADB unauthorized — confirm the RSA "
                     "dialog on the watch now ('always allow' makes it stick)")
        el = time.monotonic() - st
        if el >= nxt:
            emit(f"  …waiting for boot ({int(el)}/{secs}s)")
            nxt += 20
        time.sleep(1.5)
    return None, blocker


def _sweep_map_to_port(loc, port, serial, codename, ssh_ip, emit):
    """Map the watch to its port, clearing any stale seat the same serial held
    elsewhere. The fleet-registry write is done by the caller from the full CC
    read (kernel/Qt/MACs/battery), not here."""
    with _config_lock:
        cfg = load_config()
        cfg.setdefault("serials", {})[serial] = codename
        for hub in cfg.get("hubs", []):
            ports_m = hub.get("ports", {})
            serials_m = hub.get("port_serials", {})
            stale = [k for k, s in serials_m.items()
                     if s == serial and not (hub["location"] == loc and k == str(port))]
            stale += [k for k, v in ports_m.items()
                      if v == codename and k not in serials_m
                      and not (hub["location"] == loc and k == str(port))]
            for k in stale:
                ports_m.pop(k, None)
                serials_m.pop(k, None)
                emit(f"  cleared stale seat {hub['location']}:p{k}")
        hub_entry = next((h for h in cfg.get("hubs", []) if h["location"] == loc), None)
        if hub_entry is not None:
            hub_entry.setdefault("ports", {})[str(port)] = codename
            hub_entry.setdefault("port_serials", {})[str(port)] = serial
        if ssh_ip:
            cfg.setdefault("ssh_ips", {})[serial] = ssh_ip
        save_config(cfg)
    emit(f"  mapped {codename} -> {loc}:p{port}")


def _sweep_one_port(loc: str, port: int, prefer_adb: bool, emit) -> "tuple[str | None, str | None]":
    """Onboard whatever is on one port, end-to-end, then leave it cleanly OFF.
    Returns (codename, None) on success, else (None, reason): "no_show" (empty
    or too drained to boot — port cut) or "unauthorized" (watch alive, awaiting
    the on-watch ADB confirmation — port left POWERED so the user can confirm
    and onboard it individually; cutting would strand it running on battery)."""
    slot = f"{loc}:{port}"
    sysfs_path = f"{loc}.{port}"
    # A freshly-equipped watch on a port that `prepare` powered off has to
    # COLD-boot before it exposes ADB. Cold-boot time varies (40-70s), so give it
    # a real window — at least 90s, more if the per-port onboard wait is higher.
    wait_secs = max(charge_config(load_config()).onboard_wait_seconds or 0, 90)
    emit(f"[{slot}] powering on…")
    try:
        uhubctl_set_power(loc, port, True)
    except RuntimeError as e:
        emit(f"[{slot}] power warning: {e}")

    serial, blocked = _sweep_wait_adb(sysfs_path, wait_secs, emit)
    transport, ssh_ip = "adb", None

    # No ADB → maybe it came up in developer/SSH mode as RNDIS on the shared .15.
    if not serial and _detect_rndis():
        emit(f"[{slot}] no ADB — found an SSH/RNDIS watch on {USB_SSH_IP}")
        info = orbit.probe(USB_SSH_IP) or {}
        s = info.get("serial")
        if s:
            with _config_lock:
                cfg = load_config()
                ssh_ip = allocate_ssh_ip(cfg, s)   # sticky unique IP for this serial
                save_config(cfg)
            if prefer_adb:
                emit(f"[{slot}] prefer-ADB: switching {s} SSH→ADB…")
                try:
                    _switch_ssh_to_adb(USB_SSH_IP)
                except Exception as e:
                    emit(f"[{slot}] switch warning: {e}")
                s2, _ = _sweep_wait_adb(sysfs_path, wait_secs, emit)
                if s2:
                    serial, transport, ssh_ip = s2, "adb", None
                else:
                    serial, transport = s, "ssh"     # switch didn't take; keep ssh
            else:
                serial, transport = s, "ssh"
                emit(f"[{slot}] keeping SSH, assigned IP {ssh_ip}")

    if not serial and blocked == "unauthorized":
        usb_serial = _sysfs_serial_at(loc, port)
        emit(f"[{slot}] watch alive but ADB UNAUTHORIZED"
             + (f" ({usb_serial})" if usb_serial else "")
             + " — approve this computer on the watch ('always allow'), then "
               "onboard this port individually. Leaving it powered.")
        registry.note(usb_serial, source="onboard-sweep",
                      note="adb unauthorized — awaiting on-watch confirmation")
        return None, "unauthorized"
    if not serial:
        emit(f"[{slot}] no watch enumerated in {wait_secs}s — drained or empty. "
             f"Cutting power, logging as needs-charge.")
        try:
            uhubctl_set_power(loc, port, False)
        except RuntimeError as e:
            emit(f"[{slot}] power-cut warning: {e}")
        return None, "no_show"

    # Pull the FULL Control Center data while the watch is LIVE — battery, kernel,
    # Qt / release / SoC, MACs, resolution, settings — and cache it, so a shelved
    # watch is NOT a dead click: everything the CC window and the fleet log show
    # is gathered now, once, and kept. Done before the poweroff so `safe_off_ts`
    # stays newer than this reading and the row still reads "shelved".
    w = (Watch(serial, transport=SshTransport(ssh_ip or USB_SSH_IP))
         if transport == "ssh" else Watch(serial))
    emit(f"[{slot}] reading full data over {transport.upper()}…")
    try:
        data = w.cc_data() or {}
    except Exception as e:
        emit(f"[{slot}] data read warning: {e}")
        data = {}
    try:
        geo = w.geometry() or {}
    except Exception:
        geo = {}
    codename = (geo.get("machine")
                or load_config().get("serials", {}).get(serial)
                or (get_watch_codename(serial) if transport == "adb" else None)
                or serial)
    battery = data.get("bat_cap")
    emit(f"[{slot}] {transport.upper()}: {codename} ({serial})"
         + (f" @ {ssh_ip}" if ssh_ip else "")
         + (f" — {battery}%" if battery is not None else ""))

    if data:
        last_seen.record(serial, cc=data, cc_ts=time.time(), battery=battery)
    elif battery is not None:
        last_seen.record(serial, battery=battery)
    # Full fleet-registry entry from the same read (identity/versions change-logged).
    registry.note(serial, source="onboard-sweep", codename=codename,
                  resolution=geo.get("resolution"), kernel=data.get("kernel"),
                  qt=data.get("qt"), release=data.get("release"),
                  soc=data.get("soc"), wlanmac=data.get("wlanmac"),
                  btmac=data.get("btmac_self"), battery=battery, ip=data.get("ip"))
    emit(f"  registered {codename} in the fleet log (full data cached)")

    _sweep_map_to_port(loc, port, serial, codename, ssh_ip, emit)

    # PPPS test — the watch is up, so this verifies a real VBUS cut for free.
    if transport == "adb":
        try:
            smart, msg = test_port_power_switching(loc, port, serial)
            with _config_lock:
                cfg = load_config()
                for h in cfg.get("hubs", []):
                    if h["location"] == loc:
                        _store_smart_verdict(h, port, smart)
                        break
                save_config(cfg)
            emit(f"[{slot}] PPPS: "
                 + ("smart ✓" if smart else "NOT smart" if smart is False else "unverified"))
        except RuntimeError as e:
            emit(f"[{slot}] PPPS error: {e}")

    # Clean shutdown, then cut VBUS IMMEDIATELY — same order as port.poweroff:
    # the shutdown command is synchronous, so it is delivered before the cut,
    # and any wait here races the halt (a watch still up when VBUS drops can
    # bounce back on). The old wait-for-adb-drop also faked confirmations:
    # adb drops on a REBOOT too, so watches that rebooted instead of halting
    # were stamped "shelved" while running on battery (2026-07-25 fleet drain,
    # audit F4). Delivery of the command is the only evidence claimed here.
    emit(f"[{slot}] clean poweroff…")
    graceful = False
    if transport == "adb":
        rc, _, _ = _run(f"adb -s {serial} shell poweroff", check=False, timeout=15)
        graceful = (rc == 0)
    else:
        try:
            SshTransport(ssh_ip or USB_SSH_IP).shell("poweroff", timeout=10)
            graceful = True
        except Exception:
            pass
    try:
        uhubctl_set_power(loc, port, False)
    except RuntimeError as e:
        emit(f"[{slot}] power-cut warning: {e}")
    # Stamp a graceful shutdown so the row reads "shelved" (deliberate, not
    # draining) rather than an ambiguous "---". Only on a delivered poweroff —
    # a bare VBUS cut must never claim to be a safe shelve.
    if graceful:
        last_seen.mark(serial, safe_off_ts=time.time())
    emit(f"[{slot}] {'shelved' if graceful else 'powered off (halt delivery unconfirmed)'}"
         f" — {codename} done.")
    return codename, None


@DISPATCH.op("onboard.sweep_prepare")
def _onboard_sweep_prepare(args):
    """Power every watch socket DOWN so the user can equip them all with watches
    before the sweep (on an unpowered port a docked watch stays dark — no flood)."""
    with _config_lock:
        cfg = load_config()
    ports = _sweep_leaf_ports(cfg)
    held = _sweep_held_ports(cfg, ports)
    n = 0
    for loc, port in ports:
        if (loc, port) in held:
            continue          # a long operation owns this watch — leave it alone
        try:
            uhubctl_set_power(loc, port, False)
            n += 1
        except Exception as e:
            log.debug("sweep_prepare %s.%s: %s", loc, port, e)
    if held:
        log.info("sweep_prepare: left %d held port(s) powered — %s",
                 len(held), _describe_held_ports(held))
    return {"ok": True, "ports": n,
            "held": len(held), "held_detail": _describe_held_ports(held)}


@DISPATCH.op("onboard.sweep_restore")
def _onboard_sweep_restore(args):
    """Power the sweep's sockets back ON after an aborted sweep.

    sweep_prepare cuts VBUS on every leaf port. Until this existed there was no
    way back: declining to run the sweep left the whole rig dark, and a watch
    that loses VBUS without a delivered poweroff keeps running on battery,
    invisible to the host — so an abort quietly started draining the fleet.

    Serialized like every other bulk power path: one port at a time, never the
    whole rig at once.
    """
    with _config_lock:
        cfg = load_config()
    ports = _sweep_leaf_ports(cfg)
    held = _sweep_held_ports(cfg, ports)
    n = 0
    for loc, port in ports:
        if (loc, port) in held:
            continue                      # never touched, nothing to restore
        try:
            uhubctl_set_power(loc, port, True)
            n += 1
        except Exception as e:
            log.debug("sweep_restore %s.%s: %s", loc, port, e)
    log.info("sweep restore: powered %d socket(s) back on", n)
    return {"ok": True, "ports": n}


@DISPATCH.op("onboard.sweep_skip")
def _onboard_sweep_skip(args):
    """Skip the running sweep's current port: its boot-wait aborts on the next
    tick and the port is cut + logged as a no-show. No-op error when no sweep
    is running, so a stale button can't arm a skip for a future sweep."""
    if _remap_tasks.get("__sweep__", {}).get("done") is not False:
        return {"ok": False, "error": "no sweep is running"}
    _sweep_skip.set()
    return {"ok": True}


@DISPATCH.stream_op("onboard.sweep_run")
def _onboard_sweep_run(args):
    """Run the onboard sweep: one port at a time, watches already equipped."""
    if _remap_tasks.get("__sweep__", {}).get("done") is False:
        yield "a sweep is already running"
        return
    _sweep_skip.clear()                 # a stale pre-sweep skip must not fire
    cfg = load_config()
    prefer_adb = usb_mode_preference(cfg) != "ssh"
    ports = _sweep_leaf_ports(cfg)
    # Step around watches under an operation lock rather than cutting them: the
    # sweep powers ports off and reboots what it finds, which would end a dump
    # or a wanze run the operator cannot see from here. Named, not silent.
    held = _sweep_held_ports(cfg, ports)
    ports = [p for p in ports if p not in held]
    yield (f"Onboard sweep starting: {len(ports)} sockets, one at a time "
           f"(prefer {'ADB' if prefer_adb else 'SSH'}).")
    if held:
        yield (f"Skipping {len(held)} held socket(s), left untouched: "
               f"{_describe_held_ports(held)}")
    q: "queue.Queue[str | None]" = queue.Queue()
    result = {"on": [], "skip": [], "unauthorized": []}

    def _run():
        for loc, port in ports:
            try:
                cn, why = _sweep_one_port(loc, port, prefer_adb, q.put)
                if cn:
                    result["on"].append(f"{loc}:{port} {cn}")
                elif why == "unauthorized":
                    result["unauthorized"].append(f"{loc}:{port}")
                else:
                    result["skip"].append(f"{loc}:{port}")
            except Exception as e:
                q.put(f"[{loc}:{port}] ERROR: {e}")
                result["skip"].append(f"{loc}:{port}")
        q.put(f"═══ sweep done: {len(result['on'])} onboarded, "
              f"{len(result['skip'])} need charge/empty, "
              f"{len(result['unauthorized'])} unauthorized"
              + (f", {len(held)} held (skipped)" if held else "") + " ═══")
        if held:
            q.put("held, not touched: " + _describe_held_ports(held))
        if result["skip"]:
            q.put("needs charge / empty: " + ", ".join(result["skip"]))
        if result["unauthorized"]:
            q.put("ADB unauthorized (left powered — confirm on the watch, "
                  "then onboard individually): "
                  + ", ".join(result["unauthorized"]))
        _remap_tasks["__sweep__"] = {"done": True}
        q.put(None)

    _remap_tasks["__sweep__"] = {"done": False}
    threading.Thread(target=_run, daemon=True).start()
    while True:
        try:
            msg = q.get(timeout=15)
        except queue.Empty:
            yield ""
            continue
        if msg is None:
            return
        yield msg
