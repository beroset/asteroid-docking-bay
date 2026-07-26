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


BOOTCHART_OUT = """\
UserspaceTimestampMonotonic=4866151
FinishTimestampMonotonic=28918533
---UNITS---
Id=android-init.service
InactiveExitTimestampMonotonic=8172854
ActiveEnterTimestampMonotonic=8193771

Id=never-ran.service
InactiveExitTimestampMonotonic=0
ActiveEnterTimestampMonotonic=0

Id=user@1000.service
After=android-init.service basic.target
InactiveExitTimestampMonotonic=9830898
ActiveEnterTimestampMonotonic=12922169
"""


def test_parse_bootchart_summary_units_and_zero_drop():
    """Manager stamps become the kernel/userspace/finish summary; each ran
    unit carries start and activation span; units with a zero start stamp
    never ran this boot and are dropped. Planted-bug: remove the ie<=0 skip
    and a phantom bar at t=0 appears, failing the unit count."""
    from asteroid_docking_bay.boottime import parse_bootchart
    d = parse_bootchart(BOOTCHART_OUT)
    assert d["userspace_s"] == 4.87 and d["finish_s"] == 28.92
    assert [u["unit"] for u in d["units"]] == ["android-init.service",
                                               "user@1000.service"]
    assert d["units"][1]["start_s"] == 9.83
    assert d["units"][1]["dur_s"] == 3.09


def test_critical_chain_walks_gating_after_edges():
    """The chain starts at the latest-finishing unit and walks back through
    After= deps that FINISHED before it started — later-running or timing-less
    deps (targets) never gate. Planted-bug: pick the gating dep by start
    instead of finish and the chain picks early.service, failing this."""
    from asteroid_docking_bay.boottime import critical_chain
    units = [
        {"unit": "slowdep.service", "start_s": 1.0, "dur_s": 3.0,
         "after": []},
        {"unit": "quick.service",  "start_s": 2.0, "dur_s": 0.1,
         "after": []},
        {"unit": "late.service",   "start_s": 5.0, "dur_s": 2.0,
         "after": ["slowdep.service", "quick.service", "ghost.target"]},
        # timer-fired long after boot: must never anchor the chain
        {"unit": "tmpfiles-clean.service", "start_s": 900.0, "dur_s": 0.3,
         "after": []},
    ]
    # quick STARTS later (2.0) but slowdep FINISHES later (4.0): the gating
    # dep is the late finisher.
    assert critical_chain(units, finish_s=8.0) == ["slowdep.service",
                                                   "late.service"]


def test_parse_bootchart_carries_chain():
    from asteroid_docking_bay.boottime import parse_bootchart
    d = parse_bootchart(BOOTCHART_OUT)
    assert d["critical_chain"] == ["android-init.service", "user@1000.service"]
