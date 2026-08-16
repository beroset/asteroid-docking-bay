# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Port power control: direct sysfs, uhubctl discovery/fallback, PPPS test."""

from __future__ import annotations

import fcntl
import glob
import os
import sys
import threading
import time
from pathlib import Path

from .util import _run, log
from .flap import flaps
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


def _sysfs_disable_paths(location: str, port: int) -> "list[Path]":
    """The sysfs 'disable' paths that control this port's VBUS.

    On a USB3-connected hub the physical VBUS is shared between the USB2 port
    and its USB3 companion (the kernel `peer` link). Cutting only one side
    leaves VBUS held on by the other — measured 2026-07-24 after the A16s moved
    to USB3 ports, where every port read OFF in software while physically ON and
    nothing switched. So return the port's disable AND its peer's when present.
    A USB2-only hub has no peer and yields a single path (unchanged behaviour)."""
    paths: list[Path] = []
    for iface in _SYSFS_USB.glob(f"{location}:*"):
        portdir = iface / f"{location}-port{port}"
        d = portdir / "disable"
        if d.exists():
            paths.append(d)
        peer = portdir / "peer"
        if peer.exists():
            try:
                pd = peer.resolve() / "disable"
                if pd.exists():
                    paths.append(pd)
            except OSError:
                pass
    return paths


def _sysfs_get_power(location: str, port: int) -> "bool | None":
    """True = on, False = off, None = unavailable. VBUS is on if EITHER side
    (the port or its USB3 peer) is enabled; off only when all are disabled."""
    vals = []
    for p in _sysfs_disable_paths(location, port):
        try:
            vals.append(p.read_text().strip())
        except OSError:
            pass
    if not vals:
        return None
    if "0" in vals:
        return True
    if all(v == "1" for v in vals):
        return False
    return None


def _sysfs_set_power(location: str, port: int, on: bool) -> bool:
    """Write the port's power via sysfs on BOTH sides (USB2 + USB3 peer) so a
    USB3-connected hub actually cuts/restores VBUS instead of the other side
    holding it on. True only if EVERY side wrote — a partial write leaves VBUS
    mixed, so the caller falls back to uhubctl."""
    paths = _sysfs_disable_paths(location, port)
    if not paths:
        return False
    val = "0" if on else "1"
    for p in paths:
        try:
            p.write_text(val)
        except OSError as e:
            log.debug("sysfs set_power %s:%d (%s) failed (%s) — falling back",
                      location, port, p, e)
            return False
    power_cache.put((location, port), on)
    return True


def _sysfs_hub_scan(cfg: dict) -> list[dict]:
    """Fast uhubctl_list() replacement for the status page. Hub locations come
    from a cheap directory glob (no USB queries); power/presence are read only
    for the configured hubs' ports, and a present child device proves the port
    is powered, so the ~50ms disable read is skipped for occupied ports."""
    config_locs = {hub["location"] for hub in cfg.get("hubs", [])}
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
                    # Serve the cached value; the background warmer keeps it fresh.
                    power[n] = power_cache.get((loc, n))
        # NEVER read the hub's `product` here. On a wedged hub that read blocks
        # the kernel USB core for a minute+ (measured 88s on the rig for one
        # flaky A16 chip), freezing EVERY status refresh — one bad hub takes the
        # whole UI down. The description is static, so it is captured once at map
        # time into the config; webstatus prefers that.
        hubs.append({"location": loc, "ppps": True,
                     "ports": sorted(ports), "power": power, "connect": connect})
    return hubs


def hub_vendors() -> "list[dict]":
    """Every USB2-side hub chip under the host, as {'location', 'vendor'}. Used
    to auto-name physical hubs (Realtek A16 chips vs a Lenovo dock differ by
    vendor). SuperSpeed (USB3) companion hubs are skipped: they mirror the same
    physical box as their USB2 peer and bear no watches (watches are USB2), so
    counting them would double every box. One cheap idVendor read per hub; hubs
    are bDeviceClass 09."""
    out: list[dict] = []
    for dev in _SYSFS_USB.glob("*-*"):
        if ":" in dev.name:                       # skip interface dirs
            continue
        try:
            if (dev / "bDeviceClass").read_text().strip() != "09":
                continue
            if int((dev / "speed").read_text().strip().split(".")[0]) >= 5000:
                continue                          # USB3 companion — skip
            vendor = (dev / "idVendor").read_text().strip()
        except OSError:
            continue
        out.append({"location": dev.name, "vendor": vendor})
    return out


def discover_hubs() -> "list[dict]":
    """Every USB2-side hub under the host, whether or not it can switch power.

    `uhubctl` reports ONLY hubs with per-port power switching, so a hub without
    it — the Sabrent's Genesys chips, for instance — was invisible to `map` and
    could not be registered at all, however many watches were sitting on it.
    So the hub list comes from sysfs (bDeviceClass 09, the same walk
    hub_vendors() uses) and uhubctl is consulted purely to decide which of
    those hubs can switch power.

    Returns uhubctl_list()-shaped dicts: location, description, ports, ppps,
    plus `internal` for chipset hubs that carry no physical socket (Intel's
    rate-matching hub, for one) — callers skip those rather than growing rows
    no watch can ever appear on. Flagged rather than dropped here, so a caller
    can still report what it passed over.
    No power state — map does not switch power, and reading it here would cost
    a slow USB query per port.

    Reading `product` is safe HERE and nowhere on the status path: on a wedged
    hub that read blocks the kernel USB core for a minute or more (88 s
    measured on the rig), which is why the description is captured once at map
    time into the config and never re-read per refresh.
    """
    # Class-09 devices from these vendors are chipset-internal hubs (Intel's
    # 8087:8000 Integrated Rate Matching Hub), soldered between the controller
    # and the real ports. They have no sockets of their own.
    INTERNAL_VENDORS = {"8087"}

    switchable = {}
    try:
        for hub in uhubctl_list():
            switchable[hub["location"]] = hub
    except Exception as exc:
        # No uhubctl, or no permission: every hub then registers as non-PPPS,
        # which is honest — nothing can switch power without it.
        log.warning("uhubctl unavailable, registering hubs as non-PPPS: %s", exc)

    out: list[dict] = []
    for dev in sorted(_SYSFS_USB.glob("*-*")):
        if ":" in dev.name:                       # interface dir, not a device
            continue
        loc = dev.name
        try:
            if (dev / "bDeviceClass").read_text().strip() != "09":
                continue
            # Watches are USB 2.0 and only enumerate on the 2.x bus; a USB 3.x
            # companion mirrors the same physical box, so counting it would
            # double every hub.
            if int((dev / "speed").read_text().strip().split(".")[0]) >= 5000:
                continue
        except OSError:
            continue

        ports: list[int] = []
        for iface in _SYSFS_USB.glob(f"{loc}:*"):
            for pd in iface.glob(f"{loc}-port*"):
                try:
                    ports.append(int(pd.name.rsplit("port", 1)[1]))
                except ValueError:
                    continue
        if not ports:
            continue

        try:
            vendor = (dev / "idVendor").read_text().strip()
        except OSError:
            vendor = ""

        known = switchable.get(loc)
        if known:
            desc, ppps = known.get("description", ""), known.get("ppps", False)
        else:
            try:
                desc = (dev / "product").read_text().strip()
            except OSError:
                desc = ""
            ppps = False
        out.append({"location": loc, "description": desc, "ppps": ppps,
                    "ports": sorted(set(ports)),
                    "internal": vendor in INTERNAL_VENDORS})
    return out


def _sysfs_switch_mode(cfg: dict) -> str:
    """Human-readable: is port switching going via direct sysfs (instant) or
    falling back to uhubctl (slow)? Determined by whether a configured hub's
    port `disable` attr is writable by us — i.e. whether the udev rule is in
    effect. Logged at startup so the fast/slow state is never a mystery."""
    for hub in cfg.get("hubs", []):
        loc = hub["location"]
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


def _hub_has_usb3_companion(location: str) -> bool:
    """Whether this hub is USB3-connected — its USB2 and USB3 sides are distinct
    branches. Inferred from a `peer` link on the cascade port that leads to it
    (that peer survives even when the leaf hub's own port peers do not, e.g. a
    hub behind a dock), or, for a root-connected hub, on its own ports."""
    parent, _, n = location.rpartition(".")
    if parent and n.isdigit():
        for iface in _SYSFS_USB.glob(f"{parent}:*"):
            if (iface / f"{parent}-port{n}" / "peer").exists():
                return True
        return False
    for peer in _SYSFS_USB.glob(f"{location}:*/{location}-port*/peer"):
        if peer.exists():
            return True
    return False


def _sysfs_power_is_complete(location: str, port: int) -> bool:
    """Whether the sysfs `disable` attrs reach EVERY side of this port's shared
    VBUS. False on a USB3-connected hub whose USB3 companion has no sysfs disable
    (a hub behind a dock — no leaf `peer`): there sysfs cuts only the USB2 side
    and VBUS stays up, so uhubctl (companion-aware) must drive it instead
    (measured on the rig 2026-07-24 — shelving a Hub-B watch only rebooted it)."""
    if len(_sysfs_disable_paths(location, port)) >= 2:
        return True
    return not _hub_has_usb3_companion(location)


def uhubctl_set_power(location: str, port: int, on: bool) -> bool:
    """
    Set hub port power.  Returns True if the port state was confirmed to have
    changed, False if uhubctl accepted the command but a read-back shows the
    port is still in the old state (indicates a hub that cannot actually switch
    per-port power despite claiming to support it).
    Raises RuntimeError on uhubctl command failure.
    """
    action = "on" if on else "off"
    # The reconnect tally is "since this port was last powered", so our own
    # switching resets it — otherwise every shelve/cycle would inflate the
    # badge and the one number that means something would stop meaning it.
    flaps.reset(location, port)
    # Fast path: write the port's power directly via sysfs (no bus scan).
    # Powering ON only needs ONE side up — VBUS is on if either side is enabled,
    # and a watch (a USB2 device) enumerates on the USB2 side — so sysfs
    # disable=0 always suffices and is instant. Powering OFF needs EVERY side
    # of the shared VBUS down, so it takes the sysfs path only when sysfs reaches
    # them all (a USB3 companion with no sysfs disable would otherwise keep VBUS
    # up); when it can't, it falls through to companion-aware uhubctl.
    if (on or _sysfs_power_is_complete(location, port)) and _sysfs_set_power(location, port, on):
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


# Every AUTOMATIC recovery power-cycle acquires this before actuating, so that
# several wedged ports crossing their thresholds in one status pass — or an op's
# own enumeration-recovery firing alongside the status healer — can never cut and
# raise VBUS on many ports at once. That simultaneity is the inrush brownout and
# adb-server crash the "never power many ports at once" rule exists to prevent.
# It is deliberately NOT held by uhubctl_cycle itself: a deliberate operator
# cycle or an onboarding sweep must stay immediate, not queue behind a background
# heal. The three automatic callers (the fake-power heal, the SSH-stray recovery,
# and adb's not-enumerating recovery) take it; nobody else does.
recovery_cycle_lock = threading.Lock()


def uhubctl_cycle(location: str, port: int, delay: int = 3) -> None:
    """Power-cycle a port in a single uhubctl invocation (off → delay → on).
    Used as the stale-node recovery primitive: unlike a plain off→on pair,
    the cycle makes this dock raise a proper connect event."""
    flaps.reset(location, port)
    if _sysfs_set_power(location, port, False):
        time.sleep(delay)
        _sysfs_set_power(location, port, True)
        return
    _uhubctl_exec(f"uhubctl -S -l {location} -p {port} -a cycle -d {delay}")


def _port_device_present(location: str, port: int) -> bool:
    """Kernel's view: does an enumerated USB device exist on this hub port?"""
    child = f"{location}.{port}" if "-" in location else f"{location}-{port}"
    return Path(f"/sys/bus/usb/devices/{child}").exists()


# Vendor IDs a watch can enumerate under.
#
# The AsteroidOS gadget and Wear OS adb both use Google's 18d1, so for a
# RUNNING watch one ID covers the fleet. Bootloaders do not follow that rule:
# they are the vendor's own code and carry the vendor's own ID. sparrow runs as
# 18d1:0a02 and sits in fastboot as 0b05:7771 (ASUSTek) — so a scan filtered on
# Google alone reported "no watches" with sparrow plugged in and waiting.
#
# This table is therefore a list of BOOTLOADER SIGNATURES, and it is expected to
# be incomplete: Mobvoi and Fossil bootloaders have not been observed here yet.
# Add an entry when one turns up — and note that the entry is not what makes the
# scan correct, it only makes it faster. Anything fastboot or adb can name is a
# watch by definition, whatever ID it enumerates under, which is why callers can
# pass those paths in (see watch_devices_on_bus).
_WATCH_VENDORS = {
    "18d1": "Google — AsteroidOS gadget, Wear OS adb, and Google-made bootloaders",
    "0b05": "ASUSTek — ASUS watch bootloaders (observed: sparrow 0b05:7771)",
}
_WATCH_VENDOR = "18d1"   # the running-watch ID; kept for callers that mean only that


def watch_devices_on_bus(known_paths: "set[str] | None" = None) -> "list[dict]":
    """Every watch currently enumerated anywhere on the bus, from sysfs alone.

    The guided onboarding's read-backs need "what is plugged in RIGHT NOW",
    independent of adb, of fastboot, and of the config's idea of the fleet — a
    watch that is present but wedged, in the bootloader, or entirely unknown
    still counts as something the user must unplug before the bus is empty.

    Two ways in, because a vendor ID list can never be complete. A device
    matches if its ID is in _WATCH_VENDORS, OR if the caller already knows the
    path holds a watch — pass the sysfs paths fastboot or adb reported, and
    anything they can name is included whatever it enumerates under. That is
    what stops an unlisted bootloader from being invisible: sparrow sat in
    fastboot as 0b05 and this returned nothing at all.

    Returns [{path, serial, product, pid, vendor}], sorted by path so a diff
    between two calls is stable.
    """
    known = known_paths or set()
    out = []
    for dev in sorted(_SYSFS_USB.glob("*")):
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
        except OSError:
            continue          # disappeared mid-scan, or not a device dir
        if vid not in _WATCH_VENDORS and dev.name not in known:
            continue
        # A hub is never a watch, however it identifies itself.
        if _read_attr(dev / "bDeviceClass") == "09":
            continue
        out.append({
            "path": dev.name,
            "serial": _read_attr(dev / "serial"),
            "product": _read_attr(dev / "product"),
            "pid": _read_attr(dev / "idProduct").lower(),
            "vendor": vid,
        })
    return out


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


# ── Gadget composition: what a watch actually offers on the wire ─────────────
# Read the INTERFACE list, never idProduct. idProduct is a userspace artefact
# and ambiguous: 0afe is used by both the initramfs gadget (which carries adb)
# and usb-moded's charging-only fallback (which carries nothing useful). They
# are distinguishable only here. It is also not a boot-state signal -- the
# porting session read 0afe as "stuck in initramfs" and diagnosed a boot hang
# while the watch was sitting in LPM with a working launcher.
_IFACE_ADB = ("ff", "42", "01")      # vendor-specific; the adb server claims it
_IFACE_NCM_CTRL = ("02", "0d")       # CDC-NCM control -> the ssh-capable network
_IFACE_MASS_STORAGE = "08"


def usb_interfaces(path: str) -> "list[dict]":
    """Every USB interface a device exposes: class/subclass/protocol + driver.

    The bound driver is a second signal worth keeping: `cdc_ncm` bound means the
    host has actually claimed the network function, not merely that the
    descriptor advertises it.
    """
    out: list[dict] = []
    for iface in sorted(glob.glob(f"{_SYSFS_USB}/{path}:*")):
        try:
            cls = Path(f"{iface}/bInterfaceClass").read_text().strip().lower()
            sub = Path(f"{iface}/bInterfaceSubClass").read_text().strip().lower()
            proto = Path(f"{iface}/bInterfaceProtocol").read_text().strip().lower()
        except OSError:
            continue
        driver = None
        try:
            driver = os.path.basename(os.path.realpath(f"{iface}/driver"))
        except OSError:
            pass
        out.append({"iface": os.path.basename(iface), "cls": cls, "sub": sub,
                    "proto": proto, "driver": driver})
    return out


def gadget_composition(path: str) -> dict:
    """What this watch offers right now, from its interfaces alone.

    Returns {adb, ncm, mass_storage_only, interfaces}. `ncm` True means the
    network function is live NOW -- a host netdev exists for it. It says
    nothing about whether the watch is CAPABLE of NCM but not carrying it,
    which only an on-device configfs probe can answer.

    `mass_storage_only` is the dead composition: usb-moded's charging-only
    fallback, reached when it is asked for a mode this kernel cannot provide.
    A port power cycle CANNOT fix it -- the problem is the composition, not the
    enumeration, so cycling re-enumerates the same broken gadget. Only a reboot
    recovers it, and saying so beats offering a cycle that silently does
    nothing.
    """
    ifaces = usb_interfaces(path)
    adb = any((i["cls"], i["sub"], i["proto"]) == _IFACE_ADB for i in ifaces)
    ncm = any((i["cls"], i["sub"]) == _IFACE_NCM_CTRL for i in ifaces)
    storage = any(i["cls"] == _IFACE_MASS_STORAGE for i in ifaces)
    return {"adb": adb, "ncm": ncm,
            "mass_storage_only": bool(storage and not adb and not ncm),
            "interfaces": ifaces}


def usb_netdev_for(path: str) -> "str | None":
    """The host network interface belonging to this watch, resolved through USB
    topology.

    Never match on interface names: they are generated from the USB path
    (enp0s20u3u4u3i1), differ per port and per host, and adjacent watches differ
    by one character -- the porting session lost an evening to an ssh attempt
    against a neighbour's interface name.
    """
    for netdir in glob.glob("/sys/class/net/*"):
        try:
            dev = os.path.realpath(f"{netdir}/device")
        except OSError:
            continue
        if f"/{path}/" in dev + "/" or dev.rstrip("/").endswith(f"/{path}"):
            return os.path.basename(netdir)
        if f"/{path}:" in dev:
            return os.path.basename(netdir)
    return None


def ncm_peer_link_local(iface: str, timeout: float = 3.0) -> "str | None":
    """The watch's IPv6 link-local on this interface, or None.

    Discovered, never cached: the address is EUI-64 from the watch's usb0 MAC,
    and the kernel generates that MAC randomly EVERY time the ncm function is
    created -- i.e. every boot. Caching it would address a watch that has since
    moved. What is stable, and worth caching, is hub:port -> iface.

    One all-nodes multicast ping populates the neighbour table, then the table
    answers. Both are local to this link; nothing is routed and no address is
    assigned at either end.

    Returns None when the HOST interface has no link-local of its own -- with
    no source address on the link there is nothing to ping from. That happens
    when NetworkManager sets addr_gen_mode=none and has not assigned one, and
    the remedy is host configuration, not a watch problem.
    """
    _run(f"ping -6 -c 2 -W 1 ff02::1%{iface}", check=False, timeout=timeout)
    rc, out, _ = _run(f"ip -6 neigh show dev {iface}", check=False, timeout=timeout)
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        # "fe80::ba:45ff:fe0b:aeb8 lladdr 02:ba:45:0b:ae:b8 REACHABLE"
        if len(parts) >= 3 and parts[0].lower().startswith("fe80:") \
                and "lladdr" in parts and "FAILED" not in line:
            return parts[0]
    return None


def host_has_link_local(iface: str) -> bool:
    """Whether the HOST end of this link has an address to speak from."""
    rc, out, _ = _run(f"ip -6 addr show dev {iface} scope link", check=False, timeout=3)
    return rc == 0 and "inet6 fe80:" in out


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


def usb_topology_fingerprint() -> int:
    """A cheap fingerprint of what is enumerated on the bus: the set of device
    entries under /sys/bus/usb/devices, interface dirs excluded. Changes exactly
    when a device appears or vanishes — one ~1ms directory listing, no USB
    traffic. Lets the status cache keep its TTL for the expensive per-watch
    reads while reacting to enumeration changes on the very next request."""
    return hash(tuple(sorted(
        e for e in os.listdir("/sys/bus/usb/devices") if ":" not in e)))


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


def _sysfs_path_to_serial_map(serials: set[str],
                              adb_paths: "dict[str, str] | None" = None
                              ) -> dict[str, str]:
    """Single-pass sysfs scan: return {sysfs_path: serial} for the given serials.

    GHOST NODES. Move a watch between ports and the hub may never raise a
    disconnect for the old one, leaving the SAME serial readable at two paths
    at once. Both look completely alive — same bConfigurationValue, same
    authorized flag, same speed — so nothing about the node itself gives it
    away, and the UI then shows one watch sitting on two ports (moWerk saw
    sparrow on both 1-9.2 and a stale 1-9.4.1).

    `adb_paths` breaks the tie: {serial: "1-9.2"} from `adb devices -l`, which
    is authoritative because the adb server is actually talking to the device
    down that path. Where adb names a path, every other path claiming that
    serial is a ghost and is dropped. Without adb_paths the old behaviour is
    kept, ghosts and all — a caller with no adb view is no worse off.
    """
    result: dict[str, str] = {}
    for path in glob.glob("/sys/bus/usb/devices/*/serial"):
        try:
            with open(path) as f:
                s = f.read().strip()
                if s in serials:
                    result[os.path.basename(os.path.dirname(path))] = s
        except OSError:
            pass
    if not adb_paths:
        return result
    seen: dict[str, list[str]] = {}
    for p, s in result.items():
        seen.setdefault(s, []).append(p)
    for serial, paths in seen.items():
        if len(paths) < 2:
            continue
        live = adb_paths.get(serial)
        if not live or live not in paths:
            continue                      # adb has no opinion — keep them all
        for p in paths:
            if p != live:
                log.debug("dropping ghost node %s for %s (adb says %s)",
                          p, serial, live)
                result.pop(p, None)
    return result


def adb_usb_paths(devices: dict) -> "dict[str, str]":
    """{serial: sysfs path} from an `adb devices -l` parse. Pure — see tests."""
    out = {}
    for serial, info in (devices or {}).items():
        if isinstance(info, dict) and info.get("usb"):
            out[serial] = info["usb"]
    return out


def _sysfs_serial_at(loc: str, port: int) -> "str | None":
    """The USB serial of the device enumerated at this hub port, or None."""
    try:
        with open(f"/sys/bus/usb/devices/{loc}.{port}/serial") as f:
            return f.read().strip()
    except OSError:
        return None


# What a USB interface descriptor says the device is speaking. Keyed on the
# descriptor rather than idProduct because the fleet is not consistent about
# product IDs — watches enumerate adb as 18d1:d001 on some images and 18d1:0a03
# on others, and the ASUS-built ones present a 0afe vendor entirely. The
# descriptor is the same everywhere.
_LINK_BY_IFACE = {
    ("ff", "42", "01"): "adb",
    ("ff", "42", "03"): "fastboot",
    ("e0", "01", "03"): "rndis",       # SSH-over-USB (developer mode)
}


def port_device_info(loc: str, port: int) -> "dict | None":
    """Everything sysfs knows about whatever is enumerated at a hub port, or
    None if the port is genuinely empty.

    Deliberately independent of the adb server, of fastboot, and of whether we
    recognise the vendor: a port row built only from `adb devices` shows EMPTY
    for a watch that is sitting right there in fastboot, in storage mode, or
    presenting an unfamiliar vendor — which is exactly how a fastboot catfish
    and the ASUS 0afe presentations stayed invisible for a whole night.

    `configured` is the xHCI-exhaustion tell: a device can enumerate and be
    given no configuration at all when the controller is out of device slots.
    It is present on the bus and unusable, which reads identically to "broken
    watch" unless it is named.
    """
    base = Path(f"/sys/bus/usb/devices/{loc}.{port}")
    if not base.exists():
        return None
    # A hub chip is not a device a watch can be on: these are the cascade
    # ports feeding a sub-hub, and reporting them as devices would put a
    # phantom entry on every cascade row.
    if _read_attr(base / "bDeviceClass").lower() == "09":
        return None

    links = set()
    for iface in base.glob(f"{loc}.{port}:*"):
        sig = tuple(_read_attr(iface / f"bInterface{k}").lower()
                    for k in ("Class", "SubClass", "Protocol"))
        if sig in _LINK_BY_IFACE:
            links.add(_LINK_BY_IFACE[sig])
        elif sig[0] == "08":                      # mass storage
            links.add("storage")

    # One device can advertise several (adb + storage is common). Report the
    # one that decides what we can DO with it, most actionable first.
    for candidate in ("adb", "fastboot", "rndis", "storage"):
        if candidate in links:
            link = candidate
            break
    else:
        link = "unknown"

    cfg_val = _read_attr(base / "bConfigurationValue")
    return {
        "serial": _read_attr(base / "serial") or None,
        "vid": _read_attr(base / "idVendor").lower(),
        "pid": _read_attr(base / "idProduct").lower(),
        "link": link,
        # "" or "0" both mean the kernel never configured it.
        "configured": bool(cfg_val and cfg_val != "0"),
    }


def _read_attr(path: Path) -> str:
    """A sysfs attribute as text, or "" — never raises. Attribute reads race
    with device teardown constantly on this rig, and a status refresh must not
    die because a watch left mid-scan.

    Case is preserved: serials are case-sensitive identifiers and must match
    what adb reports. Callers comparing hex descriptors lowercase explicitly.
    """
    try:
        return path.read_text().strip()
    except OSError:
        return ""


# An xHCI controller has a fixed number of DEVICE SLOTS — 32 on the Intel
# 8-Series in the rig, which is where this default comes from. The exact figure
# lives in the controller's HCSPARAMS1 register, readable only via debugfs as
# root, so it is a per-controller constant here rather than a live reading.
# Override with `xhci_max_slots` in config if a different controller turns up.
XHCI_DEFAULT_MAX_SLOTS = 32


def xhci_buses() -> "list[str]":
    """The USB bus numbers driven by an xHCI controller.

    EHCI buses do not draw from the same pool, so counting them would make the
    budget look tighter than it is.
    """
    out = []
    for root in _SYSFS_USB.glob("usb*"):
        try:
            # resolve() first: these are symlinks into the real device tree,
            # and Path.parent is lexical — without it the "parent" is just the
            # devices directory and no controller is ever found.
            uevent = (root.resolve().parent / "uevent").read_text()
        except OSError:
            continue
        if "DRIVER=xhci_hcd" in uevent:
            out.append(root.name.replace("usb", ""))
    return sorted(out)


def xhci_slots(max_slots: "int | None" = None) -> dict:
    """Device-slot budget for the xHCI controller: {used, max, buses}.

    EVERY device on an xHCI bus takes a slot, hubs included — which is the
    whole trap: a cascaded hub tree spends a dozen slots before a single watch
    appears. When the pool runs dry the controller enumerates a device and then
    refuses to configure it (`error -12`), so watches appear to be present and
    broken rather than crowded out. This is the number that explains the
    two-A16 breakdown; see docs/audits/2026-07-25-usb-brittleness-xhci-slots.
    """
    buses = xhci_buses()
    used = 0
    for dev in _SYSFS_USB.glob("*-*"):
        if ":" in dev.name:                       # interface dir
            continue
        bus = dev.name.split("-", 1)[0]
        if bus in buses:
            used += 1
    return {"used": used, "max": int(max_slots or XHCI_DEFAULT_MAX_SLOTS),
            "buses": buses}


def _sysfs_adb_serials() -> set[str]:
    """Serials of watches currently exposing an ADB interface per sysfs — the
    ground truth of what is on the bus in adb mode, independent of the adb
    SERVER (which can wedge and list nothing) and of per-watch product IDs.

    Keyed on the USB interface signature (class ff / subclass 42 / protocol 01)
    rather than idProduct: the fleet's watches enumerate adb as 18d1:d001, not
    the 0a03 the mode-detector assumes, but they all advertise the standard
    Android ADB interface descriptor."""
    out: set[str] = set()
    for path in glob.glob("/sys/bus/usb/devices/*/serial"):
        dev = os.path.dirname(path)
        has_adb = False
        for iface in glob.glob(f"{dev}/*:*"):
            try:
                if (open(f"{iface}/bInterfaceClass").read().strip() == "ff"
                        and open(f"{iface}/bInterfaceSubClass").read().strip() == "42"
                        and open(f"{iface}/bInterfaceProtocol").read().strip() == "01"):
                    has_adb = True
                    break
            except OSError:
                pass
        if has_adb:
            try:
                with open(path) as f:
                    out.add(f.read().strip())
            except OSError:
                pass
    return out


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


