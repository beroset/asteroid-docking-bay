# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Driving the benchymark FPS benchmark app (docs/FPS_BENCH.md).

a-d-b owns the app's whole lifecycle — install, start, stop, remove, and
reading the last run back — but not the measurement itself: benchymark holds
the screen awake, runs its own phases and writes its own results. The host's
job is to get it onto a watch and to fetch what it wrote.

This replaced an earlier approach that pushed a benchmark *watchface*
(`nutty-benchy`) and sampled the kernel's frame counter from the host. That
could not work: Nemo.KeepAlive is not honoured for watchfaces, so the panel
blanked mid-run, and a watchface has nowhere to write results. The app can do
both, which is why none of that machinery survives here.
"""

from __future__ import annotations

import json
from pathlib import Path

APP_NAME = "benchymark"
# Where the Yocto build drops packages on this rig. Kept here rather than in
# the op so the path is one edit away if the build host changes.
IPK_DIR = Path.home() / "Git/asteroid/build/tmp-qt6/deploy/ipk"
APP_RESULTS = "/home/ceres/.local/share/benchymark/last-run.json"


def newest_ipk() -> "str | None":
    """The most recently built benchymark package, or None. Newest by mtime
    rather than by version string: during iteration the version does not
    change, only the build does."""
    # {APP_NAME}_* only: the build also emits -src, -dev and -dbg siblings,
    # and installing one of those puts no app on the watch.
    found = sorted(IPK_DIR.rglob(f"{APP_NAME}_*.ipk"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return str(found[0]) if found else None


def app_install(watch, ipk_local: str) -> "str | None":
    """Push an ipk and install it. --force-reinstall so pushing the same
    version again actually replaces it (the usual case while iterating), and
    --force-depends because a-d-b installs a single package rather than
    resolving a feed."""
    remote = f"/tmp/{Path(ipk_local).name}"
    rc, _, err = watch.t.push(ipk_local, remote, timeout=60)
    if rc != 0:
        return f"push failed: {err.strip()[:80]}"
    rc, out, err = watch.t.shell(
        f"opkg install {remote} --force-reinstall --force-depends", timeout=90)
    watch.t.shell(f"rm -f {remote}", timeout=10)
    if rc != 0:
        return f"opkg install failed: {(err or out).strip()[:160]}"
    return None


def app_start(watch) -> "str | None":
    """Launch it in the watch's own session. setsid detaches it so it outlives
    the adb shell that started it — without that the app dies with the command."""
    rc, out, err = watch.user_cmd(f"setsid {APP_NAME} >/dev/null 2>&1 &",
                                  timeout=15)
    return None if rc == 0 else f"launch failed: {(err or out).strip()[:120]}"


def app_stop(watch) -> None:
    """Kill it. Both names are tried: the wrapper script and the invoker'd
    library, since the process the launcher ends up with is the latter."""
    watch.t.shell(f"pkill -f {APP_NAME}", timeout=15)


def app_remove(watch) -> "str | None":
    rc, out, err = watch.t.shell(f"opkg remove {APP_NAME} --force-depends",
                                 timeout=60)
    return None if rc == 0 else f"opkg remove failed: {(err or out).strip()[:160]}"


def app_results(watch) -> "dict | None":
    """The last run the app wrote, or None when it has never completed one."""
    rc, out, _ = watch.t.shell(f"cat {APP_RESULTS}", timeout=15)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None
