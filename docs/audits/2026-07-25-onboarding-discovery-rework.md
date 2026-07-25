# Onboarding discovery rework — 2026-07-25

Branch: `onboard-discovery-rework` (off `0.9`, which is preserved as the fallback).

## Why

Onboarding new watches broke down badly on the two-A16 / USB3 rig. Symptoms,
all reproduced live:

- **Powering or reading an empty port knocked other watches off the bus.**
  "As soon as an empty port is powered, not even registered watches enumerate
  again — only when the whole hub is off." The A16 hubs on USB3 present
  SuperSpeed companions and renegotiate the whole tree on a port event.
- **A 24-watch power-on burst crashed the adb server**, after which nothing
  enumerated.
- **The fleet registry stayed empty.** `soft_remap` mapped a watch to its port
  but never wrote it to `registry.json`, so watches were "on a port" but never
  "in the fleet."
- **Multi-second status refreshes / minute-long onboard latencies**, largely
  from the background warmer polling every empty port's `disable` attribute — a
  read that hangs for seconds on a powered-down port *and* renegotiates the hub.

The last point is the crux: the warmer's empty-port polling is both slow and
actively destructive on this hardware.

## The redesign (moWerk's call, 2026-07-25)

Stop probing sockets; discover from the device side.

1. **Never poll empty ports.** Removed the warmer's per-empty-port `disable`
   read. Occupied ports are still read via the status path (a present child
   device already proves the port is powered — no `disable` read needed). An
   empty port is simply never touched until a watch is deliberately put on it.
2. **Discover from ADB/SSH.** A watch appearing on ADB (or SSH/RNDIS) is
   resolved to its sysfs port and onboarded — `soft_remap` already did the
   ADB→path→map half; it now also writes the watch to the fleet registry.
3. **An interactive "onboard sweep"** for filling a rig: map the topology, power
   every socket **down**, let the user equip every socket with a watch (a dark
   port doesn't enumerate → no ADB flood), then run the sweep — **one port at a
   time**:

   ```
   power on → detect ADB (else SSH/RNDIS on 192.168.2.15 → allocate a unique IP
   → switch to ADB, or keep SSH, per the prefer-ADB toggle) → map + read first
   data + write to the fleet registry → PPPS-test → clean `poweroff` → cut VBUS
   → next
   ```

   A port whose watch never boots (drained / empty) is cut and logged as
   "needs charge". Never more than one port is powered at once — no brownout,
   no flood, no renegotiation storm.

### Spec decisions (moWerk)

- **Power-down after each onboard: clean `poweroff` then cut VBUS** — the watch
  ends truly off, no on-battery drain (the failure that flattened the fleet).
- **SSH/RNDIS: full IP-relocation** — allocate a unique IP, then ADB or keep-SSH
  per the prefer-ADB toggle.
- **Drained/no-show ports: power down & skip**, logged — strictly one-port-on,
  no brownout. Drained watches are charged separately later.
- **PPPS test folded into onboarding** — the watch is already up, so
  `test_port_power_switching` verifies a real VBUS cut for free.

## Changes

- `5082d12` — stop the warmer polling empty ports; discover device-side.
- `e1b0f5a` — write onboarded watches to the fleet registry (`soft_remap`).
- `38c33ef` — the interactive onboard sweep (`onboard.sweep_prepare` /
  `onboard.sweep_run` ops, web routes + trigger, tests).

## Testing

- Unit + planted-bug: `_sweep_map_and_register` maps to the port, clears a stale
  seat, and writes the fleet registry with first data (planted-bug: dropping the
  `registry.note` fails the test). Full suite green (465).
- Rig test: reset data → remap → run the sweep with the fleet equipped.
  (Results appended below once the run completes.)

## Notes / open ends

- The SSH keep-alive path (prefer-SSH: relocate + register as an SSH member) uses
  the existing `allocate_ssh_ip` + `orbit.probe` primitives; the common case
  (prefer-ADB → switch to ADB) is the solid path and what the sweep exercises
  first.
- Much of the fleet is drained (from an earlier operational mistake this session)
  and will log as "needs charge" rather than onboard — that's expected, not a
  fault of the new logic; those cells need a separate charging pass.
