# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Per-watch JSONL event log, standby-drain math, drain-test results."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .util import log
from .config import charge_config


_DRAIN_FLOOR_PCT   = 15   # stop test when battery reaches this level
_DRAIN_POLL_SEC    = 30 * 60  # poll interval: 30 minutes
# A failed battery read costs a whole poll interval of blind discharge, so the
# drain loop cannot keep retrying forever: rubyfish (2026-07-14) stopped
# enumerating mid-test, froze the displayed reading at 71%, and discharged past
# the floor to 0% / 3.18V unseen. Cap consecutive blind polls.
_DRAIN_MAX_BLIND_POLLS = 3
# A saved drain rate is only trusted as a watch's canonical standby figure once
# it spans enough readings over enough time; below this it is per-read noise +
# the re-power charge-bump, not a standby rate (audit B4).
_DRAIN_MIN_SAMPLES = 3
_DRAIN_MIN_HOURS   = 1.5
_DRAIN_RESULTS_DIR = Path.home() / ".local/share/asteroid-docking-bay/drain-tests"


class EventLog:
    """One append-only JSONL timeline per physical watch, keyed by serial so
    two units of the same codename stay distinct (codename is the fallback
    key). Records every battery observation and power event; this single
    history feeds wear trending and the adaptive charge cadence.

    The directory is a constructor argument so tests can point an instance at
    a tmpdir; runtime code uses the `event_log` module singleton."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    @staticmethod
    def key_for(serial: "str | None", codename: "str | None") -> "str | None":
        key = serial or codename
        if not key:
            return None
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in key)

    def log(self, serial: "str | None", codename: "str | None",
            event: str, **fields) -> None:
        """Append one event record to the watch's timeline. Best-effort."""
        key = self.key_for(serial, codename)
        if key is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": round(time.time(), 1), "event": event,
                   "codename": codename, "serial": serial, **fields}
            with (self.dir / f"{key}.jsonl").open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:
            log.debug("event log write failed for %s: %s", key, exc)

    def read(self, serial: "str | None",
             codename: "str | None" = None) -> "list[dict]":
        """A watch's event timeline (oldest first). Empty if none."""
        key = self.key_for(serial, codename)
        if key is None:
            return []
        f = self.dir / f"{key}.jsonl"
        if not f.is_file():
            return []
        out: list[dict] = []
        try:
            with f.open() as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        return out

    def standby_loss_rate(self, serial: "str | None", codename: "str | None",
                          events: "list[dict] | None" = None) -> "float | None":
        """Estimate a watch's standby drain in %/hour from its event log.

        Pairs consecutive battery observations (check/drain readings) taken
        while the watch was NOT charging, keeps only intervals where the
        charge dropped, and returns the median rate (robust to outliers). A
        charge event breaks the chain. Falls back to the latest drain-test
        rate when there are no usable pairs. None when nothing is known.
        """
        evs = events if events is not None else self.read(serial, codename)
        rates: list[float] = []
        prev: "tuple[float, float] | None" = None
        for e in evs:
            ev = e.get("event")
            if ev in ("check_reading", "drain_reading"):
                pct, ts = e.get("pct"), e.get("ts")
                if pct is None or ts is None:
                    continue
                if prev is not None:
                    dt_h = (ts - prev[1]) / 3600.0
                    drop = prev[0] - pct
                    if dt_h > 0.05 and drop > 0:
                        r = drop / dt_h
                        if 0 < r < 50:          # ignore absurd spikes
                            rates.append(r)
                prev = (pct, ts)
            elif ev in ("charge_start", "charge_end", "external", "wear"):
                # Charging obviously breaks a standby chain. So does an
                # externally logged event: it means somebody did something to
                # this watch — a flash, a bench session, an app test — and the
                # interval spanning it was never passive standby. Breaking the
                # chain discards that interval rather than reporting a rate
                # measured across work, which is the safe direction to err.
                prev = None
        if rates:
            rates.sort()
            return rates[len(rates) // 2]
        # Seed from the most recent completed drain test for this watch.
        summ = _latest_drain_summaries().get((codename or "").lower())
        if summ and summ.get("rate") and (not summ.get("serial") or summ["serial"] == serial):
            return summ["rate"]
        return None

    def standby_off_to_on_rate(self, serial: "str | None",
                               codename: "str | None",
                               events: "list[dict] | None" = None) -> "float | None":
        """True standby %/hour from the last graceful power-off to the next
        boot reading.

        The watch is on battery the whole interval with NO reads between, so
        this carries none of the drain-test charge-bump that overrates standby
        (only the single ~40s boot-read bump at the far end). Pairs each
        power_off with the first battery reading after it and returns the most
        recent such drop. A period where the battery ROSE is excluded — standby
        only falls, so a rise means it was charged off the rig (worn). None
        until a usable pair exists."""
        evs = events if events is not None else self.read(serial, codename)
        off: "tuple[float, float] | None" = None   # (pct, ts) of last power_off
        rate: "float | None" = None
        for e in evs:
            ev = e.get("event")
            if ev == "power_off" and e.get("pct") is not None and e.get("ts") is not None:
                off = (e["pct"], e["ts"])
            elif off is not None and ev in ("check_reading", "drain_reading",
                                            "charge_start"):
                pct, ts = e.get("pct"), e.get("ts")
                if pct is not None and ts is not None:
                    dt_h = (ts - off[1]) / 3600.0
                    drop = off[0] - pct
                    if dt_h > 0.05 and drop > 0:   # exclude off-rig charge + too-short
                        rate = drop / dt_h
                    off = None                      # this power_off is paired
        return rate

    def next_due_ts(self, serial: "str | None", codename: "str | None",
                    cfg: dict) -> "float | None":
        """When this watch should next be woken for a charge check, as an
        epoch ts. None means 'no usable history — check now'."""
        charge_cfg = charge_config(cfg)
        low      = charge_cfg.low_threshold
        margin   = charge_cfg.adaptive_margin_pct
        max_days = charge_cfg.adaptive_max_interval_days
        evs = self.read(serial, codename)
        last = next((e for e in reversed(evs)
                     if e.get("event") in ("check_reading", "drain_reading")
                     and e.get("pct") is not None), None)
        if last is None:
            return None
        rate = self.standby_loss_rate(serial, codename, evs)
        if not rate or rate <= 0:
            return None
        headroom = last["pct"] - (low + margin)
        cap = last["ts"] + max_days * 86400
        if headroom <= 0:
            return last["ts"]                   # already near target — due now
        due = last["ts"] + (headroom / rate) * 3600
        return min(due, cap)


event_log = EventLog(Path.home() / ".local/share/asteroid-docking-bay/events")


def _save_drain_results(task: "dict | None", slot: str, codename: str) -> None:
    if not task or not task.get("readings"):
        return
    try:
        _DRAIN_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts_str  = time.strftime("%Y%m%d-%H%M%S",
                                time.localtime(task["start_ts"]))
        out     = _DRAIN_RESULTS_DIR / f"{codename}-{ts_str}.json"
        payload = {
            "codename":               codename,
            "slot":                   slot,
            "serial":                 task.get("serial"),
            "start_ts":               task["start_ts"],
            "start_pct":              task["start_pct"],
            "end_pct":                task.get("last_pct"),
            "drain_rate_pct_per_hour":task.get("drain_rate"),
            "stopped_by_user":        task.get("stopped", False),
            "features":               task.get("features"),   # WiFi/BT/AoD at start
            "readings":               task["readings"],
        }
        with out.open("w") as f:
            json.dump(payload, f, indent=2)
        log.info("%s: drain test results saved → %s", codename, out)
    except Exception as exc:
        log.warning("%s: could not save drain test results: %s", codename, exc)


# Latest completed drain test per codename, cached on the results dir mtime.
_drain_summary_cache: dict = {"mtime": None, "by_codename": {}}


def _latest_drain_summaries() -> dict[str, dict]:
    """{codename(lower): {ts, rate, est_h, serial}} from the newest saved
    drain test per watch.  est_h = estimated 100→15% standby time — the
    battery-health / wearability figure."""
    try:
        mtime = _DRAIN_RESULTS_DIR.stat().st_mtime
    except OSError:
        return {}
    if _drain_summary_cache["mtime"] == mtime:
        return _drain_summary_cache["by_codename"]
    best: dict[str, dict] = {}
    for f in _DRAIN_RESULTS_DIR.glob("*.json"):
        try:
            with f.open() as fh:
                d = json.load(fh)
        except Exception:
            continue
        cn   = (d.get("codename") or "").lower()
        rate = d.get("drain_rate_pct_per_hour")
        ts   = d.get("start_ts") or 0
        # A rate from too few readings over too short a span is dominated by
        # per-read noise and the re-power charge-bump — refuse to let it become
        # a watch's canonical est_h (drives wearability + next_due_ts). A single
        # 30-min poll used to be enough (audit B4).
        readings = d.get("readings") or []
        span_h = ((readings[-1]["ts"] - readings[0]["ts"]) / 3600.0
                  if len(readings) >= 2 else 0.0)
        if (not cn or not rate or rate <= 0
                or len(readings) < _DRAIN_MIN_SAMPLES
                or span_h < _DRAIN_MIN_HOURS):
            continue
        if cn not in best or ts > best[cn]["ts"]:
            best[cn] = {"ts": ts, "rate": rate, "est_h": 85.0 / rate,
                        "serial": d.get("serial")}
    _drain_summary_cache["mtime"] = mtime
    _drain_summary_cache["by_codename"] = best
    return best
