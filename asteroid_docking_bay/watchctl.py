# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Watch-level control: identification waits, OS detect, Control Center."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path

from .util import _run, log
from .adb import _adb_state, adb_devices, adb_shell, get_watch_codename
from .transport import AdbTransport
from .config import ChargeConfig, find_serial_for_codename, save_config
from .watch_settings import (QUICKPANEL_KEY, dconf_arg, effective_settings,
                             quickpanel_ids, quickpanel_state,
                             quickpanel_write_arg, writable)


def wait_for_adb(codename: str, cfg: dict,
                 charge_cfg: ChargeConfig,
                 serial: "str | None" = None) -> str | None:
    """
    Poll ADB until the target watch appears as 'device'. Returns its serial,
    or None on timeout.

    When `serial` is given, wait for exactly that unit — this disambiguates
    two watches that share a codename (flashing the wrong one is the danger).
    Without it, match by codename.

    If the watch needs a physical power button press after USB power-on,
    this function will time out and log an actionable warning.
    """
    wait_secs = charge_cfg.adb_wait_seconds
    retries = charge_cfg.adb_wait_retries
    known_serial = serial or find_serial_for_codename(cfg, codename)

    for attempt in range(1, retries + 1):
        devices = adb_devices()

        # Fast path: the exact serial is already online.
        if known_serial and _adb_state(devices, known_serial) == "device":
            log.debug("%s: ADB online (serial %s)", codename, known_serial)
            return known_serial

        # Codename scan only when no exact serial was requested — otherwise we
        # could return the wrong unit of a duplicated codename.
        if serial is None:
            for s, state in devices.items():
                if state['status'] != "device":
                    continue
                detected = get_watch_codename(s)
                if detected and detected.lower() == codename.lower():
                    log.info("%s: ADB online as %s", codename, s)
                    cfg.setdefault("serials", {})[s] = codename
                    save_config(cfg)
                    return s

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
echo "btmac_self=$(cat /sys/class/bluetooth/hci0/address 2>/dev/null)"
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

# Data backup: user settings (incl. the dconf database) + system WiFi
# credentials, one non-incremental dir per watch on the host. This is NOT a
# full-image backup — see the deferred "Dump mmcblk0" feature for a byte-exact
# eMMC image. (label, remote-path); the local copy keeps the remote basename.
BACKUP_ROOT = Path.home() / ".local/share/asteroid-docking-bay/backups"
_BACKUP_PATHS = (
    ("settings", "/home/ceres/.config"),   # app settings + dconf db (ceres-owned)
    ("wifi",     "/var/lib/connman"),       # connman WiFi credentials (root-owned)
)

# Diagnostics bundle: read-only state useful for a bug report. Each entry is a
# filename and a device-side (root) shell command; the command is quoted whole
# so its pipes/globs run on the watch, not the host. Keep commands free of
# double quotes (they'd break the wrapper). journalctl is tail-capped.
DIAG_ROOT = Path.home() / ".local/share/asteroid-docking-bay/diagnostics"
_DIAG_CMDS = {
    "os-release.txt": "cat /etc/os-release /etc/asteroid-release 2>/dev/null",
    "journal.txt":    "journalctl -b --no-pager 2>/dev/null | tail -n 5000",
    "dmesg.txt":      "dmesg 2>/dev/null",
    "battery.txt":    "cat /sys/class/power_supply/*/uevent 2>/dev/null",
    # grep -H, not a $(...) loop: command substitution inside the double-quoted
    # wrapper would be expanded by the host shell, not the watch.
    "thermal.txt":    ("grep -H . /sys/class/thermal/thermal_zone*/type "
                       "/sys/class/thermal/thermal_zone*/temp 2>/dev/null"),
    "df.txt":         "df -h 2>/dev/null",
    "connman.txt":    "connmanctl technologies 2>/dev/null; connmanctl services 2>/dev/null",
}


def _host_timezone() -> "str | None":
    """Host IANA timezone (e.g. 'Europe/Berlin') to push to a watch."""
    try:
        tz = os.readlink("/etc/localtime")
    except OSError:
        return None
    return tz.split("zoneinfo/", 1)[1] if "zoneinfo/" in tz else None


# Bump whenever geometry() starts collecting a new field. Cached probes are
# stamped with this by the caching layer, so a cache written before a field
# existed can be told apart from a complete one and re-probed — otherwise a
# watch cached earlier keeps serving a result that silently lacks it forever.
GEOMETRY_PROBE_VERSION = 2   # 2 added androidboot.bootloader


class Watch:
    """Everything done *to* one specific watch over ADB, bound to its serial:
    the Control Center data batch, hardware toggles, clock sync, and the
    ceres-session actions (screenshot, notification) that must not run as
    root because they talk to the user's Wayland compositor / session D-Bus."""

    def __init__(self, serial: str, transport=None):
        self.serial = serial
        # Reach the watch over ADB by default; a caller can pass an SshTransport
        # for a watch in SSH/developer mode. Every method issues the same
        # command through it, so all features work over either link.
        self.t = transport or AdbTransport(serial)

    def cc_data(self) -> dict:
        """Read About-style stats + connman toggle states in one batch.
        Returns {} if unreachable."""
        rc, out, _ = self.t.shell(shlex.quote(_CC_SCRIPT), timeout=12)
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

    def geometry(self) -> dict:
        """Screen shape + resolution, for masking screenshots and showing the
        real resolution in the Control Center.

        Shape comes from /etc/asteroid/machine.conf — the same source
        qml-asteroid's DeviceSpecs reads (Display/ROUND, Display/FLAT_TIRE),
        so a freshly-ported watch is handled without any per-codename table.
        Resolution comes from /sys/class/graphics/fb0/modes ('U:360x360p-...');
        fb0/virtual_size is double-buffered (height doubled), so it is NOT a
        reliable panel size. Returns {} when nothing could be read."""
        rc, conf, _ = self.t.shell("cat /etc/asteroid/machine.conf", timeout=10)
        geo: dict = {}
        if rc == 0 and conf.strip():
            vals: dict[str, str] = {}
            for line in conf.splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("["):
                    k, v = line.split("=", 1)
                    vals[k.strip().upper()] = v.strip()
            geo["round"] = vals.get("ROUND", "").lower() == "true"
            flat = vals.get("FLAT_TIRE", "0")
            geo["flat_tire"] = int(flat) if flat.isdigit() else 0
            if vals.get("MACHINE"):
                geo["machine"] = vals["MACHINE"]
        rc2, modes, _ = self.t.shell("cat /sys/class/graphics/fb0/modes", timeout=8)
        if rc2 == 0:
            m = re.search(r"(\d+)x(\d+)", modes)
            if m:
                geo["width"], geo["height"] = int(m.group(1)), int(m.group(2))
                geo["resolution"] = f"{m.group(1)}x{m.group(2)}"
        # androidboot.bootloader carries the bootloader's own version string,
        # which begins with the TRUE hardware codename even when the watch runs
        # a sibling's image — rover reports bootloader=rover-... while its
        # MACHINE, resolution and image all say rubyfish. It comes from the
        # bootloader rather than the rootfs, so a shared image cannot mask it.
        rc3, cmdline, _ = self.t.shell("cat /proc/cmdline", timeout=8)
        if rc3 == 0:
            m = re.search(r"androidboot\.bootloader=(\S+)", cmdline)
            if m:
                geo["bootloader"] = m.group(1)
        return geo

    def toggle(self, tech: str, on: bool) -> bool:
        """Enable/disable a connman technology (wifi|bluetooth)."""
        action = "enable" if on else "disable"
        rc, _, err = self.t.shell(f"connmanctl {action} {tech}", timeout=12)
        if rc != 0:
            log.warning("toggle %s %s on %s failed: %s", action, tech,
                        self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def set_time_from_host(self) -> "str | None":
        """Sync the watch's clock + timezone from the host: `date -s @epoch`
        then `timedatectl set-timezone`. Returns the tz applied, or None."""
        epoch = int(time.time())
        self.t.shell(f"date -s @{epoch}", timeout=10)
        tz = _host_timezone()
        if tz:
            self.t.shell(f"timedatectl set-timezone {shlex.quote(tz)}", timeout=10)
        log.info("%s: synced time from host (epoch=%d tz=%s)",
                 self.serial, epoch, tz)
        return tz

    def set_datetime(self, when: str) -> bool:
        """Set the watch clock to an explicit 'YYYY-MM-DD HH:MM:SS' — for the
        arbitrary-time screenshots devs want. Like set_time_from_host but with a
        dialled-in moment instead of the host's; reversible via Sync-from-host.
        The caller validates the format (no quotes/metacharacters). The WHOLE
        command is passed as one token so the space in the datetime survives
        adb/ssh's argument re-split — quoting only the argument (shlex.quote)
        was consumed host-side, adb re-joined and the watch re-split, leaving
        the time as a stray operand and setting the wrong time (audit C1)."""
        rc, _, err = self.t.shell(f"\"date -s '{when}'\"", timeout=10)
        if rc != 0:
            log.warning("set_datetime %s on %s failed: %s",
                        when, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def user_cmd(self, cmd: str, timeout: int = 15) -> tuple[int, str, str]:
        """Run a command in the watch's ceres user session (not as root)."""
        inner = "su ceres -c " + shlex.quote(_CERES_ENV + " " + cmd)
        return self.t.shell(shlex.quote(inner), timeout=timeout)

    def settings_read(self) -> "dict | None":
        """The mirrored settings with their current values, read with one dconf
        dump in the ceres session (the call backup() already uses): the catalog
        rows plus the quick-panel toggle states. Unset keys fall back to their
        baked defaults; None when the watch is unreachable."""
        rc, out, _ = self.user_cmd("HOME=/home/ceres dconf dump /", timeout=15)
        if rc != 0:
            return None
        return {"settings": effective_settings(out),
                "quickpanel": quickpanel_state(out)}

    def quickpanel_set(self, tid: str, on: bool) -> bool:
        """Enable/disable one quick-panel toggle. dconf stores the whole set as a
        single dict, so read the current states, flip this id, and write the full
        dict back. Refuses any id not in the catalog."""
        if tid not in quickpanel_ids():
            log.warning("quickpanel_set refused unknown toggle %s", tid)
            return False
        rc, out, _ = self.user_cmd("HOME=/home/ceres dconf dump /", timeout=15)
        if rc != 0:
            return False
        states = {r["id"]: r["enabled"] for r in quickpanel_state(out)}
        states[tid] = bool(on)
        arg = quickpanel_write_arg(states)
        rc, _, err = self.user_cmd(
            f"HOME=/home/ceres dconf write {shlex.quote(QUICKPANEL_KEY)} {shlex.quote(arg)}",
            timeout=12)
        if rc != 0:
            log.warning("quickpanel_set %s on %s failed: %s",
                        tid, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def hands(self) -> "dict | None":
        """Where a physical-hands watch's hands actually point, HH:MM, read from
        the soprod-movement sysfs (narwhal: /sys/devices/sop716/position). None
        on a watch with no such movement — the sysfs is simply absent."""
        rc, out, _ = self.t.shell('"cat /sys/devices/sop716/position 2>/dev/null"',
                                   timeout=8)
        if rc != 0:
            return None
        m = re.match(r"^\s*(\d+):(\d+)\s*$", out)
        if not m:
            return None
        return {"position": f"{int(m.group(1))}:{int(m.group(2))}",
                "h": int(m.group(1)), "m": int(m.group(2))}

    def set_hands(self, when: str) -> bool:
        """Move a hands watch's physical hands to a datetime by writing it to
        /sys/devices/sop716/time — the movement driver follows that file (narwhal;
        dodoradio's hands-timesync writes the same 'YYYY-MM-DD HH:MM:SS'). Used to
        correct drift (write now) or pose the hands for a shot. Caller validates."""
        rc, _, err = self.t.shell(f"\"echo '{when}' > /sys/devices/sop716/time\"",
                                  timeout=8)
        if rc != 0:
            log.warning("set_hands %s on %s failed: %s",
                        when, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def move_hands(self, minute: int, hour: int) -> bool:
        """Drive the physical hands to ABSOLUTE motor positions (narwhal): write
        'minute:hour' to /sys/devices/sop716/motor_move_all, each 0..179 (180 =
        one full turn, 2 deg/step). Absolute and re-syncs sop716/position — the
        primitive under Free-mode drag and the choreography. Decoded on hardware
        2026-07-23; the movement driver + convention are dodoradio's. Caller
        validates the range."""
        rc, _, err = self.t.shell(
            f'"echo {int(minute)}:{int(hour)} > /sys/devices/sop716/motor_move_all"',
            timeout=8)
        if rc != 0:
            log.warning("move_hands %s:%s on %s failed: %s",
                        minute, hour, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def weather_sync(self, writeset) -> bool:
        """Write a weather dconf set (from weather.dconf_writeset) to the watch,
        one key at a time in the ceres session — the same proven path the
        settings toggles use. Strings become single-quoted gvariant literals,
        ints are written bare. So the on-watch weather app + Today screen show it."""
        ok = True
        for key, typ, val in writeset:
            gv = ("'" + str(val).replace("'", "") + "'") if typ == "string" else str(int(val))
            rc, _, err = self.user_cmd(
                f"HOME=/home/ceres dconf write {shlex.quote(key)} {shlex.quote(gv)}",
                timeout=10)
            if rc != 0:
                log.warning("weather_sync %s on %s failed: %s",
                            key, self.serial, err.strip() or f"rc={rc}")
                ok = False
        return ok

    def weather_read(self):
        """The raw weather dconf dump currently stored on the watch (ceres
        session); None if unreadable. The caller parses it with
        weather.parse_watch_weather — so the UI can show on-watch vs incoming."""
        rc, out, _ = self.user_cmd(
            "HOME=/home/ceres dconf dump /org/asteroidos/weather/", timeout=12)
        return out if rc == 0 else None

    def settings_write(self, key: str, value) -> bool:
        """Write one togglable setting over dconf in the ceres session (same env
        the read uses). Refuses any key not in the writable catalog — display-
        only 'path' rows and anything off-catalog can't be reached here."""
        s = writable(key)
        if s is None:
            log.warning("settings_write refused non-writable key %s", key)
            return False
        cmd = f"HOME=/home/ceres dconf write {shlex.quote(key)} {dconf_arg(s, value)}"
        rc, _, err = self.user_cmd(cmd, timeout=12)
        if rc != 0:
            log.warning("settings_write %s on %s failed: %s",
                        key, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def av_read(self) -> dict:
        """Display brightness + sound volume/mute + which of speaker/mic the watch
        has. Brightness is MCE (mcetool, root); volume/mute are PulseAudio (pactl,
        ceres); HAS_SPEAKER/HAS_MIC are the machine.conf capabilities DeviceSpecs
        reads. Volume/mute are only meaningful — and only read — with a speaker
        (some watches have a mic but no speaker). Any field is None when unreadable."""
        rc, out, _ = self.t.shell(
            "\"mcetool 2>/dev/null | grep -i '^Brightness'; echo ---CAP---; "
            "grep -iE 'HAS_SPEAKER|HAS_MIC' /etc/asteroid/machine.conf 2>/dev/null\"",
            timeout=10)
        head, _, cap = out.partition("---CAP---")
        caps = {}
        for ln in cap.splitlines():
            if "=" in ln:
                k, _, v = ln.partition("=")
                caps[k.strip().upper()] = "true" in v.lower()
        bm = re.search(r"(\d+)", head)
        av = {"brightness": int(bm.group(1)) if bm else None,
              "has_speaker": caps.get("HAS_SPEAKER", False),
              "has_mic": caps.get("HAS_MIC", False),
              "volume": None, "muted": None}
        if av["has_speaker"]:
            _, vout, _ = self.user_cmd(
                "pactl get-sink-volume @DEFAULT_SINK@; echo ---; "
                "pactl get-sink-mute @DEFAULT_SINK@", timeout=10)
            vm = re.search(r"(\d+)%", vout)
            av["volume"] = int(vm.group(1)) if vm else None
            av["muted"] = "mute: yes" in vout.lower()
        return av

    def set_brightness(self, pct: int) -> bool:
        """Set display brightness 1..100 via mcetool (MCE, root). Caller clamps."""
        rc, _, err = self.t.shell(
            f'"mcetool --set-display-brightness={int(pct)}"', timeout=8)
        if rc != 0:
            log.warning("set_brightness %s on %s failed: %s",
                        pct, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def set_volume(self, pct: int) -> bool:
        """Set the master sink volume 0..100%% via pactl (ceres session)."""
        rc, _, err = self.user_cmd(
            f"pactl set-sink-volume @DEFAULT_SINK@ {int(pct)}%", timeout=10)
        if rc != 0:
            log.warning("set_volume %s on %s failed: %s",
                        pct, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def set_mute(self, on: bool) -> bool:
        """Mute/unmute the master sink via pactl (ceres session)."""
        rc, _, err = self.user_cmd(
            f"pactl set-sink-mute @DEFAULT_SINK@ {1 if on else 0}", timeout=10)
        if rc != 0:
            log.warning("set_mute %s on %s failed: %s",
                        on, self.serial, err.strip() or f"rc={rc}")
        return rc == 0

    def standby_features(self) -> dict:
        """The standby power consumers ON right now — WiFi, Bluetooth, always-on
        display — captured at drain-start so a run self-documents its config for
        per-feature drain attribution. WiFi/BT come from connmanctl (same source
        the CC reads); AoD from its dconf key (default-on, so empty == on). Any
        field is None when unreadable."""
        feats = {"wifi": None, "bt": None, "aod": None}
        _, out, _ = self.t.shell('"connmanctl technologies 2>/dev/null"', timeout=10)
        ctype = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Type ="):
                ctype = s.split("=", 1)[1].strip()
            elif s.startswith("Powered =") and ctype:
                on = s.split("=", 1)[1].strip().lower() == "true"
                if ctype == "wifi":
                    feats["wifi"] = on
                elif ctype == "bluetooth":
                    feats["bt"] = on
        rc, aod, _ = self.user_cmd(
            "HOME=/home/ceres dconf read /org/asteroidos/settings/always-on-display",
            timeout=10)
        # Only trust a successful read: dconf returns rc 0 with empty output for
        # an unset key (== default ON), but a failed read (unreachable watch, su
        # error) also yields empty — and reporting that as "on" mislabels the
        # per-feature drain attribution (audit C2). Leave it None on failure.
        if rc == 0:
            feats["aod"] = aod.strip().lower() != "false"
        return feats

    def last_recording_path(self) -> Path:
        """Stable local path the last pulled mic recording sits at (per serial)."""
        return Path(tempfile.gettempdir()) / f"dockingbay_rec_{self.serial}.wav"

    def record_audio(self, seconds: int = 5) -> "Path | None":
        """Record `seconds` of mic audio on the watch via GStreamer (ceres/
        PulseAudio pulsesrc → wav), then pull it locally like a screenshot.
        Returns the Path or None. Gated on HAS_MIC upstream; the pipeline is
        proven on-device. `timeout -s INT` + gst `-e` finalises the WAV cleanly."""
        seconds = max(1, min(30, int(seconds)))
        remote = "/tmp/dockingbay_rec.wav"
        # Kill any orphaned recorder from a prior interrupted run first, and
        # again in the finally — an orphaned gst-launch holds pulsesrc and the
        # CPU, blocking SoC suspend and draining the watch invisibly (audit B8).
        # `timeout -s INT` + gst `-e` is the clean path; the pkills are the net
        # for a stuck Pulse preroll, a busybox `timeout` that ignores -s, or a
        # host-side interruption that never lets gst's own timeout fire.
        try:
            self.user_cmd(
                f"pkill -f gst-launch-1.0 2>/dev/null; rm -f {remote}; "
                f"timeout -s INT {seconds} gst-launch-1.0 -e pulsesrc ! "
                f"audioconvert ! audioresample ! wavenc ! filesink location={remote}",
                timeout=seconds + 15)
        finally:
            try:
                self.user_cmd("pkill -f gst-launch-1.0 2>/dev/null; true", timeout=8)
            except Exception:
                pass
        local = self.last_recording_path()
        rc, _, _ = self.t.pull(remote, shlex.quote(str(local)), timeout=20)
        self.t.shell(f"rm -f {remote}", timeout=8)
        return local if (rc == 0 and local.exists()
                         and local.stat().st_size > 44) else None   # >WAV header = has data

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

    def play_notification(self) -> bool:
        """Fire the standard notification feedback so the user HEARS the volume
        just set: a transient preview notification triggers ngfd's `notification`
        event (/usr/share/sounds/notification.wav on the notification stream), the
        system's own sound at the level we just set. mo's bonus on the volume
        slider; ngfd is the sanctioned path (no paplay on the watch)."""
        rc, _, _ = self.user_cmd(
            'notificationtool -o add --application=docking-bay --urgency=2 '
            '--hint="x-nemo-preview-body volume test tone" '
            '"docking-bay" "volume"', timeout=10)
        return rc == 0

    def last_screenshot_path(self) -> Path:
        """Stable local path the last pulled screenshot sits at. It persists
        between captures, so it doubles as the stale fallback when a fresh
        grab fails (watch offline). May not exist yet."""
        return Path(tempfile.gettempdir()) / f"dockingbay_ss_{self.serial}.jpg"

    def screenshot(self) -> "Path | None":
        """Capture the screen and pull it locally. Returns the Path or None.
        screenshottool exits 10 even on success, so judge by the pulled file."""
        remote = "/home/ceres/.dockingbay_ss.jpg"
        self.user_cmd(f"screenshottool {remote} 0", timeout=15)
        local = self.last_screenshot_path()
        rc, _, _ = self.t.pull(remote, shlex.quote(str(local)), timeout=15)
        self.t.shell(f"rm -f {remote}", timeout=8)
        return local if (rc == 0 and local.exists()
                         and local.stat().st_size > 0) else None

    def buzz(self, ms: int = 300) -> bool:
        """Vibrate briefly — locate/identify the watch in a full dock."""
        rc, _, _ = self.t.shell(f'"echo {ms} > '
                                f'/sys/class/timed_output/vibrator/enable"', timeout=8)
        return rc == 0

    def screen(self, on: bool) -> bool:
        """Force the screen on (mce demo mode) or release it."""
        rc, _, _ = self.t.shell(f"mcetool -D {'on' if on else 'off'}", timeout=10)
        return rc == 0

    def _backup_dir(self) -> Path:
        codename = get_watch_codename(self.serial) or self.serial
        return BACKUP_ROOT / codename

    def backup(self) -> dict:
        """Pull user data to a per-watch dir on the host: ~ceres/.config,
        /var/lib/connman (WiFi credentials), and a portable `dconf dump`.
        Non-incremental (overwrites the watch's prior backup). Reversible via
        restore(). Not a full image — see the Dump mmcblk0 feature."""
        dest = self._backup_dir()
        dest.mkdir(parents=True, exist_ok=True)
        items: list[dict] = []
        for label, remote in _BACKUP_PATHS:
            local = dest / Path(remote).name
            _run(f"rm -rf {shlex.quote(str(local))}", check=False)   # host-side clean
            rc, _, err = self.t.pull(remote, shlex.quote(str(dest)), timeout=120)
            ok = rc == 0 and local.exists()
            items.append({"name": label, "ok": ok,
                          "error": None if ok else (err.strip() or f"rc={rc}")})
        # Portable dconf export alongside the raw db — survives a gvdb format
        # change on restore. HOME must be set: `su ceres` doesn't guarantee it,
        # and dconf keys its database off it.
        rc, out, _ = self.user_cmd("HOME=/home/ceres dconf dump /", timeout=15)
        dconf_ok = rc == 0 and bool(out.strip())
        if dconf_ok:
            (dest / "dconf.dump").write_text(out)
        items.append({"name": "dconf", "ok": dconf_ok, "error": None})
        ok = all(i["ok"] for i in items)
        log.info("%s: backup -> %s (%s)", self.serial, dest,
                 "ok" if ok else "partial")
        return {"ok": ok, "path": str(dest), "items": items}

    def restore(self) -> dict:
        """Push a previous backup() back and reconnect: ~ceres/.config
        (re-owned to ceres — adb push writes as root), /var/lib/connman, a
        `dconf load`, then restart connman so a saved WiFi network reconnects.
        Errors if this watch has no backup."""
        src = self._backup_dir()
        if not src.is_dir():
            return {"ok": False, "error": f"no backup at {src}"}
        items: list[dict] = []
        for label, remote in _BACKUP_PATHS:
            local = src / Path(remote).name
            if not local.is_dir():
                items.append({"name": label, "ok": False,
                              "error": "not in this backup"})
                continue
            # Push the dir into the remote *parent*; adb merges into the
            # existing target rather than nesting a duplicate.
            parent = str(Path(remote).parent)
            rc, _, err = self.t.push(shlex.quote(str(local)), parent, timeout=120)
            if rc == 0 and remote.startswith("/home/ceres/"):
                self.t.shell(f"chown -R ceres:ceres {remote}", timeout=15)
            items.append({"name": label, "ok": rc == 0,
                          "error": None if rc == 0 else (err.strip() or f"rc={rc}")})
        dump = src / "dconf.dump"
        if dump.is_file():
            remote = "/tmp/.dockingbay_dconf.dump"
            self.t.push(shlex.quote(str(dump)), remote, timeout=15)
            rc, _, _ = self.user_cmd(
                f"HOME=/home/ceres dconf load / < {remote}", timeout=15)
            self.t.shell(f"rm -f {remote}", timeout=8)
            items.append({"name": "dconf", "ok": rc == 0, "error": None})
        # Reconnect WiFi from the restored credentials without a reboot.
        self.t.shell("systemctl restart connman", timeout=15)
        ok = bool(items) and all(i["ok"] for i in items)
        log.info("%s: restore <- %s (%s)", self.serial, src,
                 "ok" if ok else "partial")
        return {"ok": ok, "path": str(src), "items": items}

    def collect_diagnostics(self) -> dict:
        """Gather read-only device state (logs, battery, thermal, storage,
        connman, dconf) into a per-watch .tar.gz on the host for bug reports.
        Read-only on the watch. Returns {ok, path, items}."""
        codename = get_watch_codename(self.serial) or self.serial
        ts = time.strftime("%Y%m%d-%H%M%S")
        workdir = DIAG_ROOT / f"{codename}-{ts}"
        workdir.mkdir(parents=True, exist_ok=True)
        items: list[dict] = []
        for fname, cmd in _DIAG_CMDS.items():
            # Quote the whole command so its pipes/globs run on the watch, not
            # the host shell (_run uses shell=True).
            rc, out, _ = self.t.shell(f'"{cmd}"', timeout=30)
            (workdir / fname).write_text(out)
            items.append({"name": fname, "ok": rc == 0 and bool(out.strip())})
        rc, out, _ = self.user_cmd("HOME=/home/ceres dconf dump /", timeout=15)
        (workdir / "dconf.txt").write_text(out if rc == 0 else "")
        items.append({"name": "dconf.txt", "ok": rc == 0})
        archive = shutil.make_archive(str(workdir), "gztar",
                                      root_dir=str(DIAG_ROOT),
                                      base_dir=f"{codename}-{ts}")
        shutil.rmtree(workdir, ignore_errors=True)
        ok = all(i["ok"] for i in items)
        log.info("%s: diagnostics -> %s (%s)", self.serial, archive,
                 "ok" if ok else "partial")
        return {"ok": ok, "path": archive, "items": items}
