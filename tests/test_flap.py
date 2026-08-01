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


# --- mode swaps are not cradle faults -------------------------------------

def test_a_mode_swap_is_retracted_not_counted():
    """A USB-mode swap re-adds the gadget under a different product ID. It is
    counted first (so a race can never lose a REAL reconnect) and retracted
    once the device has been read."""
    f = FlapCounter()
    f.record("1-3", 1)                  # the dock
    f.record("1-3", 1)                  # the swap's re-enumeration
    assert f.reconnects("1-3", 1) == 1  # ...counted for now
    f.excuse("1-3", 1)                  # ...then read: the product ID changed
    assert f.reconnects("1-3", 1) == 0


def test_excuse_cannot_drive_the_count_negative():
    """Excuses can arrive for a port whose tally was just reset by a power
    change; that must not create a negative debt that eats real reconnects."""
    f = FlapCounter()
    f.excuse("1-3", 1)
    f.excuse("1-3", 1)
    assert f.reconnects("1-3", 1) == 0
    f.record("1-3", 1)
    f.record("1-3", 1)
    assert f.reconnects("1-3", 1) == 1, "a phantom debt swallowed a real reconnect"


def test_excuse_is_per_port():
    f = FlapCounter()
    f.record("1-3", 2)
    f.record("1-3", 2)
    f.excuse("1-3", 1)
    assert f.reconnects("1-3", 2) == 1, "a neighbour's excuse was applied here"


def test_a_reconnect_in_the_same_mode_still_counts():
    """The whole point: a cradle fault re-enumerates as the SAME device, so
    nothing retracts it."""
    f = FlapCounter()
    f.record("1-6.2", 1)
    for _ in range(3):
        f.record("1-6.2", 1)            # three genuine drops, no excuses
    assert f.reconnects("1-6.2", 1) == 3


def test_the_monitor_retracts_a_mode_swap_but_not_a_plain_drop(monkeypatch):
    """Wiring: the deferred read is what distinguishes the two, so this drives
    the monitor rather than the counter. Removing the comparison leaves every
    unit test above green."""
    from asteroid_docking_bay import usbevents as ue
    from asteroid_docking_bay.flap import flaps

    monkeypatch.setattr(ue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ue, "_last_pid", {})
    pid = {"v": "0a03"}
    monkeypatch.setattr(ue, "port_device_info",
                        lambda loc, port: {"pid": pid["v"], "vid": "18d1",
                                           "configured": True, "serial": "S",
                                           "link": None})
    flaps.reset("1-3", 9)

    flaps.record("1-3", 9); ue.UsbEventMonitor(lambda: None)._check_configured("1-3.9")
    flaps.record("1-3", 9); ue.UsbEventMonitor(lambda: None)._check_configured("1-3.9")
    assert flaps.reconnects("1-3", 9) == 1, "a same-mode reconnect was excused"

    pid["v"] = "0a02"                   # the watch switched to SSH mode
    flaps.record("1-3", 9); ue.UsbEventMonitor(lambda: None)._check_configured("1-3.9")
    assert flaps.reconnects("1-3", 9) == 1, "the mode swap was counted as a fault"
