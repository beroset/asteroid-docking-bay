# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""The reconnect counter behind the connection shame badge.

The whole value of this badge is that 0 means "clean" and any other number
means "the link dropped". Both halves of that are easy to get wrong: counting
the legitimate first enumeration would make every healthy port read 1, and
forgetting to reset on a power change would blame the cradle for our own
shelve. Both are covered here, because both would silently turn a diagnostic
number back into noise.
"""

import threading

from asteroid_docking_bay.flap import DISPLAY_MAX, RED_AT, FlapCounter


def test_docking_once_reads_zero_not_one():
    """A watch that docks and stays put enumerates exactly once. If that read
    1, every good port on the rig would wear an orange badge and the signal
    would be worthless."""
    f = FlapCounter()
    f.record("1-3", 2)
    assert f.reconnects("1-3", 2) == 0


def test_each_re_enumeration_is_one_reconnect():
    f = FlapCounter()
    for _ in range(4):                      # the dock plus three drops
        f.record("1-3", 2)
    assert f.reconnects("1-3", 2) == 3


def test_untouched_port_is_zero():
    """A port nothing has ever happened on must not look flappy."""
    assert FlapCounter().reconnects("1-9", 1) == 0


def test_power_change_resets_the_tally():
    """Our own shelve/cycle is not the cradle's fault. Without this every port
    op would inflate the badge."""
    f = FlapCounter()
    for _ in range(5):
        f.record("1-6", 3)
    assert f.reconnects("1-6", 3) == 4
    f.reset("1-6", 3)
    assert f.reconnects("1-6", 3) == 0
    f.record("1-6", 3)                      # the watch coming back after power
    assert f.reconnects("1-6", 3) == 0


def test_ports_are_counted_independently():
    """The rig's worst offender sits next to healthy ports on the same hub."""
    f = FlapCounter()
    for _ in range(9):
        f.record("1-6.2", 1)
    f.record("1-6.2", 2)
    assert f.reconnects("1-6.2", 1) == 8
    assert f.reconnects("1-6.2", 2) == 0
    f.reset("1-6.2", 1)
    assert f.reconnects("1-6.2", 2) == 0    # reset must not touch a neighbour


def test_location_is_part_of_the_key():
    """Port 1 exists on every hub; they are different ports."""
    f = FlapCounter()
    f.record("1-3", 1)
    f.record("1-3", 1)
    assert f.reconnects("1-3", 1) == 1
    assert f.reconnects("1-9", 1) == 0


def test_snapshot_omits_clean_ports():
    f = FlapCounter()
    f.record("1-3", 1)                      # clean dock — no reconnects
    f.record("1-9", 4)
    f.record("1-9", 4)
    assert f.snapshot() == {"1-9.4": 1}


def test_reset_of_unknown_port_is_harmless():
    """Powering a port we have never seen an event on must not raise."""
    f = FlapCounter()
    f.reset("1-3", 7)
    assert f.reconnects("1-3", 7) == 0


def test_concurrent_records_lose_nothing():
    """record() runs on the udev thread while the web thread reads. A dropped
    increment would under-report exactly the port that is misbehaving most."""
    f = FlapCounter()

    def hammer():
        for _ in range(200):
            f.record("1-6.2", 1)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert f.reconnects("1-6.2", 1) == 4 * 200 - 1


def test_badge_thresholds_stay_single_digit():
    """The badge is a circle with one numeral in it; two digits break the
    shape. RED_AT must sit below the clamp or the orange band would swallow
    every visible value."""
    assert 0 < RED_AT <= DISPLAY_MAX
    assert DISPLAY_MAX < 10


# --- wiring ---------------------------------------------------------------
# The counter being correct is worth nothing if nothing feeds it. Removing
# either call site below leaves every unit test above green, so these assert
# the wiring itself rather than the arithmetic.

def test_powering_a_port_resets_its_tally(monkeypatch):
    """uhubctl_set_power must clear the count. Without this a shelve or a
    charge cycle inflates the badge and the number stops meaning anything."""
    from asteroid_docking_bay import usb
    from asteroid_docking_bay.flap import flaps

    monkeypatch.setattr(usb, "_sysfs_set_power", lambda loc, port, on: True)
    monkeypatch.setattr(usb, "_sysfs_get_power", lambda loc, port: True)
    monkeypatch.setattr(usb, "_sysfs_power_is_complete", lambda loc, port: True)

    for _ in range(6):
        flaps.record("1-testhub", 4)
    assert flaps.reconnects("1-testhub", 4) == 5
    usb.uhubctl_set_power("1-testhub", 4, True)
    assert flaps.reconnects("1-testhub", 4) == 0, \
        "powering a port did not reset its reconnect tally"


def test_cycling_a_port_resets_its_tally(monkeypatch):
    from asteroid_docking_bay import usb
    from asteroid_docking_bay.flap import flaps

    monkeypatch.setattr(usb, "_sysfs_set_power", lambda loc, port, on: True)
    monkeypatch.setattr(usb.time, "sleep", lambda s: None)

    for _ in range(4):
        flaps.record("1-testhub", 5)
    usb.uhubctl_cycle("1-testhub", 5)
    assert flaps.reconnects("1-testhub", 5) == 0, \
        "cycling a port did not reset its reconnect tally"


def test_a_udev_add_event_is_counted():
    """The monitor must feed the counter. This is the only producer — if the
    call is dropped, every port on the rig reads a permanent, reassuring 0."""
    from asteroid_docking_bay.flap import flaps
    from asteroid_docking_bay.usbevents import UsbEventMonitor

    class _Proc:
        stdout = iter([
            "UDEV  [111.1] add   /devices/pci0/usb1/1-3/1-3.7 (usb)\n",
            "UDEV  [111.2] add   /devices/pci0/usb1/1-3/1-3.7 (usb)\n",
            # An interface event for the same plug — must NOT count again.
            "UDEV  [111.3] add   /devices/pci0/usb1/1-3/1-3.7/1-3.7:1.0 (usb)\n",
            "UDEV  [111.4] remove /devices/pci0/usb1/1-3/1-3.7 (usb)\n",
        ])

    mon = UsbEventMonitor(on_change=lambda: None)
    mon._proc = _Proc()
    # Stub the slot-exhaustion probe, NOT _stop: setting _stop makes _read()
    # return on its first line, so the test would pass without counting
    # anything — a test that cannot fail.
    mon._check_configured = lambda dev: None
    flaps.reset("1-3", 7)
    mon._read()
    # Two device-level adds = one reconnect. The interface event (":1.0") is a
    # duplicate of the same plug and must not inflate the count.
    assert flaps.reconnects("1-3", 7) == 1, \
        "udev add events are not reaching the counter (or interfaces double-count)"


# --- re-enumerations we cause ourselves -----------------------------------

def test_an_expected_reenumeration_is_not_shameful():
    """A USB-mode swap (adb <-> ssh) drops the gadget and re-adds it. At the
    udev level that is an ordinary `add`; only the code that CAUSED it knows
    otherwise, so doing the rig's job must not shame a healthy cradle."""
    f = FlapCounter()
    f.record("1-3", 1)                  # the dock itself
    f.expect("1-3", 1)                  # we are about to switch modes
    f.record("1-3", 1)                  # ...and here is its re-enumeration
    assert f.reconnects("1-3", 1) == 0, "a mode swap was counted as a fault"


def test_only_the_announced_add_is_excused():
    """The grace covers exactly one add. A genuine drop straight afterwards
    must still count, or one switch would blind the port indefinitely."""
    f = FlapCounter()
    f.record("1-3", 1)
    f.expect("1-3", 1)
    f.record("1-3", 1)                  # excused
    f.record("1-3", 1)                  # real
    f.record("1-3", 1)                  # real
    assert f.reconnects("1-3", 1) == 2


def test_expectations_do_not_leak_to_other_ports():
    f = FlapCounter()
    f.expect("1-3", 1)
    f.record("1-3", 2)
    f.record("1-3", 2)
    assert f.reconnects("1-3", 2) == 1, "a neighbour consumed the grace"


def test_a_power_change_drops_a_stale_expectation():
    """A switch that was announced but never happened would otherwise swallow
    a real reconnect much later, and the badge would under-report forever."""
    f = FlapCounter()
    f.expect("1-3", 1)                  # announced...
    f.reset("1-3", 1)                   # ...but the port got power-cycled instead
    f.record("1-3", 1)                  # the dock after power
    f.record("1-3", 1)                  # a genuine drop
    assert f.reconnects("1-3", 1) == 1, "a stale expectation ate a real reconnect"
