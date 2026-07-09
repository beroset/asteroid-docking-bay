# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Operation registries and durable (restart-surviving) task state."""

import json
import threading
from pathlib import Path

from .util import log


# Per-codename flash task state for the web UI's SSE streaming.
_flash_tasks: dict[str, dict] = {}

# Per-codename charge task state for the web UI's live countdown.
_charge_tasks: dict[str, dict] = {}

# Per-slot stop event; set to cancel a running web charge cycle early.
_charge_stop: dict[str, threading.Event] = {}

# Per-slot drain test task state and cancellation events.
_drain_tasks: dict[str, dict] = {}
_drain_stop:  dict[str, threading.Event] = {}

# Per-slot workbench (checked-out watch) state and cancellation events.
_workbench_tasks: dict[str, dict] = {}
_workbench_stop:  dict[str, threading.Event] = {}

# Per-slot remap task state for the web UI's SSE streaming.
_remap_tasks: dict[str, dict] = {}


# Ensures only one watch is powered on and ADB-active at a time.
# Charge, flash, and remap all acquire this before powering a port on
# and release it only after powering the port back off.
_adb_lock = threading.Lock()


# ── Durable operation state ───────────────────────────────────────────────────
# Charge, drain and workbench run in daemon threads whose state lives in the
# dicts above — in-memory, so a web-service restart/crash silently kills them.
# We mirror each running op to disk here; on startup the web service reloads
# and resumes any unfinished one, so ops survive restarts/reboots.
_TASKS_DIR = Path.home() / ".local/state/asteroid-docking-bay/tasks"


def _task_file(kind: str, slot: str) -> Path:
    safe = slot.replace(":", "-").replace(".", "_").replace("/", "_")
    return _TASKS_DIR / f"{kind}__{safe}.json"


def _persist_task(kind: str, slot: str, loc: str, port: int, task: dict) -> None:
    """Atomically write a running op's resumable state to disk."""
    try:
        _TASKS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"kind": kind, "slot": slot, "loc": loc, "port": port,
                   "task": task}
        f = _task_file(kind, slot)
        tmp = f.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump(payload, fh)
        tmp.replace(f)
    except Exception as exc:
        log.debug("persist %s %s failed: %s", kind, slot, exc)


def _unpersist_task(kind: str, slot: str) -> None:
    try:
        _task_file(kind, slot).unlink(missing_ok=True)
    except Exception:
        pass


def _load_persisted_tasks() -> "list[dict]":
    """Return the persisted payloads for all ops that were running at shutdown."""
    out: list[dict] = []
    if not _TASKS_DIR.is_dir():
        return out
    for f in _TASKS_DIR.glob("*.json"):
        try:
            with f.open() as fh:
                out.append(json.load(fh))
        except Exception as exc:
            log.warning("could not read persisted task %s: %s", f.name, exc)
    return out


