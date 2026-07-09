# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Port power control: direct sysfs, uhubctl discovery/fallback, PPPS test."""

import fcntl
import glob
import os
import sys
import threading
import time
from pathlib import Path

from .util import _run, log
from .adb import _adb_state, _wait_adb_state, adb_devices, adb_external_power


# Serialises all uhubctl invocations: every call rescans the USB bus via
# libusb, and concurrent scans (parallel web requests, background tasks)
# contend with adb's libusb use during device churn.  RLock because
# uhubctl_set_power read-backs via uhubctl_get_power.
_uhubctl_lock = threading.RLock()

# Cross-process companion to _uhubctl_lock: the web UI and the periodic
# charge timer are separate processes, and their concurrent uhubctl bus
# scans have been observed to glitch dock ports (a port dropped power
# mid-charge during a parallel check-charge run).
_UHUBCTL_LOCKFILE = Path.home() / ".local/state/asteroid-docking-bay/uhubctl.lock"
_uhubctl_lock_fd: "int | None" = None


def _uhubctl_exec(cmd: str) -> tuple[int, str, str]:
    """Run one uhubctl command holding the in-process and cross-process locks."""
    global _uhubctl_lock_fd
    with _uhubctl_lock:
        if _uhubctl_lock_fd is None:
            _UHUBCTL_LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
            _uhubctl_lock_fd = os.open(_UHUBCTL_LOCKFILE,
                                       os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(_uhubctl_lock_fd, fcntl.LOCK_EX)
        try:
            return _run(cmd, check=False)
        finally:
            fcntl.flock(_uhubctl_lock_fd, fcntl.LOCK_UN)


# ── uhubctl wrappers ──────────────────────────────────────────────────────────

def _require_uhubctl():
    rc, _, _ = _run("which uhubctl", check=False)
    if rc != 0:
        log.error(
            "uhubctl not found. Install it:\n"
            "  Arch:    yay -S uhubctl   (AUR)\n"
            "  Debian:  sudo apt install uhubctl\n"
            "  Source:  https://github.com/mvp/uhubctl"
        )
        sys.exit(1)


# ── Direct sysfs port control ─────────────────────────────────────────────────
# uhubctl re-enumerates the whole USB tree via libusb on every command (5-13s
# on a deep cascade), and that churn races adb's own libusb use. The kernel
# exposes each port's power directly:
#   /sys/bus/usb/devices/<loc>:1.0/<loc>-port<N>/disable   (0 = on, 1 = off)
# — a single targeted op, no tree scan. Reads are world-readable; writing
# needs the udev rule (udev/70-asteroid-docking-bay.rules) or we fall back to
# uhubctl. uhubctl stays for mapping/discovery only.
_SYSFS_USB = Path("/sys/bus/usb/devices")

class PowerCache:
    """TTL'd port-power cache, keyed (location, port). Lets the status page
    skip the ~200ms/port `disable` read on empty cascade ports. TTL'd rather
    than authoritative-forever so it self-heals: an external change, or a
    write handled by a different serve process with its own cache, becomes
    visible within one TTL. Correctness-sensitive callers (re-power checks,
    set read-backs, the smart test) always read fresh via _sysfs_get_power.

    The long default TTL is deliberate: empty-port power only changes when we
    change it (every write updates the cache), so the background warmer can
    warm once and stay quiet — fewer `disable` reads means fewer kernel
    hub-lock collisions with status reads."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._data: dict = {}

    def get(self, key):
        e = self._data.get(key)
        return e[0] if e and e[1] > time.time() else None

    def put(self, key, val):
        self._data[key] = (val, time.time() + self.ttl)


power_cache = PowerCache()


def _sysfs_disable_path(location: str, port: int) -> "Path | None":
    for iface in _SYSFS_USB.glob(f"{location}:*"):
        p = iface / f"{location}-port{port}" / "disable"
        if p.exists():
            return p
    return None


def _sysfs_get_power(location: str, port: int) -> "bool | None":
    """True = on, False = off, None = attr unavailable (caller falls back)."""
    p = _sysfs_disable_path(location, port)
    if p is None:
        return None
    try:
        v = p.read_text().strip()
    except OSError:
        return None
    return True if v == "0" else (False if v == "1" else None)


def _sysfs_set_power(location: str, port: int, on: bool) -> bool:
    """Write the port's power via sysfs. True on success, False if the attr is
    missing or not writable (caller falls back to uhubctl)."""
    p = _sysfs_disable_path(location, port)
    if p is None:
        return False
    try:
        p.write_text("0" if on else "1")
        power_cache.put((location, port), on)
        return True
    except OSError as e:
        log.debug("sysfs set_power %s:%d failed (%s) — falling back to uhubctl",
                  location, port, e)
        return False


def _sysfs_hub_scan(cfg: dict) -> list[dict]:
    """Fast uhubctl_list() replacement for the status page. Hub locations come
    from a cheap directory glob (no USB queries); power/presence are read only
    for the configured hubs' ports, and a present child device proves the port
    is powered, so the ~50ms disable read is skipped for occupied ports."""
    config_locs = {h["location"] for h in cfg.get("hubs", [])}
    hubs: list[dict] = []
    for iface in _SYSFS_USB.glob("*:1.0"):
        loc = iface.name.rsplit(":", 1)[0]
        port_dirs = list(iface.glob(f"{loc}-port*"))
        if not port_dirs:
            continue
        ports: list[int] = []
        power: dict = {}
        connect: dict = {}
        want = loc in config_locs
        for pd in port_dirs:
            try:
                n = int(pd.name.rsplit("port", 1)[1])
            except ValueError:
                continue
            ports.append(n)
            if want:
                present = (_SYSFS_USB / f"{loc}.{n}").exists()
                connect[n] = present
                if present:                       # a device proves it's powered
                    power[n] = True
                    power_cache.put((loc, n), True)
                else:
                    # Never read `disable` on the status path — it's a slow,
                    # variable USB query (some empty-off ports hang for seconds).
                    # Serve the cached value; _power_cache_warmer keeps it fresh.
                    power[n] = power_cache.get((loc, n))
        desc = ""
        if want:
            try:
                desc = (_SYSFS_USB / loc / "product").read_text().strip()
            except OSError:
                pass
        hubs.append({"location": loc, "description": desc, "ppps": True,
                     "ports": sorted(ports), "power": power, "connect": connect})
    return hubs


def _sysfs_switch_mode(cfg: dict) -> str:
    """Human-readable: is port switching going via direct sysfs (instant) or
    falling back to uhubctl (slow)? Determined by whether a configured hub's
    port `disable` attr is writable by us — i.e. whether the udev rule is in
    effect. Logged at startup so the fast/slow state is never a mystery."""
    for h in cfg.get("hubs", []):
        loc = h["location"]
        for iface in _SYSFS_USB.glob(f"{loc}:*"):
            for pd in sorted(iface.glob(f"{loc}-port*")):
                cand = pd / "disable"
                if cand.exists():
                    if os.access(cand, os.W_OK):
                        return "sysfs (instant)"
                    return ("uhubctl fallback (slow) — the sysfs `disable` attr is "
                            "read-only; install the udev rule (see udev/*.rules) "
                            "for instant switching")
    return "uhubctl (no sysfs `disable` attr found for configured hubs)"



def uhubctl_list() -> list[dict]:
    """
    Return a list of controllable hubs as dicts:
        {"location": "1-1", "description": "...", "ports": [1, 2, 3, 4],
         "power": {1: True, 2: False, ...}}
    One full scan carries every port's power state, so callers that need
    many ports should use "power" instead of per-port uhubctl_get_power calls.
    """
    _require_uhubctl()
    rc, out, err = _uhubctl_exec("uhubctl -S")
    if rc != 0:
        if "Permission denied" in err or "Operation not permitted" in err:
            log.error(
                "uhubctl: permission denied. Either run as root or set up udev rules.\n"
                "See udev/70-asteroid-docking-bay.rules in this repo."
            )
        return []
    return parse_uhubctl_status(out)


def parse_uhubctl_status(out: str) -> list[dict]:
    """Parse `uhubctl` status output into the uhubctl_list() hub dicts.
    Pure — see tests."""
    hubs: list[dict] = []
    current: dict | None = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Current status for hub"):
            if current is not None:
                hubs.append(current)
            parts = stripped.split()
            loc = parts[4]
            desc = (
                stripped[stripped.find("[") + 1 : stripped.rfind("]")]
                if "[" in stripped
                else ""
            )
            # "ppps" in the description flags per-port power switching support
            # at the hub level.  We still do a live per-port test in 'map'.
            ppps = "ppps" in desc.lower()
            current = {"location": loc, "description": desc, "ppps": ppps,
                       "ports": [], "power": {}, "connect": {}}
        elif stripped.startswith("Port ") and current is not None:
            try:
                parts = stripped.split()
                port_num = int(parts[1].rstrip(":"))
                current["ports"].append(port_num)
                current["power"][port_num]   = "power" in parts[2:]
                current["connect"][port_num] = "connect" in parts[2:]
            except (ValueError, IndexError):
                pass
    if current is not None:
        hubs.append(current)
    return hubs


def uhubctl_get_power(location: str, port: int) -> bool | None:
    """Return True = powered on, False = powered off, None = unknown.
    Reads sysfs directly (no bus scan); falls back to uhubctl only if the
    sysfs disable attr isn't available for this hub."""
    v = _sysfs_get_power(location, port)
    if v is not None:
        return v
    rc, out, err = _uhubctl_exec(f"uhubctl -S -l {location} -p {port}")
    if rc != 0:
        if "Permission denied" in err or "Operation not permitted" in err:
            log.warning("uhubctl: permission denied querying %s port %d", location, port)
        return None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"Port {port}:"):
            # e.g. "Port 2: 0503 power highspeed enable connect"
            # or   "Port 2: 0000 off"
            flags = stripped.split()[2:]
            return "power" in flags
    return None


def uhubctl_set_power(location: str, port: int, on: bool) -> bool:
    """
    Set hub port power.  Returns True if the port state was confirmed to have
    changed, False if uhubctl accepted the command but a read-back shows the
    port is still in the old state (indicates a hub that cannot actually switch
    per-port power despite claiming to support it).
    Raises RuntimeError on uhubctl command failure.
    """
    action = "on" if on else "off"
    # Fast path: write the port's power directly via sysfs (no bus scan).
    if _sysfs_set_power(location, port, on):
        actual = _sysfs_get_power(location, port)       # fresh read-back
        if actual is not None:
            power_cache.put((location, port), actual)
        confirmed = actual == on
        if not confirmed:
            log.warning("sysfs set %s port %d %s: read-back did not confirm",
                        location, port, action)
        return confirmed
    # Fallback: uhubctl (sysfs attr not writable yet — needs the udev rule).
    with _uhubctl_lock:
        rc, _, err = _uhubctl_exec(f"uhubctl -S -l {location} -p {port} -a {action}")
        if rc != 0:
            raise RuntimeError(
                f"uhubctl failed setting hub {location} port {port} {action}: {err}"
            )
        confirmed = uhubctl_get_power(location, port) == on
    power_cache.put((location, port), on)
    if not confirmed:
        log.warning(
            "uhubctl set %s port %d %s: command succeeded but port state did not change"
            " — hub may not support per-port power switching",
            location, port, action,
        )
    return confirmed


def uhubctl_cycle(location: str, port: int, delay: int = 3) -> None:
    """Power-cycle a port in a single uhubctl invocation (off → delay → on).
    Used as the stale-node recovery primitive: unlike a plain off→on pair,
    the cycle makes this dock raise a proper connect event."""
    if _sysfs_set_power(location, port, False):
        time.sleep(delay)
        _sysfs_set_power(location, port, True)
        return
    _uhubctl_exec(f"uhubctl -S -l {location} -p {port} -a cycle -d {delay}")


def _port_device_present(location: str, port: int) -> bool:
    """Kernel's view: does an enumerated USB device exist on this hub port?"""
    child = f"{location}.{port}" if "-" in location else f"{location}-{port}"
    return Path(f"/sys/bus/usb/devices/{child}").exists()


# Watches enumerate with Google's vendor ID in every mode we drive them in:
# the AsteroidOS gadget (adb/ssh), Wear OS adb, and fastboot.
_WATCH_VENDOR = "18d1"


def port_foreign_device(location: str, port: int,
                        known_serials: "set[str] | None" = None) -> "str | None":
    """A human-readable description of a non-watch device enumerated on this
    port, or None if the port is empty or holds a watch. Smart hubs are not
    watch docks by definition — keyboards with built-in hubs, mice, dock
    peripherals — and map must never cut power to something it can't
    identify as a watch.

    A watch is recognised by the Google gadget vendor ID, or by its serial
    appearing in known_serials (adb/fastboot-visible devices): watches with
    hacked or vendor-specific USB identities enumerate under other VIDs, and
    an adb-answering device is a watch by definition."""
    child = f"{location}.{port}" if "-" in location else f"{location}-{port}"
    base = _SYSFS_USB / child
    if not base.is_dir():
        return None
    def read(name):
        try:
            return (base / name).read_text().strip()
        except OSError:
            return ""
    vid = read("idVendor").lower()
    if vid == _WATCH_VENDOR:
        return None
    if known_serials and read("serial") in known_serials:
        return None
    if read("bDeviceClass") == "09":
        return f"hub ({read('product') or vid})"
    return read("product") or f"usb {vid}:{read('idProduct')}"


def _wait_port_device(location: str, port: int, present: bool, timeout: float) -> bool:
    """Poll sysfs until the port's device is present/absent. True if reached."""
    deadline = time.time() + timeout
    while True:
        if _port_device_present(location, port) is present:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def test_port_power_switching(location: str, port: int,
                              serial: str | None = None) -> tuple[bool | None, str]:
    """
    Confirm a hub port actually cuts VBUS, not just its status register.
    Briefly interrupts power to any connected device (up to ~30 s with a device).

    Hubs are known to acknowledge power commands and toggle the status bit
    while VBUS stays hot, so the status register alone proves nothing.  sysfs
    is also unreliable here: hubs don't always raise a disconnect event for a
    port they powered off, leaving a stale kernel device node behind while the
    device is actually dark.  Evidence hierarchy, strongest first:
      1. ADB: the adb server actively talks to the device — if VBUS is cut it
         drops the device within seconds; if the device keeps chatting, ask it
         directly (dumpsys battery) whether it still sees external power.
      2. sysfs disappearance: positive proof of a cut (but persistence proves
         nothing, see above).
    Returns (smart, reason): True = VBUS cut confirmed, False = device
    demonstrably kept external power, None = could not be verified.

    Tests in the direction that restores the port to its initial state:
    - If port is ON:  off → verify → on   (ends ON)
    - If port is OFF: on → verify → off   (ends OFF)
    """
    initial = uhubctl_get_power(location, port)

    if initial is False:
        # Bring the port up first; a running battery-powered watch re-attaches
        # in a few seconds.  One that must cold-boot won't make the window and
        # stays unverified — rerun test-ports once the fleet is up.
        uhubctl_set_power(location, port, True)
        if serial:
            _wait_adb_state(serial, present=True, timeout=25)
        else:
            _wait_port_device(location, port, present=True, timeout=8)
        time.sleep(1)
        confirmed_on = (uhubctl_get_power(location, port) is True)
    else:
        confirmed_on = True  # measured again after the restore below

    adb_before    = bool(serial) and _adb_state(adb_devices(), serial) == "device"
    device_before = _port_device_present(location, port)

    uhubctl_set_power(location, port, False)
    time.sleep(1)
    confirmed_off = (uhubctl_get_power(location, port) is False)

    verdict: bool | None
    if adb_before:
        if _wait_adb_state(serial, present=False, timeout=10):
            verdict, why = True, "VBUS cut confirmed — device dropped off ADB"
        else:
            powered = adb_external_power(serial)
            if powered is False:
                verdict, why = True, ("VBUS cut confirmed — device reports no "
                                      "external power (self-powered data link)")
            elif powered is True:
                verdict, why = False, ("device still reports external power — "
                                       "VBUS not actually switched")
            else:
                verdict, why = None, ("device stayed on ADB but its power state "
                                      "is unreadable — VBUS cut unverified")
    elif device_before:
        if _wait_port_device(location, port, present=False, timeout=8):
            verdict, why = True, "VBUS cut confirmed — device dropped off the bus"
        else:
            verdict, why = None, ("device stayed enumerated — can't distinguish a "
                                  "VBUS cut from a stale kernel node without ADB")
    else:
        verdict, why = None, "no device on port — VBUS cut unverified"

    # Restore the port to its initial state.  For an initially-off port the
    # off-verify above already left it off; for an initially-on port power it
    # back up and re-measure the register.
    if initial is not False:
        uhubctl_set_power(location, port, True)
        if adb_before and verdict is True:
            if not _wait_adb_state(serial, present=True, timeout=30):
                # The dock can fail to raise a connect event after plain
                # off→on, leaving a stale dark node that blocks
                # re-enumeration; a power cycle makes it signal properly.
                uhubctl_cycle(location, port)
                _wait_adb_state(serial, present=True, timeout=30)
        time.sleep(1)
        confirmed_on = (uhubctl_get_power(location, port) is True)

    if not (confirmed_off and confirmed_on):
        parts = []
        if not confirmed_off:
            parts.append("power-off unresponsive")
        if not confirmed_on:
            parts.append("power-on unresponsive")
        return False, "; ".join(parts)
    return verdict, why


def _sysfs_usb_mode(sysfs_path: str) -> "str | None":
    """Detect an AsteroidOS watch's USB gadget mode from the idProduct at a
    hub port's sysfs path: adb_mode reports 0a03, developer_mode (SSH) reports
    0a02 (from usb-moded dyn-modes). Returns "adb", "ssh", or None (no device
    or not an AsteroidOS gadget)."""
    base = Path(f"/sys/bus/usb/devices/{sysfs_path}")
    try:
        vid = (base / "idVendor").read_text().strip().lower()
        pid = (base / "idProduct").read_text().strip().lower()
    except OSError:
        return None
    if vid != "18d1":
        return None
    return {"0a03": "adb", "0a02": "ssh"}.get(pid)


def _sysfs_path_to_serial_map(serials: set[str]) -> dict[str, str]:
    """Single-pass sysfs scan: return {sysfs_path: serial} for the given serials."""
    result: dict[str, str] = {}
    for path in glob.glob("/sys/bus/usb/devices/*/serial"):
        try:
            with open(path) as f:
                s = f.read().strip()
                if s in serials:
                    result[os.path.basename(os.path.dirname(path))] = s
        except OSError:
            pass
    return result


def _parse_hub_port_path(path: str) -> "tuple[str, int] | None":
    """'1-6.4.1' → ('1-6.4', 1); '1-6.2' → ('1-6', 2).
    Direct host ports ('1-3') have no hub in the path → None."""
    if "." not in path:
        return None
    loc, _, port_str = path.rpartition(".")
    try:
        return loc, int(port_str)
    except ValueError:
        return None


