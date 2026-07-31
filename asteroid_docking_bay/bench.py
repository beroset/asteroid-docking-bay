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
import shlex
import time
from pathlib import Path

APP_NAME = "benchymark"
# benchymark lives in its own repo now — https://github.com/moWerk/benchymark
# — so a-d-b consumes it as a BUILT ARTEFACT rather than carrying its source.
# Keeping a second copy of the app here would have drifted from upstream the
# first time either side was touched.
#
# Two ways to get an ipk into this directory:
#   * build it from a checkout of that repo (see its packaging/benchymark.bb),
#     which drops the package here on this rig; or
#   * download the ipk attached to a release and drop it in.
IPK_DIR = Path.home() / "Git/asteroid/build/tmp-qt6/deploy/ipk"
APP_RESULTS = "/home/ceres/.local/share/benchymark/last-run.json"
# The app publishes its live phase here. It is PERSISTENT dconf, so a value
# left by the previous run matches instantly and a poller thinks the new run
# has already reached that phase.
PHASE_BEACON = "/desktop/asteroid/benchphase"
# `pgrep -f benchymark` also matches the SHELL RUNNING THE PGREP, because its
# own command line contains the word — so a liveness check could report the
# app running when only the check was. The bracket makes the pattern fail to
# match its own literal text while still matching the process.
_PGREP = f"pgrep -f [{APP_NAME[0]}]{APP_NAME[1:]}"


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
    """Launch it in the watch's own session, and CONFIRM it is running.

    `setsid app &` on its own does not survive: `su` exits the instant the job
    is backgrounded and takes the child with it, so Start reported success
    while nothing ran — the worst failure a sweep can inherit, because every
    later step then measures an app that was never there.

    systemd-run detaches properly and --collect reaps the transient unit when
    the app exits, so repeated starts cannot collide on a stale unit name. The
    fallback keeps the shell alive a moment instead, which is enough for the
    child to detach on images without systemd-run.
    """
    # A BLANKED PANEL PRESENTS NO FRAMES. benchymark counts frameSwapped, so a
    # run on a dark screen records a complete, plausible result file of ZEROS —
    # right sample counts, every average nil. DisplayBlanking keeps a lit
    # screen lit; it does not wake a dark one, so the panel must be woken
    # before the app starts or the whole run is worthless without saying so.
    watch.t.shell(shlex.quote("mcetool --unblank-screen; mcetool --blank-prevent"),
                  timeout=15)
    # Clear the persistent phase beacon, or a stale value from the last run
    # makes a poller believe this one is already mid-flight.
    watch.user_cmd(f"HOME=/home/ceres dconf reset {PHASE_BEACON}", timeout=12)

    rc, out, err = watch.user_cmd(
        f"systemd-run --user --collect {APP_NAME}", timeout=20)
    if rc != 0:
        watch.user_cmd(f"setsid {APP_NAME} >/dev/null 2>&1 & sleep 2",
                       timeout=20)
    for _ in range(6):
        time.sleep(1)
        if watch.t.shell(shlex.quote(_PGREP), timeout=10)[1].strip():
            return None
    return (f"launch did not take: {(err or out).strip()[:100]}"
            if rc != 0 else "launch reported success but no process is running")


def app_stop(watch) -> "str | None":
    """Kill it, and confirm it died.

    NOT pkill: these images have pgrep but no pkill, so the obvious command
    failed silently and Stop did nothing while reporting nothing. Feeding
    pgrep's output to kill works with what BusyBox actually provides.
    """
    # quoted whole: the $() and the redirect would otherwise be run by the
    # HOST shell, which is what test_compound_shell_commands_are_quoted_whole
    # exists to catch — and duly did.
    watch.t.shell(shlex.quote(f"kill $({_PGREP}) 2>/dev/null"),
                  timeout=15)
    for _ in range(4):
        time.sleep(1)
        if not watch.t.shell(shlex.quote(_PGREP), timeout=10)[1].strip():
            return None
    watch.t.shell(shlex.quote(f"kill -9 $({_PGREP}) 2>/dev/null"),
                  timeout=15)
    time.sleep(1)
    if watch.t.shell(shlex.quote(_PGREP), timeout=10)[1].strip():
        return "could not stop benchymark"
    return None


def app_remove(watch) -> "str | None":
    rc, out, err = watch.t.shell(f"opkg remove {APP_NAME} --force-depends",
                                 timeout=60)
    return None if rc == 0 else f"opkg remove failed: {(err or out).strip()[:160]}"


def all_zero(result: dict) -> bool:
    """True when every phase averaged zero while still collecting samples.

    That is the signature of a run against a BLANKED PANEL: the clock ran and
    the sampler ticked, but no frame was ever presented. It looks like a
    complete result and is worth nothing, so it has to be named rather than
    quietly averaged into a campaign. Pure — see tests.
    """
    phases = result.get("phases") or []
    if not phases:
        return False
    return (all((p.get("avg") or 0) == 0 for p in phases)
            and any((p.get("samples") or 0) > 0 for p in phases))


def app_results(watch) -> "dict | None":
    """The last run the app wrote, or None when it has never completed one."""
    rc, out, _ = watch.t.shell(f"cat {APP_RESULTS}", timeout=15)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None
