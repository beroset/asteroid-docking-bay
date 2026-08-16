# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Run commands and move files to one watch, over ADB or SSH.

A watch is normally reached over ADB by serial. Switched to SSH/developer USB
mode it is reached over SSH instead — at 192.168.2.15 for a USB-SSH link, or
its WiFi address for a WiFi-SSH link (the two can be active at once). A
Transport hides which, so the same `shell`/`pull`/`push` works either way.

The command quoting is deliberately the same for both back ends: `adb -s S
shell X` and `ssh root@IP X` each forward the already-quoted remote command X
to a shell on the watch, so callers build X exactly as they do today and only
the prefix differs.
"""

from __future__ import annotations

from .util import _run

# USB-SSH: a single watch in developer_mode is reachable here (fixed /24).
USB_SSH_IP = "192.168.2.15"
# Non-interactive, don't pollute/consult known_hosts (a flash rotates the key).
_SSH_OPTS = ("-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
             "-o ConnectTimeout=8 -o BatchMode=yes")


class Transport:
    """One watch's command/file channel. `kind` labels it for the UI/logs."""

    kind = "?"

    def shell(self, cmd: str, timeout: int = 8,
              check: bool = False) -> "tuple[int, str, str]":
        raise NotImplementedError

    def pull(self, remote: str, local: str,
             timeout: int = 15) -> "tuple[int, str, str]":
        raise NotImplementedError

    def push(self, local: str, remote: str,
             timeout: int = 15) -> "tuple[int, str, str]":
        raise NotImplementedError


class AdbTransport(Transport):
    kind = "adb"

    def __init__(self, serial: str):
        self.serial = serial

    def shell(self, cmd, timeout=8, check=False):
        return _run(f"adb -s {self.serial} shell {cmd}", check=check, timeout=timeout)

    def pull(self, remote, local, timeout=15):
        return _run(f"adb -s {self.serial} pull {remote} {local}",
                    check=False, timeout=timeout)

    def push(self, local, remote, timeout=15):
        return _run(f"adb -s {self.serial} push {local} {remote}",
                    check=False, timeout=timeout)


class SshTransport(Transport):
    """SSH to a watch, over USB-RNDIS, WiFi, or a USB-NCM link.

    `user` and IPv6 exist for the NCM watches. Their login is `ceres`, not
    root: it is sufficient for every read a-d-b makes and REQUIRED for
    screenshots, because the wayland socket is ceres:ceres inside
    /run/user/1000 at mode 0700 -- root can traverse that but must set
    XDG_RUNTIME_DIR and WAYLAND_DISPLAY by hand and may still hit
    session-scoped auth.

    An NCM watch is addressed by its IPv6 LINK-LOCAL with a scope, e.g.
    fe80::ba:45ff:fe0b:aeb8%enp0s20u3u4u3i1. That needs no addressing on either
    side and cannot collide, because the scope names the interface. Note the
    address takes NO brackets here: brackets are URL syntax (scp, curl), and
    ssh rejects them -- verified on the rig, where the bracketed form failed
    with "Could not resolve hostname".
    """

    def __init__(self, ip: str = USB_SSH_IP, over: str = "usb",
                 user: str = "root"):
        self.ip = ip
        self.over = over
        self.user = user
        self.kind = f"ssh ({over})"

    @property
    def _target(self) -> str:
        return f"{self.user}@{self.ip}"

    @property
    def _family(self) -> str:
        # Force v6 for a link-local: with a scope there is nothing to guess,
        # and it keeps a v4 fallback from silently changing which watch answers.
        return " -6" if ":" in self.ip else ""

    def shell(self, cmd, timeout=8, check=False):
        return _run(f"ssh{self._family} {_SSH_OPTS} {self._target} {cmd}",
                    check=check, timeout=timeout)

    def pull(self, remote, local, timeout=15):
        # -r so a directory (backup: .config, connman) copies like `adb pull`.
        # scp DOES want brackets around a v6 address, unlike ssh.
        if ":" in self.ip:
            return _run(f"scp -6 {_SSH_OPTS} -r {self.user}@[{self.ip}]:{remote} {local}",
                        check=False, timeout=timeout)
        return _run(f"scp {_SSH_OPTS} -r root@{self.ip}:{remote} {local}",
                    check=False, timeout=timeout)

    def push(self, local, remote, timeout=15):
        if ":" in self.ip:
            return _run(f"scp -6 {_SSH_OPTS} -r {local} {self.user}@[{self.ip}]:{remote}",
                        check=False, timeout=timeout)
        return _run(f"scp {_SSH_OPTS} -r {local} root@{self.ip}:{remote}",
                    check=False, timeout=timeout)
