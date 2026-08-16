# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Subprocess and logging plumbing shared by every module."""

from __future__ import annotations

import logging
import time
import subprocess

log = logging.getLogger("asteroid-docking-bay")


# ── Shell helpers ─────────────────────────────────────────────────────────────

def _run(cmd, check=True, timeout=None) -> tuple[int, str, str]:
    """Run a command string or list; return (rc, stdout, stderr).

    stdin is closed for every child. `adb shell` READS STDIN, and when a-d-b
    is driven from a script or a heredoc it will happily swallow the caller's
    remaining lines — the script then dies half-executed with no error at all.
    Nothing here ever wants stdin, so closing it removes a whole class of
    silent truncation rather than asking each caller to remember.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd!r}\n{result.stderr.strip()}"
        )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(levelname)s: %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.root.addHandler(sh)
    logging.root.setLevel(level)
    # Soft-import python-systemd for journald integration.
    try:
        from systemd.journal import JournaldLogHandler
        jh = JournaldLogHandler()
        jh.setLevel(logging.DEBUG)
        logging.root.addHandler(jh)
    except ImportError:
        pass


# ── Onboarding quiet window ──────────────────────────────────────────────────
# While the guided setup is ON SCREEN, a-d-b must not reshape watches
# underneath the user. Fleet-wide corrections exist to keep a SETTLED fleet
# consistent; during onboarding there is no settled fleet, and the watch they
# would correct is the one the user is holding. A first-timer who connects in
# SSH mode -- which the guide tells them works -- would otherwise watch a-d-b
# switch it back with no explanation anywhere on screen.
#
# It lives HERE, in the leaf, because the corrections do not: the status path
# and the background warmer each have their own, and gating one while missing
# the other is what let a live SSH test get undone mid-experiment.
#
# The gate is the panel being OPEN, not the last request -- a screen that polls
# nothing would drop out of an activity-based window while being read. Still
# time-bounded, because a browser tab killed mid-guide must not leave the fleet
# unmanaged forever.
ONBOARD_QUIET_SECS = 60.0
# ARMED AT STARTUP, not zero. The window lives in memory, so a restart clears
# it -- and the warmer's first pass runs about a second into startup, long
# before an open guide's next heartbeat can re-arm it. Measured exactly that on
# 2026-08-16: the gate refused twice, a deploy restarted the service, and one
# second later the same watch was switched anyway.
#
# Starting armed also matches what a-d-b actually knows at that moment, which
# is nothing: it has just come up, it has not yet seen the bus, and it cannot
# tell a stray from a watch somebody is holding. A minute of not correcting
# anything is the honest default.
_onboarding_until = time.time() + ONBOARD_QUIET_SECS


def note_onboarding_activity() -> None:
    """The guided setup is open. Hold off fleet-wide corrections."""
    global _onboarding_until
    _onboarding_until = time.time() + ONBOARD_QUIET_SECS


def release_onboarding() -> None:
    """The guided setup closed. Resume at once rather than waiting the window
    out -- a user who finished setting up should not watch a stray sit
    uncorrected for another minute."""
    global _onboarding_until
    _onboarding_until = 0.0


def onboarding_active() -> bool:
    return time.time() < _onboarding_until
