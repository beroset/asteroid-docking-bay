# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Boot-time measurement — how long a watch takes from VBUS-on to usable,
recorded per watch into the fleet registry.

Everything is measured RETROACTIVELY at the first live sighting after a boot
we triggered — no tight polling, no extra bus pressure:

- T0 is `booting_since`, stamped by the op that powered the port.
- `boot_adb_s` (T1): the kernel's own timestamp of the watch enumerating
  (from `journalctl -k`, exact to the millisecond), minus T0.
- The anchor: one `/proc/uptime` read over the live link converts every
  boot-relative instant on the watch to host wall-clock. `boot_kernel_s`
  (kernel start − T0) falls out for free — that is the bootloader phase.
  The anchor also guards validity: a kernel that started BEFORE T0 means the
  watch was running all along (a re-enumeration, not a boot) — no sample.
- `boot_ui_s` (T2): optional. Config key `boot_marker_cmd` runs on the watch
  and must print the boot-relative SECONDS of the "UI is up" moment; which
  marker is trustworthy per build is a watch-side call (moWerk's), so
  nothing is assumed here — unset means not measured.
"""

from __future__ import annotations

import re
import time

from .util import _run, log

# journalctl -o short-unix: "1753500000.123456 host kernel: usb 1-3.2: SerialNumber: X"
_ENUM_RE = re.compile(r"^(\d+\.\d+)\s.*SerialNumber:\s*(\S+)\s*$")


def parse_enum_ts(journal_out: str, serial: str) -> "float | None":
    """The kernel timestamp of `serial`'s LAST enumeration in a
    `journalctl -k -o short-unix` capture, or None. Last, not first: a watch
    can re-enumerate (mode switches, retries) and the boot we are anchoring
    ends at the most recent appearance. Pure — see tests."""
    ts = None
    for line in journal_out.splitlines():
        m = _ENUM_RE.match(line.strip())
        if m and m.group(2) == serial:
            ts = float(m.group(1))
    return ts


def _kernel_enum_ts(serial: str, since: float) -> "float | None":
    """Host-side: when did this serial enumerate, per the kernel journal."""
    rc, out, _ = _run(f"journalctl -k -o short-unix --since @{int(since)} "
                      f"--no-pager", check=False, timeout=10)
    if rc != 0:
        return None
    return parse_enum_ts(out, serial)


def measure_boot(serial: str, t0: float, shell, marker_cmd: "str | None" = None,
                 now: "float | None" = None,
                 enum_ts: "float | None" = None) -> "dict | None":
    """Measure one completed boot. Returns {boot_kernel_s, boot_adb_s?,
    boot_ui_s?} rounded to 0.1s, or None when this was not a real boot.

    shell is a transport runner (cmd -> (rc, out, err)). now and enum_ts are
    injectable for tests; live callers leave them None."""
    rc, out, _ = shell("cat /proc/uptime")
    try:
        uptime = float(out.split()[0])
    except (ValueError, IndexError):
        return None
    if rc != 0:
        return None
    now = time.time() if now is None else now
    boot_instant = now - uptime
    if boot_instant <= t0:
        # The kernel predates the power-on: the watch was running the whole
        # time (VBUS restore, re-enumeration). Not a boot — no sample.
        return None
    result = {"boot_kernel_s": round(boot_instant - t0, 1)}
    if enum_ts is None:
        enum_ts = _kernel_enum_ts(serial, t0)
    if enum_ts and enum_ts > t0:
        result["boot_adb_s"] = round(enum_ts - t0, 1)
    if marker_cmd:
        rc, out, _ = shell(marker_cmd)
        try:
            rel = float(out.strip().split()[-1])
            if rc == 0 and rel >= 0:
                result["boot_ui_s"] = round((boot_instant + rel) - t0, 1)
        except (ValueError, IndexError):
            log.debug("boot marker cmd for %s: unparseable output %r",
                      serial, out[:80])
    return result
