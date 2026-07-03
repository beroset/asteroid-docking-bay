# asteroid-docking-bay

USB hub power manager for [AsteroidOS](https://asteroidos.org) smartwatches.

Manages per-port power switching on smart USB hubs (via
[uhubctl](https://github.com/mvp/uhubctl)) to keep watch batteries in a
healthy mid-range (40–80% by default) without constant trickle charging or
deep discharge while the watches are physically docked.

Also automates flashing AsteroidOS nightlies to an entire docked fleet in
one command (`flash`).

<img width="1501" height="631" alt="asteroid-docking-bay Web UI" src="https://github.com/user-attachments/assets/3bf9cc0e-ac0c-4aee-97b9-0624f2b92bb6" />

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
| fastboot | any | required for `flash`; same package as adb |
| wget | any | required for `flash` nightly downloads |
| [bottle](https://bottlepy.org/) | any recent | optional; required for `serve` (web UI) |

### Installing dependencies

**Arch Linux**
```sh
sudo pacman -S uhubctl android-tools
pip install bottle   # optional, for the web UI
```

**Debian / Ubuntu**
```sh
sudo apt install uhubctl android-tools-adb
pip install bottle   # optional, for the web UI
```

## Installation

```sh
git clone https://github.com/moWerk/asteroid-docking-bay.git
cd asteroid-docking-bay
./install.sh
```

The binary is installed to `~/.local/bin/asteroid-docking-bay`. The systemd
user units go to `~/.config/systemd/user/`.

`~/.local/bin` must be on your `PATH` for the short form to work. Most
distros add it automatically; if yours doesn't (the installer will warn you):
```sh
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc or ~/.zshrc
```

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
# Step 1: one-time hub setup — discovers hubs and tests per-port power switching
asteroid-docking-bay map

# Step 2: verify everything looks right
asteroid-docking-bay status

# Step 3: enable the automatic charging timer
systemctl --user enable --now asteroid-docking-bay-charge.timer
```

After the initial `map`, the **web UI handles everything else** — plugging in a
new watch, moving a watch to a different port, or adding a watch to a previously
empty slot. Click **Refresh** on any empty port row to detect and configure the
watch automatically (reads the codename from the device, tests PPPS, updates
config). No CLI intervention needed for day-to-day use.

<img width="999" height="907" alt="After executing asteroid-docking-bay map" src="https://github.com/user-attachments/assets/07f0756e-3188-44df-8b3e-905e868177f1" />

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
One-time hub setup wizard:
1. Discovers all connected smart hubs.
2. Powers each port on in sequence and identifies the watch via ADB.
3. **Tests per-port power switching (PPPS)** with a live toggle (up to ~30 s
   per port — see `test-ports` below for what is verified).
4. Saves hub topology, port assignments, and serial numbers to config.

Run once when you first connect a hub, and again only if the hub itself
moves to a different USB port on the host. Adding or moving watches between
ports does not require re-running `map` — use **Refresh** in the web UI instead.
The switching test can be run independently with `test-ports`.

```
asteroid-docking-bay test-ports [codename]
```
Re-test per-port power switching for all configured ports (or a specific watch).
Updates the config and reports the verdict per port. Run this after moving a
watch to a different hub port, or if you skipped the test during `map`.

The test verifies that VBUS is **actually cut**, not just that the hub's
status register toggles (many hubs ACK the command while power stays live).
Evidence, strongest first: the device dropping off ADB within seconds of the
cut; if it keeps responding, the watch itself is asked whether it still sees
external power. Verdicts are three-way:

- `SMART` — VBUS cut confirmed by a live device
- `NOT SMART` — the device demonstrably kept external power through "off"
- `UNVERIFIED` — no device on the port to test against (empty port, or a
  watch that can't enumerate); re-run once a working watch is attached

A definitive verdict therefore requires a booted, ADB-visible watch on the
port. Expect up to ~30 s per port.

```
asteroid-docking-bay discover
```
Scan for ADB-connected watches and print their codename and serial. Useful for
finding serials after a new watch is connected.

```
asteroid-docking-bay serve [--host HOST] [--port PORT]
```
Start the web UI (requires `pip install bottle`). Serves a live status page at
`http://HOST:PORT/` (default: `http://127.0.0.1:8080/`).

Each configured watch is shown as a table row. **Empty port rows** (no watch
assigned) show a **Refresh** button that triggers a full per-port remap: powers
the port on, waits for a watch, reads its codename, tests PPPS, and updates
config — all streamed live into an inline log below the row.

**Mapped watch rows** offer:

- **Refresh** — re-polls ADB state and battery level.
- **ON / OFF toggle** — switches hub port power; confirms the state changed.
- **⟳** — power-cycles the port (off → 5 s → on).
- **⏻ Halt** — submenu: graceful OS shutdown, reboot, or reboot to bootloader.
- **⚡ Charge** — manual one-shot charge cycle with a live countdown, and a
  **◼ Stop charge** button while it runs.
- **📉 Drain test** — standby battery drain measurement: powers the port off
  and lets the watch run on battery, waking it every 30 minutes for a battery
  reading until it reaches 15% or you press **◼ Stop test**. The battery
  column shows the current level, drain rate (%/h), and estimated time to the
  floor. Results are saved as JSON to
  `~/.local/share/asteroid-docking-bay/drain-tests/`.
- **Flash nightly** — full nightly flash streamed live into the inline log.

Charge and drain state live in the server, not the browser — reloading the
page (or opening it from another machine) picks up running operations and
their countdowns. On ports recorded as not power-switchable, the power
toggle, cycle, Charge and Drain buttons are disabled (Refresh, Halt and
Flash still work — they only need ADB).

The page auto-refreshes every 15 seconds. `--host 0.0.0.0` makes it
reachable from other machines on the network.

For persistent background operation, use the included systemd service:
```sh
# One-time setup
pip install bottle
systemctl --user enable --now asteroid-docking-bay-web.service
# → http://127.0.0.1:8080/
```

```
asteroid-docking-bay flash [codename|all] [--local DIR] [--dry-run]
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

## Systemd units

### Charge timer
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

### Web UI service
```sh
pip install bottle   # one-time

# Enable and start (serves at http://127.0.0.1:8080/)
systemctl --user enable --now asteroid-docking-bay-web.service

# View logs
journalctl --user -u asteroid-docking-bay-web.service -f

# Disable
systemctl --user disable --now asteroid-docking-bay-web.service
```

To change the port, override the service:
```sh
systemctl --user edit asteroid-docking-bay-web.service
# add:
# [Service]
# ExecStart=
# ExecStart=%h/.local/bin/asteroid-docking-bay serve --port 9090
```

## Hardware notes

### Watches that require a physical power button press

Some AsteroidOS watches auto-boot when USB power is applied. Others require a
physical power button press. This tool cannot automate the latter — it will
power on the port, then wait for ADB, and log a warning with instructions if
the watch doesn't appear. You will need to press the button manually.

Known behavior by codename is not tracked here; check your watch's hardware
documentation or the AsteroidOS porting guide.

### Hub PPPS: data-line disconnect vs true VBUS switching

`uhubctl` marks a hub as `ppps` (per-port power switching) if it responds
to power commands. There are two very different hardware behaviours hiding
behind this label:

**True VBUS switching** — the hub physically gates the 5 V supply rail.
The watch loses both USB data and charging current. This is required for
battery management (the charge timer is useless without it).

**Data-line disconnect only** — the hub disconnects D+/D− so the device
stops enumerating on USB (`adb devices` drops, port shows `0000 off`), but
VBUS stays live and the watch keeps charging. This is what many cheap hubs
do, including ALCOR 05e3:0606 units.

`test-ports` distinguishes these automatically: it requires the device to
actually drop off the bus (or to report loss of external power over ADB)
before recording a port as smart — a toggling status register alone is not
accepted as evidence. To check manually: switch a port off with `uhubctl`,
then check whether the watch is still charging. If it is, the hub only
controls data lines.

Data-line-only hubs are still useful for ADB operations (flash, reboot,
bootloader) since those require a live USB data connection. They cannot
stop or control charging.

The [uhubctl compatible devices list](https://github.com/mvp/uhubctl#compatible-usb-hubs)
notes which hubs do true VBUS switching.

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

The flashing sequence in `flash` is based on
[beroset/asteroid-hosttools](https://github.com/beroset/asteroid-hosttools)
(`flashy`), which is also GPL-3.0-only.
