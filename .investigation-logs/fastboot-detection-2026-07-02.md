# Fastboot detection + auto-refresh investigation (2026-07-02)

## Problem
Watches entering deep-discharge bootloop are put into fastboot/bootloader mode to precharge.
The web UI had no way to see this state — it only queries `adb devices`.

## Design decisions

### How to find which port a fastboot device is on
`fastboot devices` returns serial numbers only, not USB topology. We use sysfs:
- `/sys/bus/usb/devices/<path>/serial` files contain the USB serial number
- The directory name IS the USB path, e.g. `1-2.3` = hub `1-2` port 3, `1-2.4.1` = hub `1-2.4` port 1
- So: scan sysfs for the fastboot serial → get path → correlate to hub+port

### Known vs unknown fastboot devices
- KNOWN (serial in config): serial → codename → port → update that mapped row with "fastboot" state
- UNKNOWN (serial not in config): use sysfs path to find which empty port it's on;
  call `fastboot -s <serial> getvar product` to get the codename for display.
  Unknown watches do NOT sort up — they stay in the empty port section.

### Caching getvar product
`fastboot getvar product` is called once per unknown serial and cached in
`_fastboot_products` dict. Prevents 1-2s delay per unknown watch on every 15s poll.

### Button visibility for fastboot state
- Power toggle: YES (needed to cut VBUS to break bootloop)
- Cycle: YES (power cycle to escape bootloop)
- Halt submenu: NO (OS not running)
- Charge button: NO (ADB not available; VBUS already on so charging is automatic)
- Flash nightly: YES (this is exactly the use case for pre-staging in fastboot)

### _run timeout
Added `timeout` parameter to `_run()`. Used with `timeout=5` for fastboot calls
so a missing/slow fastboot daemon doesn't block the 15s status poll.

## Files changed
- `bin/asteroid-docking-bay`: _run timeout, _sysfs_usb_path, _fastboot_list,
  _fastboot_getvar_product, _fastboot_products cache, _web_status_data updates,
  _WEB_TEMPLATE JS/HTML updates
