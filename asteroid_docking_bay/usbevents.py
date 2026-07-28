# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Event-driven USB discovery.

The rig learned about devices by polling: a status refresh every few seconds
and a slower sweep behind it. That is fine for steady state and poor at the
moments that matter — a watch docking, a watch dropping off under load, a
port cycle — where the UI can sit a whole interval behind the bus.

udev already knows within milliseconds. This subscribes to that stream and
turns it into two things: a cache bust so the next refresh is immediate, and a
named event for the states polling cannot see at all.

`udevadm monitor` is used rather than pyudev deliberately: it needs no
dependency, and it works unprivileged (verified on the rig), where reading the
netlink socket directly does not.

Nothing here drives an operation. It observes and invalidates — every decision
still runs through the normal paths, which keeps a stuck or noisy bus from
turning into a storm of actions.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time

from .usb import port_device_info
from .util import log

MONITOR_CMD = ["udevadm", "monitor", "--udev", "--property",
               "--subsystem-match=usb"]

# A device line looks like:
#   UDEV  [12345.678] add   /devices/.../usb1/1-3/1-3.2 (usb)
_EVENT_RE = re.compile(r"^UDEV\s+\[[\d.]+\]\s+(\w+)\s+(\S+)\s+\((\w+)\)")

# "1-3.2" out of a devpath; the trailing component is the device itself.
_DEVPATH_TAIL = re.compile(r"/([0-9]+-[0-9.]+)$")

# A device that appears and is never configured is the xHCI slot-exhaustion
# signature. The kernel gives it a moment, so do not judge it instantly.
_CONFIG_GRACE_S = 2.0


def parse_event(line: str) -> "dict | None":
    """One `udevadm monitor` header line -> {action, devpath, subsystem, dev}.

    `dev` is the bus path ("1-3.2") when the event is about a whole device
    rather than one of its interfaces; interface events carry a colon and are
    reported with dev=None so callers can ignore the duplicates a single
    plug generates. Pure — see tests.
    """
    m = _EVENT_RE.match(line.strip())
    if not m:
        return None
    action, devpath, subsystem = m.group(1), m.group(2), m.group(3)
    tail = _DEVPATH_TAIL.search(devpath)
    return {"action": action, "devpath": devpath, "subsystem": subsystem,
            "dev": tail.group(1) if tail else None}


def split_dev(dev: str) -> "tuple[str, int] | None":
    """"1-3.2" -> ("1-3", 2), the hub location and port this device sits on.
    Returns None for a root device ("1-3" has no parent port here). Pure."""
    if "." not in dev:
        return None
    loc, _, port = dev.rpartition(".")
    try:
        return loc, int(port)
    except ValueError:
        return None


class UsbEventMonitor:
    """Runs `udevadm monitor` and calls `on_change()` when the bus changes.

    Deliberately dumb: every device add/remove busts the cache. Debouncing is
    the caller's business, and a plug legitimately produces several events (the
    device, then each interface) which should collapse into one refresh.
    """

    def __init__(self, on_change, debounce_s: float = 0.4):
        self.on_change = on_change
        self.debounce_s = debounce_s
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._pending = threading.Event()

    def start(self) -> bool:
        """Begin monitoring. False (with a warning) if udevadm is unavailable —
        polling still covers everything, so this is a degradation, not a
        failure."""
        try:
            self._proc = subprocess.Popen(
                MONITOR_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except (OSError, ValueError) as exc:
            log.warning("udev monitoring unavailable, polling only: %s", exc)
            return False
        self._thread = threading.Thread(target=self._read, daemon=True,
                                        name="usb-udev-monitor")
        self._thread.start()
        threading.Thread(target=self._flush, daemon=True,
                         name="usb-udev-debounce").start()
        log.info("USB event monitoring active (udev)")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            if self._stop.is_set():
                return
            ev = parse_event(line)
            if not ev or not ev["dev"]:
                continue                       # interface event or a blank line
            if ev["action"] in ("add", "remove", "bind", "unbind"):
                self._pending.set()
            if ev["action"] == "add":
                threading.Thread(target=self._check_configured, args=(ev["dev"],),
                                 daemon=True).start()
        if not self._stop.is_set():
            log.warning("udev monitor exited; falling back to polling")

    def _flush(self) -> None:
        """Collapse a burst of events into one callback. A single plug emits
        the device plus one event per interface; refreshing on each would cost
        several full scans for one physical action."""
        while not self._stop.is_set():
            if self._pending.wait(timeout=1.0):
                time.sleep(self.debounce_s)
                self._pending.clear()
                try:
                    self.on_change()
                except Exception as exc:
                    log.debug("usb event callback failed: %s", exc)

    def _check_configured(self, dev: str) -> None:
        """Name the state polling cannot see.

        A device that enumerates and is never configured is what xHCI slot
        exhaustion looks like from userspace: present on the bus, unusable, and
        indistinguishable from a broken watch unless someone says so. Polling
        only ever sees "a device that does not work".
        """
        parts = split_dev(dev)
        if not parts:
            return
        time.sleep(_CONFIG_GRACE_S)
        info = port_device_info(*parts)
        if info and not info["configured"]:
            log.warning(
                "%s enumerated but was never configured (%s) — this is what "
                "the USB controller running out of device slots looks like. "
                "Free a slot by powering a port off, then cycle this one.",
                dev, info["vid"] + ":" + info["pid"])
