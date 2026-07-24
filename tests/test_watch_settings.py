# SPDX-License-Identifier: GPL-3.0-only
"""The dconf-dump parser, the default-merge, and the read op.

Settings are dconf-direct with no GSettings schema on the watch, so an unset key
reads back empty and its effective value is the baked QML default — the merge
these tests pin. The parser must survive the shapes `dconf dump /` really emits."""

from asteroid_docking_bay import rpcops
from asteroid_docking_bay.watch_settings import (
    SETTINGS, _parse_gvariant, effective_settings, parse_dconf_dump)


def test_parse_gvariant_forms():
    assert _parse_gvariant("true") is True
    assert _parse_gvariant("false") is False
    assert _parse_gvariant("'file:///x.qml'") == "file:///x.qml"
    assert _parse_gvariant("30") == 30
    assert _parse_gvariant("uint32 30") == 30       # typed int prefix
    assert _parse_gvariant("''") == ""
    assert _parse_gvariant("0.5") == "0.5"          # a double we don't model → raw


def test_parse_dconf_dump_sections_and_keys():
    dump = ("[org/asteroidos/settings]\n"
            "use-12h-format=true\n"
            "use-fahrenheit=false\n"
            "\n"
            "[desktop/asteroid]\n"
            "background-filename='file:///w.qml'\n"
            "\n"
            "[desktop/asteroid/nightstand]\n"
            "enabled=false\n")
    d = parse_dconf_dump(dump)
    assert d["/org/asteroidos/settings/use-12h-format"] is True
    assert d["/org/asteroidos/settings/use-fahrenheit"] is False
    assert d["/desktop/asteroid/background-filename"] == "file:///w.qml"
    assert d["/desktop/asteroid/nightstand/enabled"] is False


def test_parse_ignores_comments_blanks_and_keyless_lines():
    d = parse_dconf_dump("# a comment\n\n[org/asteroidos/settings]\n"
                         "use-12h-format=true\nnostanza\n")
    assert d == {"/org/asteroidos/settings/use-12h-format": True}


def test_parse_key_before_any_section_is_dropped():
    # A key with no section header has no full path — it must not be invented as
    # '//key'; the section guard is what prevents that.
    assert parse_dconf_dump("use-12h-format=true\n") == {}


def test_effective_unset_key_shows_its_baked_default():
    rows = {r["key"]: r for r in effective_settings("")}   # empty dump = all default
    aod = rows["/org/asteroidos/settings/always-on-display"]
    assert aod["value"] is True and aod["is_set"] is False     # QML default: on
    twelve = rows["/org/asteroidos/settings/use-12h-format"]
    assert twelve["value"] is False and twelve["is_set"] is False


def test_effective_set_key_overrides_default_and_marks_set():
    rows = {r["key"]: r
            for r in effective_settings("[org/asteroidos/settings]\nuse-12h-format=true\n")}
    twelve = rows["/org/asteroidos/settings/use-12h-format"]
    assert twelve["value"] is True and twelve["is_set"] is True


def test_catalog_covers_only_non_redundant_settings():
    rows = effective_settings("")
    assert len(rows) == len(SETTINGS)
    assert all(r["type"] in ("bool", "path") for r in rows)
    keys = {r["key"] for r in rows}
    # The controls that already live in other tabs/menus must NOT be duplicated.
    assert not any(t in k for k in keys
                   for t in ("wifi", "bluetooth", "usb", "timezone", "power"))


# ── the read op ──────────────────────────────────────────────────────────────

class _FakeWatch:
    data = {"settings": [{"key": "/x", "value": True, "is_set": True}],
            "quickpanel": [{"id": "wifiToggle", "enabled": True, "is_set": False}]}

    def __init__(self, *a, **k):
        pass

    def settings_read(self):
        return type(self).data


def test_settings_read_op_returns_rows_and_quickpanel(monkeypatch):
    monkeypatch.setattr(rpcops, "Watch", _FakeWatch)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    d = rpcops.DISPATCH._data["watch.settings_read"]({"serial": "S1"})
    assert d["ok"] is True and d["settings"][0]["value"] is True
    assert d["quickpanel"][0]["id"] == "wifiToggle"


def test_settings_read_op_reports_unreachable(monkeypatch):
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch",
                        type("W", (), {"__init__": lambda self, *a, **k: None,
                                       "settings_read": lambda self: None}))
    d = rpcops.DISPATCH._data["watch.settings_read"]({"serial": "S1"})
    assert d["ok"] is False and "unreachable" in d["error"]


# ── the write gate ───────────────────────────────────────────────────────────

def test_writable_accepts_boolean_settings_only():
    from asteroid_docking_bay.watch_settings import writable
    assert writable("/org/asteroidos/settings/use-12h-format") is not None
    assert writable("/desktop/asteroid/watchface") is None        # display-only path
    assert writable("/desktop/asteroid/background-filename") is None
    assert writable("/etc/anything") is None                      # off-catalog


def test_dconf_arg_is_a_bool_literal():
    from asteroid_docking_bay.watch_settings import dconf_arg, writable
    s = writable("/org/asteroidos/settings/use-12h-format")
    assert dconf_arg(s, True) == "true" and dconf_arg(s, False) == "false"


def test_settings_write_refuses_a_display_only_key_without_touching_the_watch():
    from asteroid_docking_bay.watchctl import Watch
    calls = []
    w = Watch("S1", transport=object())
    w.user_cmd = lambda cmd, timeout=12: (calls.append(cmd), (0, "", ""))[1]
    assert w.settings_write("/desktop/asteroid/watchface", True) is False
    assert calls == [], "a display-only key still issued a dconf write"


def test_settings_write_writes_a_boolean_key():
    from asteroid_docking_bay.watchctl import Watch
    calls = []
    w = Watch("S1", transport=object())
    w.user_cmd = lambda cmd, timeout=12: (calls.append(cmd), (0, "", ""))[1]
    assert w.settings_write("/org/asteroidos/settings/use-12h-format", True) is True
    assert len(calls) == 1
    assert "dconf write" in calls[0] and "use-12h-format true" in calls[0]


def test_settings_write_op_coerces_value_and_dispatches(monkeypatch):
    seen = {}

    class W:
        def __init__(self, *a, **k):
            pass

        def settings_write(self, key, value):
            seen.update(key=key, value=value)
            return True

    monkeypatch.setattr(rpcops, "Watch", W)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    d = rpcops.DISPATCH._data["watch.settings_write"]({"serial": "S1", "key": "/k", "value": 1})
    assert d == {"ok": True} and seen == {"key": "/k", "value": True}


# ── arbitrary clock ──────────────────────────────────────────────────────────

def test_set_datetime_validates_before_touching_the_watch(monkeypatch):
    called = []
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    monkeypatch.setattr(rpcops, "Watch",
                        type("W", (), {"__init__": lambda self, *a, **k: None,
                                       "set_datetime": lambda self, w: (called.append(w), True)[1]}))
    bad = rpcops.DISPATCH._data["watch.set_datetime"]({"serial": "S1", "when": "next tuesday"})
    assert bad == {"ok": False, "error": "bad datetime"} and called == [], \
        "a malformed datetime reached the shell"
    ok = rpcops.DISPATCH._data["watch.set_datetime"]({"serial": "S1", "when": "2026-07-22 14:30:00"})
    assert ok == {"ok": True} and called == ["2026-07-22 14:30:00"]


# ── quick-panel toggle set ───────────────────────────────────────────────────

def test_quickpanel_dict_parse_and_default_merge():
    from asteroid_docking_bay.watch_settings import parse_gvariant_dict, quickpanel_state
    # Both the plain a{sb} form and the a{sv} variant form the key is stored as.
    assert parse_gvariant_dict("{'wifiToggle': true, 'musicButton': false}") == \
        {"wifiToggle": True, "musicButton": False}
    assert parse_gvariant_dict("{'wifiToggle': <true>, 'musicButton': <false>}") == \
        {"wifiToggle": True, "musicButton": False}
    rows = {r["id"]: r for r in quickpanel_state("")}          # empty dump = defaults
    assert rows["wifiToggle"]["enabled"] is True and rows["wifiToggle"]["is_set"] is False
    assert rows["musicButton"]["enabled"] is False             # music defaults off
    # A real dump stores the dict as a{sv} — the reader must accept it, else
    # every value reads back absent and the UI shows defaults over the truth.
    dump = "[desktop/asteroid/quickpanel]\nenabled={'wifiToggle': <false>, 'musicButton': <true>}\n"
    rows = {r["id"]: r for r in quickpanel_state(dump)}
    assert rows["wifiToggle"]["enabled"] is False and rows["wifiToggle"]["is_set"] is True
    assert rows["musicButton"]["enabled"] is True


def test_quickpanel_write_arg_is_a_complete_variant_dict():
    from asteroid_docking_bay.watch_settings import QUICKPANEL, quickpanel_write_arg
    arg = quickpanel_write_arg({"wifiToggle": False, "bluetoothToggle": True})
    assert arg.startswith("{") and arg.endswith("}")
    # Variant-wrapped values (a{sv}) — a plain a{sb} reads back empty in the
    # launcher and empties the quick panel (verified on skipjack).
    assert "'wifiToggle': <false>" in arg and "'bluetoothToggle': <true>" in arg
    assert all(f"'{tid}'" in arg for tid, _, _ in QUICKPANEL), "the written dict is not complete"


def test_quickpanel_set_refuses_unknown_id_without_writing():
    from asteroid_docking_bay.watchctl import Watch
    calls = []
    w = Watch("S1", transport=object())
    w.user_cmd = lambda cmd, timeout=15: (calls.append(cmd), (0, "", ""))[1]
    assert w.quickpanel_set("notAToggle", True) is False
    assert calls == [], "an unknown toggle id still hit the shell"


def test_quickpanel_set_reads_then_writes_the_full_dict():
    from asteroid_docking_bay.watchctl import Watch
    calls = []
    w = Watch("S1", transport=object())
    w.user_cmd = lambda cmd, timeout=15: (calls.append(cmd), (0, "", ""))[1]
    assert w.quickpanel_set("wifiToggle", False) is True
    assert any("dconf dump" in c for c in calls), "did not read the current dict first"
    write = [c for c in calls if "dconf write" in c][0]
    assert "quickpanel/enabled" in write and "wifiToggle" in write


def test_quickpanel_set_op_coerces_and_dispatches(monkeypatch):
    seen = {}

    class W:
        def __init__(self, *a, **k):
            pass

        def quickpanel_set(self, tid, on):
            seen.update(id=tid, on=on)
            return True

    monkeypatch.setattr(rpcops, "Watch", W)
    monkeypatch.setattr(rpcops, "_reachable_transport", lambda s: None)
    d = rpcops.DISPATCH._data["watch.quickpanel_set"]({"serial": "S1", "id": "wifiToggle", "on": 1})
    assert d == {"ok": True} and seen == {"id": "wifiToggle", "on": True}


def test_set_datetime_survives_the_adb_argument_resplit():
    """adb/ssh re-join their post-target args with spaces and hand them to a
    fresh watch shell, so only quoting INSIDE the single host-token survives.
    set_datetime must wrap the whole command as one token; quoting only the
    argument let the space in 'YYYY-MM-DD HH:MM:SS' split the time off as a
    stray operand, setting the wrong time on every call (audit C1)."""
    import shlex
    from asteroid_docking_bay.watchctl import Watch
    seen = {}

    class _T:
        def shell(self, cmd, timeout=None):
            seen["cmd"] = cmd
            return (0, "", "")
    w = Watch("S", transport=_T())
    w.set_datetime("2026-01-01 12:00:00")
    # the whole remote command must reach the transport as ONE host-shell token
    host_tokens = shlex.split(seen["cmd"])
    assert len(host_tokens) == 1, f"not wrapped — adb will re-split it: {seen['cmd']}"
    # and when the watch shell re-parses that token, the datetime stays one arg
    assert shlex.split(host_tokens[0]) == ["date", "-s", "2026-01-01 12:00:00"]
