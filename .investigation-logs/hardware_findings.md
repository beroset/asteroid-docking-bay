# Hardware Findings

Sources: uhubctl issues #664 and #665 (filed by moWerk, later closed after VBUS testing).
- https://github.com/mvp/uhubctl/issues/664  — ALCOR 05e3:0606
- https://github.com/mvp/uhubctl/issues/665  — Manhattan MondoHub II

---

## Hub 1: ALCOR 05e3:0606 "USB Hub 2.0" (×4 units)
**Product:** Generic 4-port USB 2.0 hub, AliExpress item 1005006068027297
**VID:PID:** `05e3:0606`  — note: `lsusb` misidentifies this as "D-Link DUB-H4"; actual DUB-H4 is `05e3:0608`
**USB descriptor:** iManufacturer: `ALCOR` · iProduct: `USB Hub 2.0`
**Ports:** 4 · **USB version:** 2.0

### Verdict: data-line disconnect only — NOT true VBUS switching

uhubctl marks all four units as `ppps`. Port state transitions (on→off→on) work correctly
and exit 0. However, VBUS (5V) stays live on the physical pin even when the port reports
`0000 off`. USB data lines are disconnected (ADB drops, device disappears from enumeration),
but connected watches continue charging.

**How the false positive happened:** Issue #664 was filed after testing on an *empty* port
— state transitions looked correct because there was no device to observe charging. Real VBUS
testing requires a device connected and actively checking the charging indicator. Issue closed
after discovering watches charge continuously regardless of port state.

**Useful for:** ADB operations (reboot, bootloader, flash) — need data lines only.
**Not useful for:** any battery/charge management.

**uhubctl topology:**
```
hub 1-1 [05e3:0606 ALCOR USB Hub 2.0, USB 2.00, 4 ports, ppps]
hub 1-2 [05e3:0606 ALCOR USB Hub 2.0, USB 2.00, 4 ports, ppps]
hub 1-3 [05e3:0606 ALCOR USB Hub 2.0, USB 2.00, 4 ports, ppps]
hub 1-6 [05e3:0606 ALCOR USB Hub 2.0, USB 2.00, 4 ports, ppps]
```

---

## Hub 2: Manhattan MondoHub II (28-port)
**Product:** Manhattan MondoHub II, 28-port USB 2.0 hub with physical per-port rocker switches
**Internal structure:** compound device — VIA Labs VL813 root cascading into 6 Huasheng sub-hubs

**VID:PIDs:**
- `2109:2813` — VIA Labs, Inc. USB2.0 Hub (root, 4 ports, **ppps**)
- `214b:7250` — Huasheng Electronics USB2.0 HUB (sub-hubs, 4 ports each, **ganged**)

**uhubctl topology:**
```
hub 1-3 [2109:2813 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 2: 0503 power highspeed enable connect
    [214b:7250 USB2.0 HUB, USB 2.00, 4 ports, ganged]  ← ×6 cascaded Huasheng sub-hubs
```

### What works: group switching via VIA Labs root

Switching port 2 of the VIA root (`1-3 -p 2`) powers the entire 28-port cascade on or off.
Devices downstream reappear on ADB after power restore.

### What does NOT work: individual port switching

The Huasheng sub-hubs are **ganged** — per-port switching is not possible in software.
The physical rocker switches on the MondoHub II front panel are mechanical only and
cannot be replicated via uhubctl.

### Verdict on VBUS: unclear / likely data-line only

The user observed that toggling individual ports (sub-hub level) "does nothing physically
and always stays powered." Since individual port switching doesn't work at all (ganged),
this is expected at that level.

Whether the *group* switch (VIA root port 2 off) actually cuts VBUS to all 28 ports
or only disconnects data lines was not conclusively tested before the issue was closed.
Given the pattern seen with the ALCOR hubs, treat as data-line only until proven otherwise
with a charging device test on the group switch.

**Note:** `2109:2813` is already in the uhubctl list (Aukey CB-C59, AmazonBasics U3-7HUB).
Issue #665 was filed to add Manhattan MondoHub II as another product on the same chip.

---

## uhubctl permissions on w541

Running as user `mo` (systemd user service, no sudo):
- sysfs path (`/sys/bus/usb/devices/.../disable`) requires root → Permission denied
- uhubctl falls back to libusb automatically, exit code 0, switching works
- Spurious warning was going to stderr → silenced by adding `-S` flag (commit f69379d)
- Full udev sysfs rules are in `udev/70-asteroid-docking-bay.rules` (05e3 line commented)

---

## Watch inventory (as of 2026-07-02)

| codename | serial           | hub | port | device              | notes |
|----------|-----------------|-----|------|---------------------|-------|
| sturgeon | MQB7N15C09000847 | 1-6 | 3    |                     | |
| catfish  | 720EX8C130737    | 1-3 | 3    | Mobvoi TicWatch Pro | |
| skipjack | 870AX0A150253    | 1-2 | 3    | Mobvoi TicWatch C2+ | |
| narwhal  | 901KPRW0013510   | 1-1 | 2    | LG Watch W7         | was moved to mondo hub for VBUS test, may need remap |
| sawfish  | TKQ7N17406001852 | 1-1 | 3    | HUAWEI LEO-BX9      | |
| beluga   | 100c0a32         | —   | —    |                     | in serials dict, not currently mapped |

---

## What to buy for true VBUS switching

Reference: https://github.com/mvp/uhubctl#compatible-usb-hubs

Known good:
- **Yepkit YKUSH** — explicit per-port VBUS control, confirmed working
- **Acroname USB 3.1** — gold standard, expensive
- Via Labs / Terminus USB 3 hubs — check list; some (not all) do true VBUS
