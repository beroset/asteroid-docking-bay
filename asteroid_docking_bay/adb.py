# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""ADB primitives: device list parsing, per-serial state, shell, battery."""

from __future__ import annotations

import time

from .util import _run, log


def parse_adb_devices(out: str) -> dict[str, dict]:
    """Parse `adb devices -l` output into {serial: {"status": …, key: value}}.
    The -l extras (usb:, product:, model:, device:, transport_id:) become
    dict keys; bare tokens collect under "_tokens". Device lines follow the
    "List of devices attached" header; anything before it (daemon-restart
    notices) and `*`-prefixed lines are noise, not devices. Pure — see tests."""
    result: dict[str, dict] = {}
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("List of devices"):
            lines = lines[i + 1:]
            break
    else:
        lines = lines[1:]
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or parts[0].startswith("*"):
            continue
        serial, status = parts[0], parts[1]
        parsed: dict[str, str | list[str]] = {}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                parsed[k] = v
            else:
                parsed.setdefault("_tokens", []).append(token)
        result[serial] = {"status": status, **parsed}
    return result


# `adb devices -l` answers in well under a second, and in a disconnect storm in
# a few. This is not a latency budget — it is the bound that stops a hung adb
# server from blocking a thread FOREVER, because _run defaults to no timeout at
# all. Deliberately far longer than any healthy or degraded call, so it can only
# ever fire where the alternative was hanging.
#
# Safe to bound precisely because the failure is already handled correctly: a
# timeout returns rc != 0, which this reads as "the call failed" (None) rather
# than "no devices" — and _wait_adb_state discards failed samples instead of
# treating them as a watch that vanished.
_ADB_DEVICES_TIMEOUT = 20.0


def adb_devices_checked() -> dict[str, str | list[str]] | None:
    """Like adb_devices, but None when the adb call itself failed (server
    crash / not installed / hung) — distinct from a genuinely empty device
    list."""
    rc, out, err = _run("adb devices -l", check=False,
                        timeout=_ADB_DEVICES_TIMEOUT)
    if rc != 0:
        log.warning("adb devices failed (rc=%s): %s", rc, err.strip() or "no stderr")
        return None
    return parse_adb_devices(out)


def adb_devices() -> dict[str, str | list[str]]:
    """Return {serial: state} for all ADB-visible devices ({} on failure)."""
    return adb_devices_checked() or {}


_last_adb_heal = 0.0
_prev_adb_missing: set[str] = set()
_ADB_HEAL_COOLDOWN = 60.0


def maybe_heal_wedged_adb() -> bool:
    """Restart the adb server if it has wedged — listing nothing while watches
    are enumerated in adb gadget mode.

    Seen live 2026-07-24: powered ports, LEDs lit, watches enumerated in sysfs,
    yet `adb devices` empty; kill/start-server recovered it, and there was no
    self-heal (audit A2). Detect the wedge as adb-mode serials present in sysfs
    that the server is blind to, require it to PERSIST across two checks (so a
    just-plugged watch mid-enumeration is not mistaken for a wedge), and restart
    at most once per cooldown (a restart briefly drops every watch). Returns True
    on a restart."""
    global _last_adb_heal, _prev_adb_missing
    from .usb import _sysfs_adb_serials
    on_bus  = _sysfs_adb_serials()
    seen    = set(adb_devices_checked() or {})
    missing = on_bus - seen
    persistent = missing & _prev_adb_missing
    _prev_adb_missing = missing
    if not persistent:
        return False
    now = time.time()
    if now - _last_adb_heal < _ADB_HEAL_COOLDOWN:
        return False
    # A dump streams through the local adb server (`adb -s X exec-out dd`), so
    # kill-server severs it mid-copy. The wedge that triggers this heal can be
    # spurious — one watch enumerating oddly can hide others from the server for
    # two checks — and killing a 4 GB read to fix a maybe-wedge is the wrong
    # trade. Defer while any watch is held; the lock's TTL bounds the wait, and
    # a genuine wedge is still healed on a later pass. Imported here because
    # config imports this module.
    from .config import load_config
    from . import oplock
    holders = oplock.all_held(load_config())
    if holders:
        _last_adb_heal = now          # rate-limit this notice like a real heal
        log.warning("adb server looks wedged, but %s held for %s — NOT "
                    "restarting the server, which would sever a transfer in "
                    "flight; will retry once released",
                    ", ".join(sorted(holders)),
                    ", ".join(sorted({h.get("kind", "?") for h in holders.values()})))
        return False
    _last_adb_heal = now
    log.warning("adb server wedged: %d adb-mode watch(es) on the bus but not "
                "listed by adb (%s) — restarting adb server",
                len(persistent), ", ".join(sorted(persistent)))
    _run("adb kill-server", check=False, timeout=8)
    _run("adb start-server", check=False, timeout=8)
    return True


def _adb_state(devices: dict, serial: "str | None") -> "str | None":
    """State string ('device'/'offline'/…) for a serial in an adb_devices() map,
    or None if the serial isn't present. None-safe: adb_devices() values are now
    per-device dicts (`{"status": …, "usb": …}`), and `devices.get(serial)` is None
    for an absent/offline watch — `None['status']` would raise, so callers go
    through here instead of indexing the entry directly."""
    entry = devices.get(serial)
    if isinstance(entry, dict):
        return entry.get("status")
    return entry  # None, or a plain string (defensive)


def _resolve_conn_state(adb_status: "str | None", in_fastboot: bool,
                        ssh_probe) -> "str | None":
    """Row connection state, in priority order: a live adb status (device/
    offline/…) wins; else 'fastboot' if the watch is in the bootloader; else
    'ssh' if it enumerated in SSH/developer USB mode; else None (nothing there).

    ssh_probe is a zero-arg callable, evaluated only when the cheaper checks
    fail — it does a sysfs read, and the status builder must not pay for it on
    every already-on-adb port. Pure given its inputs — see tests."""
    if adb_status is not None:
        return adb_status
    if in_fastboot:
        return "fastboot"
    if ssh_probe():
        return "ssh"
    return None


def adb_shell(serial: str, cmd: str, timeout: int = 8) -> tuple[int, str, str]:
    # Always bounded: a sluggish/half-attached watch must never stall a
    # status refresh for minutes.
    return _run(f"adb -s {serial} shell {cmd}", check=False, timeout=timeout)


# Strings a watch can hand back that are NOT an identity. `(none)` is what
# `hostname` prints when none has been set — seen on a half-ported sol, where
# it was accepted as a codename and persisted into the config, freezing the
# watch's name as "(none)" long after the real hostname appeared.
NOT_A_CODENAME = frozenset({"", "(none)", "none", "localhost", "unknown"})


def is_a_codename(value: "str | None") -> bool:
    """Whether what a watch reported can be used as its identity."""
    return bool(value) and value.strip().lower() not in NOT_A_CODENAME


# Characters no device serial can contain, and which make a value dangerous to
# carry: whitespace and quotes break shell words, `/` and `\` turn a serial
# into a path, `=` marks it as a key=value fragment rather than an identity.
_IMPOSSIBLE_IN_SERIAL = set(" \t\r\n/\\='\"`$;|&<>*?")


def is_a_serial(value: "str | None") -> bool:
    """Whether a value can be stored and used as a device serial.

    Deliberately a BLOCKLIST, not a whitelist of allowed characters: serials
    come from device USB descriptors and vendors put odd things there, so
    rejecting a real but unusual serial would make a watch permanently
    unmappable — a worse failure than the one this prevents.

    What it exists to stop, found live on 2026-08-15: the config had a port
    bound to `systempart=/dev/mapper/system`, a kernel cmdline fragment. Most
    plausibly a watch mid-port supplied gibberish while enumerating. It then
    broke every serial-keyed fastboot command for that port and reached a
    filename builder, where the slashes would have escaped the target dir.
    """
    if not value:
        return False
    s = value.strip()
    # Only an upper bound. A minimum length is NOT the discriminator — the
    # value this exists to reject is 28 characters long, while short serials
    # are perfectly legitimate — and a lower bound only risks rejecting a real
    # one. The impossible characters do the actual work.
    if len(s) > 64:
        return False
    return not any(c in _IMPOSSIBLE_IN_SERIAL for c in s)


def get_watch_codename(serial: str, shell=None) -> str | None:
    """
    Read the AsteroidOS codename from the watch.

    AsteroidOS is a plain Linux OS, not Android, so getprop is not available.
    We read /etc/asteroid-release (MACHINE= key) first, then fall back to
    /etc/os-release and finally hostname.

    `shell` runs the reads over any transport, exactly as battery_and_screen
    does. Naming was ADB-only, so a watch connected in SSH/developer mode --
    which onboarding explicitly tells the user is supported -- could not be
    named at all and came back as "did not answer". SshTransport.shell has the
    same signature as this default, so every read below is unchanged.
    """
    run = shell or (lambda c: adb_shell(serial, c))
    rc, out, _ = run("cat /etc/asteroid-release 2>/dev/null")
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            for key in ("MACHINE=", "IMAGE_CODENAME="):
                if line.startswith(key):
                    return line.split("=", 1)[1].strip("\"'")

    rc, out, _ = run("cat /etc/os-release 2>/dev/null")
    if rc == 0:
        for line in out.splitlines():
            if line.strip().startswith("VARIANT_ID="):
                return line.split("=", 1)[1].strip("\"'")

    # A Wear OS watch has neither file: it is Android. Its device codename is
    # the SAME name AsteroidOS uses (sturgeon, lenok, bass...), because the
    # port takes the vendor's, so getprop answers the identity question
    # directly. Tried before hostname because Android hostnames are usually
    # "localhost", which is refused below and would end the search early.
    rc, out, _ = run("getprop ro.product.device")
    if rc == 0 and is_a_codename(out):
        return out.strip()

    # Last resort: hostname (often set to the codename on AsteroidOS).
    rc, out, _ = run("hostname")
    if rc == 0 and is_a_codename(out):
        return out.strip()

    return None


# Prefer the named hardware fuel gauge over the generic `battery` node.
# On some watches (catfish) `battery` is a separate, miscalibrated source that
# freezes — measured 50% while the pack was full, and 50% again while the pack
# was at 8% and the watch was panicking (2026-07-28) — whereas the nanohub
# gauge, which the watch's own UI reads, tracks reality and carries the
# voltage/current/charge fields the generic node lacks entirely.
#
# THE ORDER LIVES HERE ONCE. Every battery reader derives from it: the status
# path below, and the Control Center's on-watch script via
# battery_dir_snippet(). They were separate before, only one knew the rule,
# and the Control Center, the fleet registry and the battery history all
# showed catfish frozen at 50% for weeks because of it.
BATTERY_DIRS = (
    "/sys/class/power_supply/nanohub_fuelgauge-*",
    "/sys/class/power_supply/battery",
    "/sys/class/power_supply/Battery",
    "/sys/class/power_supply/max170xx_battery",
)

_BATTERY_SYSFS_PATHS = tuple(f"{d}/capacity" for d in BATTERY_DIRS)


def battery_dir_snippet(var: str = "BATD") -> str:
    """Shell that resolves the preferred gauge DIRECTORY into `$var`, so a
    script can read several fields from one consistent source instead of
    mixing gauges field by field. An unmatched glob stays literal and is
    filtered by the -d test."""
    return (f'{var}=""; for d in ' + " ".join(BATTERY_DIRS) +
            f'; do [ -d "$d" ] && {var}="$d" && break; done')


def get_battery_level(serial: str) -> int | None:
    """
    Read battery percentage from sysfs.

    AsteroidOS exposes battery state via standard Linux power_supply class.
    dumpsys is Android-only and not available here.  All candidate paths are
    read in one shell invocation — at most one exists on any given watch.
    """
    paths = " ".join(_BATTERY_SYSFS_PATHS)
    # Pass the whole command as one quoted arg so the glob (nanohub_fuelgauge-*),
    # the redirect and the pipe all run on the *watch*. Unquoted, adb_shell uses
    # shell=True, so the host shell expands the glob against the HOST's /sys
    # (where the only supply is the laptop's BAT0) and the watch is handed a path
    # it lacks — the read returns None, which silently breaks every drain/charge
    # that reads the battery. Same reason battery_and_screen quotes its pipeline.
    rc, out, _ = adb_shell(serial, f'"cat {paths} 2>/dev/null | head -1"')
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


def battery_and_screen(serial, shell=None) -> "tuple[int | None, bool, str | None]":
    """One adb round-trip for the status path: (battery_pct, screen_forced,
    charge_status).
    screen_forced = mce is holding the display on (a `mcetool -D on` demo mode
    that was never released), which drains the watch on battery. Detected via
    mce's Blank inhibit != 'disabled' — the anchored grep excludes the
    unrelated 'Kbd slide blank inhibit' line. Safe on non-mce watches (grep
    empty -> not forced).
    charge_status = the watch's own power_supply status (Charging/Discharging/
    Full/…) — delivered-power ground truth: a docked watch that reads
    Discharging is on ADB but not actually taking charge (dirty contact)."""
    paths = " ".join(_BATTERY_SYSFS_PATHS)
    # The whole pipeline must run on the *watch*: _run uses shell=True, so an
    # unquoted `; mcetool | grep` would be parsed by the host shell (where
    # mcetool doesn't exist) instead of the device. Double-quoting keeps the
    # embedded `grep '^Blank inhibit'` single-quotes intact for the watch shell.
    remote = (f"cat {paths} 2>/dev/null | head -1; echo ---SCR---; "
              f"mcetool 2>/dev/null | grep '^Blank inhibit'; echo ---CHG---; "
              f"cat /sys/class/power_supply/*/status 2>/dev/null")
    # `shell` lets the same read run over any transport (an SshTransport.shell
    # for a watch on SSH); default is ADB by serial. The quoting is identical
    # for both back ends, so the pre-quoted remote command is passed as-is.
    run = shell or (lambda c: adb_shell(serial, c))
    rc, out, _ = run(f'"{remote}"')
    if rc != 0:
        return None, False, None
    bat_part, _, rest = out.partition("---SCR---")
    scr_part, _, chg_part = rest.partition("---CHG---")
    battery = int(bat_part.strip()) if bat_part.strip().isdigit() else None
    scr = scr_part.strip().lower()
    forced = bool(scr) and "disabled" not in scr
    # Several supplies may report a status (battery + USB); prefer a definite
    # battery verdict over an "Unknown"/"Not charging" USB line.
    statuses = [s.strip() for s in chg_part.splitlines() if s.strip()]
    charge_status = next((s for s in statuses
                          if s in ("Charging", "Discharging", "Full")),
                         statuses[0] if statuses else None)
    return battery, forced, charge_status


def _wait_adb_state(serial: str, present: bool, timeout: float) -> bool:
    """Poll `adb devices` until serial is present/absent. True if reached.

    Samples from a failed adb invocation are discarded: the adb server is
    known to segfault during disconnect storms, and its freshly-restarted
    replacement briefly reports an empty device list — which would read as
    "device gone" and fake a VBUS-cut confirmation.
    """
    deadline = time.time() + timeout
    while True:
        devices = adb_devices_checked()
        if devices is not None and ((_adb_state(devices, serial) == "device") is present):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1)


def adb_external_power(serial: str) -> bool | None:
    """Watch's own view: is it on external power (AC/USB/wireless)? None = unknown.

    This is the corroborating half of the PPPS verdict: when a watch stays on
    ADB through a power-off command, its own answer is what separates a hub
    that never cut VBUS from a self-powered watch holding the data link up.

    It asked only `dumpsys battery`, which is ANDROID. AsteroidOS is plain
    Linux and has no dumpsys, so across the whole AsteroidOS fleet this
    returned None and both branches that depend on it were unreachable -- the
    check existed and answered "unknown" every time. Measured on sturgeon
    2026-08-16: `dumpsys: command not found`.

    So read the kernel's own power_supply class first, which is the authority
    on a Linux watch. `usb/online` is 1 while external power is present and
    stays 1 with a Full battery: it reports POWER, not charging, which is
    exactly the question here. Wear OS reference units keep the dumpsys path.
    """
    # The glob is ESCAPED on purpose: adb_shell hands its command to a host
    # shell, so a bare * expands against THIS machine's /sys and ships the
    # laptop's own supply names (AC, BAT0) to the watch, which answers "No such
    # file". Escaped, the watch's own shell does the expansion. Measured here
    # 2026-08-16 -- the unescaped form returned the host's AC path.
    rc, out, _ = adb_shell(serial, r"cat /sys/class/power_supply/\*/online 2>/dev/null")
    if rc == 0 and out.strip():
        vals = [line.strip() for line in out.splitlines()
                if line.strip() in ("0", "1")]
        if vals:
            return any(v == "1" for v in vals)
    rc, out, _ = adb_shell(serial, "dumpsys battery")
    if rc != 0 or not out.strip():
        return None
    vals = []
    for line in out.splitlines():
        line = line.strip().lower()
        for key in ("ac powered:", "usb powered:", "wireless powered:"):
            if line.startswith(key):
                vals.append(line.split(":", 1)[1].strip() == "true")
    return any(vals) if vals else None


# ── ADB wait helpers ──────────────────────────────────────────────────────────

def _wait_for_new_adb_device(known_serials: set[str], timeout: int = 10) -> str | None:
    """
    Poll every second until an ADB serial not in known_serials appears as 'device',
    or timeout seconds elapse.  Used by the map wizard.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for serial, state in adb_devices().items():
            if serial not in known_serials and state['status'] == "device":
                return serial
        time.sleep(1)
    return None


def wait_serial_online(serial: str, wait_secs: int, retries: int,
                       stop_event: "threading.Event | None" = None,
                       recover_loc_port: "tuple[str, int] | None" = None) -> bool:
    """
    Poll until the serial is ADB-online. False on timeout or stop request
    (callers distinguish the two via stop_event.is_set()).

    Samples from a failed adb invocation are discarded — a crashed server's
    freshly-started replacement briefly reports an empty device list.  With
    recover_loc_port the port is power-cycled once after the first exhausted
    wait: the stale-dark-node recovery this dock needs (see uhubctl_cycle),
    after which the full wait runs once more.
    """
    attempts = 2 if recover_loc_port else 1
    for attempt in range(attempts):
        for _ in range(retries):
            devices = adb_devices_checked()
            if devices is not None and _adb_state(devices, serial) == "device":
                return True
            if stop_event is not None:
                if stop_event.wait(timeout=wait_secs):
                    return False
            else:
                time.sleep(wait_secs)
        if recover_loc_port is not None and attempt == 0:
            loc, port = recover_loc_port
            # Imported lazily: usb imports this module, so a top-level import
            # here would be circular. This is one of the two seams documented
            # in docs/ARCHITECTURE.md.
            from .usb import uhubctl_cycle, _sysfs_serial_at, recovery_cycle_lock
            here = _sysfs_serial_at(loc, port)
            if here and here != serial:
                # recover_loc_port is the target's seat as of op start. If the
                # watch moved and a DIFFERENT watch sits there now, cycling
                # would bounce that innocent one — so don't (audit A4). The wait
                # just times out and the caller handles the miss.
                log.warning("%s: not power-cycling %s:%s for recovery — a "
                            "different watch (%s) is seated there now",
                            serial, loc, port, here)
            else:
                log.info("%s: not enumerating on %s:%s — power-cycling once",
                         serial, loc, port)
                # Serialized against the status healer's cycles: an op recovering
                # one watch must not cut VBUS at the same instant the background
                # heal cuts another (never many ports at once).
                with recovery_cycle_lock:
                    uhubctl_cycle(loc, port)
    return False


