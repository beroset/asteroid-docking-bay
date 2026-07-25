# USB brittleness audit: xHCI slot exhaustion — 2026-07-25 (evening)

Branch: `onboard-discovery-rework`. Requested by moWerk after the two-A16
onboarding session ended with: 15/24 watches onboarding at ~2 min/port,
"shelved" watches rebooting instead of halting, UI port-power dots wildly
disagreeing with the hubs' LEDs, onboarded watches never reconnecting, and a
"brownout" at the 8th concurrently-connected watch that made no electrical
sense against a 100 W PSU and 0.45 A/watch measured draw.

Task: find why port handling is brittle and slow, before any further feature
work. Everything below was gathered read-only on the live rig (w541); the one
state-changing falsification test is proposed at the end, not run.

## TL;DR

The root cause of nearly every symptom is **xHCI device-slot exhaustion on
the w541's Intel 8-Series host controller** — not hub flakiness, not power,
not adb. Each A16 is internally a cascade of five 4-port Realtek chips, and
each chip enumerates twice (USB2 side + USB3 SuperSpeed companion) because
the hubs sit on USB3 host ports. Two A16s therefore burn **20 controller
slots as pure hub overhead**. The controller has **32 slots**. At audit time
the bus held **exactly 32 devices** and the kernel was refusing to configure
further watches in an endless ~67 s retry storm:

```
usb 1-2.2: Not enough host controller resources for new device state.
usb 1-2.2: can't set config #1, error -12
```

A watch in this state is half-enumerated: it has an address and answers
control transfers (descriptors, serial, product readable), but **no
configuration is applied, so no interfaces exist** — it is invisible to the
adb server, to `fastboot`, to our adb-wedge self-heal, and to every discovery
path we have. It is not dark, not drained, not unplugged: it is slot-starved.

On top of that root cause sit four self-inflicted software defects
(findings F2, F4, F5, F6) that the audit also pins down.

## Methodology

All on the rig over SSH, read-only:

1. `git log`/rsync dry-run — confirmed deployed tree == branch HEAD.
2. Full sysfs topology walk: device list, `bDeviceClass`, `idVendor`,
   `speed` for every hub chip; identity (`idVendor:idProduct`, `serial`,
   `product`) for every non-hub device on the A16 trees.
3. Timed sweep of all 40 USB2-side `disable` registers on both trees.
4. `journalctl` (service + kernel), `adb devices` before/after a manual
   `kill-server`/`start-server`, `lsusb -v` against a stuck node,
   interface-descriptor checks, thread count of the serve process.
5. Slot arithmetic: `ls /sys/bus/usb/devices | grep -E '^[13]-'`.

## Findings

### F1 — xHCI slot exhaustion is the root cause (CONFIRMED)

- Controller: `00:14.0 Intel 8 Series/C220 xHCI [8086:8c31]` — 32 device
  slots (Lynx Point).
- Live count at audit time: **32 devices** on buses 1+3 (21 + 11).
- Slot budget: 10 hub chips USB2-side + 10 SuperSpeed companions + Lenovo
  dock (2 sides) + internal peripherals (`1-5`, `1-11`, `1-12`, `3-5`)
  ≈ 25 slots of overhead → **~7 slots left for watches**.
- Kernel log: continuous `can't set config #1, error -12` retry storm
  (every ~67 s per stuck watch, hours long) for `1-2.2` (KW88),
  `1-1.3.4.3` (narwhal/LG W7), `1-1.3.3.3` (sawfish/LEO-BX9).
- The stuck devices are **live**: `lsusb -v` reads full descriptors from
  the device. They have **no `:1.0` interface directories** — enumerated,
  addressed, unconfigured.

Explains directly:
- "15 of 24 onboarded" — first-come-first-configured slot lottery.
- "8th watch flaky / others degrade" — the fleet's working 15-watch state
  on ONE hub sat just under the limit (one A16 = 10 fewer hub slots);
  the second A16 blew past it.
- "sawfish no longer enumerates" — it enumerates fine; it cannot configure.
- Reconnect "idling out" (see F5) and the self-heal blind spot (F6).
- The constant retry churn is itself bus noise that destabilises
  everything else and contended the very sysfs reads F3 measures.

### F2 — the UI port-power dots show a cold cache, not the registers (CONFIRMED mechanism)

Commit `5082d12` stopped the warmer polling empty ports but replaced the
data source with nothing: `_sysfs_hub_scan` serves `power_cache` entries
that no one writes any more. After a service restart the cache is empty
forever; only ports with an enumerated child read "powered".

Cross-check against mo's physical observation (LEDs = VBUS ground truth):

| tree | UI "powered" | occupied ports (enumerated child) | leaf ports register-ON | LEDs lit (mo) |
|---|---|---|---|---|
| 1-1 (A16 #1) | 5 | **5** | 10 | **10** |
| 1-2 (A16 #2) | 1 | **1** (2nd is a cascade port) | ~9 | **9–10** |

UI count == occupied-port count; register count == LED count. The registers
are truthful; the UI is showing stale nothing. This is a regression from the
25-07 rework, not hub misbehaviour.

### F3 — the "disable reads hang for seconds and renegotiate the hub" premise does NOT reproduce (CONFIRMED for a quiet bus)

All 40 `disable` registers across both trees read in **0.8 s total**
(3–90 ms each, slowest on ports of a chip with active children). The
multi-second hangs that motivated the device-side-discovery redesign were
almost certainly **kernel hub-lock contention** with the F1 retry storm,
uhubctl full-bus scans, and adb churn — a symptom of the environment, not a
property of the read. The redesign's *no-empty-port-polling* goal remains
right (fewer touches is better), but a **single serialized ~1 s register
sweep is cheap and safe** and is exactly what F2 needs as its truth source.

### F4 — the sweep re-implemented poweroff and lost the hard-won timing rule (CONFIRMED by code inspection; behaviour matches mo's report)

`port.poweroff` (`rpcops.py`) documents the constraint explicitly: the
shutdown command is synchronous, so **cut VBUS immediately** — "any delay
here races the halt: cutting while the watch is still up lets a watch
without offmode charging bounce back on."

`_sweep_one_port` instead: sends `poweroff`, then polls up to **36 s** for
the adb drop, then cuts VBUS via raw `_sysfs_set_power` (bypassing
`uhubctl_set_power`'s completeness gate, read-back and fallback). Two
defects in one:

1. The 3–36 s delay is exactly the race the poweroff op warns about —
   consistent with "the sweep's 14 shelved watches all came back up".
2. Using adb-drop as the poweroff confirmation violates the project's own
   evidence rule (`reference_uptime_verifies_reboot`): **adb also drops on a
   reboot**, so a watch that reboots instead of halting is stamped
   `safe_off` — a false "shelved" that hides an on-battery drain. This is
   the mechanism that flattened the fleet.

One name per concept: there must be exactly one poweroff-and-cut
implementation, and it already exists.

### F5 — reconnect latency is polling granularity stacked on slot starvation (CONFIRMED config + code)

- `wait_serial_online` polls every `adb_wait_seconds = 15` — a watch that
  appears 2 s after re-power is not seen for up to 15 s; UI refresh adds
  another 15 s. "States don't flip within a browser refresh" is by design.
- The sweep's detection loop waits ≥ 90 s on the adb *server* — which never
  lists a slot-starved watch, so the full window burns, the port cycles,
  and a second window burns: the observed ~2–3 min/port.
- A watch that IS configured shows on the bus within ~1–3 s of boot
  completing; nothing in the stack needs minutes. The latencies are all
  wait-loop artefacts.

### F6 — the adb-wedge self-heal is blind to exactly the failure that is happening (CONFIRMED)

`_sysfs_adb_serials()` recognises a watch by its adb **interface
descriptor** (class ff/42/01). A slot-starved watch has no interfaces, so
`on_bus` is empty, no wedge is detected, and the heal never fires (service
logs: zero heal lines since 13:55 despite three unreachable adb-mode watches
on the bus). Additionally a manual `adb kill-server`/`start-server` was
tested and correctly changes nothing — **this class of "nothing connects" is
not an adb-server problem at all**, and restarting the server can never fix
it. The heal needs a sibling detector: "enumerated watch without interfaces"
→ report slot exhaustion, don't restart anything.

### F7 — the "brownout" is at least partly (probably mostly) F1 (kernel-confirmed for the USB layer; electrical share is mo's domain)

The 8th-watch failure mode mo observed — flaky connection, others
degrading — is what slot exhaustion looks like from the outside. mo's own
measurements (≤ 0.45 A panic-charge, ~0.1 A idle, 100 W PSU) already said
electrical brownout was implausible; the kernel log now supplies the real
mechanism. The measured voltage droop remains mo's hardware call to
re-examine with this knowledge; it is no longer needed to explain the
symptom. (Disproven along the way: the hubs do NOT claim bus-powered —
`bmAttributes=e0`, self-powered, so the kernel 100 mA-budget theory is
dead.)

### F8 — shelved-port registers agree with LEDs; yesterday's VBUS cuts DID cut (CONFIRMED register-side; LED cross-check is mo's)

The four all-OFF registers on `1-2.3.3` (the swept skipjack/rubyfish seats)
and the OFF registers on `1-2.3.4` line up with mo's dark-LED count. So the
sweep's `_sysfs_set_power(off)` writes did drop VBUS on this direct-host
wiring — the failure was the *watches rebooting before the cut* (F4), not
the cut itself. The peer/companion write path works on the current topology.

### F9 — current stuck inventory (for cleanup once the fix lands)

- `1-2.2` KW88 (serial literally `serial`), `1-1.3.4.3` narwhal,
  `1-1.3.3.3` sawfish: live, slot-starved, retrying forever.
- `1-1.4.2` (ZenWatch 2, `GCNZ…`): enumerated `18d1:0afe`, also without
  interfaces (slot-starved in fastboot-ish mode).
- `1-1.4.3` / `1-1.4.4` (ZenWatch 2 `G3NZ…`/`H1NZ…`): `0afe` WITH
  interfaces — parked in bootloader/offmode, configured.
- `fastboot devices` returned nothing for any of them (expected for the
  starved one; the configured two may need udev/permission attention —
  minor, revisit after the slot fix).

## Disproven / retired hypotheses (dead ends worth recording)

- "Stale corpse nodes wedging adb" — the stuck nodes answer control
  transfers; they are live, not corpses. (The stale-node quirk still exists
  on this dock historically, but it is not what is happening now.)
- "A16s can't switch VBUS on this wiring" — registers track LEDs (F8).
- "disable reads are inherently slow/destructive" — F3.
- "electrical brownout at 8 watches" — F7 (USB layer); electrical residue
  is mo's to judge.
- "adb server wedge" as the current reconnect blocker — F6.

## Fix directions (decision points for moWerk)

**A. Give the controller room — the structural fix. Options, combinable:**

1. **Unbind the idle SuperSpeed companion trees** (`3-1`, `3-2`) at service
   start: +10 slots, zero lost function (watches are USB2-only). MUST be
   validated against port power switching first: with the SS side unbound
   the `peer` disable path vanishes; whether a USB2-side-only
   `disable`/uhubctl command still drives the shared physical VBUS switch
   on the A16 is an open hardware question (one live test, below).
2. **Move the A16 uplinks to the w541's USB 2.0-only host ports** — the
   companions never enumerate at all; same +10 slots and the peer
   complexity disappears permanently. Same VBUS-switch validation needed.
   Note even then: 10 hub chips + ~6 peripherals + 24 watches > 32, so an
   ALL-watches-up fleet still cannot fit on the xHCI.
3. **Route the hub host ports to the EHCI controller** (bus 2 exists and
   works): Intel 8-Series supports per-port xHCI↔EHCI routing via the
   XUSB2PR register (`setpci`). EHCI has no slot concept (127 addresses);
   the whole 32-socket fleet could enumerate simultaneously, bandwidth
   permitting. Most invasive (boot-time setpci, must ensure the kernel
   doesn't re-route), but the only option that removes the ceiling.
4. **Operational headroom**: keep watches shelved so the concurrent count
   stays under the limit — only viable once shelving is trustworthy (F4)
   and with a UI slot gauge (B) so pressure is visible.

The right choice depends on the target: is the fleet model "N live, rest
truly shelved" (then 1 or 2 + gauge suffices) or "everything up at once"
(then only 3 does it)?

**B. Port-power truth (fixes F2):** one serialized background register
sweep (~1 s for 40 ports) feeding `power_cache`; UI dot = register, with
the documented LED/physical-button caveat. Add an xHCI slot gauge
(`devices on bus / 32`) to the header with a warning threshold — slot
pressure must never be invisible again.

**C. Event-driven discovery (fixes F5, the "ninja" workflow):** subscribe
to kernel uevents (udev netlink) instead of polling the adb server on
15 s/90 s timers. Attach, config, and detach become sub-second signals;
"enumerated but unconfigured" becomes a first-class detected state with
its own UI message ("controller full — watch X can't attach") instead of a
silent 2-minute timeout. Polling remains only as a low-frequency fallback.

**D. One poweroff (fixes F4):** the sweep calls the same code path as
`port.poweroff` (synchronous delivery → immediate VBUS cut → no safe_off
claim without evidence). Post-shelve verification where wanted: re-power
later and read `/proc/uptime` (the ground-truth rule), recording per-watch
halt behaviour in the fleet registry.

**E. Smarter self-heal (fixes F6):** two detectors — (a) interfaces
present but server blind → restart adb server (existing heal); (b)
enumerated watch, no interfaces → slot exhaustion: log it, surface it,
never "fix" it by restarting things.

## Proposed falsification test (awaiting go-ahead — changes USB state)

Reversibly unbind ONE idle companion tree and watch the retry storm:

```
echo 3-2 > /sys/bus/usb/drivers/usb/unbind     # frees 5 slots
# expect within ~70 s: "usb 1-2.2: ... config #1" SUCCESS, interfaces
# appear, KW88 lists in adb devices
echo 3-2 > /sys/bus/usb/drivers/usb/bind       # restore
```

Then, separately and one port at a time: verify VBUS switching on tree
`1-2` still physically works (LED check by mo) while its SS side is
unbound — the go/no-go datum for fix A1/A2.
