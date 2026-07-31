# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Reading a wanze trace honestly.

The two failure modes worth testing are both silent. Timing computed from the
watch's wall clock looks perfectly reasonable while being months wrong
(catfish's RTC was 130 days behind while recording fine), and a gap read as
"missing data" rather than "the watch slept" throws away the one measurement
this probe uniquely provides.
"""

from asteroid_docking_bay.wanze import (analyse, clock_check, gaps, parse,
                                        segments)

HEADER = ("epoch,uptime,current_ua,capacity,status,voltage_uv,temp,charger,"
          "backlight,cpu_online,cpu_freq,load1,gauge,schema")


def row(epoch, uptime, cap=100, status="Discharging", cur=-5000, back=0):
    return (f"{epoch},{uptime},{cur},{cap},{status},4400000,319,0,{back},"
            f"4,1094400,0.5,nanohub_fuelgauge-0,1")


def csv(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


# --- parsing --------------------------------------------------------------

def test_parses_a_real_row():
    rows = parse(csv(row(1774271275, 679.73)))
    assert len(rows) == 1
    r = rows[0]
    assert r["uptime"] == 679.73 and r["capacity"] == 100
    assert r["gauge"] == "nanohub_fuelgauge-0"


def test_columns_are_read_by_name_not_position():
    """A trace harvested across an upgrade can hold two schemas. Reading by
    position would shift every later field and produce plausible nonsense."""
    text = ("uptime,epoch,capacity,current_ua,status\n"
            "500.0,1774271275,88,-4000,Discharging\n")
    rows = parse(text)
    assert rows[0]["capacity"] == 88 and rows[0]["current_ua"] == -4000


def test_torn_row_is_skipped_not_fatal():
    """Power can be lost mid-append; one bad line must not lose the trace."""
    rows = parse(csv(row(1, 100.0), "1774271280,", row(3, 400.0)))
    assert len(rows) == 2


def test_a_file_that_is_not_wanze_yields_nothing():
    assert parse("epoch,current_ua,capacity,status\n1,2,3,Full\n") == []
    assert parse("") == []


# --- reboots --------------------------------------------------------------

def test_an_uptime_drop_splits_the_trace():
    """uptime only rises inside one boot, so a drop IS a reboot — free
    forensics, and a break that timing must never be computed across."""
    rows = parse(csv(row(1, 500.0), row(2, 800.0), row(3, 40.0), row(4, 300.0)))
    segs = segments(rows)
    assert len(segs) == 2
    assert analyse(rows)["reboots"] == 1


def test_a_reboot_is_not_counted_as_a_gap():
    """Across a reboot the uptime delta is negative-then-small; treating the
    boundary as elapsed time would invent sleep that never happened."""
    rows = parse(csv(row(1, 5000.0), row(2, 30.0)))
    assert gaps(rows) == []
    assert analyse(rows)["asleep_s"] == 0


# --- gaps are data --------------------------------------------------------

def test_a_long_hole_is_recorded_as_sleep():
    rows = parse(csv(row(1, 300.0, cap=100), row(2, 11100.0, cap=97)))
    holes = gaps(rows)
    assert len(holes) == 1
    assert holes[0]["seconds"] == 10800
    assert holes[0]["capacity_from"] == 100 and holes[0]["capacity_to"] == 97


def test_normal_cadence_is_not_a_gap():
    """Samples at the nominal interval, and a late one, are the watch being
    awake — calling those sleep would inflate every standby figure."""
    rows = parse(csv(row(1, 300.0), row(2, 600.0), row(3, 1100.0)))
    assert gaps(rows) == []


def test_asleep_fraction_is_reported():
    rows = parse(csv(row(1, 0.0), row(2, 300.0), row(3, 4000.0)))
    a = analyse(rows)
    assert a["asleep_s"] == 3700
    assert a["covered_s"] == 4000
    assert a["asleep_fraction"] == round(3700 / 4000, 3)


# --- the clock is not to be trusted ---------------------------------------

def test_a_wrong_but_steady_clock_does_not_corrupt_timing():
    """catfish recorded fine with an RTC 130 days behind. A CONSTANT offset is
    the easy case — deltas survive it either way — so this only guards against
    the skew leaking into the figures, not against epoch-based timing. The two
    tests below are the ones that discriminate."""
    good = parse(csv(row(1785512000, 300.0), row(1785522800, 11100.0)))
    skewed = parse(csv(row(1774271000, 300.0), row(1774281800, 11100.0)))
    assert analyse(good)["asleep_s"] == analyse(skewed)["asleep_s"]
    assert analyse(good)["covered_s"] == analyse(skewed)["covered_s"]


def test_a_clock_jump_must_not_invent_sleep():
    """An NTP sync moves epoch by a day while the watch was awake throughout.
    Timing taken from the wall clock would report a day of standby that never
    happened — and it would look entirely plausible in a report."""
    rows = parse(csv(row(1000, 300.0), row(87400, 600.0), row(87700, 900.0)))
    assert gaps(rows) == [], "a clock jump was mistaken for sleep"
    assert analyse(rows)["asleep_s"] == 0


def test_a_frozen_clock_must_not_hide_real_sleep():
    """The inverse: a watch whose RTC does not advance across suspend. The
    wall clock says nothing happened; uptime knows three hours passed."""
    rows = parse(csv(row(1000, 300.0), row(1000, 11100.0)))
    holes = gaps(rows)
    assert len(holes) == 1 and holes[0]["seconds"] == 10800, \
        "real sleep was hidden by a stopped clock"


def test_a_clock_step_mid_trace_is_detected():
    """An NTP sync moves epoch without moving uptime. Invisible unless the two
    are compared, and it would corrupt anything epoch-based downstream."""
    stepped = parse(csv(row(1000, 300.0), row(90000, 600.0)))
    assert clock_check(stepped)["clock_stepped"] is True
    steady = parse(csv(row(1000, 300.0), row(1300, 600.0)))
    assert steady and clock_check(steady)["clock_stepped"] is False


def test_skew_against_the_host_is_reported_not_corrected():
    """A silently corrected timestamp is worse than an obviously wrong one."""
    rows = parse(csv(row(1774271323, 300.0)))
    a = analyse(rows, host_epoch=1785512378)
    assert a["clock_skew_days"] == -130.1
    assert a["last"]["epoch"] == 1774271323 if "last" in a else True
    assert rows[0]["epoch"] == 1774271323, "rows must not be rewritten"


# --- battery, via the module that already knows how -----------------------

def test_an_always_zero_sensor_is_named_not_read_as_no_drain():
    """nemo reports 0 forever. A tidy file of zeros reads as 'no drain' unless
    something says 'no instrument'."""
    rows = parse(csv(row(1, 300.0, cur=0), row(2, 600.0, cur=0)))
    assert analyse(rows)["battery"]["sensor"] == "always-zero"


def test_screen_on_samples_are_counted():
    rows = parse(csv(row(1, 300.0, back=0), row(2, 600.0, back=70)))
    assert analyse(rows)["samples_screen_on"] == 1


def test_empty_trace_reports_not_ok():
    assert analyse([])["ok"] is False
