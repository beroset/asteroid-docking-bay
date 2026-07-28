# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Provisioning a WiFi network onto a watch from a backup's connman config.

`watch.backup` already pulls /var/lib/connman, so the moment ONE watch has
joined a network the rig holds a working credential for it. This lends that
credential to any other watch, which beats typing a passphrase on a 320px
touchscreen once per watch.

The whole problem is that a connman service is NOT portable as-is. Its
identity is

    wifi_<the watch's own WiFi MAC>_<SSID as hex>_<mode>_<security>

used both as the directory name AND as the section header inside `settings`.
Copy it verbatim to another watch and connman sees a service belonging to an
interface that does not exist there: it will not match the scanned network, so
it scans, finds the SSID, has no credential for it, and does nothing. The
adaptation — rewriting that identity for the target's own MAC — is the entire
trick, and it is why this is a feature rather than an `adb push`.

Everything here is pure; the device side lives in Watch.provision_wifi().
"""

from __future__ import annotations

import re
from pathlib import Path

# wifi_<mac>_<ssid-hex>_<mode>_<security>. The tail is kept verbatim rather
# than assumed to be "managed_psk": an open network is managed_none, and an
# enterprise one differs again.
_SERVICE_RE = re.compile(r"^wifi_([0-9a-fA-F]{6,})_([0-9a-fA-F]+)_(.+)$")

# Keys that describe the SOURCE watch, not the network. Carried over they are
# at best stale and at worst actively wrong — LastAddress is another watch's
# DHCP lease, and handing it to a second watch invites a duplicate-address
# attempt on a network where the first one may still be up.
_DROP_KEYS = ("IPv4.DHCP.LastAddress", "Modified")


def parse_service_dir(name: str) -> "tuple[str, str, str] | None":
    """"wifi_9828a6ec99db_6672...68_managed_psk" -> (mac, ssid_hex, tail)."""
    m = _SERVICE_RE.match(name)
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower(), m.group(3)


def ssid_from_hex(ssid_hex: str) -> str:
    """The human SSID. Falls back to the raw hex rather than raising: an SSID
    is arbitrary bytes and need not be valid UTF-8, and a name we cannot decode
    must still be selectable."""
    try:
        return bytes.fromhex(ssid_hex).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ssid_hex


def normalise_mac(addr: str) -> str:
    """"00:90:4c:11:22:33" -> "00904c112233", connman's spelling."""
    return re.sub(r"[^0-9a-fA-F]", "", addr or "").lower()


def service_id(mac: str, ssid_hex: str, tail: str) -> str:
    return f"wifi_{normalise_mac(mac)}_{ssid_hex.lower()}_{tail}"


def parse_settings(text: str) -> dict:
    """The key/values of a connman service `settings` file (single section)."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("[", "#")):
            continue
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def adapt_settings(text: str, new_id: str) -> str:
    """Rewrite a service's `settings` for a different watch.

    Replaces the section header with the target's service id and drops the
    keys that belonged to the source watch. Everything else — Name, SSID,
    Passphrase, Favorite, AutoConnect, the IPv4/IPv6 methods — is the network's
    and travels unchanged.
    """
    kept = [f"[{new_id}]"]
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("["):
            continue
        key = stripped.partition("=")[0].strip()
        if key in _DROP_KEYS:
            continue
        kept.append(stripped)
    # AutoConnect is the point of the exercise: a provisioned watch should join
    # the network by itself, not wait to be told. Favorite is what makes
    # connman treat it as a known network at all.
    have = {ln.partition("=")[0] for ln in kept}
    for key, val in (("Favorite", "true"), ("AutoConnect", "true")):
        if key not in have:
            kept.append(f"{key}={val}")
    return "\n".join(kept) + "\n"


def find_aps(backup_root: Path) -> "list[dict]":
    """Every WiFi credential across all watch backups, newest first.

    Deduplicated by SSID: several watches on the same network yield the same
    credential, and the UI should offer the NETWORK once, not one entry per
    watch that happens to know it.
    """
    found: dict[str, dict] = {}
    root = Path(backup_root)
    if not root.is_dir():
        return []
    for connman in sorted(root.glob("*/connman")):
        source = connman.parent.name
        for svc in sorted(connman.iterdir()):
            if not svc.is_dir():
                continue
            parsed = parse_service_dir(svc.name)
            settings = svc / "settings"
            # A service dir with no `settings` carries no credential. The rig's
            # own backup has one of these — a second, locally-administered MAC
            # (the P2P/WiFi-Direct interface) with only connman's binary cache.
            if not parsed or not settings.is_file():
                continue
            mac, ssid_hex, tail = parsed
            try:
                text = settings.read_text()
            except OSError:
                continue
            kv = parse_settings(text)
            ssid = kv.get("Name") or ssid_from_hex(ssid_hex)
            entry = {
                "ssid": ssid, "ssid_hex": ssid_hex, "tail": tail,
                "source": source, "path": str(settings),
                "secured": "Passphrase" in kv,
                "modified": kv.get("Modified", ""),
            }
            prev = found.get(ssid)
            if prev is None or entry["modified"] > prev["modified"]:
                found[ssid] = entry
    return sorted(found.values(), key=lambda e: (-bool(e["modified"]), e["ssid"]))
