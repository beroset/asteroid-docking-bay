# SPDX-License-Identifier: GPL-3.0-only
"""ConfigManager file handling and the typed settings dataclasses."""

from asteroid_docking_bay.config import (ChargeConfig, ConfigManager,
                                         FlashConfig, charge_config,
                                         derive_hub_names, flash_config,
                                         hub_name_for, seed_hub_names,
                                         set_hub_name)


# The rig's real topology: two A16 boxes (0bda) — one direct (1-2), one hanging
# off the Lenovo dock (17ef 1-9) at 1-9.1 — plus the dock itself. The nested
# case is the hard one: 1-9.1.x must read as the second A16, never the dock.
_RIG_HUBS = [
    {"location": "1-2",        "vendor": "0bda"},
    {"location": "1-2.3",      "vendor": "0bda"},
    {"location": "1-2.3.3",    "vendor": "0bda"},
    {"location": "1-2.4",      "vendor": "0bda"},
    {"location": "1-9",        "vendor": "17ef"},
    {"location": "1-9.1",      "vendor": "0bda"},
    {"location": "1-9.1.3",    "vendor": "0bda"},
    {"location": "1-9.1.3.3",  "vendor": "0bda"},
    {"location": "1-9.1.4",    "vendor": "0bda"},
]


def test_derive_hub_names_separates_nested_a16_from_dock():
    names = derive_hub_names(_RIG_HUBS)
    # Only the physical tops get named — not every cascade chip.
    assert names == {"1-2": "A16 #1", "1-9": "Lenovo dock", "1-9.1": "A16 #2"}
    cfg = {"hub_names": names}
    # Every chip/port inherits its box by longest-prefix.
    assert hub_name_for(cfg, "1-2.3.3") == "A16 #1"
    assert hub_name_for(cfg, "1-9.1.3.3") == "A16 #2"   # longer prefix beats "1-9"
    assert hub_name_for(cfg, "1-9") == "Lenovo dock"
    assert hub_name_for(cfg, "1-9.1.4") == "A16 #2"


def test_loc_covers_respects_component_boundaries():
    # A sibling that merely shares a text prefix must NOT inherit the name.
    cfg = {"hub_names": {"1-9.1": "A16 #2"}}
    assert hub_name_for(cfg, "1-9.1.3") == "A16 #2"
    assert hub_name_for(cfg, "1-9.10") is None          # not a child of 1-9.1
    assert hub_name_for(cfg, "1-2") is None


def test_seed_hub_names_scopes_to_boxes_with_configured_hubs():
    cfg = {"hubs": [{"location": "1-2.3.3"}, {"location": "1-9.1.3.3"}],
           "hub_names": {}}
    stray = _RIG_HUBS + [{"location": "2-1", "vendor": "1234"}]  # unrelated bus
    assert seed_hub_names(cfg, stray) is True
    # The stray box (no configured hub beneath it) is not named; the boxes in the
    # fleet's path are — including the dock the second A16 hangs off.
    assert cfg["hub_names"] == {"1-2": "A16 #1", "1-9": "Lenovo dock",
                                "1-9.1": "A16 #2"}
    # Idempotent: an already-seeded config is left untouched.
    assert seed_hub_names(cfg, _RIG_HUBS) is False


def test_set_hub_name_assigns_and_clears():
    cfg: dict = {}
    set_hub_name(cfg, "1-9.1", "A16 #2")
    assert cfg["hub_names"]["1-9.1"] == "A16 #2"
    set_hub_name(cfg, "1-9.1", "  ")                     # blank clears
    assert "1-9.1" not in cfg["hub_names"]


def test_charge_defaults():
    c = ChargeConfig()
    assert (c.low_threshold, c.high_threshold) == (40, 80)
    assert c.adaptive_cadence is True
    assert c.graceful_poweroff is True
    assert c.charge_max_minutes == 240


def test_charge_from_dict_overrides_and_ignores_unknown():
    c = ChargeConfig.from_dict({"low_threshold": 30, "graceful_poweroff": False,
                                "some_future_key": 1})
    assert c.low_threshold == 30
    assert c.graceful_poweroff is False
    assert c.high_threshold == 80          # untouched default
    assert not hasattr(c, "some_future_key")


def test_charge_from_dict_none():
    assert ChargeConfig.from_dict(None) == ChargeConfig()


def test_typed_slices_from_raw_config():
    raw = {"charge": {"high_threshold": 90}, "flash": {"nightly_url": "http://x"}}
    assert charge_config(raw).high_threshold == 90
    assert flash_config(raw).nightly_url == "http://x"
    assert isinstance(flash_config({}), FlashConfig)


def test_manager_missing_file_gives_skeleton(tmp_path):
    cm = ConfigManager(tmp_path / "config.json")
    cfg = cm.load()
    assert cfg == {"hubs": [], "serials": {}, "charge": {}, "flash": {}}


def test_manager_roundtrip_and_defaults_merge(tmp_path):
    cm = ConfigManager(tmp_path / "config.json")
    cm.save({"hubs": [{"location": "1-2"}], "charge": {"low_threshold": 35}})
    cfg = cm.load()
    assert cfg["hubs"][0]["location"] == "1-2"
    assert cfg["serials"] == {}            # missing keys seeded
    assert charge_config(cfg).low_threshold == 35
    assert charge_config(cfg).high_threshold == 80


# ── _store_smart_verdict: a proven verdict is sticky ──────────────────────────

from asteroid_docking_bay.config import _store_smart_verdict


def test_smart_verdict_none_does_not_erase_proven_true():
    hub = {"port_smart": {"1": True}}
    _store_smart_verdict(hub, 1, None)          # a marginal re-test
    assert hub["port_smart"]["1"] is True        # kept, not flickered to '?'


def test_smart_verdict_conclusive_updates():
    hub = {"port_smart": {"1": True}}
    _store_smart_verdict(hub, 1, False)          # a port genuinely changed
    assert hub["port_smart"]["1"] is False


def test_smart_verdict_none_stored_when_nothing_proven():
    hub = {}
    _store_smart_verdict(hub, 2, None)
    assert hub["port_smart"]["2"] is None


# ── exact codenames + unambiguous addressing ────────────────────────────────

def _multi_watch_cfg():
    """A rig fragment mirroring the real ambiguity: three ports share the
    'skipjack' image, two of them are physically tunnys; two share 'rubyfish',
    one is a rover. Serials are bound per port; exact codenames known for the
    live ones."""
    return {
        "hubs": [{"location": "1-2", "ports": {"1": "skipjack", "2": "rubyfish"},
                  "port_serials": {"1": "SKIP1", "2": "RUBY1"}},
                 {"location": "1-2.3", "ports": {"1": "skipjack", "2": "skipjack"},
                  "port_serials": {"1": "TUNNYA", "2": "TUNNYB"}},
                 {"location": "1-2.4", "ports": {"1": "rubyfish"},
                  "port_serials": {"1": "ROVER1"}}],
        "exact_codenames": {"TUNNYA": "tunny", "TUNNYB": "tunny",
                            "ROVER1": "rover", "RUBY1": "rubyfish"},
    }


def test_exact_codename_addresses_one_specific_watch():
    from asteroid_docking_bay.config import resolve_single_port
    cfg = _multi_watch_cfg()
    # 'rover' and 'rubyfish' share the rubyfish image; the exact codename picks
    # exactly one, where the image name could not.
    rover = resolve_single_port(cfg, "rover")
    ruby = resolve_single_port(cfg, "rubyfish")
    assert (rover["loc"], rover["port"]) == ("1-2.4", 1), rover
    assert (ruby["loc"], ruby["port"]) == ("1-2", 2), ruby
    assert rover["serial"] == "ROVER1" and ruby["serial"] == "RUBY1"


def test_serial_is_always_an_unambiguous_address():
    from asteroid_docking_bay.config import resolve_single_port
    d = resolve_single_port(_multi_watch_cfg(), "TUNNYB")
    assert (d["loc"], d["port"]) == ("1-2.3", 2), d


def test_shared_machine_name_raises_with_the_names_to_pick():
    from asteroid_docking_bay.config import resolve_single_port, AmbiguousTargetError
    import pytest
    cfg = _multi_watch_cfg()
    with pytest.raises(AmbiguousTargetError) as ei:
        resolve_single_port(cfg, "skipjack")   # three ports
    msg = str(ei.value)
    # The error must hand the user the serial (the only guaranteed unique
    # disambiguator), plus the exact codename where it differs from the query.
    assert "SKIP1" in msg and "TUNNYA" in msg and "tunny" in msg
    assert ei.value.candidates and len(ei.value.candidates) == 3


def test_unique_machine_name_still_resolves_directly():
    """Most machine names are unique (catfish, sturgeon); those must keep
    working as a plain single-target address with no ceremony."""
    from asteroid_docking_bay.config import resolve_single_port
    cfg = {"hubs": [{"location": "1-1", "ports": {"3": "catfish"},
                     "port_serials": {"3": "CAT1"}}]}
    d = resolve_single_port(cfg, "catfish")
    assert (d["loc"], d["port"]) == ("1-1", 3)


def test_all_resolves_every_port():
    from asteroid_docking_bay.config import find_ports_for_target
    assert len(find_ports_for_target(_multi_watch_cfg(), "all")) == 5


def test_record_exact_codename_is_change_gated():
    from asteroid_docking_bay.config import record_exact_codename, exact_codename_for_serial
    cfg = {}
    assert record_exact_codename(cfg, "S1", "rover") is True
    assert exact_codename_for_serial(cfg, "S1") == "rover"
    assert record_exact_codename(cfg, "S1", "rover") is False   # unchanged
    assert record_exact_codename(cfg, "S1", "rubyfish") is True  # changed
    assert record_exact_codename(cfg, None, "x") is False
    assert record_exact_codename(cfg, "S2", None) is False


# ── SSH-mode IP allocation ──────────────────────────────────────────────────

def test_ssh_ips_are_unique_and_start_at_leet():
    from asteroid_docking_bay.config import allocate_ssh_ip, SSH_IP_BASE
    cfg = {}
    a = allocate_ssh_ip(cfg, "S1")
    b = allocate_ssh_ip(cfg, "S2")
    c = allocate_ssh_ip(cfg, "S3")
    assert a == SSH_IP_BASE == "192.168.13.37"
    assert (b, c) == ("192.168.13.38", "192.168.13.39")
    assert len({a, b, c}) == 3, "two watches got the same SSH IP — the conflict"


def test_ssh_ip_assignment_is_sticky():
    from asteroid_docking_bay.config import allocate_ssh_ip, ssh_ip_for_serial
    cfg = {}
    first = allocate_ssh_ip(cfg, "S1")
    allocate_ssh_ip(cfg, "S2")
    assert allocate_ssh_ip(cfg, "S1") == first, "a watch's SSH IP must not drift"
    assert ssh_ip_for_serial(cfg, "S1") == first
    assert ssh_ip_for_serial(cfg, "unknown") is None


def test_usb_mode_preference_defaults_to_adb_and_ignores_junk():
    from asteroid_docking_bay.config import usb_mode_preference
    assert usb_mode_preference({}) == "adb", "the standard mode is the default"
    assert usb_mode_preference({"usb_mode_preference": "ssh"}) == "ssh"
    assert usb_mode_preference({"usb_mode_preference": "adb"}) == "adb"
    assert usb_mode_preference({"usb_mode_preference": "wat"}) == "adb", (
        "a junk value must fall back to the safe standard, not pass through")


def test_ssh_ip_fills_a_freed_gap():
    """If a serial is removed, its address is reused rather than skipped."""
    from asteroid_docking_bay.config import allocate_ssh_ip
    cfg = {}
    allocate_ssh_ip(cfg, "S1")            # .37
    b = allocate_ssh_ip(cfg, "S2")        # .38
    del cfg["ssh_ips"]["S1"]              # free .37
    assert allocate_ssh_ip(cfg, "S3") == "192.168.13.37", "did not reuse the gap"
    assert b == "192.168.13.38"
