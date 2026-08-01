# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Comparing what a watch is configured to do against what it does.

The whole value is the VERDICT: which layer moved decides which project owns
the bug. A diff that merely dumps two blobs leaves the reader to make that call
at 1am on a freshly flashed watch, which is when it will be read.
"""

from asteroid_docking_bay.aodcheck import diff, parse

CAP = """@@@MCE@@@
Display state: off
Low power mode: {lpm}
Blank inhibit: disabled
@@@DCONF@@@
[desktop/asteroid/aod]
enabled={aod}

[desktop/asteroid/nightstand]
enabled=true
@@@UPTIME@@@
412.55
"""


def cap(lpm="disabled", aod="true"):
    return parse(CAP.format(lpm=lpm, aod=aod))


def test_parses_both_layers_and_uptime():
    c = cap()
    assert c["mce"]["Low power mode"] == "disabled"
    assert c["dconf"]["desktop/asteroid/aod/enabled"] == "true"
    assert c["uptime"] == 412.55


def test_dconf_keys_are_qualified_by_their_path():
    """Two schemas both have `enabled`. Flattening on the bare key would make
    one silently overwrite the other, and the diff would then miss it."""
    c = cap()
    assert "desktop/asteroid/aod/enabled" in c["dconf"]
    assert "desktop/asteroid/nightstand/enabled" in c["dconf"]
    assert c["dconf"]["desktop/asteroid/nightstand/enabled"] == "true"


def test_mce_moved_alone_names_configuration_plumbing():
    """The reported bug's signature: the settings never changed, but opening
    the app made AoD work. That means the value never reached MCE at boot."""
    d = diff(cap(lpm="disabled"), cap(lpm="enabled"))
    assert d["mce_changed"] == 1 and d["dconf_changed"] == 0
    assert d["mce"]["Low power mode"] == ["disabled", "enabled"]
    assert "never pushed to MCE at boot" in d["verdict"]


def test_neither_layer_moving_points_away_from_configuration():
    """The other real outcome, and the one most likely to be misread as 'the
    capture failed'. It is a finding: the fix was not configuration at all."""
    d = diff(cap(), cap())
    assert d["mce_changed"] == 0 and d["dconf_changed"] == 0
    assert "NEITHER layer changed" in d["verdict"]
    assert "instantiated" in d["verdict"]


def test_a_userland_write_that_never_reaches_mce_is_named():
    d = diff(cap(aod="false"), cap(aod="true"))
    assert d["dconf_changed"] == 1 and d["mce_changed"] == 0
    assert "never reached its consumer" in d["verdict"]


def test_both_moving_is_reported_as_such():
    d = diff(cap(lpm="disabled", aod="false"), cap(lpm="enabled", aod="true"))
    assert d["mce_changed"] == 1 and d["dconf_changed"] == 1
    assert "Both layers changed" in d["verdict"]


def test_an_empty_capture_does_not_masquerade_as_no_change():
    """A failed read produces two empty captures, which diff to 'nothing
    changed' — the same answer as a real finding. The caller must be able to
    tell them apart, so parse must not invent structure."""
    empty = parse("")
    assert empty["mce"] == {} and empty["dconf"] == {}
    assert empty["uptime"] is None


def test_mcetools_own_underline_does_not_eat_the_capture():
    """mcetool prints a row of dashes under its header. A pattern-based fence
    ('---...---') matched it, so everything after was discarded as a bogus
    section and the capture came back with ONE key — sparse-looking rather than
    obviously broken. Real output, verbatim from a watch."""
    real = """@@@MCE@@@
MCE status:
-----------
MCE version:                         1.109.2
Display state:                       off
Blank from lpm-on:                   1 (seconds)
@@@DCONF@@@
[org/asteroidos/settings]
always-on-display=false
@@@UPTIME@@@
3268.51
"""
    c = parse(real)
    assert c["mce"]["Display state"] == "off", \
        f"the dashed underline ate the capture: {c['mce']}"
    assert c["mce"]["Blank from lpm-on"] == "1 (seconds)"
    assert c["dconf"]["org/asteroidos/settings/always-on-display"] == "false"
    assert c["uptime"] == 3268.51


def test_capture_accepts_the_fence_it_actually_emits():
    """The guard and the command must agree on the fence. They did not after
    the fence was renamed, so every capture reported failure while the watch
    was answering perfectly — a rename that broke the check but not the read."""
    from asteroid_docking_bay import aodcheck

    class _T:
        def shell(self, cmd, timeout=0):
            assert "@@@MCE@@@" in cmd
            return (0, "@@@MCE@@@\nDisplay state: off\n@@@DCONF@@@\n"
                       "[org/asteroidos/settings]\nalways-on-display=true\n"
                       "@@@UPTIME@@@\n10.0\n", "")

    class _W:
        t = _T()

    res = aodcheck.capture(_W())
    assert res["ok"] is True, res
    assert res["dconf"]["org/asteroidos/settings/always-on-display"] == "true"


# --- the single-capture consistency check ---------------------------------

def _cap(aod, lpm):
    return parse(f"""@@@MCE@@@
MCE status:
-----------
Display state: off
Use low power mode: {lpm}
@@@DCONF@@@
[org/asteroidos/settings]
always-on-display={aod}
@@@UPTIME@@@
100.0
""")


def test_the_reported_bug_is_visible_in_one_capture():
    """Toggle on, MCE not told. This is the whole point: the broken state is
    detectable without a before/after pair, a fresh flash, or anyone watching
    the screen at the right moment."""
    from asteroid_docking_bay.aodcheck import consistency
    c = consistency(_cap("true", "disabled"))
    assert c["known"] and c["consistent"] is False
    assert c["aod_requested"] is True and c["mce_lpm_enabled"] is False
    assert "never reached the thing that draws" in c["note"]


def test_agreement_in_either_direction_is_consistent():
    from asteroid_docking_bay.aodcheck import consistency
    assert consistency(_cap("true", "enabled"))["consistent"] is True
    assert consistency(_cap("false", "disabled"))["consistent"] is True


def test_the_reverse_mismatch_is_also_named():
    from asteroid_docking_bay.aodcheck import consistency
    c = consistency(_cap("false", "enabled"))
    assert c["consistent"] is False and "reverse mismatch" in c["note"]


def test_an_unreadable_side_is_not_reported_as_agreement():
    """Missing data must never read as 'consistent' — that would hide the very
    fault this exists to find."""
    from asteroid_docking_bay.aodcheck import consistency
    c = consistency({"dconf": {}, "mce": {}})
    assert c["known"] is False and "consistent" not in c


# --- the drain attribution that was wrong ---------------------------------

def test_an_unset_toggle_is_not_reported_as_aod_running():
    """a-d-b read the dconf toggle and treated UNSET as 'default on', so it
    recorded aod:True for exactly the watches where AoD provably was not
    rendering — there is no schema for that key, so unset means nobody ever
    wrote it, which is the state in which MCE was never told.

    The authority is MCE. The toggle is recorded separately as intent."""
    from asteroid_docking_bay import watchctl

    class _T:
        def shell(self, cmd, timeout=0):
            if "connmanctl" in cmd:
                return (0, "", "")
            if "Use low power mode" in cmd:
                return (0, "Use low power mode: disabled\n", "")
            return (0, "", "")

    w = watchctl.Watch.__new__(watchctl.Watch)
    w.t = _T()
    w.user_cmd = lambda cmd, timeout=0: (0, "", "")   # unset toggle

    feats = watchctl.Watch.standby_features(w)
    assert feats["aod"] is False, \
        "an unset toggle was reported as AoD running (the old assumption)"
    assert feats["aod_toggle"] is None, "unset must be recorded as unset"


def test_mce_enabled_is_reported_as_aod_running():
    from asteroid_docking_bay import watchctl

    class _T:
        def shell(self, cmd, timeout=0):
            if "Use low power mode" in cmd:
                return (0, "Use low power mode: enabled\n", "")
            return (0, "", "")

    w = watchctl.Watch.__new__(watchctl.Watch)
    w.t = _T()
    w.user_cmd = lambda cmd, timeout=0: (0, "true", "")

    feats = watchctl.Watch.standby_features(w)
    assert feats["aod"] is True and feats["aod_toggle"] is True
