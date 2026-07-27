# SPDX-License-Identifier: GPL-3.0-only
"""The AsteroidOS settings a-d-b mirrors, and how to read them off a watch.

Only the settings NOT already covered by the Control Center's own controls are
mirrored (mo): WiFi/BT/screen/USB-mode/power live in the other tabs and menus,
timezone in Sync-time, and the About page is the System tab. What's left are the
appearance and display preferences below, plus the currently-selected
watchface/launcher/wallpaper shown read-only.

Settings are dconf-direct (Nemo ConfigurationValue) — there is NO GSettings
schema on the watch (verified on skipjack 2026-07-22: `gsettings list-schemas`
lists nothing asteroid, and `gsettings get` fails). So `dconf read` returns
empty for any key still at its default, and the effective default lives in the
asteroid-settings QML `defaultValue`, baked here as `default`. The reader dumps
the whole dconf db once (the call backup already uses) and merges it over these
defaults, so an unset key still shows its true effective value."""

import re
from collections import namedtuple

Setting = namedtuple("Setting", "group key label type default")

# type:
#   "bool" — a togglable switch backed by a dconf boolean
#   "path" — the currently-selected file, shown read-only (mo: display only, no
#            picker — a fleet manager rarely needs to set a watchface remotely)
SETTINGS = [
    Setting("Units", "/org/asteroidos/settings/use-12h-format",
            "12-hour clock", "bool", False),
    Setting("Units", "/org/asteroidos/settings/use-fahrenheit",
            "Fahrenheit units", "bool", False),
    Setting("Display", "/org/asteroidos/settings/always-on-display",
            "Always-on display", "bool", True),
    Setting("Nightstand", "/desktop/asteroid/nightstand/enabled",
            "Nightstand mode", "bool", True),
    Setting("Nightstand", "/desktop/asteroid/nightstand/always-on-display",
            "Nightstand always-on", "bool", True),
    Setting("Nightstand", "/desktop/asteroid/nightstand/use-custom-watchface",
            "Nightstand custom watchface", "bool", False),
    Setting("Display", "/org/asteroidos/settings/burn-in-protection-level",
            "Burn-in protection level", "value", 0),
    Setting("Appearance", "/desktop/asteroid/watchface", "Watchface", "path", None),
    Setting("Appearance", "/desktop/asteroid/applauncher", "Launcher", "path", None),
    Setting("Appearance", "/desktop/asteroid/background-filename", "Wallpaper", "path", None),
]


def _parse_gvariant(raw):
    """A dconf-dump scalar → a Python value. Handles the forms our keys use:
    booleans, single-quoted strings, and ints (bare or with a `uint32` prefix);
    anything else passes through as the raw string."""
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1]
    tok = raw.split()[-1] if raw else raw   # 'uint32 30' → '30'
    try:
        return int(tok)
    except ValueError:
        return raw


def parse_dconf_dump(text):
    """`dconf dump /` output (INI-style sections) → {/full/key: value}. A section
    header `[a/b]` prefixes each `key=gvariant` beneath it to `/a/b/key`."""
    out = {}
    section = ""
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip("/")
            continue
        if section and "=" in line:
            k, v = line.split("=", 1)
            out["/" + section + "/" + k.strip()] = _parse_gvariant(v)
    return out


def effective_settings(dump_text):
    """Merge a dconf dump over the baked defaults → the rows the UI renders, each
    {group,key,label,type,value,is_set}. An unset key carries its QML default as
    `value` with is_set False, so the UI can mark it '(default)' rather than
    pretending nothing is configured."""
    stored = parse_dconf_dump(dump_text)
    rows = []
    for s in SETTINGS:
        is_set = s.key in stored
        rows.append({"group": s.group, "key": s.key, "label": s.label,
                     "type": s.type, "value": stored[s.key] if is_set else s.default,
                     "is_set": is_set})
    return rows


def writable(key):
    """The Setting for a key a-d-b may write, or None. Only the boolean settings
    are writable — the 'path' rows (watchface/launcher/wallpaper) are display-
    only (mo), and any key outside the catalog is refused. This is the boundary:
    the write op can only reach a key this returns non-None for."""
    for setting in SETTINGS:
        if setting.key == key:
            return setting if setting.type == "bool" else None
    return None


def dconf_arg(setting, value):
    """The dconf-write gvariant literal for a setting's new value (bools only)."""
    if setting.type == "bool":
        return "true" if value else "false"
    raise ValueError(f"{setting.type} settings are not writable")


# ── quick-panel toggle set ───────────────────────────────────────────────────
# /desktop/asteroid/quickpanel/enabled is a dconf dict<string,bool> of which
# toggles appear in the watch's quick panel. We mirror the enable state (not the
# order, mo). (id, label, default_enabled) from asteroid-settings QuickPanelPage
# — all default true except music and flashlight.
QUICKPANEL_KEY = "/desktop/asteroid/quickpanel/enabled"
QUICKPANEL = [
    ("lockButton", "Lock button", True),
    ("settingsButton", "Settings", True),
    ("brightnessToggle", "Brightness", True),
    ("bluetoothToggle", "Bluetooth", True),
    ("hapticsToggle", "Vibration", True),
    ("wifiToggle", "Wifi", True),
    ("soundToggle", "Mute sound", True),
    ("cinemaToggle", "Cinema mode", True),
    ("aodToggle", "Always-on display", True),
    ("powerOffToggle", "Power off", True),
    ("rebootToggle", "Reboot", True),
    ("musicButton", "Music", False),
    ("flashlightButton", "Flashlight", False),
]


def parse_gvariant_dict(raw):
    """A dconf-dump dict literal → {key: bool}. Handles both a{sb} (`'a': true`)
    and the a{sv} form this key is actually stored as (`'a': <true>`, values
    variant-wrapped) — the reader must accept the same type the writer emits, or
    every value reads back as absent and the UI shows defaults. Anything that is
    not a quoted-key/boolean pair is ignored, so a partial dict degrades to what
    it could parse rather than raising."""
    if not isinstance(raw, str):
        return {}
    return {m.group(1): m.group(2) == "true"
            for m in re.finditer(r"'([^']+)'\s*:\s*<?(true|false)>?", raw)}


def quickpanel_state(dump_text):
    """The quick-panel toggles with their enabled state — the stored dict merged
    over the QML defaults. [{id,label,enabled,is_set}]."""
    stored = parse_gvariant_dict(parse_dconf_dump(dump_text).get(QUICKPANEL_KEY))
    rows = []
    for tid, label, default in QUICKPANEL:
        is_set = tid in stored
        rows.append({"id": tid, "label": label,
                     "enabled": stored[tid] if is_set else default,
                     "is_set": is_set})
    return rows


def quickpanel_ids():
    """The set of valid toggle ids — the write boundary."""
    return {tid for tid, _, _ in QUICKPANEL}


def quickpanel_write_arg(states):
    """Serialize a full {id: bool} map → the dconf gvariant dict literal for the
    enabled key. Every catalog id is emitted so the written dict is complete.

    The values are variant-wrapped (`<true>`), making the type a{sv}, NOT a{sb}:
    Nemo's ConfigurationValue stores this key as a QVariantMap, which round-trips
    as a{sv}. A plain a{sb} write reads back empty in the launcher, which then
    builds an empty quick panel (verified on skipjack — a{sb} emptied the panel,
    a{sv} restored it)."""
    body = ", ".join(f"'{tid}': <{'true' if states.get(tid) else 'false'}>"
                     for tid, _, _ in QUICKPANEL)
    return "{" + body + "}"


# ── MCE wake gestures ────────────────────────────────────────────────────────
# Tap-to-wake and tilt-to-wake are MCE settings, not dconf (upstream
# asteroid-settings drives them over MCE D-Bus; the shell equivalent is
# mcetool --set-doubletap-wakeup / --set-wrist-gesture-detection). Values are
# surfaced verbatim — the tri-state doubletap policy is the watch's own
# vocabulary, not reinterpreted as a boolean.
WAKE_FLAGS = {
    "tap":  ("--set-doubletap-wakeup", ("never", "always", "proximity")),
    "tilt": ("--set-wrist-gesture-detection", ("enabled", "disabled")),
}


def parse_mce_wake(out):
    """Current wake-gesture states from full `mcetool` output:
    {'doubletap': never|always|proximity|None, 'wrist': enabled|disabled|None}.
    None = the line is absent on this watch/mce build. Pure — see tests."""
    import re
    res = {"doubletap": None, "wrist": None}
    m = re.search(r"Double-tap wakeup policy:\s*(\S+)", out)
    if m and m.group(1) in WAKE_FLAGS["tap"][1]:
        res["doubletap"] = m.group(1)
    m = re.search(r"[Ww]rist.*?:\s*(enabled|disabled)", out)
    if m:
        res["wrist"] = m.group(1)
    return res


def parse_locale(status_out, locale_dirs):
    """Current system locale + the locales this watch actually carries.
    `localectl status` reports the live setting; the locale directory lists what
    can be selected. Pure — see tests."""
    import re
    m = re.search(r"System Locale:\s*LANG=(\S+)", status_out or "")
    current = m.group(1) if m else None
    avail = sorted({l.strip() for l in (locale_dirs or "").split()
                    if l.strip() and not l.strip().startswith(".")})
    return {"current": current, "available": avail}
