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
import threading
import time

from .util import _run, log
from .adb import _adb_state, adb_devices, get_watch_codename
from .config import (_config_lock, _store_smart_verdict, allocate_ssh_ip,
                     charge_config, ssh_ip_for_serial, usb_mode_preference,
                     find_codename_for_loc_port, find_serial_for_loc_port,
                     flash_config, load_config, save_config,
                     orbit_add, orbit_forget, orbit_members,
                     hands_cal_for, set_hands_cal, set_hub_name)
from .usb import (_sysfs_path_to_serial_map, _sysfs_serial_at,
                  test_port_power_switching, uhubctl_cycle, uhubctl_set_power)
from .watchctl import DIAG_ROOT, Watch
from .ops import ChargeOp, DrainOp, WorkbenchOp, _flash_one_watch
from .fastboot import (_switch_ssh_to_adb, _usb_moded_switch_failed,
                       _detect_rndis, _fastboot_list, fastboot_getvar_all,
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
from .tasks import (_adb_lock, _charge_tasks, _flash_tasks, _remap_tasks,
                    active_op_on_slot)
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

def _stale_cc(serial, standby):
    """The last-known Control Center blob for a watch, marked stale, or {} if
    it was never seen. No device I/O — pure last_seen read, so it is instant."""
    cached = last_seen.get(serial)
    if not (cached and cached.get("cc")):
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
    return {}


@DISPATCH.op("watch.settings_read")
def _watch_settings_read(args):
    """The mirrored watch settings (appearance/display/nightstand) with their
    current dconf values. Read-only — the write op is deliberately separate."""
    data = _watch(args["serial"]).settings_read()
    if data is None:
        return {"ok": False, "error": "watch unreachable"}
    return {"ok": True, "settings": data["settings"], "quickpanel": data["quickpanel"]}


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
    kind = _op_owning(loc, port)
    if kind is None:
        return None
    return {"ok": False, "busy": kind,
            "error": f"a {kind} operation owns this port — stop it first, "
                     f"otherwise its readings are silently corrupted"}


@DISPATCH.op("port.set")
def _port_set(args):
    busy = _refuse_if_busy(args["loc"], args["port"])
    if busy:
        return busy
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


def _mark_booting(serial):
    """Stamp when we deliberately (re)boot a known watch, so the connection
    column can show "booting up" through the ~40s window and a hedged "boot
    failed?" past it. Only a real OS sighting (last_live_ts) clears it — see
    webstatus._boot_state. A None serial (empty/unmapped port) stamps nothing."""
    if serial:
        last_seen.mark(serial, booting_since=time.time())


def _watch_action(loc, port, adb_cmd, fb_cmd, fail_msg, boots_os=False):
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
    # Fastboot presence is resolved by PORT PATH, not by the mapped serial: a
    # watch's fastboot serial differs from its adb serial, so `serial in
    # _fastboot_list()` misses it and the action wrongly routes to a dead adb
    # link (the bootloader has no adb). Command the fastboot device bound to
    # THIS port by its own fastboot serial.
    fb_serial = _fastboot_serial_for_port(loc, port)
    in_fb = fb_serial is not None
    target = fb_serial if in_fb else serial
    if not target:
        return {"ok": False, "error": "no serial found for port"}
    cmd = fb_cmd if in_fb else adb_cmd
    if cmd is None:
        return {"ok": False,
                "error": f"action not available over {'fastboot' if in_fb else 'adb'}"}
    tool = "fastboot" if in_fb else "adb"
    rc, _, err = _run(f"{tool} -s {target} {cmd}", check=False, timeout=20)
    if rc != 0:
        return {"ok": False, "error": err or fail_msg}
    if boots_os:
        _mark_booting(serial or fb_serial)
    return {"ok": True, "via": tool}


@DISPATCH.op("port.reboot")
def _port_reboot(args):
    return _watch_action(args["loc"], args["port"], "reboot", "reboot",
                         "reboot failed", boots_os=True)


@DISPATCH.op("port.bootloader")
def _port_bootloader(args):
    # From adb this enters the bootloader; from fastboot it cycles it, which
    # is also how a fastboot battery reading gets re-sampled (the bootloader
    # snapshots it on entry and never refreshes within a session).
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
    """Toggle a whole hub's hidden flag (for unused sub-hubs)."""
    loc = args["loc"]
    with _config_lock:
        cfg = load_config()
        hub = next((hub for hub in cfg.get("hubs", []) if hub["location"] == loc), None)
        if hub is None:
            return {"ok": False, "error": "hub not found"}
        hub["hidden"] = not hub.get("hidden", False)
        state = hub["hidden"]
        save_config(cfg)
    return {"ok": True, "hidden": state}


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
                               owner=args.get("owner"))
            return {"ok": False, "error": err} if err else {"ok": True}

    @DISPATCH.op(f"{name}.stop")
    def _stop(args):
        if op_cls.stop(args["loc"], args["port"]):
            return {"ok": True}
        return {"ok": False, "error": stop_error}


_register_lifecycle(ChargeOp, "charge", "no charge running")
_register_lifecycle(WorkbenchOp, "workbench", "no workbench active")
_register_lifecycle(DrainOp, "drain", "no drain test running")


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
    return {"points": points,
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
        _emit("Waiting for ADB bus…")
        with _adb_lock:
            try:
                _emit(f"Powering on {loc} p{port}…")
                try:
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
                        path_map = _sysfs_path_to_serial_map(set(devices.keys()))
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

    _remap_tasks[slot] = {"done": False}
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
    if slot in _remap_tasks and not _remap_tasks[slot].get("done", True):
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
        s = _sysfs_path_to_serial_map(set(devices.keys())).get(sysfs_path)
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
    n = 0
    for loc, port in _sweep_leaf_ports(cfg):
        try:
            uhubctl_set_power(loc, port, False)
            n += 1
        except Exception as e:
            log.debug("sweep_prepare %s.%s: %s", loc, port, e)
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
    yield (f"Onboard sweep starting: {len(ports)} sockets, one at a time "
           f"(prefer {'ADB' if prefer_adb else 'SSH'}).")
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
              f"{len(result['unauthorized'])} unauthorized ═══")
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
