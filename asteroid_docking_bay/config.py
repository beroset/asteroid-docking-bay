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

import json
import threading
from dataclasses import dataclass, fields
from pathlib import Path

from .adb import adb_devices


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
    # Adaptive cadence: skip waking a watch during check-charge until its
    # observed standby drain projects it near low_threshold. Watches with
    # no drain history are always checked.
    adaptive_cadence: bool = True
    adaptive_margin_pct: int = 10        # wake when projected at low+this
    adaptive_max_interval_days: int = 14 # never skip longer than this
    # Ideal rest state is in-band AND powered off: after a charge or drain
    # test, shut the watch down over ADB before cutting the port.
    graceful_poweroff: bool = True

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


class ConfigManager:
    """Owns the config file: load with defaults merged in, atomic-enough
    save, and the lock serializing read-modify-write cycles so concurrent
    web requests (flash, charge, remap) don't corrupt config.json."""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        if not self.path.exists():
            return {"hubs": [], "serials": {},
                    "charge": {}, "flash": {}}
        with self.path.open() as f:
            cfg = json.load(f)
        for key, default in (("hubs", []), ("serials", {}),
                             ("charge", {}), ("flash", {})):
            cfg.setdefault(key, default)
        return cfg

    def save(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")


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
    """Return (hub_location, port) for a codename, or (None, None)."""
    for hub in cfg.get("hubs", []):
        for port_str, cname in hub.get("ports", {}).items():
            if cname.lower() == codename.lower():
                return hub["location"], int(port_str)
    return None, None


def find_serial_for_codename(cfg: dict, codename: str) -> str | None:
    for serial, cname in cfg.get("serials", {}).items():
        if cname.lower() == codename.lower():
            return serial
    return None


def find_codename_for_serial(cfg: dict, serial: str) -> str | None:
    return cfg.get("serials", {}).get(serial)


def find_port_for_serial(cfg: dict, serial: str) -> tuple[str | None, int | None]:
    codename = find_codename_for_serial(cfg, serial)
    if codename is None:
        return None, None
    return find_port_for_codename(cfg, codename)


def find_codename_for_loc_port(cfg: dict, loc: str, port: int) -> str | None:
    for hub in cfg.get("hubs", []):
        if hub["location"] == loc:
            return hub.get("ports", {}).get(str(port))
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
    matching = [s for s, cn in cfg.get("serials", {}).items()
                if cn.lower() == codename.lower()]
    if len(matching) == 1:
        return matching[0]
    connected = set(adb_devices().keys())
    for s in matching:
        if s in connected:
            return s
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


def _resolve_targets(codename_arg: str, cfg: dict) -> list[str]:
    if codename_arg == "all":
        return all_configured_codenames(cfg)
    return [codename_arg]


