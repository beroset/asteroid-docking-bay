# SPDX-License-Identifier: GPL-3.0-only
"""Boot-time measurement: journal parsing and the retroactive computation.
The whole design is anchor-based (one /proc/uptime read converts watch
boot-relative instants to wall clock) — no tight polling to test."""

from asteroid_docking_bay.boottime import measure_boot, parse_enum_ts

JOURNAL = """\
1000.100000 host kernel: usb 1-3.2: new high-speed USB device number 9 using xhci_hcd
1000.200000 host kernel: usb 1-3.2: SerialNumber: AAA
1050.500000 host kernel: usb 1-6.1: SerialNumber: BBB
1090.900000 host kernel: usb 1-3.2: SerialNumber: AAA
"""


def test_parse_enum_ts_takes_last_match_per_serial():
    assert parse_enum_ts(JOURNAL, "AAA") == 1090.9   # last re-enumeration wins
    assert parse_enum_ts(JOURNAL, "BBB") == 1050.5
    assert parse_enum_ts(JOURNAL, "CCC") is None


def _shell_uptime(uptime, marker_out=None):
    def shell(cmd):
        if "uptime" in cmd:
            return 0, f"{uptime} 999.0", ""
        return 0, marker_out or "", ""
    return shell


def test_measure_boot_computes_all_three_values():
    # t0=1000; kernel started at now-uptime=2000-980=1020 (20s bootloader);
    # adb gadget enumerated at 1055 (55s); marker says UI at 60s after kernel.
    res = measure_boot("AAA", 1000.0, _shell_uptime(980.0, "60.0"),
                       marker_cmd="echo-marker", now=2000.0, enum_ts=1055.0)
    assert res == {"boot_kernel_s": 20.0, "boot_adb_s": 55.0,
                   "boot_ui_s": 80.0}


def test_measure_boot_rejects_a_watch_that_was_running_all_along():
    """A kernel older than the power-on means re-enumeration, not a boot —
    recording it would pollute the per-watch boot stats with near-zero
    'boots'. Planted-bug: drop the boot_instant<=t0 guard and this fails."""
    # now-uptime = 2000-1500 = 500 < t0=1000 -> watch was up before power-on
    assert measure_boot("AAA", 1000.0, _shell_uptime(1500.0),
                        now=2000.0, enum_ts=1055.0) is None


def test_measure_boot_survives_bad_marker_output():
    res = measure_boot("AAA", 1000.0, _shell_uptime(980.0, "not a number"),
                       marker_cmd="broken", now=2000.0, enum_ts=1055.0)
    assert res == {"boot_kernel_s": 20.0, "boot_adb_s": 55.0}   # no boot_ui_s
