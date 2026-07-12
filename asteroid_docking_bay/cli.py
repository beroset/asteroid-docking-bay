# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""argparse commands and the CLI entry point."""

import argparse
import dataclasses
import sys
import time
from pathlib import Path

from .util import log, setup_logging
from .adb import (_adb_state, _wait_for_new_adb_device, adb_devices,
                  get_battery_level, get_watch_codename)
from .config import (CONFIG_FILE, ChargeConfig, charge_config, flash_config,
                     _resolve_targets, find_port_for_codename,
                     find_serial_for_codename, find_serial_for_loc_port,
                     is_port_smart, load_config, save_config)
from .usb import (port_foreign_device, test_port_power_switching,
                  uhubctl_get_power, uhubctl_list, uhubctl_set_power)
from .events import event_log
from .fastboot import _fastboot_devices
from .ops import _flash_one_watch, charge_to_target
from .watchctl import wait_for_adb


def cmd_serve(args, cfg: dict):
    """Start the web UI (bottle imported lazily inside webapp.serve)."""
    from .webapp import serve
    serve(args, cfg)


def cmd_serve_backend(args, cfg: dict):
    """Start the RPC backend: the host-touching half of the container split.
    Owns the operations (resume + cache warmer) and serves the op table over
    a token-gated socket. No bottle dependency."""
    import threading
    from .rpc import RpcServer, load_token
    from . import rpcops
    from .usb import _sysfs_switch_mode
    from .webapp import _background_warmer
    from .ops import _resume_persisted_tasks

    token = load_token(args.token_file)
    if not token:
        log.error("serve-backend requires a token: pass --token-file PATH "
                  "or set ADB_RPC_TOKEN")
        sys.exit(1)

    _resume_persisted_tasks()
    threading.Thread(target=_background_warmer, daemon=True).start()
    log.info("Port switching: %s", _sysfs_switch_mode(cfg))
    log.info("RPC backend starting on %s:%d", args.host, args.port)
    try:
        RpcServer(args.host, args.port, token, rpcops.DISPATCH).serve_forever()
    except KeyboardInterrupt:
        log.info("RPC backend stopped.")


def cmd_status(args, cfg: dict):
    devices = adb_devices()
    rows: list[tuple] = []
    seen_serials: set[str] = set()
    has_dumb_port = False

    for hub in cfg.get("hubs", []):
        loc = hub["location"]
        for port_str, codename in sorted(hub.get("ports", {}).items(), key=lambda x: int(x[0])):
            port = int(port_str)
            power = uhubctl_get_power(loc, port)
            power_str = "ON " if power is True else ("OFF" if power is False else "---")

            smart = hub.get("port_smart", {}).get(port_str)
            if smart is True:
                smart_str = "yes"
            elif smart is False:
                smart_str = "NO!"
                has_dumb_port = True
            else:
                smart_str = "?"

            serial = find_serial_for_codename(cfg, codename)
            adb_state = (_adb_state(devices, serial) or "--") if serial else "--"
            if serial:
                seen_serials.add(serial)

            battery_str = "--"
            if adb_state == "device":
                level = get_battery_level(serial)
                battery_str = f"{level}%" if level is not None else "err"

            rows.append((codename, f"{loc}:p{port}", power_str, smart_str, adb_state, battery_str))

    # Show ADB-visible watches not yet mapped to a hub port.
    for serial, state in devices.items():
        if serial in seen_serials:
            continue
        codename = cfg.get("serials", {}).get(serial) or get_watch_codename(serial) or serial
        rows.append((codename, "(unmapped)", "---", "--", state, "--"))

    if not rows:
        print("No watches configured.  Run: asteroid-docking-bay map")
        return

    hdr = f"{'WATCH':<16}  {'PORT':<14}  {'POWER':<5}  {'SMART':<5}  {'ADB':<14}  BATTERY"
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for name, port, power, smart, adb, bat in rows:
        print(f"{name:<16}  {port:<14}  {power:<5}  {smart:<5}  {adb:<14}  {bat}")

    if has_dumb_port:
        print("\nNOTE: ports marked 'NO!' do not support power switching.")
        print("      Power on/off commands have no effect. Move to a smart hub port,")
        print("      or re-run 'asteroid-docking-bay test-ports' to recheck.")


def cmd_on(args, cfg: dict):
    for codename in _resolve_targets(args.codename, cfg):
        loc, port = find_port_for_codename(cfg, codename)
        if loc is None:
            log.error("%s: not mapped to any hub port (run: asteroid-docking-bay map)", codename)
            continue
        smart = is_port_smart(cfg, codename)
        if smart is False:
            log.warning("%s: port is NOT power-switchable — command will have no effect", codename)
        elif smart is None:
            log.warning("%s: port switching not tested — run 'test-ports' to verify", codename)
        log.info("%s: powering on hub %s port %d", codename, loc, port)
        uhubctl_set_power(loc, port, True)
        print(f"{codename}: hub {loc} port {port} → ON")


def cmd_off(args, cfg: dict):
    targets = _resolve_targets(args.codename, cfg)

    if args.codename == "all" and not args.force:
        ans = input(f"Power off ALL {len(targets)} configured watches? [y/N] ").strip().lower()
        if ans != "y":
            print("Aborted.")
            return

    for codename in targets:
        loc, port = find_port_for_codename(cfg, codename)
        if loc is None:
            log.error("%s: not mapped to any hub port", codename)
            continue
        smart = is_port_smart(cfg, codename)
        if smart is False:
            # Aborting here is important: silently failing to power off a port
            # would leave the user thinking the watch is off when it is still on.
            log.error(
                "%s: port is NOT power-switchable — refusing 'off' to avoid confusion.\n"
                "  The watch would remain powered on. Move it to a smart hub port.",
                codename,
            )
            continue
        if smart is None:
            log.warning("%s: port switching not tested — run 'test-ports' to verify first", codename)
        log.info("%s: powering off hub %s port %d", codename, loc, port)
        uhubctl_set_power(loc, port, False)
        print(f"{codename}: hub {loc} port {port} → OFF")


def cmd_cycle(args, cfg: dict):
    wait = args.wait
    for codename in _resolve_targets(args.codename, cfg):
        loc, port = find_port_for_codename(cfg, codename)
        if loc is None:
            log.error("%s: not mapped to any hub port", codename)
            continue
        smart = is_port_smart(cfg, codename)
        if smart is False:
            log.error(
                "%s: port is NOT power-switchable — cycle has no effect. Skipping.",
                codename,
            )
            continue
        if smart is None:
            log.warning("%s: port switching not tested — run 'test-ports' to verify first", codename)
        log.info("%s: cycling hub %s port %d (off for %ds)", codename, loc, port, wait)
        uhubctl_set_power(loc, port, False)
        print(f"{codename}: OFF — waiting {wait}s…", flush=True)
        time.sleep(wait)
        uhubctl_set_power(loc, port, True)
        print(f"{codename}: ON")


def cmd_charge(args, cfg: dict):
    """Manual one-time charge cycle, bypassing the low_threshold check.
    Charges to high_threshold unless --duration forces a timed charge."""
    charge_cfg = charge_config(cfg)
    for codename in _resolve_targets(args.codename, cfg):
        _charge_one(codename, cfg, charge_cfg,
                    duration_minutes=args.duration, force=True)


def _charge_one(
    codename: str,
    cfg: dict,
    charge_cfg: ChargeConfig,
    duration_minutes: int | None = None,
    force: bool = False,
) -> bool:
    """
    Power on a watch and charge it, then power off.
    Returns True on success, False if the port is not mapped.

    Default is charge-to-target: poll the battery and stop at high_threshold.
    An explicit duration_minutes forces a timed charge instead.  If the port
    is confirmed non-smart, power cycling is skipped but ADB is still used to
    read the battery level (useful even without switching capability).
    """
    loc, port = find_port_for_codename(cfg, codename)
    if loc is None:
        log.error("%s: not mapped to any hub port", codename)
        return False

    smart = is_port_smart(cfg, codename)
    can_switch = smart is not False

    high = charge_cfg.high_threshold

    if not can_switch:
        log.warning(
            "%s: port is NOT power-switchable — skipping power cycle, battery check only",
            codename,
        )
    else:
        if smart is None:
            log.warning("%s: port switching not tested — proceeding, but verify with 'test-ports'", codename)
        log.info("%s: starting charge cycle (hub %s port %d)", codename, loc, port)
        uhubctl_set_power(loc, port, True)

    serial = wait_for_adb(codename, cfg, charge_cfg)

    if serial:
        level = get_battery_level(serial)
        if level is not None:
            log.info("%s: battery at %d%%", codename, level)
            if not force and level >= high:
                log.info("%s: already at %d%% (≥%d%%), skipping charge", codename, level, high)
                if can_switch:
                    uhubctl_set_power(loc, port, False)
                return True
    elif not can_switch:
        log.warning("%s: ADB unavailable and port is non-smart — nothing to do", codename)
        return True

    if can_switch:
        if duration_minutes is not None:
            log.info("%s: timed charge for %d minutes…", codename, duration_minutes)
            time.sleep(duration_minutes * 60)
            if serial:
                level_after = get_battery_level(serial)
                if level_after is not None:
                    log.info("%s: battery now at %d%% after charging", codename, level_after)
        else:
            charge_to_target(codename, serial, charge_cfg, loc, port)

        uhubctl_set_power(loc, port, False)
        log.info("%s: port powered off, charge cycle complete", codename)

    return True


def cmd_check_charge(args, cfg: dict):
    """
    Periodic charge check — called by the systemd timer.

    For each configured watch:
      - Wake it (power on).
      - Wait for ADB.
      - If battery < low_threshold: charge to high_threshold
        (charge_to_target; blind charge_duration_minutes fallback).
      - Power back off.
    Watches that are already powered on have their battery checked in-place;
    if they are above high_threshold they are powered down.
    """
    charge_cfg = charge_config(cfg)
    low = charge_cfg.low_threshold
    high = charge_cfg.high_threshold

    log.info("Periodic charge check starting")

    for hub in cfg.get("hubs", []):
        loc = hub["location"]
        for port_str, codename in hub.get("ports", {}).items():
            port = int(port_str)
            smart = hub.get("port_smart", {}).get(port_str)
            if smart is False:
                log.info(
                    "%s: skipping — port %s:%d is not power-switchable",
                    codename, loc, port,
                )
                continue
            if smart is None:
                log.warning(
                    "%s: port switching not tested — proceeding but results may be unreliable. "
                    "Run 'test-ports' to verify.",
                    codename,
                )
            power_was_on = uhubctl_get_power(loc, port)

            # Adaptive cadence: if the watch is off and its observed standby
            # drain projects it is not yet near low_threshold, skip waking it.
            if charge_cfg.adaptive_cadence and not power_was_on:
                known_serial = find_serial_for_loc_port(cfg, loc, port)
                due = event_log.next_due_ts(known_serial, codename, cfg)
                if due is not None and time.time() < due:
                    rate = event_log.standby_loss_rate(known_serial, codename) or 0
                    log.info("%s: not due — skipping (next in ~%.0f h, "
                             "standby %.2f%%/h)", codename,
                             (due - time.time()) / 3600, rate)
                    continue

            if not power_was_on:
                log.info("%s: waking to check battery", codename)
                uhubctl_set_power(loc, port, True)

            serial = wait_for_adb(codename, cfg, charge_cfg)

            if serial is None:
                if not power_was_on:
                    # Left powered on — operator should inspect.
                    log.warning(
                        "%s: ADB unavailable after wake — leaving port ON for manual check",
                        codename,
                    )
                continue

            level = get_battery_level(serial)
            if level is None:
                log.warning("%s: could not read battery level — leaving port state unchanged", codename)
                continue

            log.info("%s: battery at %d%%", codename, level)
            event_log.log(serial, codename, "check_reading", pct=level)

            if level >= high and power_was_on:
                log.info("%s: at %d%% (≥%d%%), powering off", codename, level, high)
                uhubctl_set_power(loc, port, False)
            elif level < low:
                log.info("%s: at %d%% (<%d%%), charging to %d%%", codename, level, low, high)
                charge_to_target(codename, serial, charge_cfg, loc, port)
                uhubctl_set_power(loc, port, False)
                log.info("%s: port powered off", codename)
            else:
                log.info("%s: battery OK (%d%% — between %d%%–%d%%)", codename, level, low, high)
                if not power_was_on:
                    uhubctl_set_power(loc, port, False)
                    log.info("%s: port powered off (was off before check)", codename)

    log.info("Periodic charge check complete")


def cmd_flash_all(args, cfg: dict):
    """
    Flash AsteroidOS nightlies to all (or a specified) configured watches.

    For each watch in sequence:
      1. Download + verify nightly images (cached; skips if already current).
      2. Power on the hub port.
      3. Wait for ADB.  If the watch is in SSH/RNDIS mode, alert the user.
      4. adb reboot bootloader.
      5. Wait for fastboot device.
      6. fastboot flash userdata + boot, then fastboot continue.
      7. Remove stale SSH known_hosts entries for the next SSH session.
    """
    targets = _resolve_targets(args.codename, cfg)
    if not targets:
        print("No watches configured. Run: asteroid-docking-bay map")
        return

    dry_run   = args.dry_run
    local_dir = Path(args.local) if args.local else None
    flash_cfg = flash_config(cfg)
    if args.download_dir:
        flash_cfg = dataclasses.replace(flash_cfg, download_dir=args.download_dir)

    print()
    print("   ✨  ⋆  ˚  ✦   asteroid-docking-bay   ✦  ˚  ⋆  ✨")
    print("   ──────────────── flash sequence ─────────────────")
    print(f"          {len(targets)} watch(es) queued  {'[DRY RUN]' if dry_run else ''}")
    print()

    results: dict[str, str] = {}
    for codename in targets:
        loc, port = find_port_for_codename(cfg, codename)
        if loc is not None:
            print(f"\n── {codename}  (hub {loc} port {port}) ──")
        results[codename] = _flash_one_watch(
            codename, cfg, flash_cfg,
            dry_run=dry_run,
            local_dir=local_dir,
            force_dl=args.force_download,
        )

    print("\n=== Flash results ===\n")
    for codename, result in results.items():
        icon = "✓" if result == "ok" else "✗"
        print(f"  {icon}  {codename:<16}  {result}")
    if any(r != "ok" for r in results.values()):
        print("\nFailed watches remain powered on for manual inspection.")



def cmd_map(args, cfg: dict):
    """
    Automatic port mapping: power each port on for up to 10 seconds, read
    the codename from whatever watch appears in ADB, run PPPS test.
    No prompts mid-run.  Summary at the end lists what was found and which
    ports had no response (empty or ADB not working).
    """
    print()
    print("   ✨  ⋆  ˚  ✦   asteroid-docking-bay   ✦  ˚  ⋆  ✨")
    print("   ───── automatic port mapping & battery care ─────")
    print("          Docking sequence initialized...")
    print()

    hubs = uhubctl_list()
    # USB 3.0 companion buses expose the same physical ports as their USB 2.0
    # counterparts. Watches are USB 2.0 devices and only enumerate on the 2.x bus.
    # Scanning 3.x hubs causes double-detection and incorrect PPPS results.
    hubs = [h for h in hubs if ", USB 3." not in h.get("description", "")]
    if not hubs:
        print("No USB hubs found by uhubctl.")
        print("See udev/70-asteroid-docking-bay.rules for permission setup.")
        return

    print(f"Found {len(hubs)} hub(s):")
    for hub in hubs:
        ppps_note = "PPPS advertised" if hub.get("ppps") else "PPPS not advertised"
        print(f"  {hub['location']:12}  {hub['description']}  [{ppps_note}]")
    print()

    ans = input("Power off all ports and start mapping? [Y/n] ").strip().lower()
    if ans not in ("", "y", "yes"):
        print("Aborted.")
        return

    # Identify cascade ports: ports on a parent hub whose child hub is in our list.
    # e.g. hub "1-2.4" → parent hub "1-2" owns port 4 as a cascade port.
    # We must not power off cascade ports during the all-off phase — doing so would
    # kill the sub-hub before we can scan it.
    cascade_ports: dict[str, set[int]] = {}  # hub_location → set of cascade port numbers
    for hub in hubs:
        loc = hub["location"]
        if "." in loc:
            parent_loc, _, parent_port_str = loc.rpartition(".")
            cascade_ports.setdefault(parent_loc, set()).add(int(parent_port_str))

    # Baseline of foreign devices: any port with an enumerated non-watch
    # device (keyboard, mouse, dock peripheral, an unswitchable sub-hub) is
    # off-limits — map must never cut power to something it can't identify
    # as a watch. adb/fastboot-visible serials count as watches even under
    # non-standard vendor IDs (hacked or vendor-specific USB identities).
    watch_serials = set(adb_devices().keys()) | set(_fastboot_devices().keys())
    foreign: dict[tuple, str] = {}
    for hub in hubs:
        for port in hub["ports"]:
            desc = port_foreign_device(hub["location"], port, watch_serials)
            if desc:
                foreign[(hub["location"], port)] = desc
    if foreign:
        print("Leaving non-watch devices untouched:")
        for (loc, port), desc in sorted(foreign.items()):
            print(f"  {loc}:p{port}  {desc}")

    print("\nPowering off all ports…")
    for hub in hubs:
        skip = cascade_ports.get(hub["location"], set())
        for port in hub["ports"]:
            if port in skip or (hub["location"], port) in foreign:
                continue
            try:
                uhubctl_set_power(hub["location"], port, False)
            except RuntimeError as e:
                log.warning("hub %s port %d: %s", hub["location"], port, e)
    time.sleep(2)

    new_hubs: list[dict] = []
    no_response: list[str] = []   # ports where no watch appeared

    # Track serials already assigned so a reconnecting watch from a previous
    # port's PPPS cycle doesn't get double-detected on the next port.
    known_serials: set[str] = set(adb_devices().keys())

    # Scan deepest child hubs first so parent cascade ports stay live throughout.
    hubs_sorted = sorted(hubs, key=lambda h: h["location"].count("."), reverse=True)

    for hub in hubs_sorted:
        loc = hub["location"]
        print(f"\n── Hub {loc}  {hub['description']} ──")

        port_map: dict[str, str] = {}
        port_smart: dict[str, bool] = {}
        port_serials: dict[str, str] = {}

        skip = cascade_ports.get(loc, set())
        for port in hub["ports"]:
            if port in skip:
                print(f"  Port {port}: (cascade → sub-hub {loc}.{port})")
                continue
            if (loc, port) in foreign:
                print(f"  Port {port}: (skipped — {foreign[(loc, port)]})")
                continue

            print(f"  Port {port}: ", end="", flush=True)

            # Use known_serials as baseline: excludes watches already mapped on
            # earlier ports even if they're still reconnecting after a PPPS cycle.
            before = set(adb_devices().keys()) | known_serials
            uhubctl_set_power(loc, port, True)

            serial = _wait_for_new_adb_device(before, timeout=10)

            if serial:
                codename = get_watch_codename(serial) or serial
                print(f"{codename}  ({serial})", end="", flush=True)
                port_map[str(port)] = codename
                port_serials[str(port)] = serial
                cfg.setdefault("serials", {})[serial] = codename
                known_serials.add(serial)

                # PPPS test inline — port is ON, takes up to ~30 s.
                print(f"  …testing PPPS…", end="", flush=True)
                try:
                    smart, msg = test_port_power_switching(loc, port, serial)
                except RuntimeError as e:
                    smart, msg = False, str(e)
                port_smart[str(port)] = smart
                print(f"  {'[smart]' if smart else '[NOT SMART]' if smart is False else '[unverified]'}")
            else:
                print("(no response)")
                no_response.append(f"{loc}:p{port}")
                uhubctl_set_power(loc, port, False)

        new_hubs.append({
            "location": loc,
            "ppps": hub.get("ppps", False),
            "port_smart": port_smart,
            "ports": port_map,
            "port_serials": port_serials,
        })

    touched_locs = {h["location"] for h in new_hubs}
    new_codenames = {cn for h in new_hubs for cn in h.get("ports", {}).values()}
    # Preserve old hub entries only when they contain codenames NOT found in this
    # scan.  An old entry whose watches all reappeared at a new location is stale
    # (hub moved to a different USB path) and should be dropped to avoid duplicates.
    cfg["hubs"] = new_hubs + [
        h for h in cfg.get("hubs", [])
        if h["location"] not in touched_locs
        and not any(cn in new_codenames for cn in h.get("ports", {}).values())
    ]
    save_config(cfg)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n=== Results ===\n")

    any_non_smart = False
    for hub in cfg["hubs"]:
        for port_str, codename in sorted(hub["ports"].items(), key=lambda x: int(x[0])):
            smart = hub.get("port_smart", {}).get(port_str)
            tag = "smart" if smart is True else ("NOT SMART" if smart is False else "?")
            if smart is False:
                any_non_smart = True
            print(f"  {hub['location']}:p{port_str}  →  {codename:<16}  [{tag}]")

    if no_response:
        print(f"\n  No watch detected on:")
        for port_id in no_response:
            print(f"    {port_id}")
        print("  (empty port, disconnected cable, or ADB not enabled on the watch)")

    if any_non_smart:
        print("\nWARNING: ports marked NOT SMART cannot be power-managed.")

    print(f"\nConfig saved to {CONFIG_FILE}")
    print("Run 'asteroid-docking-bay status' to verify.")


def cmd_test_ports(args, cfg: dict):
    """
    Re-test per-port power switching for all configured ports (or a specific watch).
    Updates the config with the results.  Useful after moving watches between ports
    or if the initial 'map' test was skipped.
    """
    wanted = _resolve_targets(args.codename, cfg)
    if not wanted:
        print("No watches configured. Run: asteroid-docking-bay map")
        return

    # Iterate mapped ports, not codenames: a codename can be mapped on more
    # than one port (two units of the same watch), and a per-codename lookup
    # only ever finds the first.
    targets = [(hub["location"], int(p), cn)
               for hub in cfg.get("hubs", [])
               for p, cn in hub.get("ports", {}).items()
               if cn in wanted]

    print(f"Testing per-port power switching for: "
          f"{', '.join(f'{cn}@{loc}:p{port}' for loc, port, cn in targets)}")
    print("Each port is toggled off then on (up to ~30 s per port).\n")

    results: list[tuple[str, str, int, bool, str]] = []

    for loc, port, codename in targets:
        print(f"  {codename} (hub {loc} port {port})… ", end="", flush=True)
        try:
            smart, msg = test_port_power_switching(
                loc, port, find_serial_for_loc_port(cfg, loc, port))
        except RuntimeError as e:
            smart, msg = False, str(e)

        icon = ("SMART  [OK]" if smart
                else "NOT SMART [!!]" if smart is False else "UNVERIFIED [?]")
        print(f"{icon}  — {msg}")
        results.append((codename, loc, port, smart, msg))

        for hub in cfg.get("hubs", []):
            if hub["location"] == loc:
                hub.setdefault("port_smart", {})[str(port)] = smart
                break

    if results:
        save_config(cfg)
        print(f"\nResults saved to {CONFIG_FILE}")

        non_smart = [(c, loc, p) for c, loc, p, s, _ in results if s is False]
        if non_smart:
            print("\nNOT power-switchable:")
            for codename, loc, port in non_smart:
                print(f"  • {codename} (hub {loc} port {port})")
            print("Battery management requires moving these watches to a smart hub port.")


def cmd_discover(args, cfg: dict):
    """Scan for ADB-connected watches and show codename + serial."""
    print("Scanning for ADB-connected watches…\n")
    devices = adb_devices()
    if not devices:
        print("No ADB devices found.")
        return

    print(f"{'SERIAL':<24}  {'STATE':<14} {'USB':<12} {'ASTEROID':<10} CODENAME")
    print("-" * 74)
    for serial, state in sorted(devices.items()):
        usb = codename = "--"
        asteroid = False
        if state['status'] == "device":
            usb = state.get('usb', '--')
            try:
                codename = state['product']
            except KeyError:
                codename = get_watch_codename(serial)
                if codename:
                    asteroid = True
                else:
                    codename = "--"
        known = cfg.get("serials", {}).get(serial)
        if known and known != codename:
            codename += f"  (config: {known})"
        print(f"{serial:<24}  {state['status']:<14} {usb:<12} {asteroid!s:10} {codename}")



# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="asteroid-docking-bay",
        description="USB hub power manager for AsteroidOS smartwatches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Hub mapping:    asteroid-docking-bay map\n"
            "Quick start:    asteroid-docking-bay status\n"
            "Config file:    ~/.config/asteroid-docking-bay/config.json\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose/debug output")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    sub.add_parser("status", help="show all watches: port, power, ADB state, battery")

    p_on = sub.add_parser("on", help="power on a watch's USB port")
    p_on.add_argument("codename", help="watch codename, or 'all'")

    p_off = sub.add_parser("off", help="power off a watch's USB port")
    p_off.add_argument("codename", help="watch codename, or 'all'")
    p_off.add_argument("-f", "--force", action="store_true", help="skip confirmation for 'all'")

    p_cy = sub.add_parser("cycle", help="power-cycle a watch's USB port")
    p_cy.add_argument("codename", help="watch codename, or 'all'")
    p_cy.add_argument("--wait", type=int, default=5, metavar="SEC",
                      help="seconds to leave port off (default: 5)")

    p_ch = sub.add_parser("charge", help="manual one-time charge cycle (ignores high_threshold)")
    p_ch.add_argument("codename", help="watch codename, or 'all'")
    p_ch.add_argument("--duration", type=int, metavar="MIN",
                      help="charge duration in minutes (overrides config)")

    sub.add_parser(
        "check-charge",
        help="periodic charge check — run by systemd timer, safe to run manually",
    )

    sub.add_parser("map", help="interactive wizard: assign codenames, test switching, discover serials")

    p_tp = sub.add_parser(
        "test-ports",
        help="re-test per-port power switching for configured ports",
    )
    p_tp.add_argument(
        "codename", nargs="?", default="all",
        help="watch codename, or 'all' (default)",
    )

    sub.add_parser("discover", help="scan for ADB-connected watches and show codenames")

    p_sv = sub.add_parser("serve", help="start the web UI (requires the bottle package)")
    p_sv.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help="bind address (default: 127.0.0.1)",
    )
    p_sv.add_argument(
        "--port", type=int, default=8080, metavar="PORT",
        help="port to listen on (default: 8080)",
    )
    p_sv.add_argument(
        "--backend", metavar="HOST:PORT",
        help="proxy all operations to a remote RPC backend (split mode) "
             "instead of running them in-process",
    )
    p_sv.add_argument(
        "--token-file", metavar="PATH",
        help="shared secret for --backend (else the ADB_RPC_TOKEN env var)",
    )

    p_sb = sub.add_parser(
        "serve-backend",
        help="start the RPC backend (host-touching half of the container split)",
    )
    p_sb.add_argument("--host", default="127.0.0.1", metavar="HOST",
                      help="bind address (default: 127.0.0.1)")
    p_sb.add_argument("--port", type=int, default=8099, metavar="PORT",
                      help="RPC port to listen on (default: 8099)")
    p_sb.add_argument("--token-file", metavar="PATH",
                      help="file holding the shared secret token "
                           "(else read from the ADB_RPC_TOKEN env var)")

    p_fa = sub.add_parser(
        "flash",
        help="flash AsteroidOS nightlies to all (or a specified) configured watches",
    )
    p_fa.add_argument(
        "codename", nargs="?", default="all",
        help="watch codename, or 'all' (default)",
    )
    p_fa.add_argument(
        "--local", metavar="DIR",
        help="use image files from DIR instead of downloading nightlies",
    )
    p_fa.add_argument(
        "--dry-run", action="store_true",
        help="print fastboot commands without executing them",
    )
    p_fa.add_argument(
        "--force-download", action="store_true",
        help="re-download images even if the cached copy passes SHA512 verification",
    )
    p_fa.add_argument(
        "--download-dir", metavar="DIR",
        help="override the nightly download cache directory",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)
    cfg = load_config()

    dispatch = {
        "status": cmd_status,
        "on": cmd_on,
        "off": cmd_off,
        "cycle": cmd_cycle,
        "charge": cmd_charge,
        "check-charge": cmd_check_charge,
        "map": cmd_map,
        "test-ports": cmd_test_ports,
        "discover": cmd_discover,
        "flash": cmd_flash_all,
        "serve": cmd_serve,
        "serve-backend": cmd_serve_backend,
    }

    try:
        dispatch[args.command](args, cfg)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()


