# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Config file I/O, typed settings, and codename/serial/port lookups.

Two layers (beroset's design): ConfigManager owns the file — load, defaults
merge, save, and the lock serializing read-modify-write cycles. The typed
contents are dataclasses: ChargeConfig / FlashConfig carry the settings with
their defaults in one place, so consumers write `charge.low_threshold`
instead of scattering `.get("low_threshold", 40)` fallbacks.

The hubs/serials mappings stay as plain dicts inside the raw config: their
shape is the config *file's* shape (ports, sockets, excludes, per-port
serials), edited in place by map/remap/soft-remap."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from dataclasses import dataclass, fields
from pathlib import Path

from .adb import adb_devices, is_a_serial


CONFIG_DIR = Path.home() / ".config" / "asteroid-docking-bay"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ChargeConfig:
    low_threshold: int = 40
    high_threshold: int = 80
    charge_duration_minutes: int = 30    # blind-charge fallback duration
    charge_max_minutes: int = 240        # hard cap for charge-to-target
    # Informational — actual scheduling is done by the systemd timer.
    check_interval_hours: int = 12
    adb_wait_seconds: int = 15
    adb_wait_retries: int = 8
    onboard_wait_seconds: int = 30       # boot window per onboarding attempt
    # A drain reading powers the port on (charging) only until ADB reconnects,
    # so poll fast there — the watch is already booted and re-enumerates in a
    # few seconds. Polling on the 15 s charge interval kept it charging ~12 s
    # longer than needed every read, a systematic bump that overrates standby.
    drain_read_poll_seconds: int = 3
    # Adaptive cadence: skip waking a watch during check-charge until its
    # observed standby drain projects it near low_threshold. Watches with
    # no drain history are always checked.
    adaptive_cadence: bool = True
    adaptive_margin_pct: int = 10        # wake when projected at low+this
    adaptive_max_interval_days: int = 14 # never skip longer than this
    # Ideal rest state is in-band AND powered off: after a charge or drain
    # test, shut the watch down over ADB before cutting the port.
    graceful_poweroff: bool = True
    # A drain test ends at ~15% (the floor). When true, charge it back up to
    # low_threshold before powering off, so it stores in the healthy band
    # rather than sitting drained. Off by default: leaves the test result
    # watch as-is unless you opt in.
    drain_rest_recharge: bool = False
    # Opt-in self-heal: a mapped port that reports power but never enumerates a
    # connection for >60s is the stale-node/fake-power wedge — power-cycle it
    # once (with backoff) to recover. Off by default: it actuates hardware.
    fake_power_self_heal: bool = False

    @classmethod
    def from_dict(cls, d: "dict | None") -> "ChargeConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class FlashConfig:
    nightly_url: str = "https://release.asteroidos.org/nightlies"
    download_dir: str = str(Path.home() / ".local" / "share"
                            / "asteroid-docking-bay" / "nightlies")

    @classmethod
    def from_dict(cls, d: "dict | None") -> "FlashConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def charge_config(cfg: dict) -> ChargeConfig:
    """The typed charge settings from a raw config dict."""
    return ChargeConfig.from_dict(cfg.get("charge"))


def flash_config(cfg: dict) -> FlashConfig:
    """The typed flash settings from a raw config dict."""
    return FlashConfig.from_dict(cfg.get("flash"))


class _ConfigLock:
    """Serializes config read-modify-write cycles in this process AND across
    processes.

    A threading.Lock covers the web service's own threads, but the CLI is a
    SEPARATE process running its own load → modify → save cycles: the
    check-charge timer on a systemd timer, `map`, `on`/`off`. Two cycles
    interleaving lose one side's write completely, and one of the things that
    can vanish that way is a live operation lock — the marker that stops
    housekeeping from cutting a running dump. So the same region also takes an
    flock on a sidecar file, the pattern uhubctl access already uses.

    Used only as a context manager, which every call site already does. Not
    reentrant, exactly like the threading.Lock it replaces.
    """

    def __init__(self, path: Path):
        self._thread_lock = threading.Lock()
        self._path = Path(path)
        self._fd: "int | None" = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            if self._fd is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            # A read-only or exotic filesystem must not make the config
            # unusable: fall back to in-process serialization, which is what
            # this had before.
            self._fd = None
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc) -> bool:
        try:
            if self._fd is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._thread_lock.release()
        return False


class ConfigManager:
    """Owns the config file: load with defaults merged in, atomic-enough
    save, and the lock serializing read-modify-write cycles so concurrent
    web requests (flash, charge, remap) don't corrupt config.json."""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self.lock = _ConfigLock(Path(path).with_name(Path(path).name + ".lock"))

    def load(self) -> dict:
        if not self.path.exists():
            return {"hubs": [], "serials": {},
                    "charge": {}, "flash": {}}
        with self.path.open() as f:
            cfg = json.load(f)
        for key, default in (("hubs", []), ("serials", {}),
                             ("charge", {}), ("flash", {}), ("hub_names", {})):
            cfg.setdefault(key, default)
        return cfg

    def save(self, cfg: dict) -> None:
        """Write the config so a reader never sees a half-written file.

        The old truncate-in-place left invalid JSON behind if the process died
        mid-write (or the disk filled), and nothing catches the decode error —
        every op and the whole status page would break, with the fleet's port
        mappings and operation locks unreadable. Write a sibling temp file,
        fsync it, then os.replace, which is atomic on POSIX: a reader sees
        either the old file or the new one, never a truncated one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


config_manager = ConfigManager()

# Module-level shims: the raw-dict load/save API used throughout. The lock is
# the manager's, shared with every read-modify-write cycle.
_config_lock = config_manager.lock


def load_config() -> dict:
    return config_manager.load()


def save_config(cfg: dict) -> None:
    config_manager.save(cfg)


# ── Config lookups ────────────────────────────────────────────────────────────

def find_port_for_codename(cfg: dict, codename: str) -> tuple[str | None, int | None]:
    """Return (hub_location, port) for a codename, or (None, None).

    Machine-name only and returns the FIRST match, so it cannot address one
    of several watches that share an image. New code should route through
    find_ports_for_target / resolve_single_port, which understand exact
    codenames and serials; this stays for the map/test-ports paths that
    genuinely operate per machine-image."""
    for hub in cfg.get("hubs", []):
        for port_str, cname in hub.get("ports", {}).items():
            if cname.lower() == codename.lower():
                return hub["location"], int(port_str)
    return None, None


# ── Hub naming ────────────────────────────────────────────────────────────────
# A physical hub (an A16) presents as a tree of cascaded chips at several USB
# locations (1-2, 1-2.3, 1-2.3.3, …). Users think in physical boxes, not chip
# locations — and when one A16 hangs off a dock its locations nest INSIDE the
# dock's (the second A16 is 1-9.1 under Lenovo dock 1-9), so which row belongs to
# which box is not obvious from the address alone. hub_names maps a location
# PREFIX to a friendly name; a port inherits the name of the longest prefix that
# covers it, so naming the physical top names every chip and port beneath it.

_A16_VENDOR = "0bda"          # Realtek — the A16 smart-hub chipset
_DOCK_VENDORS = {"17ef"}      # Lenovo dock and kin


def _loc_covers(prefix: str, location: str) -> bool:
    """True if `prefix` is `location` or an ancestor of it, matched on
    dot-separated component boundaries (1-9.1 covers 1-9.1.3 but not 1-9.10)."""
    return location == prefix or location.startswith(prefix + ".")


def hub_name_entry_for(cfg: dict, location: str) -> "tuple[str | None, str | None]":
    """The (prefix, name) covering this hub location — the longest matching
    prefix in cfg['hub_names'] — or (None, None) if unnamed. The prefix is the
    physical box's address, the handle a rename targets."""
    best = None
    for prefix in cfg.get("hub_names", {}):
        if _loc_covers(prefix, location) and (best is None or len(prefix) > len(best)):
            best = prefix
    return (best, cfg["hub_names"][best]) if best is not None else (None, None)


def hub_name_for(cfg: dict, location: str) -> "str | None":
    """Friendly name covering this hub location, or None if unnamed."""
    return hub_name_entry_for(cfg, location)[1]


def set_hub_name(cfg: dict, prefix: str, name: str) -> None:
    """Assign the friendly name for a hub address; an empty name clears it."""
    names = cfg.setdefault("hub_names", {})
    if name.strip():
        names[prefix] = name.strip()
    else:
        names.pop(prefix, None)


def derive_hub_names(hubs: "list[dict]") -> "dict[str, str]":
    """Auto-name the physical hubs from a topology scan. `hubs` is a list of
    {'location', 'vendor'}. A physical box is a maximal run of same-vendor
    cascaded chips; its name is keyed to the shallowest (top) location, whose
    parent chip — if any — is a different vendor. A16 boxes are numbered in
    discovery order; a Lenovo dock is named as such."""
    vendor = {h["location"]: (h.get("vendor") or "") for h in hubs}
    locs = set(vendor)

    def parent_loc(loc: str) -> "str | None":
        p = loc.rpartition(".")[0]
        return p if p in locs else None

    tops = [loc for loc in locs
            if (p := parent_loc(loc)) is None or vendor[p] != vendor[loc]]

    names: dict[str, str] = {}
    a16_n = dock_n = 0
    for loc in sorted(tops, key=lambda l: (l.count("."), l)):
        v = vendor[loc]
        if v == _A16_VENDOR:
            a16_n += 1
            names[loc] = f"A16 #{a16_n}"
        elif v in _DOCK_VENDORS:
            dock_n += 1
            names[loc] = "Lenovo dock" if dock_n == 1 else f"Lenovo dock #{dock_n}"
        else:
            names[loc] = f"Hub @ {loc}"
    return names


def register_hubs(cfg: dict, hubs: "list[dict]") -> "list[dict]":
    """(Re)register scanned hubs into cfg["hubs"], preserving everything already
    learned for each one -- watch mappings, per-port smart verdicts, socket
    labels, excludes -- and refreshing only the advertised ppps flag. Hubs in
    the config that this scan did not see (temporarily unplugged) are kept
    untouched rather than dropped.

    Shared by the `map` command and the guided setup so the two cannot drift:
    a second implementation of this merge would be a second set of rules about
    what survives a re-map.
    """
    existing = {hub["location"]: hub for hub in cfg.get("hubs", [])}
    scanned = {hub["location"] for hub in hubs}
    registered: list[dict] = []
    for hub in hubs:
        loc = hub["location"]
        entry = {**existing.get(loc, {}), "location": loc,
                 "ppps": hub.get("ppps", False),
                 "description": hub.get("description", "")}
        entry.setdefault("ports", {})
        entry.setdefault("port_smart", {})
        registered.append(entry)
    cfg["hubs"] = registered + [hub for hub in cfg.get("hubs", [])
                                if hub["location"] not in scanned]
    return registered


def seed_hub_names(cfg: dict, vendor_hubs: "list[dict]") -> bool:
    """If hub_names is unset, auto-name the physical boxes that actually contain
    a configured hub (so stray host hubs on other buses aren't named). Returns
    True if anything was added, so the caller knows to save."""
    if cfg.get("hub_names"):
        return False
    cfg_locs = [h["location"] for h in cfg.get("hubs", [])]
    kept = {prefix: name for prefix, name in derive_hub_names(vendor_hubs).items()
            if any(_loc_covers(prefix, loc) for loc in cfg_locs)}
    if kept:
        cfg["hub_names"] = kept
    return bool(kept)


# ── exact codenames ──────────────────────────────────────────────────────────
# The config keys ports and serials on the MACHINE (image) name — the thing
# that gets flashed — which several physically different watches share (a
# TicWatch E2 ships and reports `skipjack` but is really `tunny`). The exact
# per-device codename is detected live from androidboot.bootloader (see
# variants.exact_codename) and stored here per serial, so it survives across
# processes and is available to the CLI, which has no live detection of its
# own. Exact codenames are globally unique, so they make an unambiguous
# address where a shared machine name cannot.

def exact_codename_for_serial(cfg: dict, serial: "str | None") -> "str | None":
    # A key that cannot be a serial is not an identity, wherever it came from.
    # The config can hold one from before this was checked -- the rig carries
    # `systempart=/dev/mapper/system` in serials, port_serials AND
    # exact_codenames, learned from a watch mid-port that leaked a kernel
    # cmdline fragment into its USB serial descriptor. A port configured as one
    # watch then RENDERED as another, because the poisoned lookup outranked the
    # port map. Refusing it here covers every consumer at once, instead of the
    # one call site that noticed.
    if not is_a_serial(serial):
        return None
    return cfg.get("exact_codenames", {}).get(serial) if serial else None


# ── SSH-mode IP allocation ───────────────────────────────────────────────────
# A watch in developer/SSH USB mode brings up an rndis link and, by default,
# takes 192.168.2.15 — a fixed address inherited from SailfishOS. Every watch
# uses the same one, so two watches switched to SSH in quick succession on the
# same rig would both claim 192.168.2.15 on different host interfaces, and the
# host could no longer tell them apart by address. We hand each watch a unique
# IP instead (set via usb_moded_util -n before the switch), starting at
# 192.168.13.37 — the "LEET" address, chosen to avoid the common 192.168.2.x
# home-network clash too. Assignment is sticky per serial, so a watch keeps its
# address across sessions.

SSH_IP_BASE = "192.168.13.37"


def ssh_ip_for_serial(cfg: dict, serial: "str | None") -> "str | None":
    return cfg.get("ssh_ips", {}).get(serial) if serial else None


def usb_mode_preference(cfg: dict) -> str:
    """The fleet's preferred USB mode for auto-correcting a watch that comes up
    on its own in the wrong one: "adb" (standard) or "ssh". This is the
    situational top-bar toggle, not a hard install setting — defaults to "adb"
    (stock, and how a fresh flash enumerates), and ignores any junk value."""
    pref = cfg.get("usb_mode_preference")
    return pref if pref in ("adb", "ssh") else "adb"


def allocate_ssh_ip(cfg: dict, serial: str) -> str:
    """The SSH-mode IP for a serial, assigning the next free one from the base
    up on first use. Sticky: a serial always gets back the same address."""
    import ipaddress
    table = cfg.setdefault("ssh_ips", {})
    if serial in table:
        return table[serial]
    used = set(table.values())
    base = int(ipaddress.ip_address(SSH_IP_BASE))
    for offset in range(0, 200):          # 192.168.13.37 .. .236
        cand = str(ipaddress.ip_address(base + offset))
        if cand not in used:
            table[serial] = cand
            return cand
    # Pool exhausted (200 watches on one rig is not a real scenario); fall back
    # to the base rather than raising, so a switch is never blocked by this.
    return SSH_IP_BASE


def record_exact_codename(cfg: dict, serial: "str | None",
                          exact: "str | None") -> bool:
    """Persist a detected exact codename for a serial. Returns True only when
    it actually changed, so a hot-path caller can skip the config write on the
    common case where identity is already known and stable."""
    if not serial or not exact:
        return False
    table = cfg.setdefault("exact_codenames", {})
    if table.get(serial) == exact:
        return False
    table[serial] = exact
    return True


# ── Orbit port members ───────────────────────────────────────────────────────
# The Orbit port is a virtual hub of fleet watches reachable over the air
# (WiFi-SSH) rather than on a USB socket. Members are keyed by serial — the same
# identity a docked watch has — so a watch is one fleet member whether it is in a
# cradle or in orbit. Reachability is never stored (it is probed live, like a
# docked watch's connection state); only the persistent facts learned at launch
# live here.

def orbit_members(cfg: dict) -> dict:
    """The orbiting watches: {serial: {codename, ip, wlanmac, resolution, added}}."""
    return cfg.get("orbit", {})


def orbit_add(cfg: dict, member: dict) -> None:
    """Launch a watch into orbit, or refresh one, keyed by its serial. A re-launch
    (same watch, new IP) overwrites the stored facts rather than duplicating it."""
    cfg.setdefault("orbit", {})[member["serial"]] = member


def orbit_forget(cfg: dict, serial: "str | None") -> bool:
    """De-orbit a watch. Returns True only when one was actually removed, so a
    caller can skip the config write on a no-op de-orbit."""
    return cfg.get("orbit", {}).pop(serial, None) is not None if serial else False


# ── hands calibration ────────────────────────────────────────────────────────
# A physical-hands watch (narwhal) is driven by motor_move_all "minute:hour", each
# 0..179 = an ABSOLUTE angle at 180 steps/turn (2 deg/step; decoded on hardware
# 2026-07-23, see dodoradio's sop716 driver). It re-syncs sop716/position, so once
# we know the fixed motor-zero offset (the physical angle a hand shows at motor
# value 0 — measured ~102 deg for the minute hand, ~108 deg for the hour hand), a
# drag angle maps exactly to a motor value and time/choreography are exact. The
# offset is a fixed mounting property, calibrated once (re-done only after a
# dead-battery timepiece drift), stored per serial in DEGREES per hand.

HANDS_ZERO_MIN_DEG = 102.0   # bench default: minute hand at motor value 0
HANDS_ZERO_HR_DEG = 108.0    # bench default: hour hand at motor value 0


def hands_cal_for(cfg: dict, serial: "str | None") -> dict:
    """The motor-zero offset (physical degrees at motor value 0) per hand, or the
    bench defaults. {min_deg, hr_deg}; the frontend uses it to map a drag angle to
    a motor value."""
    stored = cfg.get("hands_cal", {}).get(serial, {}) if serial else {}
    return {"min_deg": stored.get("min_deg", HANDS_ZERO_MIN_DEG),
            "hr_deg": stored.get("hr_deg", HANDS_ZERO_HR_DEG)}


def set_hands_cal(cfg: dict, serial: str, min_deg: float, hr_deg: float) -> None:
    """Persist a serial's per-hand motor-zero offset (degrees), learned by the
    Calibrate mode's overlap/oppose match."""
    cfg.setdefault("hands_cal", {})[serial] = {"min_deg": float(min_deg),
                                               "hr_deg": float(hr_deg)}


class AmbiguousTargetError(ValueError):
    """A machine/image name that maps to several physical watches, with
    nothing given to pick one. Carries the candidates so the caller can tell
    the user exactly what to type instead of guessing."""

    def __init__(self, target: str, candidates: "list[dict]"):
        self.target = target
        self.candidates = candidates
        # Always show the serial: two watches can share an exact codename (two
        # tunnys), so the serial is the only guaranteed disambiguator. Lead
        # with the exact codename when it adds anything over the target name.
        lines = "\n".join(
            f"    {c['serial'] or '(no serial)'}"
            f"{'' if not c.get('exact') or c['exact'].lower() == target.lower() else '  (' + c['exact'] + ')'}"
            f"  at {c['loc']}:p{c['port']}"
            for c in candidates)
        super().__init__(
            f"'{target}' matches {len(candidates)} watches — name one by its "
            f"serial (or exact codename where it differs):\n{lines}")


def _port_descriptors(cfg: dict) -> "list[dict]":
    """Every configured port as {loc, port, machine, serial, exact}. serial
    comes from the per-port binding (port_serials); exact is looked up from
    that serial. Both are None when unknown, which is fine — only the address
    kinds that need them fail to match."""
    out: "list[dict]" = []
    for hub in cfg.get("hubs", []):
        loc = hub["location"]
        bound = hub.get("port_serials", {})
        for port_str, machine in hub.get("ports", {}).items():
            serial = bound.get(port_str)
            out.append({
                "loc": loc, "port": int(port_str), "machine": machine,
                "serial": serial,
                "exact": exact_codename_for_serial(cfg, serial),
            })
    return out


def find_ports_for_target(cfg: dict, target: str) -> "list[dict]":
    """Resolve an address to matching port descriptors. An address is 'all',
    a serial, an exact codename, or a machine/image name, tried in that order
    of specificity. Serials and exact codenames are globally unique so they
    match at most one port; a shared machine name may match several — that is
    the ambiguity resolve_single_port turns into an actionable error."""
    descs = _port_descriptors(cfg)
    if target == "all":
        return descs
    t = target.lower()
    by_serial = [d for d in descs if d["serial"] and d["serial"].lower() == t]
    if by_serial:
        return by_serial
    by_exact = [d for d in descs if d["exact"] and d["exact"].lower() == t]
    if by_exact:
        return by_exact
    return [d for d in descs if d["machine"] and d["machine"].lower() == t]


def resolve_single_port(cfg: dict, target: str) -> "dict | None":
    """One port descriptor for a command that acts on a single watch, or None
    if nothing matches. Raises AmbiguousTargetError when a shared machine name
    matches several and no exact codename/serial was given to pick one."""
    hits = find_ports_for_target(cfg, target)
    if not hits:
        return None
    if len(hits) > 1:
        raise AmbiguousTargetError(target, hits)
    return hits[0]


def find_serial_for_codename(cfg: dict, codename: str) -> str | None:
    for serial, cname in cfg.get("serials", {}).items():
        if cname.lower() == codename.lower():
            return serial
    return None


def find_codename_for_serial(cfg: dict, serial: str) -> str | None:
    # A key that cannot be a serial is not an identity, wherever it came from.
    # The config can hold one from before this was checked -- the rig carries
    # `systempart=/dev/mapper/system` in serials, port_serials AND
    # exact_codenames, learned from a watch mid-port that leaked a kernel
    # cmdline fragment into its USB serial descriptor. A port configured as one
    # watch then RENDERED as another, because the poisoned lookup outranked the
    # port map. Refusing it here covers every consumer at once, instead of the
    # one call site that noticed.
    if not is_a_serial(serial):
        return None
    return cfg.get("serials", {}).get(serial)



def find_codename_for_loc_port(cfg: dict, loc: str, port: int) -> str | None:
    for hub in cfg.get("hubs", []):
        if hub["location"] == loc:
            return hub.get("ports", {}).get(str(port))
    return None


def loc_port_for_serial(cfg: dict, serial: str) -> "tuple[str, int] | None":
    """Where this serial is seated: (location, port), or None if unknown.

    The inverse of find_serial_for_loc_port, but deliberately NOT its mirror.
    That function falls back to the codename when no exact binding exists,
    which is right for answering "who is probably on this port". It is wrong
    here: callers use this answer to CUT POWER, and with two watches of the
    same codename a guess would cycle the innocent one. So only an exact
    per-port binding counts, and an unbound serial reports None rather than a
    best effort.
    """
    if not serial:
        return None
    for hub in cfg.get("hubs", []):
        for port_str, bound in (hub.get("port_serials") or {}).items():
            if bound != serial:
                continue
            try:
                return hub["location"], int(port_str)
            except (KeyError, ValueError):
                return None      # a malformed entry is not a port to cut
    return None


def find_serial_for_loc_port(cfg: dict, loc: str, port: int) -> str | None:
    """Return the best serial for a specific hub port.

    An exact per-port serial binding (port_serials, maintained by remap and
    the live soft-remap) wins.  Otherwise fall back to the codename, and
    prefer a currently-connected serial over a config-only entry, so two
    same-codename watches don't answer for each other.
    """
    for hub in cfg.get("hubs", []):
        if hub["location"] == loc:
            bound = hub.get("port_serials", {}).get(str(port))
            if bound:
                return bound
            break
    codename = find_codename_for_loc_port(cfg, loc, port)
    if not codename:
        return None
    matching = [serial for serial, cname in cfg.get("serials", {}).items()
                if cname.lower() == codename.lower()]
    if len(matching) == 1:
        return matching[0]
    connected = set(adb_devices().keys())
    for serial in matching:
        if serial in connected:
            return serial
    return matching[0] if matching else None


def all_configured_codenames(cfg: dict) -> list[str]:
    names: list[str] = []
    for hub in cfg.get("hubs", []):
        names.extend(hub.get("ports", {}).values())
    return names


def is_port_smart(cfg: dict, codename: str) -> bool | None:
    """
    Return True  — per-port switching confirmed by live test.
    Return False — confirmed NOT switchable (dumb hub port).
    Return None  — not yet tested; run 'map' or 'test-ports' to find out.
    """
    for hub in cfg.get("hubs", []):
        for port_str, cname in hub.get("ports", {}).items():
            if cname.lower() == codename.lower():
                return hub.get("port_smart", {}).get(port_str)
    return None


def is_slot_smart(cfg: dict, loc: str, port: int) -> bool | None:
    """Per-port variant of is_port_smart — unambiguous with duplicate codenames."""
    for hub in cfg.get("hubs", []):
        if hub["location"] == loc:
            return hub.get("port_smart", {}).get(str(port))
    return None


def _store_smart_verdict(hub: dict, port: int, smart: "bool | None") -> None:
    """Record a PPPS test result on a hub entry, keeping a proven verdict
    sticky: an inconclusive (None) re-test must not erase a previously
    confirmed smart/not-smart result — that's what made the verdict flicker
    to '?' after a marginal Refresh. A conclusive True/False always updates
    (a port genuinely can change)."""
    existing = hub.get("port_smart", {}).get(str(port))
    if smart is not None or existing is None:
        hub.setdefault("port_smart", {})[str(port)] = smart


def _resolve_targets(codename_arg: str, cfg: dict) -> list[str]:
    if codename_arg == "all":
        return all_configured_codenames(cfg)
    return [codename_arg]


