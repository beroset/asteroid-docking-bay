# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Driving the nutty-benchy FPS benchmark (docs/FPS_BENCH.md).

Push the watchface, remember what was there, switch, sample the kernel's own
frame counter while it runs, switch back. The watch shows the authoritative
per-phase numbers on its own screen; the host sampling here is the second,
independent instrument.

The phase table below MIRRORS the one in the watchface and is only valid for
the scene version it was written against — if the QML's phases change, this
must change with it, or the host will align its samples to the wrong phases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .util import log

# Upstream's watchface selector reads exactly one directory
# (`watchface = folderModel.folder + "/" + fileName` over
# file:///usr/share/asteroid-launcher/watchfaces), so that is where a face has
# to live to be a first-class citizen. It is writable over adb.
WATCHFACE_DIR = "/usr/share/asteroid-launcher/watchfaces"
WATCHFACE_KEY = "/desktop/asteroid/watchface"
BENCH_NAME = "nutty-benchy"
FPS_NODE = "/sys/class/graphics/fb0/measured_fps"

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "watchfaces"
ASSETS = (f"{BENCH_NAME}.qml", "benchy-mesh.js", "benchy-mesh-lite.js")

# (name, seconds) — must match nutty-benchy.qml's `phases`, plus its countdown.
COUNTDOWN_S = 5
PHASES = [("IDLE", 8), ("SCALE", 8), ("RERASTER", 8), ("ORBIT", 8),
          ("OVERDRAW", 8), ("DRAWCALLS", 8), ("SHAPES", 8), ("CASCADE", 8),
          ("BENCHYLITE", 8), ("BENCHY", 10)]
RUN_S = COUNTDOWN_S + sum(d for _, d in PHASES)


def scene_version() -> "str | None":
    """The watchface's own sceneVersion — results are only comparable within
    one, so it is stamped onto every result."""
    try:
        src = (ASSET_DIR / f"{BENCH_NAME}.qml").read_text()
    except OSError:
        return None
    m = re.search(r'sceneVersion:\s*"([^"]+)"', src)
    return m.group(1) if m else None


def push_assets(watch) -> "str | None":
    """Copy the watchface and its mesh onto the watch. Returns an error string,
    or None on success."""
    rc, _, err = watch.t.shell(f"mkdir -p {WATCHFACE_DIR}", timeout=10)
    if rc != 0:
        return f"could not create {WATCHFACE_DIR}: {err.strip()[:80]}"
    for name in ASSETS:
        local = ASSET_DIR / name
        if not local.is_file():
            return f"missing asset {local}"
        rc, _, err = watch.t.push(str(local), f"{WATCHFACE_DIR}/{name}",
                                  timeout=30)
        if rc != 0:
            return f"push {name} failed: {err.strip()[:80]}"
    watch.t.shell("chmod 644 " + " ".join(f"{WATCHFACE_DIR}/{a}" for a in ASSETS),
                  timeout=10)
    # A watchface file that did not exist when the launcher started is invisible
    # to it: pointing the dconf key at one leaves the homescreen blank, with no
    # QML error, because nothing is ever loaded. Proven both ways on catfish
    # (2026-07-28): a byte-identical copy of a STOCK face under a new name
    # stayed blank too, and a probe watchface whose Component.onCompleted
    # writes a dconf beacon only fired after the restart. This is why the
    # settings app's own store offers "Restart launcher"
    # (WatchfaceHelper::restartSession: fc-cache -f, then the same restart).
    return restart_launcher(watch)


def restart_launcher(watch) -> "str | None":
    """Make the launcher notice newly installed faces. Mirrors upstream's
    WatchfaceHelper::restartSession(). Costs a few seconds of black screen."""
    watch.user_cmd("fc-cache -f", timeout=30)
    rc, _, err = watch.user_cmd("systemctl --user restart asteroid-launcher",
                                timeout=30)
    if rc != 0:
        return f"launcher restart failed: {err.strip()[:80]}"
    return None


def wake_once(watch) -> None:
    """Unblank the screen the way a tap does — the normal blank timeout still
    applies afterwards. Preferred over demo mode for a quick look: demo mode
    keeps the panel lit indefinitely, which on a watch at 8% is the difference
    between charging and not (moWerk, 2026-07-28)."""
    watch.t.shell("mcetool --unblank-screen", timeout=10)


def keep_awake(watch, on: bool) -> None:
    """Hold the screen awake for the length of a run, then hand it back. Uses
    mce's blank-prevent rather than demo mode, so it is explicitly cancellable
    and mce still owns the policy."""
    watch.t.shell("mcetool --blank-prevent" if on
                  else "mcetool --cancel-blank-prevent", timeout=10)


def read_watchface(watch) -> "str | None":
    """The watchface dconf value as stored (quoted), or None if unreadable."""
    rc, out, _ = watch.user_cmd(f"HOME=/home/ceres dconf read {WATCHFACE_KEY}",
                                timeout=12)
    val = out.strip()
    return val if rc == 0 and val else None


def write_watchface(watch, quoted_value: str) -> bool:
    """Write the watchface key. `quoted_value` must already be a gvariant
    string literal (dconf read gives one back verbatim, which is exactly what
    restore needs)."""
    rc, _, err = watch.user_cmd(
        f"HOME=/home/ceres dconf write {WATCHFACE_KEY} {json.dumps(quoted_value)}",
        timeout=12)
    if rc != 0:
        log.warning("watchface write failed: %s", err.strip()[:120])
    return rc == 0


def bench_value() -> str:
    return f"'file://{WATCHFACE_DIR}/{BENCH_NAME}.qml'"


def sample_fps_script(seconds: int, interval: float = 0.5) -> str:
    """One shell command that samples the kernel FPS node for the whole run and
    prints `epoch fps` lines. A single round-trip beats several hundred: each
    adb shell call costs ~100 ms, which would otherwise dominate the sampling
    interval and perturb the very thing being measured."""
    n = int(seconds / interval)
    return (f'for i in $(seq 1 {n}); do '
            f'echo "$(date +%s.%N) $(cat {FPS_NODE} 2>/dev/null)"; '
            f'sleep {interval}; done')


def parse_samples(out: str) -> "list[tuple[float, float]]":
    """`epoch fps` lines → [(ts, fps)]. Pure — see tests."""
    rows = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return rows


def align_phases(samples, t0: float) -> "list[dict]":
    """Split samples into the phase windows the watchface is running, given the
    moment the switch was written. Approximate by nature — the watchface takes
    a moment to load, so the first phase's window is the fuzziest — which is
    why the watch's own on-screen numbers stay authoritative and these are the
    cross-check. Pure — see tests."""
    out = []
    start = t0 + COUNTDOWN_S
    for name, dur in PHASES:
        end = start + dur
        vals = [f for ts, f in samples if start <= ts < end]
        # Ignore zeros: the node reads 0.0 when the panel is not committing
        # frames at all, which is an absence of data, not a measurement.
        vals = [v for v in vals if v > 0]
        out.append({
            "phase": name,
            "avg": round(sum(vals) / len(vals), 1) if vals else None,
            "min": round(min(vals), 1) if vals else None,
            "max": round(max(vals), 1) if vals else None,
            "samples": len(vals),
        })
        start = end
    return out
