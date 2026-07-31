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


def ev(event, ts_h, pct=None):
    d = {"event": event, "ts": ts_h * 3600.0}
    if pct is not None:
        d["pct"] = pct
    return d


def test_off_to_on_standby_rate(tmp_path):
    el = make_log(tmp_path)
    # Powered off at 100%, booted 24h later at 76% → 24% over 24h = 1%/h.
    evs = [ev("power_off", 0, 100), ev("check_reading", 24, 76)]
    assert el.standby_off_to_on_rate("S", None, evs) == 1.0


def test_off_to_on_excludes_battery_rise(tmp_path):
    el = make_log(tmp_path)
    # Came back HIGHER → charged off the rig (worn), not standby → excluded.
    evs = [ev("power_off", 0, 50), ev("check_reading", 10, 90)]
    assert el.standby_off_to_on_rate("S", None, evs) is None


def test_off_to_on_returns_most_recent_pair(tmp_path):
    el = make_log(tmp_path)
    evs = [ev("power_off", 0, 100), ev("check_reading", 10, 90),   # 1%/h
           ev("power_off", 20, 100), ev("charge_start", 30, 80)]   # 2%/h
    assert el.standby_off_to_on_rate("S", None, evs) == 2.0


def test_off_to_on_none_without_a_pair(tmp_path):
    el = make_log(tmp_path)
    assert el.standby_off_to_on_rate("S", None, [ev("power_off", 0, 100)]) is None


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


def test_external_events_break_the_standby_chain(tmp_path):
    """An externally logged event means somebody worked on the watch — a
    flash, a bench session — so the interval spanning it was never passive
    standby. Counting across it reports a drain rate measured during work,
    which is exactly the pollution issue #6 asks to avoid."""
    from asteroid_docking_bay.events import EventLog
    el = EventLog(tmp_path)
    base = 1_000_000.0
    # Exactly ONE clean pair and ONE that spans bench work. With two samples
    # the median IS the polluted value, so a broken guard changes the answer.
    # (An earlier version of this test used three clean pairs and passed even
    # with the guard removed — the median absorbed the outlier. A test that
    # cannot fail is decoration.)
    evs = [
        {"ts": base,          "event": "check_reading", "pct": 100},
        {"ts": base + 3600,   "event": "check_reading", "pct": 99},   # 1%/h
        {"ts": base + 4000,   "event": "external", "note": "flashed a build"},
        # 39%/h apparent drop across the bench window — must NOT be counted.
        {"ts": base + 7200,   "event": "check_reading", "pct": 60},
    ]
    rate = el.standby_loss_rate("S1", "sturgeon", events=evs)
    assert rate is not None
    assert rate < 5, (
        f"standby rate {rate:.1f}%/h absorbed the bench-window drop — "
        "external events must break the chain")


def test_external_event_is_not_a_reading(tmp_path):
    """Injected events must never be mistakable for measured data."""
    from asteroid_docking_bay.events import EventLog
    el = EventLog(tmp_path)
    el.log("S1", "sturgeon", "external", note="flashed", source="ui-track")
    rows = el.read("S1", "sturgeon")
    assert len(rows) == 1
    assert rows[0]["event"] == "external"
    assert "pct" not in rows[0], "an external note carries no battery reading"
    assert rows[0]["note"] == "flashed" and rows[0]["source"] == "ui-track"


def test_latest_drain_summaries_ignores_low_confidence_results(tmp_path, monkeypatch):
    """A rate from one or two polls over a short span is dominated by per-read
    noise and the re-power charge-bump — it must not become a watch's canonical
    est_h and drive wearability / next_due_ts (audit B4). Only a run with enough
    samples over enough time counts."""
    import json
    import asteroid_docking_bay.events as ev
    monkeypatch.setattr(ev, "_DRAIN_RESULTS_DIR", tmp_path)

    def write(name, readings, rate, ts):
        (tmp_path / name).write_text(json.dumps({
            "codename": "pike", "serial": "S", "start_ts": ts,
            "drain_rate_pct_per_hour": rate, "readings": readings}))

    # low-confidence: 2 readings over 0.5h → ignored
    monkeypatch.setattr(ev, "_drain_summary_cache", {"mtime": None, "by_codename": {}})
    write("pike-a.json", [{"ts": 0, "pct": 100}, {"ts": 1800, "pct": 92}], 16.0, 100)
    assert "pike" not in ev._latest_drain_summaries(), "a 2-sample 0.5h rate became canonical"

    # high-confidence: 4 readings over 2h → counts
    monkeypatch.setattr(ev, "_drain_summary_cache", {"mtime": None, "by_codename": {}})
    write("pike-b.json", [{"ts": 0, "pct": 100}, {"ts": 1800, "pct": 98},
                          {"ts": 3600, "pct": 96}, {"ts": 7200, "pct": 92}], 4.0, 200)
    s = ev._latest_drain_summaries()
    assert "pike" in s and abs(s["pike"]["rate"] - 4.0) < 0.01


def test_a_single_discharge_sample_is_labelled_not_presented_as_a_mean():
    """One noisy reading must not read like a settled figure. catfish's first
    undocked run yielded exactly one discharging sample and '5000 uA' looked
    like a result."""
    from asteroid_docking_bay.drainlog import classify
    one = classify([{"current_ua": -5000, "capacity": 99,
                     "status": "Discharging", "epoch": 1},
                    {"current_ua": 0, "capacity": 100, "status": "Full",
                     "epoch": 2}])
    assert one["discharge_samples"] == 1
    assert "directional, not a measurement" in one["discharge_note"]

    many = classify([{"current_ua": -5000 - i, "capacity": 99,
                      "status": "Discharging", "epoch": i} for i in range(6)])
    assert many["discharge_samples"] == 6
    assert "discharge_note" not in many, "a real sample count must not be hedged"
