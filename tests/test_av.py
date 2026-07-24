# SPDX-License-Identifier: GPL-3.0-only
"""Display brightness + sound volume/mute (the Settings tab Display & Sound group).

Brightness is MCE (mcetool, 1..100), volume/mute are PulseAudio (pactl, 0..100),
gated on the machine.conf HAS_SPEAKER capability. The ops clamp; av_read parses
mcetool + pactl output and only reads volume/mute on a speaker watch."""

import asteroid_docking_bay.rpcops as rpcops
from asteroid_docking_bay.watchctl import Watch


class _AVWatch:
    def __init__(self):
        self.calls = []

    def set_brightness(self, p): self.calls.append(("b", p)); return True
    def set_volume(self, p): self.calls.append(("v", p)); return True
    def set_mute(self, o): self.calls.append(("m", o)); return True
    def play_notification(self): self.calls.append(("blip",)); return True
    def av_read(self): return {"brightness": 30, "has_speaker": False,
                               "has_mic": False, "volume": None, "muted": None}


# ── ops: clamp + validation ──────────────────────────────────────────────────

def test_set_brightness_clamps_and_validates(monkeypatch):
    w = _AVWatch()
    monkeypatch.setattr(rpcops, "_watch", lambda s: w)
    assert rpcops.DISPATCH._data["watch.set_brightness"](
        {"serial": "S", "pct": "150"})["pct"] == 100          # over → 100
    assert rpcops.DISPATCH._data["watch.set_brightness"](
        {"serial": "S", "pct": "0"})["pct"] == 1              # brightness never 0
    bad = rpcops.DISPATCH._data["watch.set_brightness"]({"serial": "S", "pct": "x"})
    assert bad["ok"] is False and "integer" in bad["error"]
    assert w.calls == [("b", 100), ("b", 1)]                  # nothing driven on the bad one


def test_set_volume_clamps(monkeypatch):
    w = _AVWatch()
    monkeypatch.setattr(rpcops, "_watch", lambda s: w)
    assert rpcops.DISPATCH._data["watch.set_volume"](
        {"serial": "S", "pct": "150"})["pct"] == 100
    assert rpcops.DISPATCH._data["watch.set_volume"](
        {"serial": "S", "pct": "-5"})["pct"] == 0             # volume may be 0 (silent)


def test_set_volume_blips_at_the_new_level_but_not_at_zero(monkeypatch):
    w = _AVWatch()
    monkeypatch.setattr(rpcops, "_watch", lambda s: w)
    rpcops.DISPATCH._data["watch.set_volume"]({"serial": "S", "pct": "40"})
    assert w.calls == [("v", 40), ("blip",)]                 # blip fires after the set
    w.calls.clear()
    rpcops.DISPATCH._data["watch.set_volume"]({"serial": "S", "pct": "0"})
    assert w.calls == [("v", 0)]                             # silent at 0 — no blip
    w.calls.clear()
    rpcops.DISPATCH._data["watch.set_volume"]({"serial": "S", "pct": "60", "blip": False})
    assert w.calls == [("v", 60)]                            # blip can be suppressed


def test_set_mute_and_av_read_passthrough(monkeypatch):
    w = _AVWatch()
    monkeypatch.setattr(rpcops, "_watch", lambda s: w)
    assert rpcops.DISPATCH._data["watch.set_mute"]({"serial": "S", "on": True})["ok"]
    assert w.calls == [("m", True)]
    d = rpcops.DISPATCH._data["watch.av_read"]({"serial": "S"})
    assert d["ok"] is True and d["brightness"] == 30 and d["has_speaker"] is False


# ── av_read parsing ──────────────────────────────────────────────────────────

class _T:
    def __init__(self, out): self._out = out
    def shell(self, cmd, timeout=None): return (0, self._out, "")


def test_av_read_parses_speaker_and_mic(monkeypatch):
    w = Watch("S", transport=_T(
        "Brightness: 30 (1-100)\n---CAP---\nHAS_SPEAKER = true\nHAS_MIC = true\n"))
    monkeypatch.setattr(w, "user_cmd", lambda cmd, timeout=None: (
        0, "Volume: front-left: 65536 / 40% / 0.00 dB\n---\nMute: yes\n", ""))
    av = w.av_read()
    assert av["brightness"] == 30 and av["has_speaker"] is True and av["has_mic"] is True
    assert av["volume"] == 40 and av["muted"] is True


def test_av_read_mic_without_speaker(monkeypatch):
    # A mic-but-no-speaker watch: has_mic True, has_speaker False, no pactl call.
    w = Watch("S", transport=_T("Brightness: 55 (1-100)\n---CAP---\nHAS_MIC = true\n"))
    called = {"n": 0}
    monkeypatch.setattr(w, "user_cmd",
                        lambda cmd, timeout=None: (called.__setitem__("n", 1), (0, "", ""))[1])
    av = w.av_read()
    assert av["has_speaker"] is False and av["has_mic"] is True
    assert av["volume"] is None and av["muted"] is None and called["n"] == 0


def test_record_audio_op(monkeypatch, tmp_path):
    f = tmp_path / "rec.wav"
    f.write_bytes(b"\x00" * 120)
    monkeypatch.setattr(rpcops, "_watch",
                        lambda s: type("W", (), {"record_audio": lambda self, n: f})())
    d = rpcops.DISPATCH._data["watch.record_audio"]({"serial": "S", "seconds": "5"})
    assert d["ok"] is True and d["seconds"] == 5 and d["bytes"] == 120


def test_record_audio_op_failure(monkeypatch):
    monkeypatch.setattr(rpcops, "_watch",
                        lambda s: type("W", (), {"record_audio": lambda self, n: None})())
    d = rpcops.DISPATCH._data["watch.record_audio"]({"serial": "S"})
    assert d["ok"] is False and "mic" in d["error"]


# ── standby feature capture (per-feature drain attribution) ──────────────────

def test_standby_features_parses_connman_and_aod(monkeypatch):
    conn = ("  Type = wifi\n  Powered = True\n  Connected = yes\n"
            "  Type = bluetooth\n  Powered = False\n")
    w = Watch("S", transport=_T(conn))
    monkeypatch.setattr(w, "user_cmd", lambda c, timeout=None: (0, "true\n", ""))
    assert w.standby_features() == {"wifi": True, "bt": False, "aod": True}


def test_standby_features_aod_defaults_on_when_empty(monkeypatch):
    w = Watch("S", transport=_T(""))                      # connman unreadable
    monkeypatch.setattr(w, "user_cmd", lambda c, timeout=None: (0, "", ""))
    f = w.standby_features()
    assert f["aod"] is True and f["wifi"] is None and f["bt"] is None  # empty aod = default on



def test_standby_features_aod_is_none_on_a_failed_read(monkeypatch):
    """A failed dconf read (rc != 0 — unreachable watch / su error) also yields
    empty output; it must NOT be reported as aod=on, which would mislabel the
    per-feature drain attribution (audit C2)."""
    w = Watch("S", transport=_T(""))
    monkeypatch.setattr(w, "user_cmd", lambda c, timeout=None: (1, "", "dconf error"))
    assert w.standby_features()["aod"] is None
