# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Builder for the /api/status document + live soft-remap of moved watches."""

import time

from .util import log
from .adb import _adb_state, adb_devices, get_battery_level, get_watch_codename
from .config import _config_lock, find_codename_for_serial, load_config, save_config
from .usb import (_parse_hub_port_path, _port_device_present, _sysfs_hub_scan,
                  _sysfs_path_to_serial_map, _sysfs_usb_mode, uhubctl_list)
from .fastboot import _fastboot_getvar_product, _fastboot_list
from .events import _latest_drain_summaries
from .tasks import (_charge_tasks, _drain_tasks, _flash_tasks, _remap_tasks,
                    _workbench_tasks)
from .watchctl import _watch_os, _watch_os_for


# Serials that could not be identified via ADB — don't re-probe every refresh.
_soft_remap_unknown: set[str] = set()

# slot → first time the port was seen powered+connected but not enumerating.
# That combination persisting is the signature of a flat/bootlooping watch
# (or a bad cable): the hub sees the device chirp, but enumeration never
# completes — the kernel logs -110/-62 errors while the UI showed nothing.
_enum_stuck_since: dict[str, float] = {}
_ENUM_STUCK_GRACE_SEC = 60  # normal boots enumerate well within this


def _soft_remap(cfg: dict, online_by_path: dict[str, str]) -> "dict | None":
    """
    Follow physical relocations of ADB-online watches.

    sysfs tells us each online watch's real hub port; when that disagrees
    with the config mapping, the watch was demonstrably moved — update the
    mapping. Only booted, ADB-online watches can be followed; relocating a
    powered-off watch still needs a manual Refresh on the new port.

    Also maintains hub["port_serials"] ({port: serial}), so two units of the
    same codename stay distinguishable. The old seat is cleared only on
    positive evidence: an exact serial binding, or an unambiguous single
    codename match — with duplicate codenames and no serial bindings the old
    mapping is left alone rather than guessed at.

    Returns the updated config, or None if nothing changed.
    """
    hub_locs = {h["location"] for h in cfg.get("hubs", [])}
    moves: list[tuple[str, str, str]] = []
    for path, serial in online_by_path.items():
        parsed = _parse_hub_port_path(path)
        if parsed is None or parsed[0] not in hub_locs or serial in _soft_remap_unknown:
            continue
        loc, port = parsed
        hub = next(h for h in cfg["hubs"] if h["location"] == loc)
        port_str = str(port)
        codename = cfg.get("serials", {}).get(serial)
        if (codename is not None
                and hub.get("ports", {}).get(port_str) == codename
                and hub.get("port_serials", {}).get(port_str) == serial):
            continue  # mapping already correct
        moves.append((loc, port_str, serial))
    if not moves:
        return None

    with _config_lock:
        cfg = load_config()
        changed = False
        for loc, port_str, serial in moves:
            codename = (cfg.get("serials", {}).get(serial)
                        or get_watch_codename(serial))
            if not codename:
                _soft_remap_unknown.add(serial)
                continue
            hub = next((h for h in cfg.get("hubs", [])
                        if h["location"] == loc), None)
            if hub is None:
                continue
            ports        = hub.setdefault("ports", {})
            port_serials = hub.setdefault("port_serials", {})
            if (ports.get(port_str) == codename
                    and port_serials.get(port_str) == serial):
                continue  # already correct after reload

            # Clear the watch's previous seat — exact serial binding first,
            # otherwise a single unambiguous codename match.
            old_seats = [(h2, k) for h2 in cfg["hubs"]
                         for k, s in h2.get("port_serials", {}).items()
                         if s == serial
                         and not (h2["location"] == loc and k == port_str)]
            if not old_seats:
                named = [(h2, k) for h2 in cfg["hubs"]
                         for k, cn in h2.get("ports", {}).items()
                         if cn.lower() == codename.lower()
                         and not (h2["location"] == loc and k == port_str)
                         and h2.get("port_serials", {}).get(k) is None]
                if len(named) == 1:
                    old_seats = named
            for h2, k in old_seats:
                old = h2.get("ports", {}).pop(k, None)
                h2.get("port_serials", {}).pop(k, None)
                log.info("soft-remap: cleared %s:p%s (was %s)",
                         h2["location"], k, old)

            prev = ports.get(port_str)
            ports[port_str] = codename
            port_serials[port_str] = serial
            cfg.setdefault("serials", {})[serial] = codename
            changed = True
            log.info("soft-remap: %s (%s) now at %s:p%s%s",
                     codename, serial, loc, port_str,
                     f" (replacing {prev})" if prev and prev != codename else "")
        if changed:
            save_config(cfg)
            return cfg
    return None


def _web_status_data(cfg: dict) -> list[dict]:
    """
    Return hub-structured status including unmapped (empty) ports.
    Result: [{"location", "description", "ports": [...]}, ...]
    Mapped ports sort first (by number) within each hub; empty ports follow.
    Must never block on a USB scan: power/fastboot come from caches fed by
    webapp's background warmer.
    """
    _t0 = time.perf_counter()
    devices = adb_devices()
    fb_devices = _fastboot_list()   # {serial: sysfs_path | None}
    # Reverse maps for empty-port detection: sysfs_path → serial
    fb_by_path: dict[str, str] = {
        path: serial
        for serial, path in fb_devices.items()
        if path is not None
    }
    adb_by_path: dict[str, str] = _sysfs_path_to_serial_map(set(devices.keys()))
    # Live soft-remap: follow booted watches that were physically moved.
    online_by_path = {p: s for p, s in adb_by_path.items()
                      if _adb_state(devices, s) == "device"}
    cfg = _soft_remap(cfg, online_by_path) or cfg
    # Evict OS cache entries for offline watches → re-detected on next boot.
    for s in list(_watch_os):
        if _adb_state(devices, s) != "device":
            _watch_os.pop(s)
    physical = {h["location"]: h for h in (_sysfs_hub_scan(cfg) or uhubctl_list())}
    # Every hub location, used to spot cascade ports: a port whose child is
    # itself a hub (e.g. 1-2 port 3 feeds sub-hub 1-2.3). Those are internal
    # chip-to-chip links, not watch sockets — powering one off cuts the whole
    # sub-hub and every watch below it, so they must never appear as
    # toggleable/refreshable rows.
    hub_locs = set(physical.keys())
    drain_summaries = _latest_drain_summaries()
    result: list[dict] = []

    for cfg_hub in cfg.get("hubs", []):
        loc         = cfg_hub["location"]
        mapped      = cfg_hub.get("ports", {})        # {str(port): codename}
        port_smart  = cfg_hub.get("port_smart", {})
        sockets     = cfg_hub.get("sockets", {})      # {str(port): physical socket label}
        excludes    = cfg_hub.get("exclude", {})      # {str(port): reason} — do-not-use

        phys        = physical.get(loc, {})
        all_ports   = phys.get("ports", sorted(int(p) for p in mapped))
        description = phys.get("description", "")

        mapped_nums = sorted(int(p) for p in mapped)
        empty_nums  = sorted(n for n in all_ports
                             if str(n) not in mapped
                             and f"{loc}.{n}" not in hub_locs)

        hub_ports: list[dict] = []

        for port_num in mapped_nums:
            port_str  = str(port_num)
            codename  = mapped[port_str]
            # Power state comes from the single full uhubctl scan above —
            # a per-port uhubctl_get_power here would spawn one bus rescan
            # per mapped port on every refresh.
            power     = phys.get("power", {}).get(port_num)
            smart     = port_smart.get(port_str)
            slot      = f"{loc}:{port_num}"
            # Exact per-port serial binding wins; otherwise prefer a
            # currently-connected serial over the first config match so two
            # same-codename watches each see their own ADB state.
            serial = cfg_hub.get("port_serials", {}).get(port_str)
            if not serial:
                serials_for_codename = [s for s, cn in cfg.get("serials", {}).items()
                                        if cn.lower() == codename.lower()]
                serial = (next((s for s in serials_for_codename if s in devices), None)
                          or next((s for s in serials_for_codename if s in fb_devices), None)
                          or (serials_for_codename[0] if serials_for_codename else None))
            adb_state = _adb_state(devices, serial) if serial else None
            if adb_state is None and serial and serial in fb_devices:
                adb_state = "fastboot"
            if adb_state is None and _sysfs_usb_mode(f"{loc}.{port_num}") == "ssh":
                adb_state = "ssh"
            battery   = get_battery_level(serial) if adb_state == "device" else None
            watch_os  = _watch_os_for(serial) if adb_state == "device" else None
            # Powered + hub sees a connection + nothing ever enumerates:
            # flat-battery bootloop or bad cable. Flag after a boot grace.
            connect = phys.get("connect", {}).get(port_num)
            if (power and connect and adb_state is None
                    and not _port_device_present(loc, port_num)):
                _enum_stuck_since.setdefault(slot, time.time())
            else:
                _enum_stuck_since.pop(slot, None)
            not_enumerating = (slot in _enum_stuck_since
                               and time.time() - _enum_stuck_since[slot]
                                   > _ENUM_STUCK_GRACE_SEC)
            flashing  = ((slot in _flash_tasks and not _flash_tasks[slot].get("done", True))
                         or (slot in _remap_tasks and not _remap_tasks[slot].get("done", True)))
            charging_active = (slot in _charge_tasks
                                and not _charge_tasks[slot].get("done", True))
            ct = _charge_tasks.get(slot, {})
            charge_end_ts = ct.get("charge_end_ts")
            charge_pct    = ct.get("pct")    if charging_active else None
            charge_target = ct.get("target") if charging_active else None
            charge_losing = ct.get("losing_power") if charging_active else None
            wb = _workbench_tasks.get(slot)
            workbench = None
            if wb and not wb.get("done", True):
                workbench = {"active": True, "pct": wb.get("pct"),
                             "phase": wb.get("phase"),
                             "blind": wb.get("blind", False)}
            drain_last = drain_summaries.get(codename.lower())
            if (drain_last and drain_last.get("serial") and serial
                    and drain_last["serial"] != serial):
                drain_last = None  # result belongs to another unit of this codename
            drain = None
            if slot in _drain_tasks:
                dt = _drain_tasks[slot]
                drain = {
                    "active":      not dt.get("done", True),
                    "last_pct":    dt.get("last_pct"),
                    "drain_rate":  dt.get("drain_rate"),
                    "start_ts":    dt.get("start_ts"),
                    "done":        dt.get("done", True),
                    "stopped":     dt.get("stopped", False),
                }
            hub_ports.append({
                "port": port_num, "codename": codename, "serial": serial,
                "slot_loc": loc,
                "power": power, "smart": smart, "connected": connect,
                "adb": adb_state, "battery": battery, "os": watch_os,
                "not_enumerating": not_enumerating,
                "flashing": flashing, "empty": False,
                "charging_active": charging_active,
                "charge_end_ts": charge_end_ts,
                "charge_pct": charge_pct, "charge_target": charge_target,
                "charge_losing": charge_losing,
                "drain": drain, "drain_last": drain_last,
                "workbench": workbench,
                "socket": sockets.get(port_str),
                "excluded": excludes.get(port_str),
            })

        for port_num in empty_nums:
            sysfs_path = f"{loc}.{port_num}"
            fb_serial  = fb_by_path.get(sysfs_path)
            adb_serial = adb_by_path.get(sysfs_path)
            fb_product = None
            if fb_serial:
                # Prefer config-known name; fall back to fastboot getvar product.
                fb_product = cfg.get("serials", {}).get(fb_serial) or _fastboot_getvar_product(fb_serial)
            adb_codename = find_codename_for_serial(cfg, adb_serial) if adb_serial else None
            empty_slot = f"{loc}:{port_num}"
            remapping = (empty_slot in _remap_tasks
                         and not _remap_tasks[empty_slot].get("done", True))
            hub_ports.append({
                "port": port_num, "codename": adb_codename,
                "slot_loc": loc,
                # power state is free from the single full scan above
                "power": phys.get("power", {}).get(port_num),
                "smart": port_smart.get(str(port_num)),
                "adb": (_adb_state(devices, adb_serial) if adb_serial
                        else ("fastboot" if fb_serial
                              else ("ssh" if _sysfs_usb_mode(sysfs_path) == "ssh"
                                    else None))),
                "os": (_watch_os_for(adb_serial)
                       if adb_serial and _adb_state(devices, adb_serial) == "device" else None),
                "battery": None,
                "flashing": remapping, "empty": True,
                "fastboot_product": fb_product,
                "unmapped": adb_codename is not None,
                "socket": sockets.get(str(port_num)),
                "excluded": excludes.get(str(port_num)),
            })

        # Order rows by physical socket when known, so the UI reads in the
        # order the sockets sit on the hub rather than internal chip order.
        hub_ports.sort(key=lambda p: (p.get("socket") is None,
                                      p.get("socket") or 0, p["port"]))

        result.append({
            "location": loc,
            "description": description,
            "ports": hub_ports,
            "hidden": cfg_hub.get("hidden", False),
        })

    # Group the cascaded chips of one physical hub together (by root location,
    # e.g. all 1-2.* stay together) and order each group by its lowest socket,
    # so a multi-chip hub reads in socket order rather than internal chip order.
    def _hub_key(h):
        socks = [p["socket"] for p in h["ports"] if p.get("socket") is not None]
        return (h["location"].split(".")[0], min(socks) if socks else 9999)
    result.sort(key=_hub_key)
    elapsed = time.perf_counter() - _t0
    if elapsed > 1.0:     # quiet when fast; flag only the occasional slow refresh
        log.info("slow status refresh: %.2fs", elapsed)
    return result


