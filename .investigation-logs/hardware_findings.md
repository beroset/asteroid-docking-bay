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

### Verdict on VBUS: CONFIRMED data-line only (2026-07-02)

Both levels tested:
- Individual ports (Huasheng sub-hubs, ganged): no effect at all — no switching possible
- Group switch (VIA Labs root, `uhubctl -l 1-3 -p 2 -a off`): data-line disconnect only —
  VBUS stays live, watch continues charging

The VIA Labs `2109:2813` chip in this product does NOT cut VBUS despite being marked ppps.
Same failure mode as the ALCOR hubs.

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

Lenovo DK-1523 dock layout (current):

| codename | serial           | hub   | port | device              | notes |
|----------|-----------------|-------|------|---------------------|-------|
| skipjack | 8605X96200684    | 1-2   | 2    | Mobvoi TicWatch C2+ | second skipjack unit |
| narwhal  | 901KPRW0013510   | 1-2   | 3    | LG Watch W7         | |
| catfish  | 720EX8C130737    | 1-2.4 | 1    | Mobvoi TicWatch Pro | |
| sawfish  | TKQ7N17406001852 | 1-2.4 | 2    | Huawei Watch 2 (LEO-BX9) | |
| lenok    | 411KPCA0121867   | 1-2.4 | 3    | LG G Watch R        | NEW — identified from uhubctl |
| skipjack | 870AX0A150253    | 1-2.4 | 4    | Mobvoi TicWatch C2+ | |

Not currently on dock:

| codename | serial           | device              | notes |
|----------|-----------------|---------------------|-------|
| sturgeon | MQB7N15C09000847 | Huawei Watch        | was on old ALCOR hubs |
| beluga   | 100c0a32         | OPPO Watch          | in serials dict, unmapped |

## Full AsteroidOS device codename reference

Provided by maintainer 2026-07-02. Use this to identify devices by codename from USB descriptors or ADB.

| codename    | device                     | stars | notes |
|-------------|----------------------------|-------|-------|
| beluga      | OPPO Watch                 | 5     | |
| catfish     | TicWatch Pro 2018/20       | 5     | |
| bass        | LG Watch Urbane            | 4     | |
| carp        | Moto 360 2015              | 4     | |
| dory        | LG G Watch                 | 4     | |
| lenok       | LG G Watch R               | 4     | |
| narwhal     | LG Watch W7                | 4     | |
| smelt       | Moto 360 2015              | 4     | |
| sparrow     | Asus Zenwatch 2            | 4     | |
| sturgeon    | Huawei Watch               | 4     | |
| anthias     | Asus Zenwatch 1            | 3     | |
| pike        | Polar M600                 | 3     | |
| ray/firefish| Fossil Gen 4               | 3     | |
| rubyfish    | TicWatch Pro 3             | 3     | |
| sawfish     | Huawei Watch 2             | 3     | |
| skipjack    | TicWatch C2/C2+            | 3     | |
| hoki        | Fossil Gen 6               | 2     | |
| mooneye     | TicWatch E & S             | 2     | |
| swift       | Asus Zenwatch 3            | 2     | |
| triggerfish | Fossil Gen 5               | 2     | |
| koi         | Casio WSD-F10/F20          | 3     | experimental |
| nemo        | LG Watch Urbane 2nd Ed.    | 2     | experimental — active porting target |
| minnow      | Moto 360 2014              | 1     | experimental |
| rinato      | Samsung Gear 2             | 1     | experimental |
| sprat       | Samsung Gear Live          | 1     | experimental |
| tetra       | Sony Smartwatch 3          | —     | experimental |
| aurora      | (TBD)                      | —     | not published, expected on hub during porting |
| eos         | (TBD)                      | —     | not published, expected on hub during porting |
| lucky7      | (TBD)                      | —     | not published, expected on hub during porting |
| r11         | (TBD)                      | —     | not published, expected on hub during porting |

---

## Found / powered up

### Lenovo ThinkPad USB 3.0 Ultra Dock DK-1523 — CONFIRMED TRUE VBUS SWITCHING ✓
Found in cellar, powered with a recovered slim tip Lenovo charger. Connected to w541 on
a USB 3.0 port (Bus 003, 5000M confirmed).

**VID:PIDs:**
- `17ef:1014` — Lenovo TP USB 3.0 Ultra Dock (root hub, 4 ports, **ppps**)
- `17ef:1015` — Lenovo TP USB 3.0 Ultra Dock (sub-hub, 4 ports, **ppps**)
- `17e9:4340` — DisplayLink ThinkPad USB 3.0 Ultra Dock (video, not relevant for hubs)

**Topology:**
```
1-2 / 3-2  [17ef:1014, ppps, 4 ports]
  Port 1: DisplayLink video chip
  Port 2: free (watch here → 1-2 port 2)
  Port 3: free
  Port 4: sub-hub →
           1-2.4 / 3-2.4  [17ef:1015, ppps, 4 ports — all free]
```
6 free ports total (2 on root + 4 on sub). Watches enumerate on USB 2.0 companion
buses (1-2 / 1-2.4) since they are USB 2.0 devices.

**VBUS test (2026-07-02, PASSED):**
Narwhal (LG Watch W7, USB-C) was plugged in via a cable with a blue LED power indicator.
`uhubctl -l 1-2 -p 2 -a off` — blue LED went off instantly. VBUS confirmed cut.
First hub in the fleet to do true per-port VBUS switching.

---

## Ordered / incoming hardware

### RSHTECH A-16 (16-port) — arriving ~2026-07-06
Explicitly listed on the uhubctl compatible devices list as fully supported with per-port
PPPS. First hub in the fleet expected to do true VBUS switching. Once it arrives:
1. Connect to w541, confirm uhubctl detects it and lists ppps
2. Test VBUS cut: switch a port off, confirm watch stops charging
3. `asteroid-docking-bay map` to assign watches to ports
4. Re-enable charge timer and verify battery management actually works

---

## What to buy for true VBUS switching

Reference: https://github.com/mvp/uhubctl#compatible-usb-hubs

Known good:
- **RSHTECH A-16** — ordered 2026-07-02, explicitly on uhubctl supported list (incoming)
- **Yepkit YKUSH** — explicit per-port VBUS control, confirmed working
- **Acroname USB 3.1** — gold standard, expensive
- Via Labs / Terminus USB 3 hubs — check list; some (not all) do true VBUS
