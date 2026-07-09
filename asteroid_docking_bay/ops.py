# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Long-running operations: charge, drain test, workbench, flash, resume."""

import threading
import time
from pathlib import Path

from .util import _run, log
from .adb import adb_devices_checked, get_battery_level, wait_serial_online
from .config import (find_codename_for_loc_port, find_port_for_codename,
                     find_serial_for_loc_port, is_port_smart, load_config)
from .usb import uhubctl_cycle, uhubctl_get_power, uhubctl_set_power
from .fastboot import (_clear_ssh_known_hosts, _detect_rndis, _download_nightly,
                       _fastboot_devices, _flash_watch, _wait_for_fastboot)
from .events import (_DRAIN_FLOOR_PCT, _DRAIN_POLL_SEC, _log_event,
                     _save_drain_results)
from .tasks import (_adb_lock, _charge_stop, _charge_tasks, _drain_stop,
                    _drain_tasks, _persist_task, _unpersist_task,
                    _load_persisted_tasks, _workbench_stop, _workbench_tasks)
from .watchctl import wait_for_adb


_CHARGE_POLL_SEC = 120  # battery poll interval in charge-to-target mode
_CHARGE_STALL_POLLS = 2  # consecutive drops while charging → "losing power" alarm


def _ensure_port_powered(codename: str, loc: "str | None",
                         port: "int | None") -> None:
    """Re-assert port power if something external cut it mid-charge
    (concurrent process, dock glitch)."""
    if loc is None or port is None:
        return
    if uhubctl_get_power(loc, port) is False:
        log.warning("%s: port %s:%s lost power during charge — re-powering",
                    codename, loc, port)
        try:
            uhubctl_set_power(loc, port, True)
        except RuntimeError as e:
            log.warning("%s: re-power failed: %s", codename, e)


def _end_port(loc: str, port: int, serial: "str | None", charge_cfg: dict,
              reason: str = "") -> None:
    """End a powered-on operation: shut the watch down over ADB, then cut
    VBUS immediately — the exact proven sequence of the manual Power-off
    button (adb shell poweroff is synchronous, so cutting right after leaves
    the watch halting on battery with no power to bounce back on). Always
    leaves the port off.

    Never powers a port on or blocks when ADB is unhealthy: doing so during a
    bus wedge just keeps watches powered and feeds the churn. If ADB is down,
    it just cuts VBUS."""
    graceful = charge_cfg.get("graceful_poweroff", True) and bool(serial)
    if graceful and adb_devices_checked() is None:
        log.warning("%s: ADB unavailable — cutting power without graceful "
                    "shutdown (%s)", serial, reason or "op end")
        graceful = False
    try:
        if graceful:
            # During a charge the watch is already powered + on ADB; after a
            # drain it's on battery with the port off, so bring it up briefly
            # (it's already booted, so ADB returns in a few seconds).
            if uhubctl_get_power(loc, port) is not True:
                uhubctl_set_power(loc, port, True)
                wait_serial_online(serial, 5, 4)
            log.info("%s: graceful poweroff (%s)", serial, reason or "op end")
            _run(f"adb -s {serial} shell poweroff", check=False, timeout=10)
    except Exception as exc:
        log.debug("graceful poweroff of %s failed: %s", serial, exc)
    finally:
        try:
            uhubctl_set_power(loc, port, False)
        except Exception:
            pass


def charge_to_target(codename: str, serial: "str | None", charge_cfg: dict,
                     loc: "str | None" = None,
                     port: "int | None" = None) -> "int | None":
    """
    Charge a watch whose port is already powered until high_threshold,
    polling the battery every _CHARGE_POLL_SEC and hard-capped by
    charge_max_minutes.  Falls back to a blind charge_duration_minutes sleep
    when the battery can't be read.  Returns the final battery level (None
    in blind mode).  With loc/port given, port power is re-asserted each
    poll in case something external cut it.  Used by the CLI charge command
    and the periodic timer; the web UI has its own loop with stop-event and
    live task state.
    """
    target  = charge_cfg.get("high_threshold", 80)
    max_sec = charge_cfg.get("charge_max_minutes", 240) * 60
    level = get_battery_level(serial) if serial else None
    if level is None:
        duration = charge_cfg.get("charge_duration_minutes", 30)
        log.info("%s: battery unreadable — charging blind for %d min",
                 codename, duration)
        time.sleep(duration * 60)
        return None
    if level >= target:
        log.info("%s: already at %d%% (≥%d%%) — nothing to do",
                 codename, level, target)
        return level
    log.info("%s: charging %d%% → %d%%", codename, level, target)
    deadline = time.time() + max_sec
    while time.time() < deadline:
        time.sleep(_CHARGE_POLL_SEC)
        _ensure_port_powered(codename, loc, port)
        lvl = get_battery_level(serial)
        if lvl is None:
            continue  # transient read failure — keep charging
        if lvl != level:
            log.info("%s: %d%%", codename, lvl)
        level = lvl
        if level >= target:
            log.info("%s: reached %d%% (target %d%%)", codename, level, target)
            return level
    log.warning("%s: target %d%% not reached within charge_max_minutes — "
                "stopping at %d%%", codename, target, level)
    return level


def _run_charge_for_web(slot: str, loc: str, port: int, cfg: dict) -> None:
    """Run a manual charge cycle for the web UI; updates _charge_tasks[slot].

    Charge-to-target: when the battery is readable over ADB, charge until
    high_threshold is reached (capped by charge_max_minutes) instead of
    blindly for a fixed duration.  The blind fixed-duration countdown remains
    the fallback for watches that never enumerate or expose no battery.
    """
    charge_cfg = cfg.get("charge", cfg)
    duration_sec = charge_cfg.get("charge_duration_minutes", 30) * 60
    target       = charge_cfg.get("high_threshold", 80)
    max_sec      = charge_cfg.get("charge_max_minutes", 240) * 60
    codename = find_codename_for_loc_port(cfg, loc, port) or slot
    stop_event = _charge_stop[slot]
    task = _charge_tasks[slot]

    log.info("%s: waiting for ADB bus…", codename)
    with _adb_lock:
        try:
            uhubctl_set_power(loc, port, True)
            serial = find_serial_for_loc_port(cfg, loc, port)
            if serial:
                # Charging works without enumeration (VBUS is on either way),
                # so a timeout here is logged by the helper and charge proceeds.
                wait_serial_online(serial,
                                   charge_cfg.get("adb_wait_seconds", 15),
                                   charge_cfg.get("adb_wait_retries", 8),
                                   stop_event, recover_loc_port=(loc, port))
            if stop_event.is_set():
                log.info("%s: charge cancelled while waiting for ADB", codename)
                return

            level = get_battery_level(serial) if serial else None
            if level is not None and level >= target:
                task["pct"], task["target"] = level, target
                log.info("%s: already at %d%% (≥%d%%) — nothing to do",
                         codename, level, target)
            elif level is not None:
                task["pct"], task["target"] = level, target
                _persist_task("charge", slot, loc, port, task)
                _log_event(serial, codename, "charge_start", pct=level, target=target)
                log.info("%s: charging %d%% → %d%%", codename, level, target)
                deadline = time.time() + max_sec
                prev_pct = level
                dropping = 0
                while not stop_event.wait(timeout=_CHARGE_POLL_SEC):
                    if time.time() >= deadline:
                        log.warning("%s: target %d%% not reached within "
                                    "charge_max_minutes — stopping at %s%%",
                                    codename, target, task.get("pct"))
                        break
                    _ensure_port_powered(codename, loc, port)
                    lvl = get_battery_level(serial)
                    if lvl is None:
                        continue  # transient read failure — keep charging
                    task["pct"] = lvl
                    # Alarm: a charging watch must never LOSE charge. Consecutive
                    # drops mean it's losing power despite the charge attempt —
                    # dirty contacts / bad cable / failing port (the sawfish case
                    # we used to silently discard). Flag it but keep trying; a
                    # flaky contact may recover.
                    if lvl < prev_pct:
                        dropping += 1
                        if dropping >= _CHARGE_STALL_POLLS and not task.get("losing_power"):
                            task["losing_power"] = True
                            log.warning("%s: battery DROPPING while charging "
                                        "(%d%% → %d%%) — losing power despite charge; "
                                        "check contacts/cable/port",
                                        codename, prev_pct, lvl)
                            _log_event(serial, codename, "charge_power_loss",
                                       pct=lvl, from_pct=prev_pct)
                    elif lvl > prev_pct:
                        if task.pop("losing_power", None):
                            log.info("%s: charge recovered — gaining again (%d%%)",
                                     codename, lvl)
                        dropping = 0
                    prev_pct = lvl
                    _persist_task("charge", slot, loc, port, task)
                    if lvl >= target:
                        log.info("%s: reached %d%% (target %d%%)",
                                 codename, lvl, target)
                        break
                _log_event(serial, codename, "charge_end", pct=task.get("pct"))
            else:
                end_ts = time.time() + duration_sec
                task["charge_end_ts"] = end_ts
                _persist_task("charge", slot, loc, port, task)
                log.info("%s: battery unreadable — charging blind for %d min",
                         codename, duration_sec // 60)
                # Sleep in 5-second chunks so a stop is noticed promptly.
                ticks = 0
                while not stop_event.wait(timeout=5):
                    if time.time() >= end_ts:
                        break
                    ticks += 1
                    if ticks % 12 == 0:  # every ~60 s
                        _ensure_port_powered(codename, loc, port)
            if stop_event.is_set():
                log.info("%s: charge stopped by user", codename)
        except Exception as exc:
            log.warning("%s: charge failed: %s", codename, exc)
        finally:
            _end_port(loc, port, serial, charge_cfg, "charge ended")
            task["done"] = True
            task.pop("charge_end_ts", None)
            _charge_stop.pop(slot, None)
            _unpersist_task("charge", slot)


def _adb_read_battery(loc: str, port: int, serial: str | None,
                      charge_cfg: dict, stop_event: threading.Event) -> int | None:
    """Power on port, wait for ADB, read battery %, power off. Returns None on failure.

    Intentionally does NOT hold _adb_lock: each drain test is pinned to one
    port via _drain_tasks, so port-level exclusion is already guaranteed by the
    UI.  Holding the lock for the full ADB wait (up to ~2 min) blocks charge,
    flash and remap on every other port.
    """
    if not serial:
        return None
    try:
        uhubctl_set_power(loc, port, True)
        if wait_serial_online(serial,
                              charge_cfg.get("adb_wait_seconds", 15),
                              charge_cfg.get("adb_wait_retries", 8),
                              stop_event, recover_loc_port=(loc, port)):
            return get_battery_level(serial)
        if not stop_event.is_set():
            log.warning("drain: ADB timeout for %s on %s:%s", serial, loc, port)
        return None
    finally:
        try:
            uhubctl_set_power(loc, port, False)
        except Exception:
            pass


def _run_workbench_for_web(slot: str, loc: str, port: int, cfg: dict) -> None:
    """
    Workbench mode: the watch is checked out for hands-on work (over
    WiFi/SSH), and the rig babysits its battery in the low–high band instead
    of letting a powered dock peg it at 100%.

    Hysteresis loop: charge (port on, battery polls) until high_threshold,
    then rest (port off, watch on battery) and re-check every
    workbench_poll_minutes; charge again once at/below low_threshold.  If
    the battery can't be read (e.g. USB switched to RNDIS/SSH mode), fall
    back to a blind duty cycle: workbench_blind_charge_minutes of power per
    rest period.  Note the port is unpowered during rest phases — hands-on
    work is expected to happen over WiFi, not the USB link.
    """
    charge_cfg = cfg.get("charge", cfg)
    low   = charge_cfg.get("low_threshold", 40)
    high  = charge_cfg.get("high_threshold", 80)
    rest_sec  = cfg.get("workbench_poll_minutes", 30) * 60
    blind_sec = cfg.get("workbench_blind_charge_minutes", 15) * 60
    codename   = find_codename_for_loc_port(cfg, loc, port) or slot
    stop_event = _workbench_stop[slot]
    task       = _workbench_tasks[slot]

    def _rest_and_recheck(serial: "str | None") -> None:
        task["phase"] = "resting"
        uhubctl_set_power(loc, port, False)
        if stop_event.wait(timeout=rest_sec):
            return
        task["phase"] = "checking"
        uhubctl_set_power(loc, port, True)
        if serial:
            wait_serial_online(serial,
                               charge_cfg.get("adb_wait_seconds", 15), 4,
                               stop_event, recover_loc_port=(loc, port))

    try:
        log.info("%s: workbench mode — holding %d–%d%%", codename, low, high)
        serial = find_serial_for_loc_port(cfg, loc, port)
        task["phase"] = "boot"
        uhubctl_set_power(loc, port, True)
        if serial:
            wait_serial_online(serial,
                               charge_cfg.get("adb_wait_seconds", 15),
                               charge_cfg.get("adb_wait_retries", 8),
                               stop_event, recover_loc_port=(loc, port))
        while not stop_event.is_set():
            lvl = get_battery_level(serial) if serial else None
            if lvl is not None:
                task["pct"], task["blind"] = lvl, False
                _persist_task("workbench", slot, loc, port, task)
                if lvl >= high or (task.get("phase") == "checking" and lvl > low):
                    _rest_and_recheck(serial)
                    continue
                task["phase"] = "charging"
                if stop_event.wait(timeout=_CHARGE_POLL_SEC):
                    break
                _ensure_port_powered(codename, loc, port)
            else:
                # Battery unreadable — blind duty cycle.
                task["blind"] = True
                task["phase"] = "charging (blind)"
                _persist_task("workbench", slot, loc, port, task)
                if stop_event.wait(timeout=blind_sec):
                    break
                _rest_and_recheck(serial)
    except Exception as exc:
        log.warning("%s: workbench failed: %s", codename, exc)
    finally:
        try:
            uhubctl_set_power(loc, port, False)
        except Exception:
            pass
        task["done"] = True
        _workbench_stop.pop(slot, None)
        _unpersist_task("workbench", slot)
        log.info("%s: workbench ended — returned to fleet (port off)", codename)


def _run_drain_for_web(slot: str, loc: str, port: int, cfg: dict) -> None:
    """
    Standby drain test: power off the watch, poll battery every 30 min,
    compute drain rate, stop at floor or on user request.
    Results saved to DRAIN_RESULTS_DIR as JSON.
    """
    charge_cfg = cfg.get("charge", cfg)
    codename   = find_codename_for_loc_port(cfg, loc, port) or slot
    stop_event = _drain_stop[slot]
    task       = _drain_tasks[slot]
    resuming   = bool(task.get("readings"))

    try:
        if resuming:
            # Restored after a restart — keep the existing readings/start_ts and
            # just continue polling. Rate math is timestamp-based so the gap is
            # harmless.
            log.info("%s: drain test resumed at %s%% (%d readings so far)",
                     codename, task.get("last_pct"), len(task["readings"]))
        else:
            log.info("%s: drain test — reading initial battery", codename)
            serial = find_serial_for_loc_port(cfg, loc, port)
            task["serial"] = serial
            start_pct = _adb_read_battery(loc, port, serial, charge_cfg, stop_event)
            if start_pct is None:
                log.warning("%s: drain test: could not read initial battery — aborting", codename)
                # Remove task so the UI doesn't show a stale done/null state.
                _drain_tasks.pop(slot, None)
                _unpersist_task("drain", slot)
                return

            now = time.time()
            task.update({
                "start_ts":   now,
                "start_pct":  start_pct,
                "last_ts":    now,
                "last_pct":   start_pct,
                "drain_rate": None,
                "readings":   [{"ts": now, "pct": start_pct}],
            })
            _persist_task("drain", slot, loc, port, task)
            _log_event(serial, codename, "drain_reading", pct=start_pct)
            log.info("%s: drain test started at %d%%", codename, start_pct)

        while not stop_event.wait(timeout=_DRAIN_POLL_SEC):
            # Reload config in case serial mapping changed.
            serial  = find_serial_for_loc_port(load_config(), loc, port)
            new_pct = _adb_read_battery(loc, port, serial, charge_cfg, stop_event)
            if stop_event.is_set():
                break
            if new_pct is None:
                log.warning("%s: drain test: poll failed — skipping", codename)
                continue

            now = time.time()
            task["readings"].append({"ts": now, "pct": new_pct})
            task["last_ts"]  = now
            task["last_pct"] = new_pct

            elapsed_h = (now - task["start_ts"]) / 3600
            if elapsed_h > 0:
                task["drain_rate"] = (task["start_pct"] - new_pct) / elapsed_h
            _persist_task("drain", slot, loc, port, task)
            _log_event(task.get("serial"), codename, "drain_reading",
                       pct=new_pct, rate=task.get("drain_rate"))
            log.info("%s: drain test: %d%% (%.2f%%/h)", codename,
                     new_pct, task["drain_rate"] or 0)

            if new_pct <= _DRAIN_FLOOR_PCT:
                log.info("%s: drain test: reached floor (%d%%) — done", codename, _DRAIN_FLOOR_PCT)
                break

    except Exception as exc:
        log.warning("%s: drain test failed: %s", codename, exc)
    finally:
        task["done"]    = True
        task["stopped"] = stop_event.is_set()
        _drain_stop.pop(slot, None)
        _unpersist_task("drain", slot)
        # Return the watch to rest: shut it down instead of leaving it
        # draining at the floor.
        _end_port(loc, port, task.get("serial"), charge_cfg, "drain ended")
        _save_drain_results(task, slot, codename)


def _resume_persisted_tasks() -> None:
    """On web startup, re-spawn workers for any op that was running when the
    service last stopped, so charge/drain/workbench survive restarts."""
    cfg = load_config()
    hub_locs = {h["location"] for h in cfg.get("hubs", [])}
    runners = {"charge": (_charge_tasks, _charge_stop, _run_charge_for_web),
               "drain":  (_drain_tasks,  _drain_stop,  _run_drain_for_web),
               "workbench": (_workbench_tasks, _workbench_stop, _run_workbench_for_web)}
    resumed = 0
    for p in _load_persisted_tasks():
        kind = p.get("kind"); slot = p.get("slot")
        loc = p.get("loc"); port = p.get("port"); task = p.get("task") or {}
        if kind not in runners or not slot:
            _unpersist_task(kind or "?", slot or "?")
            continue
        if task.get("done") or loc not in hub_locs:
            # already finished, or its hub is gone (config changed) — drop it.
            if loc not in hub_locs:
                log.warning("resume: hub %s missing, dropping %s task %s",
                            loc, kind, slot)
            _unpersist_task(kind, slot)
            continue
        tasks, stops, runner = runners[kind]
        task["done"] = False
        tasks[slot] = task
        stops[slot] = threading.Event()
        threading.Thread(target=runner, args=(slot, loc, port, cfg),
                         daemon=True).start()
        resumed += 1
        log.info("resumed %s op on %s:%s", kind, loc, port)
    if resumed:
        log.info("resumed %d operation(s) from previous run", resumed)


def _flash_one_watch(
    codename: str,
    cfg: dict,
    flash_cfg: dict,
    dry_run: bool = False,
    local_dir: "Path | None" = None,
    force_dl: bool = False,
) -> str:
    """
    Flash one watch end-to-end. Progress is reported via log.*; returns "ok"
    or a short error string. Used by both cmd_flash_all and the web UI SSE handler.
    """
    loc, port = find_port_for_codename(cfg, codename)
    if loc is None:
        log.error("%s: not mapped to any hub port", codename)
        return "not mapped"
    if is_port_smart(cfg, codename) is False:
        log.error("%s: port not power-switchable — skipping", codename)
        return "non-smart port"

    dl_dir = Path(flash_cfg["download_dir"])
    nightly_url = flash_cfg["nightly_url"]

    if local_dir:
        boot_file = local_dir / f"zImage-dtb-{codename}.fastboot"
        img_file  = local_dir / f"asteroid-image-{codename}.rootfs.ext4"
        missing = [f for f in (boot_file, img_file) if not f.exists()]
        if missing:
            log.error("%s: not found in --local: %s", codename, ", ".join(f.name for f in missing))
            return "local images not found"
        log.info("%s: using local images from %s", codename, local_dir)
    else:
        try:
            boot_file, img_file = _download_nightly(codename, dl_dir, nightly_url, force=force_dl)
        except Exception as e:
            log.error("%s: download failed: %s", codename, e)
            return "download failed"

    log.info("%s: powering on port %s:%d", codename, loc, port)
    if not dry_run:
        uhubctl_set_power(loc, port, True)

    serial = None
    if not dry_run:
        serial = wait_for_adb(codename, cfg, cfg["charge"])
        if serial is None:
            if _detect_rndis():
                log.warning(
                    "%s: watch at 192.168.2.15 (SSH/RNDIS mode) — "
                    "switch to ADB mode: Settings → USB on the watch",
                    codename,
                )
                serial = wait_for_adb(codename, cfg, cfg["charge"])
            if serial is None:
                log.error("%s: ADB not available — skipping", codename)
                uhubctl_set_power(loc, port, False)
                return "ADB unavailable"

    log.info("%s: rebooting to bootloader…", codename)
    before_fb = set(_fastboot_devices().keys())
    if not dry_run:
        _run(f"adb -s {serial} reboot bootloader", check=False)

    fb_serial = None
    if not dry_run:
        fb_serial = _wait_for_fastboot(before_fb, timeout=30)
        if fb_serial is None:
            log.error("%s: no fastboot device appeared — skipping", codename)
            return "fastboot timeout"
        log.info("%s: fastboot device ready (%s)", codename, fb_serial)

    try:
        _flash_watch(boot_file, img_file, fb_serial, dry_run=dry_run)
    except RuntimeError as e:
        log.error("%s: flash failed: %s", codename, e)
        return "flash failed"

    if not dry_run:
        _clear_ssh_known_hosts()

    log.info("%s: done — watch is rebooting into AsteroidOS", codename)
    return "ok"


