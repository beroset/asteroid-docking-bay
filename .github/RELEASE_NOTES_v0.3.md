# asteroid-docking-bay 0.3 — the watch, mirrored

0.2 was going to be a quiet performance release. It became 0.3 instead, because working out *why* port switching was slow turned into rewriting how the tool touches USB at all — and once the dashboard was fast enough to feel alive, it wanted to do more than switch power. The commit history has the whole arc: the sysfs migration, one genuine regression, a near-revert-to-0.1 at 1 a.m. ("USB is a bitch and we gotta tame it"), the root cause finally cornered with falsifiable measurements instead of guesses, and then a creative burst where the tool learned to read the watch's own *About* page and drive its hardware from a browser tab. **0.2 is folded in; this is everything since 0.1.**

The [README](https://github.com/moWerk/asteroid-docking-bay#readme) covers day-to-day use. These notes are the technical companion — detailed enough to review.

## What's new since 0.1

- **Direct sysfs port switching** — near-instant; `uhubctl` demoted to discovery.
- **Status page off the USB scan path** — typically under 100 ms (was 13–70 s).
- **Rootless switching** via a udev rule, with a startup diagnostic.
- **Durable operations** — running charges / drains / workbench survive a restart.
- **Adaptive charge cadence** + a per-watch event log.
- **Control Center** — full device telemetry and hardware control from the browser.
- **Row actions regrouped into floating menus** + new watch actions.
- **Charge power-loss alarm** and **graceful return-to-rest**.

## Direct sysfs switching — the core change

`uhubctl` re-enumerates the entire USB tree via libusb on *every* command (5–13 s on this project's 5-level hub cascade). That was both the "snail" UI and — the part that took a night to see — the churn that raced the adb daemon into `LIBUSB_ERROR` wedges. Every port toggle was shaking the bus while adb tried to enumerate it.

0.3 switches power by writing the kernel's per-port attribute directly:

```
/sys/bus/usb/devices/<hub>:1.0/<hub>-portN/disable   # 0 = on, 1 = off
```

One targeted write, no tree scan — a blink instead of ~5 s. Power *state* is read the same way. `uhubctl` stays for discovery/mapping and the PPPS test, and as a transparent fallback when the attribute isn't writable.

**Correction to the 0.1 concurrency model.** The elaborate uhubctl-serialization-plus-`flock` described in the 0.1 notes was treating a symptom. With uhubctl off the hot path the cross-process bus contention it guarded against largely evaporates; the lock remains only around the residual discovery calls. This is called out honestly because the 0.1 notes presented that machinery as the fix, and it wasn't the root cause.

## Rootless switching

The `disable` attribute is root-only by default. A udev rule matches each smart hub by vendor ID (the same set used for uhubctl access) and `RUN+=`'s a `chmod` over its child `*-portN/disable` attributes, granting the `users` group write — a USB port is not itself a udev device, so the hub is matched and its ports chmod'd. The host's own root hub is a different vendor and is never touched (verified by a dry-run: 40 attrs granted, root hub untouched). Without the rule the tool falls back to `uhubctl`; the `serve` process logs `Port switching: sysfs (instant)` or the fallback at startup so the state is never a mystery.

## Status path — fast, off the bus

The status page no longer scans the bus. Port power comes from a TTL'd in-memory cache (the tool is the only thing that switches these ports, so it is authoritative; updated on every write), filled by a gentle *sequential* background warmer so that a slow, variable `disable` read never blocks a refresh — and a present child device proves a port is powered with no read at all. `fastboot devices` (another multi-second USB scan that ignores its own subprocess timeout) moved into the same warmer, and every per-watch `adb shell`/battery read gained a timeout (an unbounded `dumpsys` on a sluggish watch was a multi-minute page stall). Measured end to end: 13–70 s → typically under 100 ms, with occasional sub-second cache-fill blips. No parallel USB reads — that concurrency is exactly what wedges.

## Durable operations

Charge, drain, and workbench state persist to atomic-write JSON task files and resume on startup, so a service restart (or a deploy) no longer forfeits a running drain test. This closes the first 0.1 known-limitation.

## Adaptive cadence + event log

The 12-hour sustain timer skips waking a watch whose measured standby drain projects it still comfortably above `low_threshold`, computed from a per-watch JSONL event log of every off-state reading. Watches with no drain history are always checked.

## Control Center — the watch, mirrored on the host

The flagship new feature. The data sources were read from AsteroidOS's own settings app (`AboutPage.qml` → `SysInfo`/`uname`/`qVersion`, `QuickPanelPage.qml` → connman), then every command confirmed live on hardware. Click a watch's codename → a wide, multi-column panel reads, in a **single** `adb shell` batch:

- **System** — kernel, Qt, SoC, CPU clock, uptime, boot reason (catches unexpected reboots), memory, storage.
- **Battery** — charge, status, health, technology, voltage, **real charge/discharge current in mA (signed)**, temperature, cycle count, and USB-input voltage (confirms the port is truly delivering power, not just switched on). `CURRENT_NOW` is a far better drain signal than the coarse percentage.
- **Network & links** — WiFi state, IP, traffic, Bluetooth, and the connected companion phone.

Controls from the same panel: WiFi/Bluetooth toggles (connman), **buzz** (the `/sys/class/timed_output/vibrator` haptic — locate a watch in a full dock), **screen-on** (`mcetool -D on`), **screenshot**, and **clock + timezone sync** from the host. Session tools (screenshot, notifications) run in the watch's `ceres` user session via `su`, because `adb shell` is root and cannot reach the user's Wayland compositor or D-Bus session bus. The panel flips above or below the click to stay on-screen and reflows to one column when narrow.

## Row actions → menus, plus new actions

The per-row action column regrouped into three floating menus — **Power** (charge, drain, power-off/reboot/bootloader), **Workbench** (band-hold checkout plus attended actions that leave the watch on), and **Flash** — that flip above/below to fit the viewport; Refresh became a small leading recycle icon. New watch actions were harvested from the AsteroidOS wiki and beroset's `watch-util` and confirmed live: set-time-from-host (`date -s` + `timedatectl set-timezone`), screenshot (`screenshottool`, pulled + served as a JPEG), and test notification (`notificationtool`). `usb_moded_util -s adb_mode` is catalogued as a clean SSH→ADB switch for onboarding recovery.

## Charge power-loss alarm

The charge loop used to discard a non-rising battery read as "transient — keep charging." But a charging watch that *loses* charge over consecutive polls is not transient — it is losing power despite the attempt (dirty contacts, a bad cable, a failing port). That case now raises a visible **losing power!** alarm plus a `charge_power_loss` event, keeps trying in case a flaky contact recovers, and clears the moment the battery gains again.

## Graceful return-to-rest

Charge and drain now end by shutting the watch down over ADB and cutting VBUS *immediately* — a shutdown that completes with power still present makes these watches auto-boot back — so a finished operation leaves the watch genuinely off rather than silently draining. It never powers a port on or blocks when adb is unhealthy.

## Provenance (unchanged stance)

Written end-to-end by an LLM coding agent (Anthropic Claude), directed, tested, and reviewed by a human maintainer from the user/tester side, verified on a real mixed fleet before each commit. GPL-3.0-only. The flash sequence and the new backup / settings command patterns derive from beroset's GPL-3.0 [asteroid-hosttools](https://github.com/beroset/asteroid-hosttools). The commit history is the unedited record — including a genuine regression and a near-revert to 0.1, both left in the log.

## Known limitations in 0.3

- The sysfs `disable` write needs the udev rule (or root); without it the tool transparently falls back to slow `uhubctl`.
- Motion / heart-rate / step sensors are exposed via sensorfw (D-Bus), not sysfs, and are not yet surfaced.
- Flash-menu **Backup / Restore** and versioned (2.x) flashes are placeholders — the backup/restore commands are known (pull `~ceres/.config` + `/var/lib/connman` + `dconf dump`; restore = push back + `dconf load`), but 2.x releases do not exist yet.
- The web service and the periodic timer still coordinate at the USB-bus level, not the decision level.
- The web service was once observed running two `serve` processes (a restart leftover); the power cache is TTL'd so a per-process cache cannot serve stale state regardless.

## Requirements

Python ≥ 3.9 (stdlib only), `adb`; `uhubctl` for discovery, fallback switching, and the PPPS test; `fastboot` and `wget` for flashing; `bottle` for the web UI. Rootless instant switching wants the udev rule (see README). A hub that does true per-port VBUS switching — the built-in test tells you honestly whether yours does.
