# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""Fastboot device polling, nightly download + verify, flash sequence."""

from __future__ import annotations

import glob
import os
import socket
import time
from pathlib import Path

from .transport import USB_SSH_IP
from .util import _run, log
from .usb import _sysfs_path_to_serial_map


# Cache: fastboot serial → product codename, populated on first getvar call.
_fastboot_products: dict[str, str] = {}


def _fastboot_devices() -> dict[str, str]:
    """Return {serial: state} for all fastboot-visible devices."""
    rc, out, _ = _run("fastboot devices", check=False)
    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0]] = parts[1]
    return result


_fb_list_cache: dict = {"ts": 0.0, "val": {}, "due": False}

# How stale the fastboot list may get with nothing happening on the bus. It is
# long because `fastboot devices` is a libusb sweep that races enumeration and
# can wedge these hubs (audit B9) — it must not run on a short timer.
FB_LIST_MAX_AGE = 60.0
# ...but when udev says the bus actually CHANGED, waiting out that ceiling is
# pointless: a watch entering the bootloader re-enumerates in 1-2s and then sat
# unrecognised for over a minute, because the status cache was busted promptly
# and simply re-read this stale list. An event can bring the next scan forward
# to this floor, which still bounds how often the sweep can run when a port is
# flapping or a cycle produces a burst.
FB_LIST_MIN_INTERVAL = 8.0


def invalidate_list_cache() -> None:
    """Note that the bus changed, so the list is worth re-reading sooner.

    Only marks it due — it never scans. The scan stays on the warmer thread,
    off the status and event paths, which is the property that keeps a noisy
    bus from turning into a storm of libusb sweeps.
    """
    _fb_list_cache["due"] = True


def _fb_list_due(now: "float | None" = None) -> bool:
    """Whether the warmer should re-read the fastboot list this tick."""
    age = (now or time.time()) - _fb_list_cache["ts"]
    if age >= FB_LIST_MAX_AGE:
        return True
    return bool(_fb_list_cache["due"]) and age >= FB_LIST_MIN_INTERVAL


def _fastboot_list() -> dict[str, str | None]:
    """
    Return {serial: sysfs_usb_path} for devices in fastboot mode, from a cache
    refreshed by the background warmer (_fastboot_poll) — never blocks on the
    slow `fastboot devices` scan. The flash/onboard flows poll fastboot directly.
    sysfs_usb_path may be None if the device's path cannot be determined.
    """
    return _fb_list_cache["val"]


def _fastboot_poll() -> None:
    """Refresh the fastboot device cache. `fastboot devices` is a slow USB scan
    (5-8s when nothing is in fastboot and it ignores the subprocess timeout), so
    ONLY the background warmer calls this — never the status path."""
    rc, out, _ = _run("fastboot devices", check=False, timeout=8)
    serials = {parts[0] for line in out.splitlines()
               if len(parts := line.split()) >= 2 and parts[1] == "fastboot"}
    if not serials:
        result: dict[str, str | None] = {}
    else:
        path_by_serial = {s: p for p, s in _sysfs_path_to_serial_map(serials).items()}
        result = {serial: path_by_serial.get(serial) for serial in serials}
    _fb_list_cache["ts"] = time.time()
    _fb_list_cache["val"] = result
    _fb_list_cache["due"] = False


def _fastboot_getvar_product(serial: str) -> str | None:
    """
    Read the product codename from a fastboot device via 'getvar product'.
    Result is cached in _fastboot_products so repeated status polls don't re-call fastboot.
    fastboot writes getvar output to stderr, not stdout.
    """
    if serial in _fastboot_products:
        return _fastboot_products[serial]
    _, out, err = _run(f"fastboot -s {serial} getvar product", check=False, timeout=5)
    for line in (err + "\n" + out).splitlines():
        if "product:" in line.lower():
            val = line.split("product:", 1)[1].strip()
            if val:
                _fastboot_products[serial] = val
                return val
    return None


def fastboot_getvar_all(serial: str) -> str:
    """The device's full `getvar all` dump as text — the bootloader's ground
    truth: identity (product, serialno, boardid), BT/WLAN MACs, bootloader
    version, unlock/secure state, live battery-voltage + battery-soc-ok, and
    the partition table. Readable even on a watch too flat to boot.

    fastboot writes getvar output to stderr, and prefixes each line with
    "(bootloader) " — both are normalised here."""
    _, out, err = _run(f"fastboot -s {serial} getvar all", check=False, timeout=20)
    lines = []
    for line in (err + "\n" + out).splitlines():
        line = line.replace("(bootloader) ", "", 1).rstrip()
        if line and not line.startswith(("Finished.", "Total time:")):
            lines.append(line)
    return "\n".join(lines)


def parse_getvar(text: str) -> dict:
    """`getvar all` as a dict. Pure — the report is already normalised text.

    Later lines win: fastboot prints some keys more than once and the last
    value is the settled one."""
    out = {}
    for line in (text or "").splitlines():
        # Split on the LAST ": ", not the first colon: fastboot keys contain
        # colons of their own ("partition-size:boot: 0x1600000"), and taking
        # the first would file that under "partition-size" with the real key
        # buried in the value.
        key, sep, value = line.rpartition(": ")
        if not sep:
            key, sep, value = line.rpartition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


def bootloader_unlocked(text: str) -> "bool | None":
    """Whether this watch's bootloader is unlocked, or None when it does not
    say so.

    This is the capability that decides whether a watch can be dumped cleanly
    at all: the debug-ramdisk method needs `fastboot boot`, and a locked
    bootloader refuses it outright ("not supported in locked device", nemo
    603KPVH000855, 2026-08-03). Knowing it per watch turns an hour of setup
    ending in a refusal into a question answered before starting.

    None rather than False when unknown — absence of the field is not evidence
    of a lock, and treating it as one would quietly write off dumpable watches.
    """
    kv = parse_getvar(text)
    for key in ("unlocked", "device-unlocked", "secure-boot"):
        raw = kv.get(key)
        if raw is None:
            continue
        low = raw.lower()
        if key == "secure-boot":          # inverted sense
            if low in ("yes", "true", "1"):
                return False
            if low in ("no", "false", "0"):
                return True
            continue
        if low in ("yes", "true", "1"):
            return True
        if low in ("no", "false", "0"):
            return False
    return None


def fastboot_serial_for_codename(codename: str,
                                 want_serial: "str | None" = None) -> "str | None":
    """A fastboot device already present that IS this watch, or None.

    Identity in the bootloader is `getvar product`, not the adb serial and not
    a hub seat — which is what lets an unseated watch be recognised at all. If
    the caller knows the fastboot serial it wants, that wins outright.
    """
    present = _fastboot_devices()
    if want_serial and want_serial in present:
        return want_serial
    for serial in present:
        if (_fastboot_getvar_product(serial) or "").strip().lower() == codename.strip().lower():
            return serial
    return None


def _wait_for_fastboot(known_serials: set[str], timeout: int = 30) -> str | None:
    """Wait for a fastboot serial not present in known_serials."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for serial in _fastboot_devices():
            if serial not in known_serials:
                return serial
        time.sleep(1)
    return None


def _detect_rndis(ip: str = USB_SSH_IP, timeout: float = 1.0) -> bool:
    """Return True if a watch answers SSH at `ip` (its RNDIS address). A
    bounded TCP connect to :22, not a ping: every caller goes on to use SSH,
    and the old 2s ping timeout, paid per SSH watch per status refresh, was
    the rig's constant 4.25s render (measured 2026-07-25 with two dead
    allocated addresses)."""
    try:
        with socket.create_connection((ip, 22), timeout=timeout):
            return True
    except OSError:
        return False


def rndis_links(base: str = "/sys/bus/usb/devices") -> "list[dict]":
    """Host-side RNDIS links, purely from sysfs — no network round-trips:
    [{'iface': host interface name, 'usb_path': device path, 'serial': …}].
    Each entry is a watch enumerated in SSH/developer mode; the USB serial
    identifies it even when its address is unknown or colliding. This is the
    ground truth of who is in SSH mode, independent of any IP guess."""
    links: list[dict] = []
    for netdir in glob.glob(f"{base}/*:*/net/*"):
        ifacedir = os.path.dirname(os.path.dirname(netdir))
        dev = os.path.basename(ifacedir).split(":")[0]
        try:
            with open(f"{base}/{dev}/serial") as f:
                serial = f.read().strip()
        except OSError:
            serial = None
        links.append({"iface": os.path.basename(netdir),
                      "usb_path": dev, "serial": serial})
    return links


def usb_net_link_for(serial: "str | None") -> "dict | None":
    """This watch's host-side USB network link, or None.

    rndis_links() is named for the only gadget this fleet used to offer, but it
    globs any USB interface that exposes a net device -- so it already finds a
    CDC-NCM link too. Newer kernels drop RNDIS, and the NCM gadget can run
    ALONGSIDE adb rather than replacing it, which breaks the assumption baked
    into every USB-mode path here: that a watch is in adb mode OR ssh mode,
    never both.
    """
    if not serial:
        return None
    return next((l for l in rndis_links() if l.get("serial") == serial), None)


def _route_winner_iface(ip: str = USB_SSH_IP) -> "str | None":
    """Which host interface the kernel would route `ip` over right now. With
    several RNDIS links whose watches all sit on the shared default address,
    only the winner's watch is reachable — the others are shadowed (the
    2026-07-25 collision). No privileges needed: `ip route get` only reads."""
    rc, out, _ = _run(f"ip route get {ip}", check=False)
    if rc != 0:
        return None
    parts = out.split()
    return parts[parts.index("dev") + 1] if "dev" in parts else None


def _pick_ssh_ip(serial, links, allocated_ip, winner_iface, probe) -> "str | None":
    """The address this watch actually answers on, or None. Pure — see tests.
    Order: no live RNDIS link for the serial → None without any probe (a dead
    allocated address must cost nothing); its allocated address if it answers;
    the shared default if this watch's link is the current route winner and
    the default answers. probe is an injected reachability callable."""
    link = next((l for l in links if l["serial"] == serial), None)
    if link is None:
        return None
    if allocated_ip and probe(allocated_ip):
        return allocated_ip
    if link["iface"] == winner_iface and probe(USB_SSH_IP):
        return USB_SSH_IP
    return None


def ssh_reach_ip(cfg: dict, serial: "str | None") -> "str | None":
    """Where to reach this watch over USB-SSH right now, or None. Wraps
    _pick_ssh_ip with live sysfs links, the kernel's route winner and a
    bounded probe."""
    if not serial:
        return None
    links = rndis_links()
    if not any(l["serial"] == serial for l in links):
        return None
    allocated = cfg.get("ssh_ips", {}).get(serial)
    return _pick_ssh_ip(serial, links, allocated, _route_winner_iface(),
                        _detect_rndis)


def _stray_ssh_to_realign(links, ssh_ips, winner_iface, probe) -> "str | None":
    """The serial of one stray SSH watch to peel off the shared default now,
    or None. Pure — see tests.

    A stray is a live RNDIS link whose watch does not answer on its allocated
    address (fresh boots land on the shared default, where several watches
    shadow each other). Only the route winner is reachable, so that is the
    one to act on; each peel frees the default for the next stray, and
    repeated calls converge without any host-side routing changes."""
    strays: list[dict] = []
    for link in links:
        serial = link["serial"]
        if not serial:
            continue
        allocated = ssh_ips.get(serial)
        if allocated and probe(allocated):
            continue                        # aligned: answers where assigned
        strays.append(link)
    if not strays:
        return None
    winner = next((l for l in strays if l["iface"] == winner_iface), None)
    if winner is None or not probe(USB_SSH_IP):
        return None
    return winner["serial"]


def _switch_ssh_to_adb(ip: str = "192.168.2.15") -> dict:
    """Switch a watch that enumerated in SSH/developer USB mode over to adb_mode.
    The watch is reachable at `ip` — its assigned SSH address (192.168.2.15 by
    default, or the per-watch address handed out by allocate_ssh_ip). The switch
    re-enumerates the USB gadget and drops the ssh session, so a non-zero return
    is expected — success is the watch reappearing on adb, which the caller
    waits for. ok=False when nothing was reachable there, or when the watch's
    usb-moded refused the switch (printed an error while the link stayed up)."""
    if not _detect_rndis(ip):
        return {"ok": False, "error": f"no SSH watch reachable at {ip}"}
    _clear_ssh_known_hosts(ip)   # a fresh flash rotates the host key
    _, out, err = _run(
        "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=6 "
        f"root@{ip} usb_moded_util -s adb_mode", check=False, timeout=15)
    if _usb_moded_switch_failed(out, err):
        return {"ok": False,
                "error": "usb-moded refused the switch on this watch (its "
                         "service may be down)"}
    return {"ok": True}


def _usb_moded_switch_failed(out: str, err: str) -> bool:
    """usb_moded_util prints its failure on stdout while still exiting 0; a
    switch that took would have dropped the link before any reply. So the error
    text, not the return code, is the signal that the mode did not change."""
    blob = f"{out} {err}".lower()
    return "not processed" in blob or "an error occured" in blob \
        or "an error occurred" in blob


# Which files a release actually ships, read from its own SHA512SUMS.
#
# The names are not stable across channels, and hardcoding the newest pair
# silently broke the older ones:
#
#   1.0        zImage-dtb-<cn>.fastboot   asteroid-image-<cn>.ext4
#   2.0        zImage-dtb-<cn>.fastboot   asteroid-image-<cn>.rootfs.ext4
#   2.1        BOTH boot names            asteroid-image-<cn>.rootfs.ext4
#   nightlies  asteroid-<cn>-boot.img     asteroid-image-<cn>.rootfs.ext4
#              (the nightlies folder IS 2.2 in development)
#
# a-d-b asked for the nightly pair everywhere, so flashing 2.0 404'd on the
# boot image and 1.0 404'd on both — the channel selector offered releases it
# could not actually fetch. SHA512SUMS is downloaded first anyway and lists
# precisely what a release ships, so it is the authority.
#
# The boot preference is zImage FIRST, which looks backwards and is not. The
# new name applies from 2.2 onward, and the nightlies folder IS 2.2 (moWerk);
# 2.1 merely published the new name early, beside the old one. So zImage is the
# artefact every RELEASED channel carries: preferring it picks the intended
# image on 1.0/2.0/2.1, and falls through to the new name on the nightlies,
# the one place zImage is absent.
_BOOT_NAMES = ("zImage-dtb-{cn}.fastboot", "asteroid-{cn}-boot.img")
_IMG_NAMES = ("asteroid-image-{cn}.rootfs.ext4", "asteroid-image-{cn}.ext4")


def _release_filenames(codename: str, sha_file: Path) -> tuple[str, str]:
    """(boot_name, img_name) for this release, from its SHA512SUMS."""
    try:
        listed = {ln.split()[1].lstrip("*./")
                  for ln in sha_file.read_text().splitlines()
                  if len(ln.split()) >= 2}
    except OSError:
        listed = set()

    def pick(cands, what):
        for c in cands:
            name = c.format(cn=codename)
            if name in listed:
                return name
        # No manifest (offline, or a 404 left an empty file): fall back to the
        # newest name so behaviour is unchanged from before this existed.
        if listed:
            raise RuntimeError(
                f"{codename}: this release lists no {what} image "
                f"(looked for {', '.join(c.format(cn=codename) for c in cands)})")
        return cands[0].format(cn=codename)

    return pick(_BOOT_NAMES, "boot"), pick(_IMG_NAMES, "rootfs")


def _download_nightly(codename: str, download_dir: Path, nightly_url: str, force: bool = False) -> tuple[Path, Path]:
    """
    Download nightly images for codename into download_dir, verify SHA512.
    Skips files that already exist and pass verification (unless force=True).
    Returns (boot_file, img_file).
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    sha_name  = "SHA512SUMS"
    sha_file  = download_dir / sha_name
    base_url  = f"{nightly_url}/{codename}"

    # Always fetch SHA512SUMS so we detect when a new nightly has landed — and,
    # since it lists exactly what this release ships, so the file NAMES can be
    # read from it instead of assumed.
    log.info("%s: fetching SHA512SUMS…", codename)
    _run(f"wget -q '{base_url}/{sha_name}' -O '{sha_file}'")
    boot_name, img_name = _release_filenames(codename, sha_file)
    boot_file = download_dir / boot_name
    img_file  = download_dir / img_name

    for url, local_file in [(f"{base_url}/{boot_name}", boot_file),
                            (f"{base_url}/{img_name}",  img_file)]:
        if local_file.exists() and not force:
            rc, _, _ = _run(
                f"cd '{download_dir}' && grep '{local_file.name}' SHA512SUMS | sha512sum --check --quiet",
                check=False,
            )
            if rc == 0:
                log.info("%s: cached and verified — skipping", local_file.name)
                continue
            log.info("%s: cached but checksum mismatch — re-downloading", local_file.name)
        log.info("%s: downloading %s…", codename, local_file.name)
        _run(f"wget -q --show-progress '{url}' -O '{local_file}'")

    # Final verification of both files.
    rc, _, err = _run(
        f"cd '{download_dir}' && grep -E '{boot_name}|{img_name}' SHA512SUMS | sha512sum --check --quiet",
        check=False,
    )
    if rc != 0:
        raise RuntimeError(f"SHA512 verification failed for {codename}: {err}")
    log.info("%s: images verified OK", codename)
    return boot_file, img_file


def _flash_watch(boot_file: Path, img_file: Path, fb_serial: str | None, dry_run: bool = False):
    """
    Flash a watch already in fastboot mode.
    Permanent install: flash userdata + boot, then fastboot continue.
    """
    sflag = f"-s {fb_serial}" if fb_serial else ""

    def fb(subcmd: str, fatal: bool = True):
        cmd = f"fastboot {sflag} {subcmd}".strip()
        if dry_run:
            print(f"    [dry-run] {cmd}")
            return
        rc, _, err = _run(cmd, check=False)
        if rc != 0:
            if fatal:
                raise RuntimeError(f"{cmd}: {err.strip() or f'rc={rc}'}")
            log.info("%s: non-fatal (%s)", cmd, err.strip() or f"rc={rc}")

    # No `oem unlock` here: a watch already running AsteroidOS is unlocked by
    # definition (a locked bootloader wouldn't boot it), so unlocking on every
    # reflash is a pointless — and warranty-voiding — side effect. Unlocking a
    # still-locked WearOS watch is a deliberate, warned action of its own; see
    # the bootloader-unlock toggle in the roadmap (gated on WearOS detection).
    fb(f"flash userdata '{img_file}'")
    fb(f"flash boot '{boot_file}'")
    fb("continue")


def _clear_ssh_known_hosts(ip: str = "192.168.2.15"):
    """Remove stale SSH host keys left by the previous AsteroidOS install."""
    _run("ssh-keygen -R watch", check=False)
    _run(f"ssh-keygen -R {ip}", check=False)


