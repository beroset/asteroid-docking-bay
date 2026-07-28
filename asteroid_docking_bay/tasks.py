# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Operation registries and durable (restart-surviving) task state."""

from __future__ import annotations

import json
import threading
import time
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

# Serializes ONBOARDING specifically. Onboarding owns port power for its whole
# flow — that serialization is a hard rule, since powering several ports at
# once floods ADB and browns the bus out — but most of that flow is spent
# WAITING for a watch to cold-boot, which disturbs nothing. Holding _adb_lock
# across those waits made every other ADB operation on the rig queue behind a
# two-minute boot window, so one slow onboard made the whole rig look busy.
# Splitting the two keeps onboards strictly serial while letting a flash or a
# charge proceed meanwhile.
_onboard_lock = threading.Lock()


def task_active(tasks: dict, slot: str) -> bool:
    """True while a task is genuinely running.

    A worker that dies without clearing `done` — a hard crash, a thread that
    never started, an exception escaping the `finally` — would otherwise leave
    its row dimmed and its slot refusing new work forever. So a record that
    carries a `deadline` and has outlived it reads as finished: the UI recovers
    on its own instead of needing a service restart.
    """
    t = tasks.get(slot)
    if not t or t.get("done", True):
        return False
    deadline = t.get("deadline")
    return not (deadline is not None and time.monotonic() > deadline)


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




def active_op_on_slot(slot: str) -> "str | None":
    """The kind of operation currently owning this slot, or None.

    A running charge, drain or workbench owns its port's power state, and
    changing it underneath silently falsifies the measurement rather than
    failing loudly.

    Two views, because there are two kinds of caller. Inside the web service
    the in-memory registries are authoritative and current. A separate CLI
    process sees empty registries — the ops live in another process — so it
    falls through to the durable store, which is the only view it has. One
    function for both keeps the answer identical whoever asks.

    Fails closed: a stale file left by a crashed service refuses an operation
    that would in fact have been safe. That is the right direction to be
    wrong, and `_resume_persisted_tasks` clears it on the next web start.
    """
    for kind, registry in (("charge", _charge_tasks),
                           ("drain", _drain_tasks),
                           ("workbench", _workbench_tasks)):
        task = registry.get(slot)
        if task is not None and not task.get("done", True):
            return kind
    for payload in task_store.load_all():
        if (payload.get("slot") == slot
                and not (payload.get("task") or {}).get("done", True)):
            return payload.get("kind")
    return None
