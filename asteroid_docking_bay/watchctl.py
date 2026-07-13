# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Watch-level control: identification waits, OS detect, Control Center."""

from __future__ import annotations

import os
import shlex
import tempfile
import time
from pathlib import Path

from .util import _run, log
from .adb import _adb_state, adb_devices, adb_shell, get_watch_codename
from .config import ChargeConfig, find_serial_for_codename, save_config


def wait_for_adb(codename: str, cfg: dict,
                 charge_cfg: ChargeConfig) -> str | None:
    """
    Poll ADB until the watch with the given codename appears as 'device'.
    Returns the serial string, or None on timeout.

    If the watch needs a physical power button press after USB power-on,
    this function will time out and log an actionable warning.
    """
    wait_secs = charge_cfg.adb_wait_seconds
    retries = charge_cfg.adb_wait_retries
    known_serial = find_serial_for_codename(cfg, codename)

    for attempt in range(1, retries + 1):
        devices = adb_devices()

        # Fast path: known serial is already online.
        if known_serial and _adb_state(devices, known_serial) == "device":
            log.debug("%s: ADB online (serial %s)", codename, known_serial)
            return known_serial

        # Slower path: scan all 'device'-state entries for the codename.
        for serial, state in devices.items():
            if state['status'] != "device":
                continue
            detected = get_watch_codename(serial)
            if detected and detected.lower() == codename.lower():
                log.info("%s: ADB online as %s", codename, serial)
                cfg.setdefault("serials", {})[serial] = codename
                save_config(cfg)
                return serial

        log.info(
            "%s: waiting for ADB (%d/%d, %ds intervals)…",
            codename, attempt, retries, wait_secs,
        )
        time.sleep(wait_secs)

    log.warning(
        "%s: ADB not available after %ds total wait.\n"
        "  If this watch requires a physical power button press after USB power-on,\n"
        "  press it now and re-run: asteroid-docking-bay charge %s",
        codename, wait_secs * retries, codename,
    )
    return None


# Cache: serial → detected OS ("asteroidos", "WearOS", or an os-release NAME).
# Entries are dropped when the watch goes offline, so a reflash is re-detected
# on its next boot.
_watch_os: dict[str, str] = {}


def detect_watch_os(serial: str) -> str:
    """Best-effort OS identification for an ADB-online watch."""
    rc, out, _ = adb_shell(serial, "cat /etc/os-release 2>/dev/null")
    if rc == 0 and out.strip():
        kv = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip('"')
        if kv.get("ID", "").lower() == "asteroid" or "asteroid" in kv.get("NAME", "").lower():
            return "asteroidos"
        if kv.get("NAME"):
            return kv["NAME"]
    # Android-based (Wear OS) watches have getprop instead of os-release.
    rc, out, _ = adb_shell(serial, "getprop ro.build.version.release")
    if rc == 0 and out.strip():
        return "WearOS"
    return "unknown"


def _watch_os_for(serial: str) -> str:
    if serial not in _watch_os:
        _watch_os[serial] = detect_watch_os(serial)
    return _watch_os[serial]


# ── Control Center: per-watch stats + hardware toggles over ADB ───────────────
# Data sources mirror asteroid-settings AboutPage.qml / QuickPanelPage.qml,
# confirmed on AsteroidOS 1.1 / kernel 3.10. adb shell runs as root. One shell
# batch keeps it to a single round-trip. See reference_watch_control_commands.
_CC_SCRIPT = r'''. /etc/os-release 2>/dev/null; echo "os=$NAME $VERSION"
echo "host=$(hostname)"
echo "kernel=$(uname -r)"
echo "qt=$(ls /usr/lib/libQt*Core.so.*.*.* 2>/dev/null | grep -o "[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*" | head -1)"
echo "uptime=$(cut -d" " -f1 /proc/uptime)"
echo "load=$(cut -d" " -f1-3 /proc/loadavg)"
echo "threads=$(cut -d" " -f4 /proc/loadavg)"
echo "memtotal=$(grep -m1 MemTotal /proc/meminfo | tr -dc 0-9)"
echo "memfree=$(grep -m1 MemFree /proc/meminfo | tr -dc 0-9)"
echo "membuffers=$(grep -m1 Buffers /proc/meminfo | tr -dc 0-9)"
echo "memcached=$(grep -m1 "^Cached" /proc/meminfo | tr -dc 0-9)"
echo "wlanmac=$(cat /sys/class/net/wlan0/address 2>/dev/null)"
echo "tz=$(readlink /etc/localtime 2>/dev/null | sed "s,.*/zoneinfo/,,")"
echo "datetime=$(date "+%Y-%m-%d %H:%M:%S")"
echo "soc=$(grep -m1 Hardware /proc/cpuinfo | cut -d: -f2)"
echo "cpufreq=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null)"
echo "cores=$(cat /sys/devices/system/cpu/online 2>/dev/null)"
echo "bootreason=$(tr " " "\n" < /proc/cmdline | grep bootreason | cut -d= -f2)"
echo "df=$(df -h / 2>/dev/null | tail -1)"
echo "bat_cap=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)"
echo "bat_status=$(cat /sys/class/power_supply/battery/status 2>/dev/null)"
echo "bat_health=$(cat /sys/class/power_supply/battery/health 2>/dev/null)"
echo "bat_tech=$(cat /sys/class/power_supply/battery/technology 2>/dev/null)"
echo "bat_volt=$(cat /sys/class/power_supply/battery/voltage_now 2>/dev/null)"
echo "bat_curr=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null)"
echo "bat_temp=$(cat /sys/class/power_supply/battery/temp 2>/dev/null)"
echo "bat_cycles=$(cat /sys/class/power_supply/battery/cycle_count 2>/dev/null)"
echo "usb_online=$(cat /sys/class/power_supply/usb/online 2>/dev/null)"
echo "usb_volt=$(cat /sys/class/power_supply/usb/voltage_now 2>/dev/null)"
echo "ip=$(ip -o -4 addr show wlan0 2>/dev/null | grep -o "inet [0-9.]*" | cut -d" " -f2)"
echo "net_rx=$(cat /sys/class/net/wlan0/statistics/rx_bytes 2>/dev/null)"
echo "net_tx=$(cat /sys/class/net/wlan0/statistics/tx_bytes 2>/dev/null)"
echo "btcount=$(hcitool con 2>/dev/null | grep -c "<")"
echo "btmac=$(hcitool con 2>/dev/null | grep -o "[0-9A-F:]\{17\}" | head -1)"
echo "blank_inhibit=$(mcetool 2>/dev/null | grep "^Blank inhibit" | cut -d: -f2 | tr -d " ")"
echo "--connman--"
connmanctl technologies 2>/dev/null'''

# UI-session tools (screenshots, notifications) talk to the Wayland compositor
# and the user's D-Bus session, which live under the `ceres` account — adb shell
# is root and can't reach them, so Watch.user_cmd runs them via `su ceres` with
# the session env.
_CERES_ENV = ("XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 "
              "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus")


def _host_timezone() -> "str | None":
    """Host IANA timezone (e.g. 'Europe/Berlin') to push to a watch."""
    try:
        tz = os.readlink("/etc/localtime")
    except OSError:
        return None
    return tz.split("zoneinfo/", 1)[1] if "zoneinfo/" in tz else None


class Watch:
    """Everything done *to* one specific watch over ADB, bound to its serial:
    the Control Center data batch, hardware toggles, clock sync, and the
    ceres-session actions (screenshot, notification) that must not run as
    root because they talk to the user's Wayland compositor / session D-Bus."""

    def __init__(self, serial: str):
        self.serial = serial

    def cc_data(self) -> dict:
        """Read About-style stats + connman toggle states in one adb batch.
        Returns {} if unreachable."""
        rc, out, _ = _run(f"adb -s {self.serial} shell {shlex.quote(_CC_SCRIPT)}",
                          check=False, timeout=12)
        if rc != 0 or not out.strip():
            return {}
        info: dict = {}
        in_conn = False
        ctype = None
        tech: dict = {}
        for line in out.splitlines():
            if line.strip() == "--connman--":
                in_conn = True
                continue
            if not in_conn:
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            else:
                s = line.strip()
                if s.startswith("Type ="):
                    ctype = s.split("=", 1)[1].strip()
                elif s.startswith("Powered =") and ctype:
                    tech[ctype] = (s.split("=", 1)[1].strip().lower() == "true")
        info["serial"] = self.serial
        info["wifi"] = tech.get("wifi")
        info["bluetooth"] = tech.get("bluetooth")
        # mce demo mode (mcetool -D on) forces the screen on and drains the
        # watch. Blank inhibit is 'disabled' normally, 'stay-on' when forced;
        # empty on watches without mce. Surface it as the Screen toggle's state.
        bi = info.get("blank_inhibit", "").lower()
        info["screen_forced"] = bool(bi) and bi != "disabled"
        return info

    def toggle(self, tech: str, on: bool) -> bool:
        """Enable/disable a connman technology (wifi|bluetooth)."""
        action = "enable" if on else "disable"
        rc, _, err = _run(f"adb -s {self.serial} shell connmanctl {action} {tech}",
                          check=False, timeout=12)
        if rc != 0:
            log.warning("toggle %s %s on %s failed: %s", action, tech,
                        self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def set_time_from_host(self) -> "str | None":
        """Sync the watch's clock + timezone from the host: `date -s @epoch`
        then `timedatectl set-timezone`. Returns the tz applied, or None."""
        epoch = int(time.time())
        _run(f"adb -s {self.serial} shell date -s @{epoch}", check=False,
             timeout=10)
        tz = _host_timezone()
        if tz:
            _run(f"adb -s {self.serial} shell timedatectl set-timezone "
                 f"{shlex.quote(tz)}", check=False, timeout=10)
        log.info("%s: synced time from host (epoch=%d tz=%s)",
                 self.serial, epoch, tz)
        return tz

    def user_cmd(self, cmd: str, timeout: int = 15) -> tuple[int, str, str]:
        """Run a command in the watch's ceres user session (not as root)."""
        inner = "su ceres -c " + shlex.quote(_CERES_ENV + " " + cmd)
        return _run(f"adb -s {self.serial} shell {shlex.quote(inner)}",
                    check=False, timeout=timeout)

    def notify(self) -> bool:
        """Send a test notification."""
        cmd = ('notificationtool -o add --application=docking-bay --urgency=2 '
               '--icon=ios-happy '
               '--hint="x-nemo-preview-summary asteroid-docking-bay" '
               '--hint="x-nemo-preview-body test notification from the host" '
               '"docking-bay" "ping"')
        rc, _, err = self.user_cmd(cmd, timeout=12)
        if rc != 0:
            log.warning("notify %s failed: %s", self.serial, err.strip())
        return rc == 0

    def screenshot(self) -> "Path | None":
        """Capture the screen and pull it locally. Returns the Path or None.
        screenshottool exits 10 even on success, so judge by the pulled file."""
        remote = "/home/ceres/.dockingbay_ss.jpg"
        self.user_cmd(f"screenshottool {remote} 0", timeout=15)
        local = Path(tempfile.gettempdir()) / f"dockingbay_ss_{self.serial}.jpg"
        rc, _, _ = _run(f"adb -s {self.serial} pull {remote} "
                        f"{shlex.quote(str(local))}", check=False, timeout=15)
        _run(f"adb -s {self.serial} shell rm -f {remote}", check=False, timeout=8)
        return local if (rc == 0 and local.exists()
                         and local.stat().st_size > 0) else None

    def buzz(self, ms: int = 300) -> bool:
        """Vibrate briefly — locate/identify the watch in a full dock."""
        rc, _, _ = _run(f'adb -s {self.serial} shell "echo {ms} > '
                        f'/sys/class/timed_output/vibrator/enable"',
                        check=False, timeout=8)
        return rc == 0

    def screen(self, on: bool) -> bool:
        """Force the screen on (mce demo mode) or release it."""
        rc, _, _ = _run(f"adb -s {self.serial} shell mcetool -D "
                        f"{'on' if on else 'off'}", check=False, timeout=10)
        return rc == 0
