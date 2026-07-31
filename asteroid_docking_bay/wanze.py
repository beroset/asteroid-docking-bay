# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Host side of wanze — the probe that records while the watch is away.

wanze samples opportunistically: its timer never wakes the watch, so rows
appear only when the watch was awake anyway. Everything here exists to read
that irregular trace honestly.

Two hazards drive the whole design, both found on live hardware:

* **The watch's wall clock cannot be trusted.** catfish's RTC was 130 DAYS
  behind the host while recording perfectly well. So `epoch` is useless on its
  own, and every timing conclusion is drawn from `uptime` instead, which no
  clock adjustment can move. The skew is measured at harvest and reported
  rather than silently corrected.
* **A gap is data, not a hole.** The probe not firing for three hours means the
  watch slept for three hours. Treating that as missing data throws away the
  one thing a wearable telemetry probe is uniquely able to measure.

Sensor classification is deliberately NOT reimplemented here: the rows carry
the same keys drainlog already understands, so `drainlog.classify` handles the
per-watch sign convention and the always-zero case for both.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from .drainlog import classify
from .util import log

REMOTE_LOG = "/var/log/wanze.csv"
REMOTE_BIN = "/usr/bin/wanze-sample"
UNIT = "wanze.timer"
SCHEMA = 1

# The timer's nominal period. Only used to decide what counts as a gap; the
# real interval is whatever the watch's wakefulness allowed.
INTERVAL_S = 300

# A sample later than this multiple of the interval means the watch was not
# awake in between. Generous, because a loaded watch legitimately runs late and
# calling that "asleep" would inflate every standby figure.
GAP_FACTOR = 3


def parse(csv_text: str) -> "list[dict]":
    """CSV -> rows, driven by the header rather than by column position.

    Header-driven on purpose: wanze stamps a schema version and starts a fresh
    file when the columns change, but a trace harvested across an upgrade can
    still hold both shapes. Reading by name means an added column cannot
    silently shift every later field. Pure — see tests.
    """
    lines = [ln for ln in (csv_text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    cols = [c.strip() for c in lines[0].split(",")]
    if "uptime" not in cols:                    # not a wanze file at all
        return []

    def num(v, cast):
        v = (v or "").strip()
        try:
            return cast(v)
        except ValueError:
            return None

    rows = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < len(cols):
            continue                            # torn row, e.g. power lost mid-write
        rec = dict(zip(cols, parts))
        row = {
            "epoch": num(rec.get("epoch"), int),
            "uptime": num(rec.get("uptime"), float),
            # Same key names drainlog uses, so classify() works on these rows
            # unchanged rather than being duplicated for a second format.
            "current_ua": num(rec.get("current_ua"), int),
            "capacity": num(rec.get("capacity"), int),
            "status": (rec.get("status") or "").strip(),
            "voltage_uv": num(rec.get("voltage_uv"), int),
            "temp": num(rec.get("temp"), int),
            "charger": num(rec.get("charger"), int),
            "backlight": num(rec.get("backlight"), int),
            "cpu_online": num(rec.get("cpu_online"), int),
            "cpu_freq": num(rec.get("cpu_freq"), int),
            "load1": num(rec.get("load1"), float),
            "gauge": (rec.get("gauge") or "").strip(),
        }
        if row["uptime"] is None:
            continue
        rows.append(row)
    return rows


def segments(rows: "list[dict]") -> "list[dict]":
    """Split the trace where the watch rebooted.

    `uptime` only ever rises inside one boot, so a DROP is a reboot — which
    makes reboot forensics fall out of a battery trace for free. Timing must
    never be computed across such a break, because the uptime delta there is
    meaningless. Pure — see tests.
    """
    out: "list[dict]" = []
    cur: "list[dict]" = []
    for row in rows:
        if cur and row["uptime"] < cur[-1]["uptime"]:
            out.append({"rows": cur})
            cur = []
        cur.append(row)
    if cur:
        out.append({"rows": cur})
    return out


def gaps(rows: "list[dict]", interval_s: int = INTERVAL_S) -> "list[dict]":
    """Stretches where the probe did not fire — i.e. the watch was not awake.

    Measured on `uptime`, never on `epoch`: a watch whose clock is wrong (or
    which syncs mid-trace) would otherwise report invented gaps or hide real
    ones. Pure — see tests.
    """
    out = []
    for seg in segments(rows):
        srows = seg["rows"]
        for a, b in zip(srows, srows[1:]):
            delta = b["uptime"] - a["uptime"]
            if delta > interval_s * GAP_FACTOR:
                out.append({
                    "from_uptime": a["uptime"], "to_uptime": b["uptime"],
                    "seconds": round(delta),
                    "capacity_from": a["capacity"], "capacity_to": b["capacity"],
                })
    return out


def clock_check(rows: "list[dict]") -> dict:
    """Does the watch's wall clock agree with its own uptime?

    Both advance in real time, so within one boot their deltas should match.
    A divergence means the clock was adjusted mid-trace (an NTP sync, say),
    which is exactly the event that would corrupt any epoch-based reading and
    which is invisible if you only look at one of the two. Pure — see tests.
    """
    worst = 0.0
    for seg in segments(rows):
        srows = [r for r in seg["rows"] if r["epoch"] is not None]
        for a, b in zip(srows, srows[1:]):
            drift = (b["epoch"] - a["epoch"]) - (b["uptime"] - a["uptime"])
            worst = max(worst, abs(drift))
    return {"max_step_s": round(worst), "clock_stepped": worst > 60}


def analyse(rows: "list[dict]", host_epoch: "float | None" = None) -> dict:
    """What the trace says, with every timing figure taken from uptime.

    `host_epoch` is the host's clock AT HARVEST. It is used only to report the
    watch's skew — never to rewrite the rows, because a corrected timestamp
    that looks trustworthy is worse than an obviously wrong one. Pure.
    """
    if not rows:
        return {"ok": False, "error": "no wanze rows"}

    segs = segments(rows)
    covered = sum(s["rows"][-1]["uptime"] - s["rows"][0]["uptime"] for s in segs)
    holes = gaps(rows)
    asleep = sum(g["seconds"] for g in holes)

    out = {
        "ok": True,
        "samples": len(rows),
        "boots": len(segs),
        "reboots": len(segs) - 1,
        "covered_s": round(covered),
        "asleep_s": asleep,
        # The headline the probe exists to produce: of the time we watched, how
        # much did the watch spend not awake? A high number is a healthy watch.
        "asleep_fraction": round(asleep / covered, 3) if covered else None,
        "gaps": holes[:20],
        "gap_count": len(holes),
        "gauge": rows[-1]["gauge"],
        **clock_check(rows),
    }
    if host_epoch is not None and rows[-1]["epoch"] is not None:
        skew = rows[-1]["epoch"] - host_epoch
        out["clock_skew_s"] = round(skew)
        out["clock_skew_days"] = round(skew / 86400, 1)
    # Sensor + discharge direction, from the module that already knows how.
    out["battery"] = classify(rows)
    # Screen-on time is what makes a drain figure attributable rather than
    # merely true; a watch that drained while lit is a different story.
    lit = [r for r in rows if r["backlight"]]
    out["samples_screen_on"] = len(lit)
    return out


# --- watch-side control ---------------------------------------------------

# wanze lives in its own repo, so a-d-b has to FIND its files rather than own
# them. Duplicating the sampler here would give the fleet two sources of truth
# for the thing that produces every number, which is worse than a search path.
SRC_CANDIDATES = (
    Path(__file__).resolve().parent / "wanze-probe",   # a bundled copy, if built with one
    Path.home() / "Git/wanze/src",                     # a developer checkout
    Path("/usr/share/wanze"),                          # installed from the ipk
)
SRC_FILES = ("wanze-sample", "wanze.service", "wanze.timer")


def find_src() -> "Path | None":
    """The first candidate that holds a COMPLETE set. A directory carrying only
    some of the files would install a probe that cannot run, and the failure
    would not show up until the trace came back empty days later."""
    for cand in SRC_CANDIDATES:
        if all((cand / f).is_file() for f in SRC_FILES):
            return cand
    return None


def install(watch, src_dir: "Path | None" = None) -> "str | None":
    """Push the sampler and its units, then enable the timer.

    Pushes files rather than piping a script: `adb shell` reads stdin and will
    swallow the remainder of a heredoc, which truncated several scripts during
    the benchmark work.
    """
    src_dir = src_dir or find_src()
    if src_dir is None:
        return ("wanze sources not found — looked in: "
                + ", ".join(str(c) for c in SRC_CANDIDATES))
    for name, dest in (("wanze-sample", REMOTE_BIN),
                       ("wanze.service", "/etc/systemd/system/wanze.service"),
                       ("wanze.timer", "/etc/systemd/system/wanze.timer")):
        src = Path(src_dir) / name
        if not src.exists():
            return f"missing {src}"
        rc, _, err = watch.t.push(str(src), dest, timeout=30)
        if rc != 0:
            return f"push {name} failed: {err.strip()[:80]}"
    rc, out, err = watch.t.shell(
        shlex.quote(f"chmod +x {REMOTE_BIN} && systemctl daemon-reload && "
                    f"systemctl enable --now {UNIT}"), timeout=40)
    if rc != 0:
        return f"enable failed: {(err or out).strip()[:120]}"
    # Confirm rather than assume: a timer that never armed produces an empty
    # trace days later, by which time the window is gone.
    if watch.t.shell(f"systemctl is-active {UNIT}", timeout=15)[1].strip() != "active":
        return "timer did not become active"
    log.info("%s: wanze installed and armed", watch.serial)
    return None


def stop(watch) -> None:
    watch.t.shell(f"systemctl disable --now {UNIT}", timeout=25)


def harvest(watch, clear: bool = False) -> dict:
    """Pull the trace and say what it means. `clear` truncates the on-watch
    buffer, which is only safe once the rows are actually in hand."""
    rc, out, _ = watch.t.shell(f"cat {REMOTE_LOG}", timeout=40)
    host_epoch = time.time()
    if rc != 0 or not out.strip():
        return {"ok": False, "error": "no wanze trace on this watch"}
    rows = parse(out)
    if not rows:
        return {"ok": False, "error": "wanze trace is empty or unreadable"}
    if clear:
        watch.t.shell(shlex.quote(f"rm -f {REMOTE_LOG}"), timeout=20)
    return {**analyse(rows, host_epoch), "first": rows[0], "last": rows[-1]}
