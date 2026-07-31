# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Measuring battery current on a watch that is running on its own battery.

The problem this exists for: a battery measurement needs the watch OFF charge,
but cutting VBUS also takes ADB with it, so the host cannot watch the thing
while it matters. Reading current over USB is useless — the charger dominates,
and a docked watch reports `Full` with a meaningless figure.

So the sampler runs ON THE WATCH. It survives losing USB entirely: start it,
cut power, let the watch sit, restore power, pull the log. That turns a
capacity-delta measurement (which needs hours before 1% granularity moves) into
a current trace at whatever interval you like.

Two hazards this module exists to handle, both found on live hardware:

* **The sign convention is not shared between watches.** In the identical
  state — Full, on charge — beluga reports +488 uA and sawfish -468 uA. A
  cross-device comparison of raw sign is therefore meaningless; the direction
  has to be learned per watch, which `classify()` does from the readings
  themselves rather than assuming.
* **Not every watch has the sensor.** nemo reports 0 forever. A run there
  yields a tidy log full of zeros, which reads as "no drain" rather than "no
  instrument", so it must be named.
"""

from __future__ import annotations

import shlex
import statistics
import time
from pathlib import Path

from .adb import battery_dir_snippet
from .util import log

UNIT = "adb-drainlog"
REMOTE_SCRIPT = "/tmp/adb-drainlog.sh"
REMOTE_LOG = "/tmp/adb-drainlog.csv"
DEFAULT_INTERVAL = 5


def sampler_script(interval: int = DEFAULT_INTERVAL) -> str:
    """The on-watch sampler. Resolves the gauge directory the same way every
    other a-d-b reader does, so the numbers are comparable with the Control
    Center's rather than coming from whichever supply a glob happened to sort
    first."""
    return f"""#!/bin/sh
{battery_dir_snippet("BATD")}
echo "epoch,current_ua,capacity,status,temp,gauge" > {REMOTE_LOG}
while true; do
  echo "$(date +%s),$(cat $BATD/current_now 2>/dev/null),$(cat $BATD/capacity 2>/dev/null),$(cat $BATD/status 2>/dev/null),$(cat $BATD/temp 2>/dev/null),$(basename $BATD)" >> {REMOTE_LOG}
  sleep {interval}
done
"""


def parse(csv_text: str) -> "list[dict]":
    """CSV -> rows. Tolerant: a row the watch wrote mid-shutdown is skipped
    rather than killing the parse. Pure — see tests."""
    rows = []
    for line in (csv_text or "").splitlines()[1:]:      # skip header
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            rows.append({
                "epoch": int(parts[0]),
                "current_ua": int(parts[1]) if parts[1].strip() else None,
                "capacity": int(parts[2]) if parts[2].strip() else None,
                "status": parts[3].strip(),
                "temp": int(parts[4]) if len(parts) > 4 and parts[4].strip() else None,
                "gauge": parts[5].strip() if len(parts) > 5 else "",
            })
        except ValueError:
            continue
    return rows


def classify(rows: "list[dict]") -> dict:
    """What the trace actually says, with the per-watch sign learned rather
    than assumed.

    Returns `sensor`: "usable" when the current varies, "always-zero" when the
    watch reports a flat 0 (no instrument, not no drain), "absent" when there
    is nothing to read at all.

    `discharge_sign` is taken from rows whose status is Discharging: whichever
    sign dominates there IS this watch's discharge direction. That is the only
    honest way to compare two watches whose gauges disagree about which way is
    out. Pure — see tests.
    """
    vals = [r["current_ua"] for r in rows if r["current_ua"] is not None]
    if not vals:
        return {"sensor": "absent", "samples": len(rows)}
    if all(v == 0 for v in vals):
        return {"sensor": "always-zero", "samples": len(rows),
                "note": "the watch reports a flat 0 — no usable current "
                        "sensor. This is no instrument, not no drain; use the "
                        "slow capacity-delta method here."}

    disch = [r["current_ua"] for r in rows
             if r["status"].lower().startswith("discharg")
             and r["current_ua"] is not None]
    sign = None
    if disch:
        neg = sum(1 for v in disch if v < 0)
        sign = -1 if neg > len(disch) / 2 else 1

    caps = [r["capacity"] for r in rows if r["capacity"] is not None]
    span = (rows[-1]["epoch"] - rows[0]["epoch"]) if len(rows) > 1 else 0
    out = {
        "sensor": "usable",
        "samples": len(rows),
        "span_s": span,
        "current_mean_ua": round(statistics.fmean(vals)),
        "current_median_ua": round(statistics.median(vals)),
        "discharge_sign": sign,
        "capacity_from": caps[0] if caps else None,
        "capacity_to": caps[-1] if caps else None,
    }
    if len(vals) > 1:
        # The readings are genuinely noisy — sawfish gave -1560, -156 and +936
        # inside fifteen seconds. Reporting the spread stops a short window
        # being mistaken for a settled figure.
        out["current_stdev_ua"] = round(statistics.stdev(vals))
    if sign is not None and disch:
        out["discharge_mean_ua"] = round(abs(statistics.fmean(disch)))
        # How many samples that mean rests on. Without it a figure derived from
        # a SINGLE reading is indistinguishable from a settled one — catfish's
        # first undocked run produced exactly one discharging sample, and
        # "5000 uA" read like a result rather than a single noisy datum.
        out["discharge_samples"] = len(disch)
        if len(disch) < 3:
            out["discharge_note"] = (
                f"only {len(disch)} discharging sample(s) — directional, not a "
                "measurement. These readings are noisy enough that a short "
                "window says little.")
    return out


def start(watch, interval: int = DEFAULT_INTERVAL) -> "str | None":
    """Install and launch the sampler. systemd-run, not a background job: a
    backgrounded child dies when the adb shell exits, which is the same trap
    that made the benchmark's Start button report success while nothing ran."""
    local = Path("/tmp") / f"adb-drainlog-{watch.serial}.sh"
    local.write_text(sampler_script(interval))
    watch.t.shell(shlex.quote(f"systemctl stop {UNIT} 2>/dev/null; "
                              f"rm -f {REMOTE_LOG}"), timeout=20)
    rc, _, err = watch.t.push(str(local), REMOTE_SCRIPT, timeout=30)
    local.unlink(missing_ok=True)
    if rc != 0:
        return f"push failed: {err.strip()[:80]}"
    rc, out, err = watch.t.shell(
        shlex.quote(f"chmod +x {REMOTE_SCRIPT} && "
                    f"systemd-run --collect --unit={UNIT} {REMOTE_SCRIPT}"),
        timeout=25)
    if rc != 0:
        return f"could not start sampler: {(err or out).strip()[:120]}"
    # Confirm rather than assume — a sampler that never ran produces an empty
    # log an hour later, by which time the window is gone.
    for _ in range(6):
        time.sleep(1)
        if watch.t.shell(f"systemctl is-active {UNIT}",
                         timeout=10)[1].strip() == "active":
            log.info("%s: drain sampler running (%ss interval)",
                     watch.serial, interval)
            return None
    return "sampler did not come up"


def stop(watch) -> None:
    watch.t.shell(f"systemctl stop {UNIT}", timeout=20)


def fetch(watch) -> dict:
    """Pull the trace and say what it means."""
    rc, out, _ = watch.t.shell(f"cat {REMOTE_LOG}", timeout=25)
    if rc != 0 or not out.strip():
        return {"ok": False, "error": "no drain log on this watch"}
    rows = parse(out)
    if not rows:
        return {"ok": False, "error": "drain log is empty — sampler never ran?"}
    return {"ok": True, "rows": len(rows), **classify(rows),
            "first": rows[0], "last": rows[-1]}
