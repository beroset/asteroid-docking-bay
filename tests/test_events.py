# SPDX-License-Identifier: GPL-3.0-only
"""EventLog round-trips and the standby-drain / adaptive-cadence math."""

import time

from asteroid_docking_bay.events import EventLog


def make_log(tmp_path):
    return EventLog(tmp_path / "events")


def test_key_sanitization():
    assert EventLog.key_for("411KPCA0121867", None) == "411KPCA0121867"
    assert EventLog.key_for("a/b:c d", None) == "a_b_c_d"
    assert EventLog.key_for(None, "lenok") == "lenok"     # codename fallback
    assert EventLog.key_for(None, None) is None


def test_log_read_roundtrip(tmp_path):
    el = make_log(tmp_path)
    el.log("S1", "lenok", "check_reading", pct=80)
    el.log("S1", "lenok", "charge_start", pct=35, target=80)
    evs = el.read("S1")
    assert [e["event"] for e in evs] == ["check_reading", "charge_start"]
    assert evs[0]["pct"] == 80 and evs[0]["serial"] == "S1"


def test_read_unknown_watch_is_empty(tmp_path):
    assert make_log(tmp_path).read("nobody") == []


def reading(ts_h, pct):
    return {"event": "check_reading", "ts": ts_h * 3600.0, "pct": pct}


def test_loss_rate_median(tmp_path):
    el = make_log(tmp_path)
    # 1%/h, 1%/h, then a 3%/h outlier interval: median stays 1.
    evs = [reading(0, 90), reading(1, 89), reading(2, 88), reading(3, 85)]
    assert el.standby_loss_rate("S", None, evs) == 1.0


def test_loss_rate_charge_breaks_chain(tmp_path):
    el = make_log(tmp_path)
    evs = [reading(0, 50),
           {"event": "charge_start", "ts": 1 * 3600.0},
           reading(2, 80)]   # 50→80 spans a charge: no usable pair
    assert el.standby_loss_rate("S", None, evs) is None


def test_loss_rate_ignores_gains_and_spikes(tmp_path):
    el = make_log(tmp_path)
    evs = [reading(0, 50), reading(1, 60),          # gain: not a drain pair
           reading(1.001, 10)]                      # absurd spike: filtered
    assert el.standby_loss_rate("S", None, evs) is None


def test_next_due_projection(tmp_path):
    el = make_log(tmp_path)
    now = time.time()
    # 1%/h steady drain, last seen at 80%: with low=40 + margin=10, headroom
    # is 30% → due ~30h after the last reading.
    el.log("S", "w", "check_reading", pct=82)
    for h, pct in ((2, 81), (4, 80)):
        # Write with controlled timestamps by appending directly.
        el.log("S", "w", "check_reading", pct=pct)
    evs = el.read("S")
    # Rewrite timestamps for determinism (log() stamps wall-clock).
    for i, e in enumerate(evs):
        e["ts"] = now + i * 3600.0
    f = el.dir / "S.jsonl"
    import json
    f.write_text("".join(json.dumps(e) + "\n" for e in evs))

    cfg = {"charge": {"low_threshold": 40, "adaptive_margin_pct": 10,
                      "adaptive_max_interval_days": 14}}
    due = el.next_due_ts("S", "w", cfg)
    assert due is not None
    hours_out = (due - (now + 2 * 3600.0)) / 3600.0   # from the last reading
    assert 29 < hours_out < 31


def test_next_due_none_without_history(tmp_path):
    el = make_log(tmp_path)
    cfg = {"charge": {}}
    assert el.next_due_ts("ghost", None, cfg) is None
