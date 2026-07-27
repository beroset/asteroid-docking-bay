# SPDX-License-Identifier: GPL-3.0-only
"""The a-d-b-doctor parser: raw one-round-trip capture -> structured diag."""

from asteroid_docking_bay.diag import parse_diag

SAMPLE = """\
---wakeup---
name\tactive_count\tevent_count\twakeup_count\texpire_count\tactive_since\ttotal_time\tmax_time\tlast_change\tprevent_suspend_time
mce_input_handler \t1\t1\t0\t0\t0\t0\t0\t33195\t0
alarmtimer \t4\t4\t0\t0\t0\t8123\t4000\t99\t7500
---suspend---
0
0
---freq---
800000 100
1094400 900
---emmc---
0x07 0x09
01
---failed---
swclock-offset-boot.service loaded failed failed
---errors---
Jul 27 kernel: something bad
---boots---
2
---psi---
---batfull---
300000
400000
---irq---
           CPU0       CPU1
 18:     100      50       GIC  20  arch_t
 20:       5       5       GIC  39  arch_m
"""


def test_parse_diag_full_sample():
    """Every section lands structured; wakeup sources sort blockers first
    (prevent_suspend_time), eMMC hex maps to JEDEC decile bands. Planted-bug:
    shift the JEDEC band mapping by one and the 60-70% assertion fails."""
    d = parse_diag(SAMPLE)
    assert d["wakeup_sources"][0]["name"] == "alarmtimer"      # blocker first
    assert d["wakeup_sources"][0]["prevent_ms"] == 7500
    assert d["suspend"] == {"success": 0, "fail": 0}
    assert d["freq_residency"] == [{"mhz": 800, "pct": 10.0},
                                   {"mhz": 1094, "pct": 90.0}]
    assert d["emmc_life"] == ["60-70% used", "80-90% used"]
    assert d["emmc_pre_eol"] == "normal"
    assert d["failed_units"] == ["swclock-offset-boot.service"]
    assert d["boots"] == 1
    assert d["bat_capacity_pct"] == 75.0
    assert d["top_irqs"][0]["irq"] == "18" and d["top_irqs"][0]["count"] == 150
    assert "psi" not in d


def test_parse_diag_degrades_to_empty():
    d = parse_diag("---wakeup---\n")
    assert d["wakeup_sources"] == [] and d["failed_units"] == []
