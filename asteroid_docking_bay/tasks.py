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


class TaskStore:
    """Durable operation state. Charge, drain and workbench run in daemon
    threads whose live state sits in the registries above — in-memory, so a
    web-service restart/crash would silently kill them. Each running op is
    mirrored to one JSON file here (written atomically via tmp+rename); on
    startup the web service reloads and resumes any unfinished one.

    The directory is a constructor argument so tests can use a tmpdir;
    runtime code uses the `task_store` module singleton."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _file(self, kind: str, slot: str) -> Path:
        safe = slot.replace(":", "-").replace(".", "_").replace("/", "_")
        return self.dir / f"{kind}__{safe}.json"

    def persist(self, kind: str, slot: str, loc: str, port: int,
                task: dict) -> None:
        """Atomically write a running op's resumable state to disk."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = {"kind": kind, "slot": slot, "loc": loc, "port": port,
                       "task": task}
            f = self._file(kind, slot)
            tmp = f.with_suffix(".tmp")
            with tmp.open("w") as fh:
                json.dump(payload, fh)
            tmp.replace(f)
        except Exception as exc:
            log.debug("persist %s %s failed: %s", kind, slot, exc)

    def unpersist(self, kind: str, slot: str) -> None:
        try:
            self._file(kind, slot).unlink(missing_ok=True)
        except Exception:
            pass

    def load_all(self) -> "list[dict]":
        """The persisted payloads for all ops running at last shutdown."""
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        for f in self.dir.glob("*.json"):
            try:
                with f.open() as fh:
                    out.append(json.load(fh))
            except Exception as exc:
                log.warning("could not read persisted task %s: %s", f.name, exc)
        return out


task_store = TaskStore(Path.home() / ".local/state/asteroid-docking-bay/tasks")


