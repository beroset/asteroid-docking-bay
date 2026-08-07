# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Restore a watch to its stock ROM from a full-disk dump.

Users already do this by hand, brute-force, and arrive in chat afterwards —
so the risk of offering it is measured against THAT baseline, not against an
imaginary one where nobody tries. What a tool can add is the checking a person
cannot do by eye: that the image belongs to this model, and that the partitions
it writes are the ones that may safely be written.

Three rules carry the whole design, all of them learned from the manufacturer's
own flashing manifest (see the RAG, platform_and_build stock_rom_dump_and_restore):

1. A dump is trusted for what it CONTAINS, never for where it came from.
2. `userdata` is restored EMPTY, never from the capture. A populated userdata is
   encrypted against key material a stock restore erases, so writing it back
   boots to the vendor's spinner and never leaves it — the reason a beluga had
   never been successfully restored here before 2026-08-03.
3. Per-device state (calibration, radio NV, identity) is never taken from a
   dump of a DIFFERENT unit. It is not recoverable and nothing reports an error
   when it is wrong.
"""

from __future__ import annotations

import re
import struct
import subprocess
from pathlib import Path

from .util import log

GPT_SIG = b"EFI PART"
SECTOR = 512


class Partition:
    """One GPT entry. Sizes are derived, never stored twice."""

    __slots__ = ("name", "first_lba", "last_lba")

    def __init__(self, name: str, first_lba: int, last_lba: int):
        self.name, self.first_lba, self.last_lba = name, first_lba, last_lba

    @property
    def sectors(self) -> int:
        return self.last_lba - self.first_lba + 1

    @property
    def bytes(self) -> int:
        return self.sectors * SECTOR

    @property
    def offset(self) -> int:
        return self.first_lba * SECTOR

    def __eq__(self, other):
        return (isinstance(other, Partition) and self.name == other.name
                and self.first_lba == other.first_lba
                and self.last_lba == other.last_lba)

    def __repr__(self):
        return f"<{self.name} {self.first_lba}-{self.last_lba}>"


def parse_gpt(head: bytes) -> list[Partition]:
    """Partitions from the first sectors of a disk (or a dump of one).

    Reads the primary header at LBA 1 and the entry array it points at, so a
    disk whose entries do not start at the usual LBA 2 still parses. Returns []
    when there is no GPT rather than raising: callers treat "not a disk image"
    as a verification failure, not as a crash.
    """
    if len(head) < 2 * SECTOR or head[SECTOR:SECTOR + 8] != GPT_SIG:
        return []
    hdr = head[SECTOR:SECTOR + 92]
    ent_lba, n_ent, ent_sz = struct.unpack("<QII", hdr[72:88])
    if not (0 < ent_sz <= 4096) or n_ent > 4096:
        return []
    out, base = [], ent_lba * SECTOR
    for i in range(n_ent):
        off = base + i * ent_sz
        e = head[off:off + ent_sz]
        if len(e) < 56 or e[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack("<QQ", e[32:48])
        name = e[56:128].decode("utf-16-le", "ignore").rstrip("\x00")
        if name:
            out.append(Partition(name, first, last))
    return out


# --- what may be written, and what may never be -----------------------------
#
# Straight out of the vendor's rawprogram manifest. Grouping by CLASS rather
# than listing partitions per operation is what keeps the dangerous ones from
# being swept into a convenient "restore everything" loop.

FIRMWARE = ("system", "vendor", "boot", "recovery", "cache", "oem",
            "splash", "LOGO", "modem", "opporeserve1", "opporeserve2",
            "sbl1", "sbl1bak", "aboot", "abootbak", "rpm", "rpmbak",
            "tz", "tzbak", "cmnlib", "cmnlibbak", "keymaster", "keymasterbak")

# Regenerated at first boot. Erasing them is how the vendor's own flow leaves
# them, and none of it identifies the device.
SAFE_ERASE = ("misc", "keystore", "config", "opporeserve3", "pad", "fsc",
              "ssd", "DDR")

# Calibration, radio NV, identity. The vendor erases some of these on a factory
# line, where the device is about to be re-provisioned anyway. We are not a
# factory line: on a watch in the field this data is irreplaceable and its loss
# is silent, so a-d-b neither writes nor erases it.
PER_DEVICE = ("persist", "oppodycnvbk", "oppostanvbk", "modemst1", "modemst2",
              "fsg", "sec", "devinfo")

USERDATA = "userdata"

# The subset proven to work on beluga by the community (MagneWater on reddit,
# confirmed still working 2026-06 by xuv). Deliberately smaller than FIRMWARE:
# it never touches the bootloader chain, so a failure cannot cost fastboot.
BELUGA_STAGE1 = ("boot", "system", "vendor", "recovery", "cache")


def classify(name: str) -> str:
    if name == USERDATA:
        return "userdata"
    if name in PER_DEVICE:
        return "per_device"
    if name in FIRMWARE:
        return "firmware"
    if name in SAFE_ERASE:
        return "safe_erase"
    return "unknown"


def gpt_gate(dump: list[Partition], live: list[Partition]) -> tuple[bool, str]:
    """Cheap first gate: the dump's layout must match the watch's own.

    Everything up to `userdata` must agree on name AND position; `userdata` is
    last and grows to fill the disk, so it is expected to differ between units
    with different eMMC sizes and is excluded.

    This gate is NECESSARY BUT NOT SUFFICIENT and must never be the only check:
    beluga and belugaxl have byte-identical partition tables. It rejects a
    genuinely different device; it cannot tell two variants of one design apart.
    Use fingerprint_gate for that.
    """
    if not dump:
        return False, "no GPT in the dump — not a full-disk image"
    if not live:
        return False, "could not read the watch's own GPT"
    d = [p for p in dump if p.name != USERDATA]
    l = [p for p in live if p.name != USERDATA]
    if len(d) != len(l):
        return False, f"partition count differs: dump {len(d)}, watch {len(l)}"
    for a, b in zip(d, l):
        if a != b:
            return False, (f"layout differs at {a.name!r}: dump "
                           f"{a.first_lba}-{a.last_lba}, watch "
                           f"{b.first_lba}-{b.last_lba}")
    return True, f"{len(d)} partitions match exactly"


_FINGERPRINT = re.compile(rb"ro\.build\.fingerprint=([!-~]{1,120})")
_DEVICE = re.compile(rb"ro\.product\.(?:device|name)=([!-~]{1,40})")


def fingerprints(blob: bytes) -> set[str]:
    """Every build fingerprint in a raw partition image.

    Read straight out of the bytes rather than by mounting: the check must work
    on a dump file on a host that cannot mount the filesystem, and build.prop is
    plain text inside the image.
    """
    return {m.group(1).decode("ascii", "ignore")
            for m in _FINGERPRINT.finditer(blob)}


def devices(blob: bytes) -> set[str]:
    return {m.group(1).decode("ascii", "ignore")
            for m in _DEVICE.finditer(blob)}


def fingerprint_gate(dump_blob: bytes, expect_device: str) -> tuple[bool, str]:
    """The gate that actually establishes compatibility.

    A shared image may legitimately carry SEVERAL device names — beluga and
    belugaxl ship one system image naming both — so this asks whether the
    target is among them, not whether it is the only one.
    """
    found = devices(dump_blob)
    if not found:
        return False, "no ro.product.device in the image — cannot identify it"
    if expect_device not in found:
        return False, (f"image is for {sorted(found)}, "
                       f"but this watch reports {expect_device!r}")
    return True, f"image names {sorted(found)}, includes {expect_device!r}"


def restore_plan(parts: list[Partition], names: "tuple[str, ...]",
                 erase_safe: bool = False) -> list[dict]:
    """The ordered list of actions, with every one justified by its class.

    Refuses to build a plan that touches per-device state at all. That is a hard
    stop rather than a warning: the caller cannot opt in, because the damage is
    silent and unrecoverable and no UI affordance should exist for it.
    """
    by_name = {p.name: p for p in parts}
    plan: list[dict] = []
    for n in names:
        cls = classify(n)
        if cls == "per_device":
            raise ValueError(f"refusing to restore per-device partition {n!r}")
        if cls != "firmware":
            raise ValueError(f"{n!r} is {cls}, not firmware — not restorable")
        if n not in by_name:
            raise ValueError(f"{n!r} is not in this disk layout")
        plan.append({"action": "flash", "partition": n, "part": by_name[n]})
    if erase_safe:
        for n in SAFE_ERASE:
            if n in by_name:
                plan.append({"action": "erase", "partition": n})
    plan.append({"action": "flash_empty", "partition": USERDATA})
    return plan


# --- host-side execution ----------------------------------------------------

def live_gpt(serial: str) -> list[Partition]:
    """The watch's own partition table, over adb. exec-out, not shell: shell
    mangles binary on some hosts and a corrupted GPT read would fail the gate
    for the wrong reason."""
    p = subprocess.run(["adb", "-s", serial, "exec-out",
                        "dd if=/dev/block/mmcblk0 bs=512 count=64 2>/dev/null"],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    return parse_gpt(p.stdout)


def extract(dump_cmd: str, part: Partition, dest: str, extra_mb: int = 0) -> int:
    """Carve one partition out of a (possibly compressed) full-disk dump.

    Takes a shell command that STREAMS the image rather than a path, so a
    .tar.gz, a .zst and a raw .img are all the same to the caller and no 7.65 GB
    temporary copy is ever written.
    """
    skip = part.offset // (1024 * 1024)
    count = -(-part.bytes // (1024 * 1024)) + extra_mb
    if part.offset % (1024 * 1024):
        raise ValueError(f"{part.name} is not MiB-aligned; needs a byte-exact carve")
    cmd = (f"{dump_cmd} | dd bs=1M skip={skip} count={count} "
           f"iflag=fullblock 2>/dev/null > {dest}")
    subprocess.run(["bash", "-c", cmd], check=True,
                   stdin=subprocess.DEVNULL, timeout=3600)
    log.info("stockrom: extracted %s (%d MiB) -> %s", part.name, count, dest)
    return count


# --- taking a dump ----------------------------------------------------------

DUMP_ROOT = Path.home() / ".local/share/asteroid-docking-bay/dumps"


def dump_command(serial: str, ip: "str | None", dest: str) -> str:
    """The shell that streams a watch's whole disk into `dest`.

    Built as a string rather than run piecemeal because the copy has to be ONE
    pipeline: buffering 4 GB in the host process to hand it along would be
    slower and would turn a link hiccup into a lost dump instead of a short
    file. `ip` selects the link — SSH when the watch is in developer mode,
    otherwise adb exec-out, which is the binary-safe channel (`adb shell`
    mangles bytes on some hosts).
    """
    if ip:
        return (f'ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 '
                f'root@{ip} "dd if=/dev/mmcblk0" | dd of={dest} bs=4096')
    return (f'adb -s {serial} exec-out "dd if=/dev/mmcblk0 2>/dev/null" '
            f'> {dest}')


def disk_bytes(watch) -> "int | None":
    """The watch's own idea of how big its disk is, in bytes.

    Asked BEFORE the copy so a truncated result can be recognised as one. A
    short dump is the failure mode that hides best: it looks like a file.
    """
    rc, out, _ = watch.t.shell("cat /sys/class/block/mmcblk0/size", timeout=15)
    if rc != 0:
        return None
    try:
        return int(out.strip()) * SECTOR
    except ValueError:
        return None


def write_manifest(path: Path, **fields) -> None:
    """Record what this dump IS, beside it.

    A filesystem timestamp is not a dump date — it is reset by any copy, which
    cost several exchanges of archaeology on 2026-08-03 to work out that an
    archive dated June had in fact been taken years earlier. The method matters
    too: a runtime capture is trustworthy per partition and never for userdata,
    and that distinction is lost the moment the file leaves this machine.
    """
    lines = [f"{k}: {v}" for k, v in fields.items() if v is not None]
    path.write_text("\n".join(lines) + "\n")
