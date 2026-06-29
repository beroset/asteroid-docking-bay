# asteroid-docking-bay

USB hub power manager for [AsteroidOS](https://asteroidos.org) smartwatches.

Manages per-port power switching on smart USB hubs (via
[uhubctl](https://github.com/mvp/uhubctl)) to keep watch batteries in a
healthy mid-range (40–80% by default) without constant trickle charging or
deep discharge while the watches are physically docked.

Also automates flashing AsteroidOS nightlies to an entire docked fleet in
one command (`flash-all`).

## How it works

1. Watches are physically connected to smart USB hubs that support per-port
   power control.
2. The `asteroid-docking-bay` CLI controls hub port power via `uhubctl` and
   communicates with watches via `adb`.
3. A systemd user timer fires every 12 hours, powers each watch on, checks
   the battery over ADB, charges for ~30 minutes if below 40%, then powers
   the port back off.

Battery level is read from `/sys/class/power_supply/battery/capacity` over
ADB shell. This is the standard Linux power-supply class — `dumpsys` and
`getprop` are Android-only and not available on AsteroidOS.

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Python 3 | ≥ 3.9 | stdlib only; `python-systemd` optional for journald |
| [uhubctl](https://github.com/mvp/uhubctl) | any recent | for hub power control |
| adb | any | `android-tools` or `android-sdk-platform-tools` |
| fastboot | any | required for `flash-all`; same package as adb |
| wget | any | required for `flash-all` nightly downloads |

### Installing dependencies

**Arch Linux**
```sh
sudo pacman -S uhubctl android-tools
```

**Debian / Ubuntu**
```sh
sudo apt install uhubctl android-tools-adb
```

## Installation

```sh
git clone https://github.com/moWerk/asteroid-docking-bay.git
cd asteroid-docking-bay
./install.sh
```

The binary is installed to `~/.local/bin/asteroid-docking-bay`. The systemd
user units go to `~/.config/systemd/user/`.

### Rootless setup (recommended)

By default uhubctl requires root. To run without sudo:

1. Find your hub's USB vendor ID:
   ```sh
   lsusb | grep -i hub
   # or
   uhubctl -l
   ```

2. Edit `udev/70-asteroid-docking-bay.rules` to uncomment the line matching
   your hub's vendor ID.

3. Install the rules:
   ```sh
   sudo cp udev/70-asteroid-docking-bay.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   sudo usermod -aG plugdev $USER
   # log out and back in
   ```

4. For ADB access (if not already configured):
   ```sh
   # Arch:
   sudo pacman -S android-udev
   # Other: copy udev/70-asteroid-docking-bay.rules and uncomment the ADB line
   ```

## Quick start

```sh
# Step 1: map hub ports to watch codenames
asteroid-docking-bay map

# Step 2: verify everything looks right
asteroid-docking-bay status

# Step 3: enable the automatic charging timer
systemctl --user enable --now asteroid-docking-bay-charge.timer
```

## Commands

```
asteroid-docking-bay status
```
Show all configured watches with hub port, power state, ADB reachability, and
battery percentage.

```
asteroid-docking-bay on  <codename|all>
asteroid-docking-bay off <codename|all>
```
Power a port on or off. `off all` asks for confirmation unless `--force` is
passed.

```
asteroid-docking-bay cycle <codename|all> [--wait SEC]
```
Power off a port, wait (default: 5 s), power back on. Useful to hard-reset a
stuck watch.

```
asteroid-docking-bay charge <codename|all> [--duration MIN]
```
Manual one-time charge cycle: power on → wait for ADB → charge for
`--duration` minutes (or `charge_duration_minutes` from config) → power off.
The `high_threshold` check is skipped — the charge always runs.

```
asteroid-docking-bay check-charge
```
Periodic charge logic (same as what the timer runs). Safe to run manually for
testing. For each configured watch: wakes it, checks battery, charges if
below `low_threshold`, powers off.

```
asteroid-docking-bay map
```
Interactive wizard that:
1. Assigns watch codenames to hub ports.
2. **Tests each port's power switching capability** with a live toggle (~3 s per port).
3. Discovers ADB serial numbers.

Re-run any time you add or move a watch. The switching test can be skipped and
run separately with `test-ports`.

```
asteroid-docking-bay test-ports [codename]
```
Re-test per-port power switching for all configured ports (or a specific watch).
Updates the config and reports smart vs. non-smart results. Run this after
moving a watch to a different hub port, or if you skipped the test during `map`.

```
asteroid-docking-bay discover
```
Scan for ADB-connected watches and print their codename and serial. Useful for
finding serials after a new watch is connected.

```
asteroid-docking-bay flash-all [codename|all] [--local DIR] [--dry-run]
                                [--force-download] [--download-dir DIR]
```
Flash AsteroidOS nightlies to all configured watches (or a single codename) in
sequence, fully automated:

1. Downloads `zImage-dtb-{codename}.fastboot` and
   `asteroid-image-{codename}.rootfs.ext4` from
   `release.asteroidos.org/nightlies/{codename}/` and verifies SHA512. Already-
   downloaded files that pass the checksum are reused without re-downloading.
2. Powers on the hub port.
3. Waits for ADB. If the watch answers at `192.168.2.15` instead (SSH/RNDIS
   mode rather than ADB mode), it prints a prompt to switch to ADB mode in
   Settings → USB on the watch, then waits again.
4. `adb reboot bootloader`
5. Waits for a fastboot device to appear.
6. `fastboot flash userdata` + `fastboot flash boot` + `fastboot continue`
7. Removes stale `192.168.2.15` and `watch` entries from `~/.ssh/known_hosts`
   so the next SSH session isn't blocked by a key mismatch.

`--dry-run` prints the fastboot commands without executing them.  
`--local DIR` uses pre-built images from a local directory instead of
downloading.  
`--force-download` re-downloads even if the cached copy passes SHA512.  
`--download-dir DIR` overrides the default cache location
(`~/.local/share/asteroid-docking-bay/nightlies/`).

## Typical status output

```
WATCH             PORT            POWER  SMART  ADB             BATTERY
nemo              1-1:p1          ON     yes    device          67%
sparrow           1-1:p2          OFF    yes    --              --
dory              1-1:p3          ON     NO!    unauthorized    --
beluga            2-3.4:p2        ON     ?      offline         --
```

SMART column values:
- `yes` — per-port power switching confirmed by live test
- `NO!` — confirmed NOT switchable; on/off/cycle/charge have no effect
- `?`   — not yet tested; run `test-ports` to check

ADB states:
- `device` — fully authorized and online
- `unauthorized` — authorize the ADB connection on the watch screen
- `offline` — USB connected but ADB not yet ready (still booting, or sleeping)
- `--` — port is powered off or watch not recognized

## Configuration

Config is stored at `~/.config/asteroid-docking-bay/config.json`.
See `config.example.json` in this repo for a fully-annotated example.

| Key | Default | Description |
|---|---|---|
| `low_threshold` | `40` | Charge if battery is below this % |
| `high_threshold` | `80` | Power off if battery is at or above this % |
| `charge_duration_minutes` | `30` | How long to charge per cycle |
| `adb_wait_seconds` | `15` | Seconds between ADB availability retries |
| `adb_wait_retries` | `8` | Max retries (total wait: wait_seconds × retries) |
| `check_interval_hours` | `12` | Documentation only — actual interval is set in the systemd timer |
| `flash.nightly_url` | `https://release.asteroidos.org/nightlies` | Base URL for nightly image downloads |
| `flash.download_dir` | `~/.local/share/asteroid-docking-bay/nightlies` | Local cache for downloaded images |

The `check_interval_hours` field does **not** drive scheduling. Edit
`~/.config/systemd/user/asteroid-docking-bay-charge.timer` to change the
interval, then reload: `systemctl --user daemon-reload`.

## Systemd timer

```sh
# Enable and start
systemctl --user enable --now asteroid-docking-bay-charge.timer

# Check status / last run
systemctl --user status asteroid-docking-bay-charge.timer
systemctl --user status asteroid-docking-bay-charge.service

# View logs
journalctl --user -u asteroid-docking-bay-charge.service -f

# Disable
systemctl --user disable --now asteroid-docking-bay-charge.timer
```

## Hardware notes

### Watches that require a physical power button press

Some AsteroidOS watches auto-boot when USB power is applied. Others require a
physical power button press. This tool cannot automate the latter — it will
power on the port, then wait for ADB, and log a warning with instructions if
the watch doesn't appear. You will need to press the button manually.

Known behavior by codename is not tracked here; check your watch's hardware
documentation or the AsteroidOS porting guide.

### Identifying smart hub ports

After `uhubctl -l` shows your hub location (e.g. `1-1`), plug in one watch at
a time and use `asteroid-docking-bay discover` to identify which serial
appeared. Then use `asteroid-docking-bay map` to assign the codename.

Alternatively, `uhubctl -l 1-1 -p 2 -a off` powers off port 2 on hub `1-1`;
if a watch disappears from `adb devices`, you have the right port.

### Multiple hubs

`uhubctl` identifies hubs by their USB location string (e.g. `1-1`, `2-3.4`).
These strings are stable as long as you plug hubs into the same physical ports.
If you rearrange cables, re-run `asteroid-docking-bay map`.

## Uninstall

```sh
./install.sh --uninstall
```

This removes the binary and systemd units. Config and serial mappings at
`~/.config/asteroid-docking-bay/` are preserved. Remove manually with
`rm -rf ~/.config/asteroid-docking-bay/`.

## License

GPL-3.0-only — see [LICENSE](LICENSE).

Copyright (C) 2026 Timo Könnecke (moWerk) \<mo@mowerk.net\>  
Copyright (C) 2023 Ed Beroset \<beroset@ieee.org\>

The flashing sequence in `flash-all` is based on
[beroset/asteroid-hosttools](https://github.com/beroset/asteroid-hosttools)
(`flashy`), which is also GPL-3.0-only.
