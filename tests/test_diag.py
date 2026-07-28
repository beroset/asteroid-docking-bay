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


# ── nutty-benchy driving ────────────────────────────────────────────────────

def test_parse_samples_and_phase_alignment():
    """Kernel FPS samples are split into the watchface's phase windows. Zeros
    are dropped: measured_fps reads 0.0 when the panel is not committing at
    all, which is an absence of data, not a measurement of zero frames.
    Planted-bug: keep the zeros and IDLE's average collapses."""
    from asteroid_docking_bay.bench import parse_samples, align_phases, COUNTDOWN_S
    t0 = 1000.0
    lines = []
    # inside IDLE (starts at t0+5): three good samples and one zero
    for i, v in enumerate((60.0, 59.5, 0.0, 60.0)):
        lines.append(f"{t0 + COUNTDOWN_S + 1 + i} {v}")
    # inside RERASTER (third phase: starts at t0+5+16)
    lines.append(f"{t0 + COUNTDOWN_S + 17} 22.5")
    rows = align_phases(parse_samples("\n".join(lines)), t0)
    by = {r["phase"]: r for r in rows}
    assert by["IDLE"]["samples"] == 3 and by["IDLE"]["min"] == 59.5
    assert by["IDLE"]["avg"] == 59.8
    assert by["RERASTER"]["avg"] == 22.5
    assert by["SCALE"]["avg"] is None          # no samples landed there
    assert [r["phase"] for r in rows][-1] == "BENCHY"


def test_bench_phase_table_matches_the_watchface():
    """The host's phase table only aligns samples correctly while it mirrors
    the QML. If someone edits the scene's phases, this fails loudly instead of
    silently attributing frames to the wrong phase."""
    import re
    from pathlib import Path
    from asteroid_docking_bay.bench import PHASES, ASSET_DIR
    qml = (ASSET_DIR / "nutty-benchy.qml").read_text()
    names = re.findall(r'\{ name: "(\w+)",\s*dur: (\d+)', qml)
    assert [(n, int(d)) for n, d in names] == PHASES


def test_installer_carries_the_bench_assets():
    """bench.py resolves its assets relative to the installed package, so
    install.sh must copy assets/ next to it. Without this the benchmark fails
    at push time on an installed host while working fine from a checkout —
    exactly the deploy-gap class of bug. Planted-bug: drop the cp line and
    this fails."""
    from pathlib import Path
    sh = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    assert 'cp -r assets "${LIB_DIR}/assets"' in sh
