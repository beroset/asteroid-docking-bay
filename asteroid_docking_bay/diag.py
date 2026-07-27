# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""The a-d-b-doctor dataset: standard-kernel diagnostics no watch image ships
tools for, gathered raw in ONE transport round-trip and parsed host-side —
the a-d-b-analyze recipe generalized (kido's lead, 2026-07-27).

Sources (each degrades to absent, never errors): wakeup sources (suspend
blockers with per-source prevent_suspend_time — the drain detective),
suspend statistics, cpufreq residency, eMMC JEDEC lifetime, failed units,
journal error digest, interrupt totals, boot count, PSI (mainline kernels),
battery true-capacity fade (where the gauge exposes it)."""

from __future__ import annotations

_M = "echo ---%s---"

DIAG_SCRIPT = "; ".join([
    _M % "wakeup",
    "cat /sys/kernel/debug/wakeup_sources 2>/dev/null | head -60",
    _M % "suspend",
    "cat /sys/power/suspend_stats/success /sys/power/suspend_stats/fail 2>/dev/null",
    "grep -sE '^(success|fail):' /sys/kernel/debug/suspend_stats 2>/dev/null | head -2",
    _M % "freq",
    "cat /sys/devices/system/cpu/cpu0/cpufreq/stats/time_in_state 2>/dev/null",
    _M % "emmc",
    "cat /sys/class/mmc_host/mmc*/mmc*/life_time 2>/dev/null",
    "cat /sys/class/mmc_host/mmc*/mmc*/pre_eol_info 2>/dev/null",
    _M % "failed",
    "systemctl --failed --no-legend --plain 2>/dev/null",
    _M % "errors",
    "journalctl -p 3 -b --no-pager 2>/dev/null | tail -12",
    _M % "boots",
    "journalctl --list-boots 2>/dev/null | wc -l",
    _M % "psi",
    "grep -r . /proc/pressure 2>/dev/null",
    _M % "batfull",
    "cat /sys/class/power_supply/battery/charge_full "
    "/sys/class/power_supply/battery/charge_full_design 2>/dev/null",
    _M % "irq",
    "cat /proc/interrupts 2>/dev/null",
])

# JEDEC eMMC 5.0 device-life-time estimates: 0x01..0x0A = consumed decile
# bands, 0x0B = estimates exceeded. pre_eol: reserved-block consumption.
_LIFE = {1: "0-10% used", 2: "10-20% used", 3: "20-30% used",
         4: "30-40% used", 5: "40-50% used", 6: "50-60% used",
         7: "60-70% used", 8: "70-80% used", 9: "80-90% used",
         10: "90-100% used", 0x0B: "EXCEEDED"}
_PRE_EOL = {1: "normal", 2: "warning (80% reserved blocks used)",
            3: "urgent (90% reserved blocks used)"}


def _sections(out: str) -> dict:
    sec, cur = {}, None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("---") and s.endswith("---"):
            cur = s.strip("-")
            sec[cur] = []
        elif cur is not None:
            sec[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in sec.items()}


def parse_diag(out: str) -> dict:
    """Raw one-round-trip capture → structured diagnostics. Pure — see tests."""
    sec = _sections(out)
    d: dict = {}

    # Wakeup sources: name then 9 numeric columns; sort by prevent_suspend_time
    # (ms the source blocked suspend), then total_time — the blockers first.
    rows = []
    for line in sec.get("wakeup", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            nums = [int(x) for x in parts[-9:]]
        except ValueError:
            continue
        name = " ".join(parts[:-9])
        rows.append({"name": name, "active_count": nums[0],
                     "total_ms": nums[5], "prevent_ms": nums[8]})
    rows.sort(key=lambda r: (r["prevent_ms"], r["total_ms"]), reverse=True)
    d["wakeup_sources"] = rows[:10]

    # Two shapes: bare numbers (/sys/power/suspend_stats/*) or labeled lines
    # from the debugfs fallback ("success: 0"). Take the digits either way.
    ss = [t for t in sec.get("suspend", "").replace(":", " ").split()
          if t.isdigit()]
    if len(ss) >= 2:
        d["suspend"] = {"success": int(ss[0]), "fail": int(ss[1])}

    freq = []
    for line in sec.get("freq", "").splitlines():
        p = line.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            freq.append((int(p[0]), int(p[1])))
    total = sum(t for _, t in freq)
    if total:
        d["freq_residency"] = [
            {"mhz": f // 1000, "pct": round(t * 100 / total, 1)}
            for f, t in sorted(freq)]

    em = sec.get("emmc", "").split()
    hexes = [int(x, 16) for x in em if x.lower().startswith("0x")]
    plains = [int(x, 16) for x in em if not x.lower().startswith("0x") and x]
    if hexes:
        d["emmc_life"] = [_LIFE.get(h, f"0x{h:02x}") for h in hexes[:2]]
    if plains:
        d["emmc_pre_eol"] = _PRE_EOL.get(plains[0], str(plains[0]))

    d["failed_units"] = [l.split()[0] for l in sec.get("failed", "").splitlines()
                         if l.strip() and not l.startswith("0 loaded")]
    errs = [l for l in sec.get("errors", "").splitlines()
            if l.strip() and "-- No entries --" not in l]
    d["errors"] = errs[-12:]

    b = sec.get("boots", "").strip()
    if b.isdigit() and int(b) > 1:          # minus the header line
        d["boots"] = int(b) - 1

    if sec.get("psi", "").strip():
        d["psi"] = sec["psi"].splitlines()

    bf = sec.get("batfull", "").split()
    if len(bf) >= 2 and all(x.lstrip("-").isdigit() for x in bf[:2]) \
            and int(bf[1]) > 0:
        d["bat_capacity_pct"] = round(int(bf[0]) * 100 / int(bf[1]), 1)

    irqs = []
    for line in sec.get("irq", "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or not parts[0].rstrip(":").isdigit():
            continue
        counts = []
        for p in parts[1:]:
            if p.isdigit():
                counts.append(int(p))
            else:
                break
        if counts:
            irqs.append({"irq": parts[0].rstrip(":"),
                         "count": sum(counts),
                         "desc": " ".join(parts[1 + len(counts):])[:48]})
    irqs.sort(key=lambda r: r["count"], reverse=True)
    d["top_irqs"] = irqs[:6]
    return d
