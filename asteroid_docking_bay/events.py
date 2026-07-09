# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Per-watch JSONL event log, standby-drain math, drain-test results."""

import json
import time
from pathlib import Path

from .util import log
from .config import charge_config


_DRAIN_FLOOR_PCT   = 15   # stop test when battery reaches this level
_DRAIN_POLL_SEC    = 30 * 60  # poll interval: 30 minutes
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
            elif ev in ("charge_start", "charge_end"):
                prev = None                     # charging breaks the standby chain
        if rates:
            rates.sort()
            return rates[len(rates) // 2]
        # Seed from the most recent completed drain test for this watch.
        summ = _latest_drain_summaries().get((codename or "").lower())
        if summ and summ.get("rate") and (not summ.get("serial") or summ["serial"] == serial):
            return summ["rate"]
        return None

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
        if not cn or not rate or rate <= 0:
            continue
        if cn not in best or ts > best[cn]["ts"]:
            best[cn] = {"ts": ts, "rate": rate, "est_h": 85.0 / rate,
                        "serial": d.get("serial")}
    _drain_summary_cache["mtime"] = mtime
    _drain_summary_cache["by_codename"] = best
    return best
