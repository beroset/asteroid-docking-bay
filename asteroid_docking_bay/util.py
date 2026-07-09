# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Subprocess and logging plumbing shared by every module."""

import logging
import subprocess
import sys

log = logging.getLogger("asteroid-docking-bay")


# ── Shell helpers ─────────────────────────────────────────────────────────────

def _run(cmd, check=True, timeout=None) -> tuple[int, str, str]:
    """Run a command string or list; return (rc, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
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


