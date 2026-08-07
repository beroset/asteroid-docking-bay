# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Stock-ROM restore: the checks that stand between a dump and a flashed watch.

These matter more than most tests in this repo, because the failure they guard
against is silent — a wrong-model image or a restored calibration partition
does not raise, it just leaves a watch subtly or permanently wrong.
"""

import struct

import pytest

from asteroid_docking_bay import stockrom as sr


# --- fixtures ---------------------------------------------------------------

def _gpt(parts, entries_lba=2, disk_sectors=14942208):
    """A minimal but real GPT: header at LBA1, entries where the header says."""
    head = bytearray(SECTORS := 64 * 512)
    hdr = bytearray(92)
    hdr[0:8] = b"EFI PART"
    struct.pack_into("<QQ", hdr, 40, 34, disk_sectors - 34)
    struct.pack_into("<QII", hdr, 72, entries_lba, len(parts), 128)
    head[512:512 + 92] = hdr
    for i, (name, first, last) in enumerate(parts):
        e = bytearray(128)
        e[0:16] = b"\x01" * 16                      # non-zero type GUID
        struct.pack_into("<QQ", e, 32, first, last)
        e[56:56 + len(name) * 2] = name.encode("utf-16-le")
        off = entries_lba * 512 + i * 128
        head[off:off + 128] = e
    return bytes(head)


BELUGA = [("oppodycnvbk", 34, 20513), ("persist", 475136, 540671),
          ("boot", 606208, 671743), ("system", 671744, 4767743),
          ("vendor", 4767744, 5791743), ("userdata", 6127616, 14942174)]


# --- GPT parsing ------------------------------------------------------------

def test_parse_gpt_reads_a_real_table_and_survives_junk():
    parts = sr.parse_gpt(_gpt(BELUGA))
    assert [p.name for p in parts] == [n for n, _, _ in BELUGA]
    sysp = [p for p in parts if p.name == "system"][0]
    assert sysp.sectors == 4096000 and sysp.bytes == 4096000 * 512
    assert sysp.offset == 671744 * 512

    # Not a disk image at all, and a truncated one: [] rather than an exception,
    # because "unverifiable" must reach the caller as a failed gate.
    assert sr.parse_gpt(b"\x00" * 4096) == []
    assert sr.parse_gpt(b"") == []
    assert sr.parse_gpt(_gpt(BELUGA)[:600]) == []


def test_parse_gpt_follows_the_header_rather_than_assuming_lba2():
    """The entry array location is a header field. Hard-coding LBA 2 works on
    most disks and silently returns nothing on the ones it doesn't."""
    assert [p.name for p in sr.parse_gpt(_gpt(BELUGA, entries_lba=6))] == \
           [n for n, _, _ in BELUGA]


def test_parse_gpt_rejects_absurd_entry_geometry():
    """A corrupt header must not send the parser reading gigabytes."""
    head = bytearray(_gpt(BELUGA))
    struct.pack_into("<QII", head, 512 + 72, 2, 999999, 128)
    assert sr.parse_gpt(bytes(head)) == []


# --- classification ---------------------------------------------------------

def test_every_beluga_partition_classifies_and_the_dangerous_ones_are_flagged():
    assert sr.classify("system") == "firmware"
    assert sr.classify("userdata") == "userdata"
    assert sr.classify("misc") == "safe_erase"
    for n in ("persist", "oppodycnvbk", "oppostanvbk",
              "modemst1", "modemst2", "fsg", "sec", "devinfo"):
        assert sr.classify(n) == "per_device", f"{n} lost its protection"
    assert sr.classify("something-new") == "unknown"


def test_no_partition_is_both_firmware_and_per_device():
    """An overlap would let a per-device partition reach a flash plan through
    the firmware door."""
    assert not (set(sr.FIRMWARE) & set(sr.PER_DEVICE))
    assert not (set(sr.SAFE_ERASE) & set(sr.PER_DEVICE))


# --- the GPT gate -----------------------------------------------------------

def test_gpt_gate_accepts_identical_layouts_and_ignores_userdata_size():
    """Two units of one model with different eMMC sizes differ ONLY in the last
    partition, because it grows to fill the disk. Failing them would reject
    every legitimate cross-unit restore."""
    dump = sr.parse_gpt(_gpt(BELUGA))
    grown = [(n, f, l) for n, f, l in BELUGA[:-1]] + \
            [("userdata", 6127616, 30777310)]
    live = sr.parse_gpt(_gpt(grown, disk_sectors=30777344))
    ok, why = sr.gpt_gate(dump, live)
    assert ok, why


def test_gpt_gate_rejects_a_different_device_and_an_unreadable_dump():
    dump = sr.parse_gpt(_gpt(BELUGA))
    moved = [("oppodycnvbk", 34, 20513), ("persist", 475136, 540671),
             ("boot", 600000, 665535), ("system", 671744, 4767743),
             ("vendor", 4767744, 5791743), ("userdata", 6127616, 14942174)]
    ok, why = sr.gpt_gate(dump, sr.parse_gpt(_gpt(moved)))
    assert not ok and "boot" in why

    fewer = sr.parse_gpt(_gpt(BELUGA[:3]))
    assert not sr.gpt_gate(dump, fewer)[0]
    assert not sr.gpt_gate([], dump)[0]
    assert not sr.gpt_gate(dump, [])[0]


# --- the fingerprint gate ---------------------------------------------------

BOTH = (b"junk\x00ro.build.fingerprint=OPPO/beluga/beluga:9/PXDR/01:user/rel\x00"
        b"ro.build.fingerprint=OPPO/belugaxl/beluga:9/PXDR/01:user/rel\x00"
        b"ro.product.device=beluga\x00ro.product.name=belugaxl\x00")


def test_fingerprint_gate_accepts_a_shared_image_naming_several_devices():
    """beluga and belugaxl ship ONE system image naming both. Demanding a single
    device name would reject the vendor's own image."""
    assert sr.devices(BOTH) == {"beluga", "belugaxl"}
    assert len(sr.fingerprints(BOTH)) == 2
    assert sr.fingerprint_gate(BOTH, "beluga")[0]
    assert sr.fingerprint_gate(BOTH, "belugaxl")[0]


def test_fingerprint_gate_rejects_a_foreign_image_and_an_anonymous_one():
    ok, why = sr.fingerprint_gate(BOTH, "sturgeon")
    assert not ok and "sturgeon" in why
    ok, why = sr.fingerprint_gate(b"\x00" * 5000, "beluga")
    assert not ok, "an image with no identity at all was accepted"


# --- the plan ---------------------------------------------------------------

def test_plan_flashes_firmware_and_always_ends_with_an_empty_userdata():
    """Restoring a captured userdata is THE failure that made beluga restores
    impossible here for years, so an empty one is not an option the caller
    passes — it is the only ending the plan has."""
    parts = sr.parse_gpt(_gpt(BELUGA))
    plan = sr.restore_plan(parts, ("boot", "system", "vendor"))
    assert [(a["action"], a["partition"]) for a in plan] == [
        ("flash", "boot"), ("flash", "system"), ("flash", "vendor"),
        ("flash_empty", "userdata")]
    assert not any(a["action"] == "flash" and a["partition"] == "userdata"
                   for a in plan), "would have written a captured userdata back"


def test_plan_refuses_per_device_partitions_outright():
    """A hard refusal, not a warning: the damage is silent and unrecoverable,
    so there must be no way for a caller to ask for it."""
    parts = sr.parse_gpt(_gpt(BELUGA))
    for bad in ("persist", "oppodycnvbk"):
        with pytest.raises(ValueError, match="per-device"):
            sr.restore_plan(parts, ("boot", bad))


def test_plan_refuses_partitions_absent_from_this_layout():
    parts = sr.parse_gpt(_gpt(BELUGA))
    with pytest.raises(ValueError, match="not in this disk layout"):
        sr.restore_plan(parts, ("boot", "oem"))


def test_safe_erase_never_reaches_the_per_device_row():
    """The vendor erases calibration on a factory line because the device is
    about to be re-provisioned. A watch in the field is not that case."""
    parts = sr.parse_gpt(_gpt(BELUGA + [("misc", 347136, 349183),
                                        ("modemst1", 340992, 344063)]))
    plan = sr.restore_plan(parts, ("boot",), erase_safe=True)
    erased = {a["partition"] for a in plan if a["action"] == "erase"}
    assert "misc" in erased
    assert not (erased & set(sr.PER_DEVICE)), f"would erase device identity: {erased}"


def test_extract_refuses_an_unaligned_partition():
    """The carve is MiB-based for speed; a partition that is not MiB-aligned
    would be silently written from the wrong offset."""
    with pytest.raises(ValueError, match="aligned"):
        sr.extract("cat x", sr.Partition("odd", 1, 100), "/dev/null")


# ── taking a dump ────────────────────────────────────────────────────────────

def test_dump_streams_over_whichever_link_is_up():
    """One pipeline, not a buffered relay: holding 4 GB in the host process to
    hand it along would be slower and would turn a link hiccup into a lost dump
    rather than a short file. adb uses exec-out, the binary-safe channel —
    `adb shell` mangles bytes on some hosts."""
    ssh = sr.dump_command("S1", "192.168.13.39", "/tmp/x.img")
    assert "ssh" in ssh and "192.168.13.39" in ssh and "dd if=/dev/mmcblk0" in ssh
    assert "of=/tmp/x.img" in ssh

    adb = sr.dump_command("S1", None, "/tmp/x.img")
    assert "adb -s S1 exec-out" in adb, "adb shell mangles binary; exec-out does not"
    assert "shell" not in adb.split("exec-out")[0]


def test_disk_size_is_asked_before_the_copy_so_truncation_is_detectable():
    """A short dump is the failure that hides best: it looks like a file. The
    only way to know is to ask the WATCH how big its disk is, before starting."""
    class _W:
        class t:
            @staticmethod
            def shell(cmd, timeout=None):
                assert "/sys/class/block/mmcblk0/size" in cmd
                return 0, "7634944\n", ""
    assert sr.disk_bytes(_W) == 7634944 * 512     # nemo, measured 2026-08-03

    class _Bad:
        class t:
            @staticmethod
            def shell(cmd, timeout=None):
                return 1, "", "not found"
    assert sr.disk_bytes(_Bad) is None

    class _Junk:
        class t:
            @staticmethod
            def shell(cmd, timeout=None):
                return 0, "not a number", ""
    assert sr.disk_bytes(_Junk) is None, "a junk size would be compared against"


def test_manifest_records_what_a_filesystem_timestamp_cannot(tmp_path):
    """A file's mtime is not a dump date — any copy resets it. That cost
    several rounds of archaeology on 2026-08-03 to establish that an archive
    dated June had been taken years earlier. The method matters just as much:
    a runtime capture is trustworthy per partition and never for userdata."""
    p = tmp_path / "m.txt"
    sr.write_manifest(p, codename="nemo", serial="S1", taken="2026-08-07 10:00",
                      method="runtime", disk_bytes=123, complete=True, note=None)
    text = p.read_text()
    assert "serial: S1" in text and "taken: 2026-08-07 10:00" in text
    assert "method: runtime" in text
    assert "note" not in text, "a None field was written as an empty claim"
