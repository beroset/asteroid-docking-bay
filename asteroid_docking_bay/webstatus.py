# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Builder for the /api/status document + live soft-remap of moved watches."""

from __future__ import annotations

import threading
import time

from .util import log
from .adb import (_adb_state, _resolve_conn_state, adb_devices,
                  battery_and_screen, get_watch_codename)
from .config import (_config_lock, charge_config, find_codename_for_serial,
                     hub_name_entry_for, load_config, orbit_members,
                     record_exact_codename, save_config, ssh_ip_for_serial,
                     usb_mode_preference)
from . import orbit
from .usb import (_parse_hub_port_path, _port_device_present, _sysfs_hub_scan,
                  _sysfs_path_to_serial_map, _sysfs_usb_mode, uhubctl_cycle,
                  uhubctl_list)
from .fastboot import _detect_rndis, _fastboot_getvar_product, _fastboot_list
from .transport import SshTransport
from .events import _latest_drain_summaries
from .lastseen import last_seen
from .variants import exact_codename
from .tasks import (_charge_tasks, _drain_tasks, _flash_tasks, _remap_tasks,
                    _workbench_tasks)
from .watchctl import (GEOMETRY_PROBE_VERSION, Watch, _watch_os,
                       _watch_os_for)


# Serials that could not be identified via ADB — don't re-probe every refresh.
_soft_remap_unknown: set[str] = set()

# slot → first time the port was seen powered+connected but not enumerating.
# That combination persisting is the signature of a flat/bootlooping watch
# (or a bad cable): the hub sees the device chirp, but enumeration never
# completes — the kernel logs -110/-62 errors while the UI showed nothing.
_enum_stuck_since: dict[str, float] = {}
_ENUM_STUCK_GRACE_SEC = 60  # normal boots enumerate well within this


def _enum_stuck(slot: str, power, adb_state, present: bool, now: float) -> bool:
    """Whether a mapped, powered port has failed to enumerate a watch.

    Every port in the status loop is config-mapped, so a watch is EXPECTED
    here. If the port is powered but has no adb link and no enumerated device
    node — after a boot grace — the docked watch never came up: flat-battery
    bootloop, bad contact, or removed.

    The old inline guard also required a `connect` bit, but on the sysfs rig
    `connect` was derived from the very device-node existence checked here
    (`connect == present`), so `connect and not present` was always False and
    this never fired — a docked-but-stuck watch instead read as "no link / no
    watch docked", the inverse of the truth (audit A1, 2026-07-24)."""
    if power and adb_state is None and not present:
        _enum_stuck_since.setdefault(slot, now)
    else:
        _enum_stuck_since.pop(slot, None)
    return (slot in _enum_stuck_since
            and now - _enum_stuck_since[slot] > _ENUM_STUCK_GRACE_SEC)

# Fake-power self-heal (opt-in): a mapped port that reports power but never
# enumerates a connection is the stale-node wedge. Track how long it's been
# wedged and when we last auto-cycled it, so recovery fires once per episode.
_fake_power_since: dict[str, float] = {}
_fake_power_cycled: dict[str, float] = {}
_fake_power_cycles: dict[str, int] = {}   # cycles this episode, to stop futile ones
_FAKE_POWER_GRACE_SEC = 60
_FAKE_POWER_BACKOFF_SEC = 300
_FAKE_POWER_MAX_CYCLES = 2   # a register cycle can't fix a physical button cut


def _maybe_self_heal_fake_power(slot: str, loc: str, port: int,
                                wedged: bool, busy: bool, cfg: dict) -> None:
    """Power-cycle a mapped port stuck powered-but-not-connecting for >60s.
    Opt-in (charge.fake_power_self_heal); once per episode with a backoff; never
    during an active op; never blocks the status path (the cycle runs in a
    daemon thread)."""
    if not wedged or busy or not charge_config(cfg).fake_power_self_heal:
        _fake_power_since.pop(slot, None)
        _fake_power_cycles.pop(slot, None)   # episode over — reset the count
        return
    now = time.time()
    if now - _fake_power_since.setdefault(slot, now) < _FAKE_POWER_GRACE_SEC:
        return
    if now - _fake_power_cycled.get(slot, 0) < _FAKE_POWER_BACKOFF_SEC:
        return
    _fake_power_cycled[slot] = now
    n = _fake_power_cycles.get(slot, 0) + 1
    _fake_power_cycles[slot] = n
    if n > _FAKE_POWER_MAX_CYCLES:
        # Cycled and still wedged: a sysfs register cycle CANNOT restore VBUS
        # cut by the hub's physical per-port button (LEDs are truth), and it is
        # not a stale node either. Stop actuating and name the physical cause,
        # rather than implying a recovery we can't deliver (audit A3).
        if n == _FAKE_POWER_MAX_CYCLES + 1:   # warn once per episode
            log.warning("%s: still wedged after %d auto-cycles — a register "
                        "cycle can't restore a VBUS cut by the hub's physical "
                        "port button; check the button/LED and cable",
                        slot, _FAKE_POWER_MAX_CYCLES)
        return
    log.info("%s: fake-power wedge (powered, no connect >%ds) — auto-cycling "
             "(best-effort; a register cycle can't fix a physical button cut)",
             slot, _FAKE_POWER_GRACE_SEC)
    threading.Thread(target=uhubctl_cycle, args=(loc, port), daemon=True).start()


# Stray SSH watches: a watch that self-enumerated in developer/SSH mode without
# going through switch_ssh has no allocated IP, so it is on the shared default
# 192.168.2.15 — the address every such watch takes, the source of the conflict.
# Track the last time we acted on one, so an in-flight relocation (a mode
# round-trip spanning several polls) does not re-fire and a failed attempt backs
# off rather than churning.
_STRAY_SSH_IP = "192.168.2.15"
_ssh_align_attempt: dict[str, float] = {}
_SSH_ALIGN_BACKOFF_SEC = 90


def _maybe_align_usb_mode(serial: "str | None", adb_state: "str | None",
                          cfg: dict) -> None:
    """Correct a stray SSH watch to match the fleet USB-mode preference: under
    "adb" switch it back to the standard mode; under "ssh" relocate it to its
    own IP so several watches can run SSH without colliding on the default.

    Only the stray is ever touched — a watch WITH an allocated IP was switched
    deliberately (switch_ssh allocates), so it is left alone, and a manual
    per-watch SSH switch is never undone. Guarded (per-serial backoff), runs in
    a daemon thread, never blocks the status path."""
    if adb_state != "ssh" or not serial or ssh_ip_for_serial(cfg, serial):
        if serial:
            _ssh_align_attempt.pop(serial, None)
        return
    now = time.time()
    if now - _ssh_align_attempt.get(serial, 0) < _SSH_ALIGN_BACKOFF_SEC:
        return
    _ssh_align_attempt[serial] = now
    pref = usb_mode_preference(cfg)
    log.info("%s: stray SSH watch on the default IP — aligning to '%s'",
             serial, pref)
    threading.Thread(target=_align_usb_mode_worker, args=(serial, pref),
                     daemon=True).start()


def _align_usb_mode_worker(serial: str, pref: str) -> None:
    """The mode round-trip, off the poll path. Reuses the two proven ops: get
    the stray off the shared IP onto adb, and under an SSH preference hand it a
    unique IP via the adb-side switch_ssh (the IP cannot change under a live SSH
    session, so it must be set while on adb, then applied on the switch back)."""
    from .fastboot import _switch_ssh_to_adb
    res = _switch_ssh_to_adb(_STRAY_SSH_IP)
    if not res.get("ok"):
        log.warning("%s: could not reach the stray SSH watch to align it: %s",
                    serial, res.get("error"))
        return
    if pref == "adb":
        return   # back on the standard mode — done
    for _ in range(20):
        time.sleep(1)
        if serial in adb_devices():
            break
    else:
        log.warning("%s: did not reappear on adb to receive its SSH IP", serial)
        return
    from . import rpcops   # local: rpcops imports this module
    out = rpcops.DISPATCH._data["watch.switch_ssh"]({"serial": serial})
    if not out.get("ok"):
        log.warning("%s: SSH IP relocation failed: %s", serial, out.get("error"))


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
    hub_locs = {hub["location"] for hub in cfg.get("hubs", [])}
    moves: list[tuple[str, str, str]] = []
    for path, serial in online_by_path.items():
        parsed = _parse_hub_port_path(path)
        if parsed is None or parsed[0] not in hub_locs or serial in _soft_remap_unknown:
            continue
        loc, port = parsed
        hub = next(hub for hub in cfg["hubs"] if hub["location"] == loc)
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
            hub = next((hub for hub in cfg.get("hubs", [])
                        if hub["location"] == loc), None)
            if hub is None:
                continue
            ports        = hub.setdefault("ports", {})
            port_serials = hub.setdefault("port_serials", {})
            if (ports.get(port_str) == codename
                    and port_serials.get(port_str) == serial):
                continue  # already correct after reload

            # Clear the watch's previous seat — exact serial binding first,
            # otherwise a single unambiguous codename match.
            old_seats = [(other_hub, k) for other_hub in cfg["hubs"]
                         for k, s in other_hub.get("port_serials", {}).items()
                         if s == serial
                         and not (other_hub["location"] == loc and k == port_str)]
            if not old_seats:
                named = [(other_hub, k) for other_hub in cfg["hubs"]
                         for k, cn in other_hub.get("ports", {}).items()
                         if cn.lower() == codename.lower()
                         and not (other_hub["location"] == loc and k == port_str)
                         and other_hub.get("port_serials", {}).get(k) is None]
                if len(named) == 1:
                    old_seats = named
            for other_hub, k in old_seats:
                old = other_hub.get("ports", {}).pop(k, None)
                other_hub.get("port_serials", {}).pop(k, None)
                log.info("soft-remap: cleared %s:p%s (was %s)",
                         other_hub["location"], k, old)

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


# A healthy watch enumerates within ~40s of a boot (30-40s observed), so a
# port powered with a boot we triggered but still no watch is "booting up"
# below the window and a hedged "boot failed?" above it — up to a cap, after
# which we stop claiming anything and let the plain connection state show.
BOOT_WINDOW = 45.0
BOOT_FAIL_CAP = 300.0


def _boot_state(ls: dict, power: "bool | None") -> "str | None":
    """The in-flight state after a (re)power we triggered. Distinguishes a real
    boot from a mere re-enumeration:

    - A gracefully-shelved watch (safe_off marker) we power on is OFF, so it
      actually boots: "booting" in the window, then "bootfail" past it (a
      question, since it can equally be a watch that never enumerates).
    - A watch that was just RUNNING when its VBUS was cut keeps running on
      battery; restoring power only makes it re-enumerate on the bus, not
      reboot. That reads "reconnecting" for the window, then no claim.

    Only meaningful with the port powered; a real adb sighting bumps
    last_live_ts past booting_since and ends it with no explicit clear."""
    if not power:
        return None
    bs = ls.get("booting_since") or 0
    llt = ls.get("last_live_ts") or 0
    if not bs or llt >= bs:
        return None
    so = ls.get("safe_off_ts") or 0
    cold = bool(so and so >= llt)   # was shelved/down → a real boot
    dt = time.time() - bs
    if dt < BOOT_WINDOW:
        return "booting" if cold else "reconnecting"
    if cold and dt < BOOT_FAIL_CAP:
        return "bootfail"
    return None


def _lifecycle(serial: "str | None", present: bool, power: "bool | None") -> "str | None":
    """The power-states we can positively assert, shown in the connection
    column. "down": a confirmed graceful shutdown (safe_off_ts) with the watch
    not seen live since and its port off — safely halted, not draining. A raw
    port cut never stamps safe_off_ts, so its ambiguous off-state stays
    unmarked — absence is "no claim", never "definitely off". "booting"/
    "bootfail": a deliberate (re)boot in progress or overdue (see _boot_state).
    Self-clears: the next time the watch is seen live, last_live_ts advances
    past both markers and this returns None."""
    if not serial:
        return None
    ls = last_seen.get(serial) or {}
    if ls.get("wear"):
        # Wear-held: while docked it is topping off (no pill — the button shows
        # the armed state); once it leaves the bus it is being worn.
        return None if present else "worn"
    if present:
        return None
    boot = _boot_state(ls, power)
    if boot:
        return boot
    # A powered watch that was just in fastboot and has now dropped off the bus
    # is almost certainly booting (mo): a flash or a fastboot reboot takes it off
    # the bus for the boot, and the bare "no link" that showed instead read as a
    # dead watch. Claim "booting" for a bounded window after the last fastboot
    # sighting; past the cap we stop claiming and let the plain state show.
    if power and ls.get("last_conn_state") == "fastboot":
        llt = ls.get("last_live_ts") or 0
        if llt and time.time() - llt < BOOT_FAIL_CAP:
            return "booting"
    if power:
        return None
    so = ls.get("safe_off_ts") or 0
    llt = ls.get("last_live_ts") or 0
    if so and so >= llt:
        return "down"
    return None


def _battery_view(adb_state: "str | None", serial: "str | None",
                  battery: "int | None", screen_forced: bool,
                  watch_os: "str | None") -> "tuple[int | None, float | None]":
    """Record a live reading, or fall back to the last-seen one when offline.

    A watch on ADB has its current values stored (last_live_ts stamped now);
    an offline watch returns the cached (battery, last_live_ts) so the UI can
    show a stale value instead of a blank. The live `battery` contract is left
    untouched — the caller keeps it None when offline and prefers cached only
    for display, so nothing mistakes a cached number for a fresh one."""
    if adb_state in ("device", "ssh"):
        # SSH is a live link too — record its reading so the row shows it fresh
        # and the cache stays current (os is read only over ADB, so it stays
        # None here and record()'s None-filter leaves any prior value intact).
        last_seen.record(serial, battery=battery,
                         screen_forced=screen_forced, os=watch_os)
        return None, None
    cached = last_seen.get(serial) if serial else None
    if not cached:
        return None, None
    return cached.get("battery"), cached.get("last_live_ts")


def _ssh_battery(cfg, serial) -> "tuple[int | None, bool, str | None]":
    """Battery / screen / charge for a watch on SSH, read over its SSH link so
    its row shows a live reading instead of the last ADB one. None when it has
    no assigned SSH IP or isn't reachable there — the caller then falls back to
    the cached value. Mirrors rpcops._reachable_transport's selection."""
    ip = ssh_ip_for_serial(cfg, serial) if serial else None
    if not ip or not _detect_rndis(ip):
        return None, False, None
    return battery_and_screen(serial, shell=SshTransport(ip).shell)


def _geometry_view(adb_state: "str | None", serial: "str | None") -> "dict | None":
    """The watch's screen geometry, probed once and cached forever.

    Geometry is static per watch, so probe it lazily — only when the watch is
    live and we've never stored it — and read it back from the cache on every
    later refresh (including while offline, for the screenshot mask). A watch
    never seen live has None until it appears."""
    if not serial:
        return None
    geo = (last_seen.get(serial) or {}).get("geometry")
    if geo and geo.get("probe_v", 1) >= GEOMETRY_PROBE_VERSION:
        return geo
    # Either nothing cached, or cached before a field we now collect existed —
    # re-probe while the watch is live so the cache catches up on its own.
    if adb_state == "device":
        fresh = Watch(serial).geometry()
        if fresh:
            fresh = {**fresh, "probe_v": GEOMETRY_PROBE_VERSION}
            last_seen.record(serial, geometry=fresh)
            return fresh
    # Offline with an outdated cache: incomplete beats nothing (the screenshot
    # mask only needs shape, which older probes already carry).
    return geo or None


def _web_status_data(cfg: dict) -> list[dict]:
    """
    Return hub-structured status including unmapped (empty) ports.
    Result: [{"location", "description", "ports": [...]}, ...]
    Mapped ports sort first (by number) within each hub; empty ports follow.
    Must never block on a USB scan: power/fastboot come from caches fed by
    webapp's background warmer.
    """
    _t0 = time.perf_counter()
    # serial -> exact codename learned this pass (flushed to config at the end).
    _detected_exact: dict[str, str] = {}
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
    for serial in list(_watch_os):
        if _adb_state(devices, serial) != "device":
            _watch_os.pop(serial)
    physical = {hub["location"]: hub for hub in (_sysfs_hub_scan(cfg) or uhubctl_list())}
    # Every hub location, used to spot cascade ports: a port whose child is
    # itself a hub (e.g. 1-2 port 3 feeds sub-hub 1-2.3). Those are internal
    # chip-to-chip links, not watch sockets — powering one off cuts the whole
    # sub-hub and every watch below it, so they must never appear as
    # toggleable/refreshable rows.
    hub_locs = set(physical.keys())
    drain_summaries = _latest_drain_summaries()
    result: list[dict] = []
    # Serials CURRENTLY connected on a physical port (adb/ssh/fastboot). A watch
    # that leaves the cradle but is still reachable in orbit hands off: it drops
    # out of its hub row (the port frees to available) and surfaces in the Orbit
    # section; redocking reverses it. So the Orbit section excludes exactly the
    # watches physically present now — not the ones merely mapped to a port.
    connected_serials: set[str] = set()
    orbit_here = orbit_members(cfg)

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
                serials_for_codename = [serial for serial, cname in cfg.get("serials", {}).items()
                                        if cname.lower() == codename.lower()]
                serial = (next((x for x in serials_for_codename if x in devices), None)
                          or next((x for x in serials_for_codename if x in fb_devices), None)
                          or (serials_for_codename[0] if serials_for_codename else None))
            # Fastboot detection must survive a bootloader serial that differs
            # from the adb serial (many watches report a different, or no, serial
            # in fastboot -- beluga: adb 22979c8c vs fastboot 100c0a32). The port
            # is bound to the adb serial, so also accept a fastboot device sitting
            # at THIS port's sysfs path -- the port uniquely identifies the watch.
            in_fastboot = bool((serial and serial in fb_devices)
                               or f"{loc}.{port_num}" in fb_by_path)
            adb_state = _resolve_conn_state(
                _adb_state(devices, serial) if serial else None,
                in_fastboot,
                lambda: _sysfs_usb_mode(f"{loc}.{port_num}") == "ssh")
            if serial and adb_state in ("device", "ssh", "fastboot"):
                connected_serials.add(serial)
            elif _port_handed_off(serial, adb_state, orbit_here,
                                  orbit.is_reachable_cached):
                # Handoff: this port's watch left the cradle but is reachable in
                # orbit — free the port to available and let the Orbit section
                # show it. A dim hint keeps the port's identity ("skipjack ↗").
                hub_ports.append({
                    "port": port_num, "codename": None, "slot_loc": loc,
                    "power": power, "smart": smart, "adb": None,
                    "battery": None, "empty": True,
                    "orbited_codename": codename,
                    "socket": sockets.get(port_str),
                    "excluded": excludes.get(port_str),
                })
                continue
            if adb_state == "device":
                battery, screen_forced, charge_status = battery_and_screen(serial)
            elif adb_state == "ssh":
                # A watch on SSH must show a LIVE battery in the row, not freeze
                # at its last ADB reading (mo: tunny stuck at 71% over SSH, jumped
                # to 100% on ADB). Read the same values over its SSH link.
                battery, screen_forced, charge_status = _ssh_battery(cfg, serial)
            else:
                battery, screen_forced, charge_status = None, False, None
            watch_os  = _watch_os_for(serial) if adb_state == "device" else None
            # Remember that a watch was last seen in the bootloader. Cutting
            # VBUS does NOT stop a watch in fastboot — measured 2026-07-18: it
            # keeps running on battery, invisible to the host, until flat. That
            # is how sturgeon reached 0%. Once the port is off the watch cannot
            # be seen at all, so the only way to warn is to remember the state
            # it was in when it vanished.
            if adb_state in ("fastboot", "device", "ssh"):
                last_seen.record(serial, last_conn_state=adb_state)
            # Store the live reading, or fall back to the last-seen one when
            # the watch is off the bus, so the row shows a stale value + age
            # rather than a blank cell.
            battery_cached, last_live_ts = _battery_view(
                adb_state, serial, battery, screen_forced, watch_os)
            geometry = _geometry_view(adb_state, serial)
            # Show the exact hardware codename (tunny, belugaxl) rather than the
            # shared MACHINE/image name — resolved from the watch's resolution
            # where a family shares one image. Cosmetic: config + ops still key
            # on the machine name (the local `codename`); only the display name
            # changes. Falls back to the machine name when it can't refine.
            machine = (geometry.get("machine") if geometry else None) or codename
            observed = ({"resolution": geometry.get("resolution"),
                         "bootloader": geometry.get("bootloader")}
                        if geometry else {})
            display_codename = exact_codename(machine, observed)
            # Remember the exact codename so the CLI — which has no live
            # detection — can address the watch by it. Record whenever the
            # identity is TRUSTWORTHY: the bootloader named it (authoritative,
            # even when it confirms the base name, so a real `skipjack` is
            # addressable as itself and not lumped with the tunnys sharing its
            # image), or resolution actively refined the image name. A bare
            # base name with no bootloader is just "not yet refined" — low
            # confidence, so it is not written as identity. Flushed once at end.
            trustworthy = bool(geometry and geometry.get("bootloader")) \
                or (display_codename and display_codename != machine)
            if serial and display_codename and trustworthy:
                _detected_exact[serial] = display_codename
            # Powered + hub sees a connection + nothing ever enumerates:
            # flat-battery bootloop or bad cable. Flag after a boot grace.
            connect = phys.get("connect", {}).get(port_num)
            not_enumerating = _enum_stuck(
                slot, power, adb_state,
                _port_device_present(loc, port_num), time.time())
            # A watch that vanished from an unpowered port while it was in the
            # bootloader is almost certainly still running on battery, because
            # LK does not shut down when USB goes away. Nothing else in the UI
            # can show this: with the port off there is no watch to read, so it
            # drains silently — the sturgeon failure. An op owning the port is
            # excluded: a drain test powers the port off deliberately.
            op_owns_slot = any(
                not tasks.get(slot, {}).get("done", True)
                for tasks in (_charge_tasks, _drain_tasks, _workbench_tasks))
            fb_draining = bool(
                serial and not power and adb_state is None and not op_owns_slot
                and (last_seen.get(serial) or {}).get("last_conn_state")
                    == "fastboot")
            flashing  = ((slot in _flash_tasks and not _flash_tasks[slot].get("done", True))
                         or (slot in _remap_tasks and not _remap_tasks[slot].get("done", True)))
            charging_active = (slot in _charge_tasks
                                and not _charge_tasks[slot].get("done", True))
            ct = _charge_tasks.get(slot, {})
            # A blind-mode countdown only exists while charging without a
            # target; anything else (stale key on a resumed task) would feed
            # the UI a countdown already in the past.
            charge_end_ts = (ct.get("charge_end_ts")
                             if charging_active and ct.get("target") is None
                             else None)
            charge_pct    = ct.get("pct")    if charging_active else None
            charge_target = ct.get("target") if charging_active else None
            charge_losing = ct.get("losing_power") if charging_active else None
            wb = _workbench_tasks.get(slot)
            workbench = None
            if wb and not wb.get("done", True):
                workbench = {"active": True, "pct": wb.get("pct"),
                             "phase": wb.get("phase"),
                             "blind": wb.get("blind", False),
                             "owner": wb.get("owner")}
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
                    "features":    dt.get("features"),   # WiFi/BT/AoD config of this run
                }
            # Powered but nothing ever connects = the stale-node/fake-power
            # wedge; self-heal it (opt-in) when the port is otherwise idle.
            # A wear-held port is powered with no watch on purpose (worn) —
            # never auto-cycle it.
            wear_held = bool((last_seen.get(serial) or {}).get("wear")) if serial else False
            wedged = bool(power) and not connect and adb_state is None and not wear_held
            busy   = bool(flashing or charging_active
                          or (drain and drain["active"])
                          or (workbench and workbench["active"]))
            _maybe_self_heal_fake_power(slot, loc, port_num, wedged, busy, cfg)
            if not busy:
                _maybe_align_usb_mode(serial, adb_state, cfg)
            hub_ports.append({
                "port": port_num, "codename": display_codename,
                "machine": machine, "serial": serial,
                "slot_loc": loc,
                "power": power, "smart": smart, "connected": connect,
                "adb": adb_state, "battery": battery, "os": watch_os,
                # The watch's assigned SSH-mode address, so the row can show
                # which watch holds which IP — most useful while it's in SSH
                # mode, but shown whenever one has been allocated.
                "ssh_ip": ssh_ip_for_serial(cfg, serial),
                "lifecycle": _lifecycle(serial, adb_state in ("device","ssh","fastboot"), power),
                "wear": bool((last_seen.get(serial) or {}).get("wear")) if serial else False,
                "battery_cached": battery_cached, "last_live_ts": last_live_ts,
                "geometry": geometry,
                "charge_status": charge_status,
                "screen_forced": screen_forced,
                "not_enumerating": not_enumerating,
                "fb_draining": fb_draining,
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
                "adb": _resolve_conn_state(
                    _adb_state(devices, adb_serial) if adb_serial else None,
                    bool(fb_serial),
                    lambda: _sysfs_usb_mode(sysfs_path) == "ssh"),
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

        name_prefix, name = hub_name_entry_for(cfg, loc)
        result.append({
            "location": loc,
            "description": description,
            "name": name,                          # physical box, e.g. "A16 #2"
            "name_prefix": name_prefix or loc,      # what a rename targets
            "ports": hub_ports,
            "hidden": cfg_hub.get("hidden", False),
        })

    # Group the cascaded chips of one physical box together and order each group
    # by its lowest socket, so a multi-chip hub reads in socket order rather than
    # internal chip order. Group by the box name when known (so an A16 hanging
    # off a dock clusters under its own name, not lumped with the dock by root
    # location); fall back to root location for unnamed hubs.
    def _hub_key(h):
        socks = [p["socket"] for p in h["ports"] if p.get("socket") is not None]
        return (h.get("name") or h["location"].split(".")[0],
                min(socks) if socks else 9999)
    result.sort(key=_hub_key)
    orbit_view = _orbit_hub_view(cfg, connected_serials)
    if orbit_view:
        result.append(orbit_view)          # always last, below the physical hubs
    _persist_exact_codenames(_detected_exact)
    elapsed = time.perf_counter() - _t0
    if elapsed > 1.0:     # quiet when fast; flag only the occasional slow refresh
        log.info("slow status refresh: %.2fs", elapsed)
    return result


def _port_handed_off(serial, adb_state, orbit_map, reachable) -> bool:
    """True when a mapped port's watch has left the cradle but is reachable in
    orbit: not connected on any wire here, yet an orbit member the warmer can
    still reach. Such a port frees to available and the watch shows in Orbit."""
    return bool(serial and adb_state not in ("device", "ssh", "fastboot")
                and serial in orbit_map and reachable(serial))


def _orbit_hub_view(cfg: dict, connected_serials: set) -> "dict | None":
    """The Orbit port as a virtual hub-view: one row per orbiting watch that is
    not physically on a rig port right now (a docked watch stays on its USB row;
    an undocked one that is still reachable hands off to here). Reachability comes
    from the warmer-fed cache and battery/geometry from last_seen, so this stays
    pure cache reads — no probe, no block. None when nothing is in orbit."""
    members = orbit_members(cfg)
    rows: list[dict] = []
    for serial, member in members.items():
        if serial in connected_serials:
            continue                        # on a rig port now → its USB row wins
        reachable = orbit.is_reachable_cached(serial)
        cached = last_seen.get(serial) or {}
        machine = member.get("codename") or find_codename_for_serial(cfg, serial)
        observed = {"resolution": member.get("resolution")}
        display = exact_codename(machine, observed) if machine else (machine or serial)
        rows.append({
            "codename": display, "machine": machine, "serial": serial,
            "orbit": True, "empty": False,
            "ip": member.get("ip"),
            # A reachable orbiting watch is a live SSH link, so the row and the
            # Control Center treat it exactly like a docked SSH watch.
            "adb": "ssh" if reachable else None,
            "reachable": reachable,
            # Reachable: the warmer's WiFi reading is live (coloured gauge).
            # Off WiFi: no live value — the row shows the last-known one stale.
            "battery": cached.get("battery") if reachable else None,
            "battery_cached": cached.get("battery"),
            "last_live_ts": cached.get("last_live_ts"),
            "geometry": cached.get("geometry"),
            "added": member.get("added"),
        })
    if not rows:
        return None
    rows.sort(key=lambda r: (r["codename"] or "").lower())
    return {"location": "orbit", "description": "Orbit — over the air",
            "ports": rows, "virtual": True, "hidden": False}


def _persist_exact_codenames(detected: dict) -> None:
    """Store newly-learned exact codenames in the config, once, under the lock.
    record_exact_codename is change-gated, so a fleet whose identities are all
    known writes nothing — the save happens only when something actually
    changed this pass."""
    if not detected:
        return
    with _config_lock:
        cfg = load_config()
        changed = False
        for serial, exact in detected.items():
            changed = record_exact_codename(cfg, serial, exact) or changed
        if changed:
            save_config(cfg)


