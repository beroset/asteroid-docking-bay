# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Counting how often a port re-enumerates — the connection shame badge.

A failing cradle does not announce itself. It produces an impression: "that
one's flakey", "it drops sometimes". Meanwhile the kernel has been counting the
whole time, and nobody was reading it — the fleet's worst offender was found by
hand-counting `dmesg`, which is not a thing anyone does twice.

So count it. A watch that docks once and stays put enumerates ONCE; every
enumeration after that is the connection failing and recovering. That number
is the difference between a hunch and a diagnosis, and it is the one signal
that distinguishes a bad cradle from a bad watch without touching either.

Deliberately NOT a diagnosis. It reports how often the bus said "add" and
nothing more — the rig moves real atoms, and what a high count MEANS (bent
lead, dirty pad, tired cable, a wiggle-prone print) is mo's call at the bench.

Counts live in memory only. A service restart forgets them, which is honest
enough — the badge says "since this port was last powered", and a restart
means we simply have not been watching since then.
"""

from __future__ import annotations

import threading

# Above this many reconnects the badge goes red. Below it, but non-zero, it is
# orange: something moved, but not yet a pattern.
RED_AT = 6

# The badge is a circle with a numeral in it, so a second digit would break the
# shape. Counts above this are shown as this value; the tooltip carries truth.
DISPLAY_MAX = 9


class FlapCounter:
    """Enumerations per port since that port was last powered.

    `record()` is called from the udev monitor thread and `reconnects()` from
    the web thread, so the map is guarded. The lock is held only around dict
    access — never across a callback — so a noisy bus cannot stall a page load.
    """

    def __init__(self) -> None:
        self._adds: dict[tuple[str, int], int] = {}
        # Re-enumerations WE asked for. A USB-mode swap (adb <-> ssh) tears the
        # gadget down and brings it back under a different product ID, which is
        # an `add` like any other — but it is the rig doing its job, not a
        # cradle failing, and counting it would put shame on a healthy port.
        self._expected: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def record(self, location: str, port: int) -> None:
        """One `add` event landed on this port. An add we were told to expect
        is consumed instead of counted."""
        with self._lock:
            key = (location, port)
            if self._expected.get(key):
                self._expected[key] -= 1
                return
            self._adds[key] = self._adds.get(key, 0) + 1

    def expect(self, location: str, port: int, count: int = 1) -> None:
        """Announce a re-enumeration the rig is about to cause itself.

        Used by the USB-mode switch: it drops the gadget and re-adds it, which
        is indistinguishable from a cradle glitch at the event level. Only the
        code that CAUSED it knows the difference, so it has to say so."""
        with self._lock:
            key = (location, port)
            self._expected[key] = self._expected.get(key, 0) + count

    def reset(self, location: str, port: int) -> None:
        """The port's power was changed, so the tally starts over. Counting
        across a power cut would blame the cradle for something we did."""
        with self._lock:
            self._adds.pop((location, port), None)
            self._expected.pop((location, port), None)

    def reconnects(self, location: str, port: int) -> int:
        """How many times the connection has come BACK — which is one fewer
        than the number of enumerations, because docking legitimately produces
        the first one. A clean dock therefore reads 0, not 1."""
        with self._lock:
            return max(0, self._adds.get((location, port), 0) - 1)

    def snapshot(self) -> dict[str, int]:
        """{"1-3.2": n} for every port with at least one reconnect. Ports that
        are behaving are omitted rather than sent as zeros — the status payload
        is built on every refresh and most of the rig is fine."""
        with self._lock:
            items = list(self._adds.items())
        return {f"{loc}.{port}": adds - 1
                for (loc, port), adds in items if adds > 1}


# One counter for the process: the udev monitor, the power ops and the status
# builder all have to agree about the same ports.
flaps = FlapCounter()
