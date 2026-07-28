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


# ── benchymark driving ──────────────────────────────────────────────────────

def test_installer_clears_a_stale_assets_tree():
    """The withdrawn nutty-benchy watchface used to be installed as assets/
    beside the package. An upgrade must clear it, or an install tree keeps
    carrying files nothing reads. Planted-bug: drop the rm line and this
    fails."""
    from pathlib import Path
    sh = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    assert 'rm -rf "${LIB_DIR}/assets"' in sh
    assert 'cp -r assets' not in sh


def test_newest_ipk_ignores_sibling_packages(tmp_path, monkeypatch):
    """The build emits benchymark-src/-dev/-dbg beside the real package, and
    installing one of those puts no app on the watch. Planted-bug: widen the
    glob to benchymark* and this picks -src."""
    from asteroid_docking_bay import bench
    d = tmp_path / "armv7vehf-neon"
    d.mkdir()
    real = d / "benchymark_1.0-r0_armv7vehf-neon.ipk"
    real.write_text("real")
    for sib in ("benchymark-src", "benchymark-dev", "benchymark-dbg"):
        (d / f"{sib}_1.0-r0_armv7vehf-neon.ipk").write_text("nope")
    monkeypatch.setattr(bench, "IPK_DIR", tmp_path)
    assert bench.newest_ipk() == str(real)


def test_newest_ipk_returns_none_when_nothing_built(tmp_path, monkeypatch):
    """A missing build directory must read as 'nothing built' rather than
    raising — the UI button carries no arguments and the op turns this into a
    readable error."""
    from asteroid_docking_bay import bench
    monkeypatch.setattr(bench, "IPK_DIR", tmp_path / "does-not-exist")
    assert bench.newest_ipk() is None
