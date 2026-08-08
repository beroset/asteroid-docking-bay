# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""A "leave this watch alone" marker for long operations.

a-d-b runs housekeeping continuously: it peels stray SSH watches back onto adb,
aligns USB modes to the fleet preference, and power-cycles ports that look
wedged. All of that is correct behaviour for an idle fleet and destructive to an
operation already in flight.

It cost a dump on 2026-08-03. A 3.9 GB read was running over SSH when the stray
peeler decided the watch belonged on adb and switched its USB mode mid-transfer.
The result was a 0-byte image — the same silent shape as the long-dead
`original-sprat.img`, a backup that looks present in a directory listing and
contains nothing.

Three properties matter, and each comes from a way this can fail:

* **Persisted**, not held in memory. A dump or a flash outlives a service
  restart, and a restart must not quietly drop the one indicator saying the
  watch is busy. Same reasoning as wanze's probing marker, which is this idea
  in a wanze-shaped form and should eventually fold into this one.
* **Expiring**. An operation that crashes never releases its lock. Without a
  TTL a single dead process would exempt a watch from all housekeeping forever,
  which fails in the quietest possible way: nothing happens, and nothing says
  why.
* **Visible**. A human looking at the UI needs to see that a watch is held and
  by what, or they will fight the lock by hand — the same collision, slower.
"""

from __future__ import annotations

import time

from .util import log

# Generous by design. A full-disk dump over USB 2.0 runs ~12 min per GB-ish and
# a flash cycle can take an hour; the number is not a prediction of how long
# work takes, it is a backstop against a crashed holder. Callers that know
# better pass their own.
DEFAULT_TTL_SEC = 3 * 3600

_KEY = "op_locks"


def held(cfg: dict, serial: "str | None") -> "dict | None":
    """The live lock on this watch, or None.

    Expiry is evaluated on READ rather than by a sweeper: there is no periodic
    task guaranteed to run, and a lock that outlives its holder must not depend
    on one existing to become harmless.
    """
    if not serial:
        return None
    lock = (cfg.get(_KEY) or {}).get(serial)
    if not lock:
        return None
    until = lock.get("until") or 0
    if until and time.time() > until:
        return None
    return lock


def hold(serial: str, kind: str, note: str = "",
         ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Claim a watch for a long operation.

    Refuses to displace a LIVE lock of a DIFFERENT kind. Without this a dump
    started on a watch already running a wanze probe silently overwrote the
    wanze lock, and its release deleted it outright — leaving the run
    unprotected from the housekeeping this exists to hold off, and, because
    probing() keys on the kind, invisible in the UI as well. The whole point of
    the marker is defeated if the next operation may quietly stamp over it.

    Re-holding the SAME kind is a renewal, not a collision: it refreshes the
    expiry, which is what a caller extending its own claim wants. An EXPIRED
    lock does not block anyone. The check and the write share `_config_lock`,
    so two callers cannot both pass the test and one clobber the other.
    """
    from .config import _config_lock, load_config, save_config
    now = time.time()
    with _config_lock:
        cfg = load_config()
        locks = cfg.setdefault(_KEY, {})
        existing = locks.get(serial)
        if existing and existing.get("kind") != kind:
            until = existing.get("until") or 0
            if not until or now <= until:            # still live → do not displace
                log.info("%s: refused '%s' — already held for '%s'",
                         serial, kind, existing.get("kind"))
                return {"ok": False, "held": True, "conflict": True,
                        "kind": existing.get("kind"),
                        "note": existing.get("note", "")}
        locks[serial] = {"kind": kind, "note": note,
                         "since": now, "until": now + ttl}
        save_config(cfg)
    log.info("%s: held for '%s'%s (expires in %.0f min)",
             serial, kind, f" — {note}" if note else "", ttl / 60)
    return {"ok": True, "held": True, "kind": kind}


def release(serial: str) -> dict:
    """Release a watch back to normal housekeeping."""
    from .config import _config_lock, load_config, save_config
    with _config_lock:
        cfg = load_config()
        gone = (cfg.get(_KEY) or {}).pop(serial, None)
        if gone is not None:
            save_config(cfg)
    if gone is not None:
        log.info("%s: released from '%s'", serial, gone.get("kind"))
    return {"ok": True, "held": False}


def all_held(cfg: dict) -> dict:
    """Every live lock, for the status document. Expired ones are omitted so
    the UI cannot show a hold that no longer applies."""
    out = {}
    for serial in list((cfg.get(_KEY) or {}).keys()):
        lock = held(cfg, serial)
        if lock:
            out[serial] = lock
    return out


def describe(lock: "dict | None") -> str:
    """One line for a human, including how long it has been held — a lock that
    has been sitting for hours is usually a crashed holder, and saying so is
    cheaper than making someone work it out."""
    if not lock:
        return ""
    mins = max(0, (time.time() - (lock.get("since") or 0)) / 60)
    note = f" — {lock['note']}" if lock.get("note") else ""
    return f"{lock.get('kind', 'operation')} for {mins:.0f} min{note}"
