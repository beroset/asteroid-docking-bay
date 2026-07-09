# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""ADB primitives: device list parsing, per-serial state, shell, battery."""

import time

from .util import _run, log


def adb_devices_checked() -> dict[str, str | list[str]] | None:
    """Like adb_devices, but None when the adb call itself failed (server
    crash / not installed) — distinct from a genuinely empty device list."""
    rc, out, err = _run("adb devices -l", check=False)
    if rc != 0:
        log.warning("adb devices failed (rc=%s): %s", rc, err.strip() or "no stderr")
        return None
    result: dict[str, dict[str, str | list[str]]] = {}
    lines = out.splitlines()[1:]
    for line in lines:
        parts = line.split()
        serial = parts[0]
        status = parts[1]

        extras = parts[2:]

        parsed: dict[str, str | list[str]] = {}

        for token in extras:
            if ":" in token:
                k, v = token.split(":", 1)
                parsed[k] = v
            else:
                parsed.setdefault("_tokens", []).append(token)

        result[serial] = {"status": status, **parsed}
    return result


def adb_devices() -> dict[str, str | list[str]]:
    """Return {serial: state} for all ADB-visible devices ({} on failure)."""
    return adb_devices_checked() or {}


def _adb_state(devs: dict, serial: "str | None") -> "str | None":
    """State string ('device'/'offline'/…) for a serial in an adb_devices() map,
    or None if the serial isn't present. None-safe: adb_devices() values are now
    per-device dicts (`{"status": …, "usb": …}`), and `devs.get(serial)` is None
    for an absent/offline watch — `None['status']` would raise, so callers go
    through here instead of indexing the entry directly."""
    entry = devs.get(serial)
    if isinstance(entry, dict):
        return entry.get("status")
    return entry  # None, or a plain string (defensive)


def adb_shell(serial: str, cmd: str, timeout: int = 8) -> tuple[int, str, str]:
    # Always bounded: a sluggish/half-attached watch must never stall a status
    # refresh (dumpsys with no timeout was the page-killer).
    return _run(f"adb -s {serial} shell {cmd}", check=False, timeout=timeout)


def get_watch_codename(serial: str) -> str | None:
    """
    Read the AsteroidOS codename from the watch.

    AsteroidOS is a plain Linux OS, not Android, so getprop is not available.
    We read /etc/asteroid-release (MACHINE= key) first, then fall back to
    /etc/os-release and finally hostname.
    """
    rc, out, _ = adb_shell(serial, "cat /etc/asteroid-release 2>/dev/null")
    if rc == 0:
        for line in out.splitlines():
            line = line.strip()
            for key in ("MACHINE=", "IMAGE_CODENAME="):
                if line.startswith(key):
                    return line.split("=", 1)[1].strip("\"'")

    rc, out, _ = adb_shell(serial, "cat /etc/os-release 2>/dev/null")
    if rc == 0:
        for line in out.splitlines():
            if line.strip().startswith("VARIANT_ID="):
                return line.split("=", 1)[1].strip("\"'")

    # Last resort: hostname (often set to the codename on AsteroidOS).
    rc, out, _ = adb_shell(serial, "hostname")
    if rc == 0 and out.strip() not in ("", "localhost"):
        return out.strip()

    return None


_BATTERY_SYSFS_PATHS = (
    "/sys/class/power_supply/battery/capacity",
    "/sys/class/power_supply/Battery/capacity",
    "/sys/class/power_supply/max170xx_battery/capacity",
)


def get_battery_level(serial: str) -> int | None:
    """
    Read battery percentage from sysfs.

    AsteroidOS exposes battery state via standard Linux power_supply class.
    dumpsys is Android-only and not available here.  All candidate paths are
    read in one shell invocation — at most one exists on any given watch.
    """
    paths = " ".join(_BATTERY_SYSFS_PATHS)
    rc, out, _ = adb_shell(serial, f"cat {paths} 2>/dev/null | head -1")
    if rc == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


def _wait_adb_state(serial: str, present: bool, timeout: float) -> bool:
    """Poll `adb devices` until serial is present/absent. True if reached.

    Samples from a failed adb invocation are discarded: the adb server is
    known to segfault during disconnect storms, and its freshly-restarted
    replacement briefly reports an empty device list — which would read as
    "device gone" and fake a VBUS-cut confirmation.
    """
    deadline = time.time() + timeout
    while True:
        devs = adb_devices_checked()
        if devs is not None and ((_adb_state(devs, serial) == "device") is present):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(1)


def adb_external_power(serial: str) -> bool | None:
    """Watch's own view: is it on external power (AC/USB/wireless)? None = unknown."""
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
            devs = adb_devices_checked()
            if devs is not None and _adb_state(devs, serial) == "device":
                return True
            if stop_event is not None:
                if stop_event.wait(timeout=wait_secs):
                    return False
            else:
                time.sleep(wait_secs)
        if recover_loc_port is not None and attempt == 0:
            loc, port = recover_loc_port
            log.info("%s: not enumerating on %s:%s — power-cycling once",
                     serial, loc, port)
            # Imported lazily: usb imports this module, so a top-level import
            # here would be circular. This is one of the two seams documented
            # in docs/ARCHITECTURE.md.
            from .usb import uhubctl_cycle
            uhubctl_cycle(loc, port)
    return False


