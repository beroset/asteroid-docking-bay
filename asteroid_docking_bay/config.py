# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Config file I/O, defaults, and codename/serial/port lookups."""

import json
import threading
from pathlib import Path

from .adb import adb_devices


CONFIG_DIR = Path.home() / ".config" / "asteroid-docking-bay"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "hubs": [],
    "serials": {},
    "charge": {
        "low_threshold": 40,
        "high_threshold": 80,
        "charge_duration_minutes": 30,
        # check_interval_hours is informational — actual scheduling is done by
        # the systemd timer (asteroid-docking-bay-charge.timer).
        "check_interval_hours": 12,
        "adb_wait_seconds": 15,
        "adb_wait_retries": 8,
        # Adaptive cadence: skip waking a watch during check-charge until its
        # observed standby drain projects it near low_threshold. Watches with
        # no drain history are always checked.
        "adaptive_cadence": True,
        "adaptive_margin_pct": 10,        # wake when projected to reach low+this
        "adaptive_max_interval_days": 14, # never skip a watch longer than this
        # Ideal rest state is 40-80% AND powered off. After a charge or drain
        # test, shut the watch down over ADB before cutting the port so it
        # doesn't sit silently draining.
        "graceful_poweroff": True,
    },
    "flash": {
        "nightly_url": "https://release.asteroidos.org/nightlies",
        "download_dir": str(Path.home() / ".local" / "share" / "asteroid-docking-bay" / "nightlies"),
    },
}


# Serialises all config read-modify-save cycles so concurrent web requests
# (flash, charge, remap) don't corrupt config.json.
_config_lock = threading.Lock()


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {k: v for k, v in DEFAULT_CONFIG.items()}
    with CONFIG_FILE.open() as f:
        cfg = json.load(f)
    # Fill in any missing top-level and charge sub-keys with defaults.
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["charge"].items():
        cfg["charge"].setdefault(k, v)
    cfg.setdefault("flash", {})
    for k, v in DEFAULT_CONFIG["flash"].items():
        cfg["flash"].setdefault(k, v)
    return cfg


def save_config(cfg: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


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


