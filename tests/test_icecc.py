# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""The icecream compile cluster panel.

Samples here are VERBATIM from the running 1.4.0 scheduler, captured by the
sysadmin session that stood the cluster up (docs/ICECC_NODES.md). The quirks
they carry — the leading space on node lines, speed=0.00 on a fresh cluster —
are the reason to keep them verbatim rather than tidied.
"""

import socket
import threading

import pytest

from asteroid_docking_bay import icecc


BANNER = ("200-ICECC 1.4.0: 3602s uptime, 3 hosts, 0 jobs in queue (0 total).\n"
          "200 Use 'help' for help and 'quit' to quit.\n")

LISTCS = (
    " mo-e15-eos (192.168.176.164:10245) [x86_64] speed=0.00 jobs=0/14 load=118\n"
    " mo-p14s-omarchy (192.168.176.117:10245) [x86_64] speed=0.00 jobs=0/10 load=571\n"
    " mo-w541-eos (192.168.176.21:10245) [x86_64] speed=0.00 jobs=0/8 load=54\n"
)


# ── detection: must degrade to nothing ───────────────────────────────────────

def test_a_host_that_is_not_a_cluster_member_renders_nothing(tmp_path):
    """a-d-b runs on machines that have never heard of icecream. Each missing
    condition must yield None — not an empty panel, not an error strip."""
    conf = tmp_path / "icecream.conf"
    binary = tmp_path / "icecc"

    # no config file at all
    assert icecc.configured(str(conf), (str(binary),)) is None

    conf.write_text('ICECREAM_SCHEDULER_HOST="192.168.176.164"\n')
    # config exists but icecream is not installed
    assert icecc.configured(str(conf), (str(binary),)) is None

    binary.write_text("")
    assert icecc.configured(str(conf), (str(binary),)) is not None

    # An EMPTY scheduler host means the daemon uses broadcast discovery: there
    # is no address to ask, so there is nothing to show even though icecream is
    # installed and running.
    conf.write_text('ICECREAM_SCHEDULER_HOST=""\nICECREAM_NETNAME="asteroid"\n')
    assert icecc.configured(str(conf), (str(binary),)) is None


def test_conf_is_parsed_not_sourced(tmp_path):
    """Shell syntax, read by a long-running service. Evaluating it would hand a
    config file the ability to run code."""
    conf = tmp_path / "icecream.conf"
    binary = tmp_path / "icecc"
    binary.write_text("")
    conf.write_text('# a comment\n'
                    'ICECREAM_NETNAME="asteroid"\n'
                    "ICECREAM_SCHEDULER_HOST='192.168.176.164'\n"
                    'ICECREAM_MAX_JOBS="8"\n'
                    'MALFORMED\n')
    got = icecc.configured(str(conf), (str(binary),))
    assert got == {"scheduler": "192.168.176.164", "netname": "asteroid",
                   "max_jobs": "8"}


def test_only_read_only_commands_may_be_sent():
    """removecs/blockcs/unblockcs mutate cluster state. A monitor that can
    change what it monitors is not a monitor."""
    for bad in ("removecs", "blockcs", "unblockcs", "internals"):
        with pytest.raises(ValueError, match="read-only"):
            icecc.query("127.0.0.1", (bad,))


# ── parsing the verbatim scheduler output ────────────────────────────────────

def test_node_lines_parse_including_their_leading_space():
    """The scheduler indents every node line. An anchored pattern without that
    allowance silently matches nothing and the panel shows an empty cluster."""
    nodes = icecc.parse_nodes(LISTCS)
    assert len(nodes) == 3, "the leading space swallowed the node lines"
    e15 = nodes[0]
    assert e15["host"] == "mo-e15-eos" and e15["ip"] == "192.168.176.164"
    assert e15["arch"] == "x86_64"
    assert (e15["jobs_used"], e15["jobs_max"]) == (0, 14)
    assert e15["load"] == 118
    assert [n["jobs_max"] for n in nodes] == [14, 10, 8]


def test_speed_zero_is_a_fresh_cluster_not_a_broken_one():
    """speed stays 0.00 until a node has completed real jobs, so a newly built
    cluster reports zeros everywhere. Rendering that as a fault would make the
    panel cry wolf on day one."""
    nodes = icecc.parse_nodes(LISTCS)
    assert all(n["speed"] == 0.0 for n in nodes)
    summary = icecc.build_summary({"scheduler": "x"},
                                  {"ok": True, "banner": BANNER,
                                   "out": {"listcs": LISTCS, "listjobs": ""}})
    assert summary["reachable"] is True
    assert summary["building"] is False, "an idle cluster read as building"


def test_banner_alone_reports_uptime_hosts_and_queue():
    """It arrives unprompted and is live whether or not a build is running, so
    it alone answers 'is the cluster there'."""
    assert icecc.parse_banner(BANNER) == {"uptime_s": 3602, "hosts": 3,
                                          "queue": 0}
    assert icecc.parse_banner("nonsense") == {}


def test_moving_jobs_are_what_prove_a_build_is_distributing():
    """THE POINT OF THE PANEL. icecream fails silently: Yocto notes a
    non-distributing build at bb.note level and the cluster still looks
    healthy. Job counts are the only reliable tell."""
    busy = LISTCS.replace("jobs=0/14", "jobs=9/14").replace("jobs=0/10", "jobs=4/10")
    jobs = (" 1234 mo-w541-eos compile foo.cpp\n"
            " 1235 mo-e15-eos compile bar.cpp\n")
    s = icecc.build_summary({"scheduler": "x"},
                            {"ok": True, "banner": BANNER,
                             "out": {"listcs": busy, "listjobs": jobs}})
    assert s["building"] is True
    assert s["jobs_used"] == 13 and s["jobs_listed"] == 2
    assert s["slots"] == 32


def test_an_empty_listjobs_is_idle_not_broken():
    s = icecc.build_summary({"scheduler": "x"},
                            {"ok": True, "banner": BANNER,
                             "out": {"listcs": LISTCS, "listjobs": "\n"}})
    assert s["building"] is False and s["jobs_listed"] == 0


# ── the failure modes that must not stall the fleet ──────────────────────────

def test_connection_refused_is_reported_not_raised():
    """An unreachable scheduler is a normal state, and a real signal: it means
    builds are running local right now."""
    # port 1 on loopback: refused immediately, no network wait
    r = icecc.query("127.0.0.1", ("listcs",), port=1, timeout=0.5)
    assert r["ok"] is False and r["error"]
    s = icecc.build_summary({"scheduler": "127.0.0.1"}, r)
    assert s["reachable"] is False and s["nodes"] == []


def test_a_scheduler_that_accepts_then_says_nothing_times_out():
    """A host that is up but wedged would otherwise hold the poll forever. The
    read is bounded, so this costs `timeout` seconds and no more."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    held = []

    def accept_and_stay_silent():
        conn, _ = srv.accept()
        held.append(conn)          # accepted, never written to

    t = threading.Thread(target=accept_and_stay_silent, daemon=True)
    t.start()
    import time
    start = time.monotonic()
    r = icecc.query("127.0.0.1", ("listcs",), port=port, timeout=0.6)
    elapsed = time.monotonic() - start
    for c in held:
        c.close()
    srv.close()
    assert r["ok"] is False
    assert elapsed < 4, f"a wedged scheduler stalled the caller for {elapsed:.1f}s"


def test_an_unreachable_refresh_keeps_the_last_known_nodes(monkeypatch):
    """Blinking the panel out on one failed poll would throw away the fact that
    a cluster exists at all. The nodes stay, marked stale and unreachable —
    which is exactly the state worth seeing during a local-only build."""
    monkeypatch.setattr(icecc, "configured",
                        lambda *a, **k: {"scheduler": "1.2.3.4",
                                         "netname": "asteroid", "max_jobs": "8"})
    monkeypatch.setattr(icecc, "query",
                        lambda *a, **k: {"ok": True, "banner": BANNER,
                                         "out": {"listcs": LISTCS, "listjobs": ""}})
    icecc._cache.update(ts=0.0, data=None)
    first = icecc.summary()
    assert first["reachable"] and len(first["nodes"]) == 3

    monkeypatch.setattr(icecc, "query",
                        lambda *a, **k: {"ok": False, "banner": "", "out": {},
                                         "error": "No route to host"})
    second = icecc.summary(force=True)
    assert second["reachable"] is False
    assert second["stale"] is True
    assert len(second["nodes"]) == 3, "the cluster vanished on one failed poll"
    assert "No route" in second["error"]


def test_summary_is_none_on_a_host_without_icecream(monkeypatch):
    monkeypatch.setattr(icecc, "configured", lambda *a, **k: None)
    icecc._cache.update(ts=0.0, data=None)
    assert icecc.summary() is None


# ── node temperature: strictly optional ──────────────────────────────────────

_HWMON = """/sys/class/hwmon/hwmon0/name:AC
/sys/class/hwmon/hwmon1/name:acpitz
/sys/class/hwmon/hwmon2/name:BAT0
/sys/class/hwmon/hwmon4/name:coretemp
/sys/class/hwmon/hwmon1/temp1_input:96000
/sys/class/hwmon/hwmon4/temp1_input:97000
/sys/class/thermal/thermal_zone1/type:x86_pkg_temp
/sys/class/thermal/thermal_zone1/temp:95000
"""


def test_sensors_pair_by_directory_not_by_order():
    """hwmon3/name goes with hwmon3/temp1_input, and the two greps interleave
    arbitrarily. Pairing by position would attach a CPU temperature to the
    battery."""
    t = icecc.parse_temps(_HWMON)
    assert t["coretemp"] == 97.0
    assert t["acpitz"] == 96.0
    assert t["x86_pkg_temp"] == 95.0
    assert "AC" not in t and "BAT0" not in t, "a sensor with no reading appeared"
    assert icecc.parse_temps("") == {}
    assert icecc.parse_temps("garbage with no colon") == {}


def test_the_cpu_sensor_wins_over_the_dozen_others():
    """A laptop reports battery, wifi, nvme and GPU zones. The one that answers
    'is this node cooking' is the CPU package."""
    assert icecc.best_temp(icecc.parse_temps(_HWMON)) == (97.0, "coretemp")
    # No CPU sensor at all: surface the hottest thing rather than nothing —
    # an unknown but alarming reading still matters.
    assert icecc.best_temp({"nvme": 40.0, "amdgpu": 78.0}) == (78.0, "amdgpu")
    assert icecc.best_temp({}) is None
    # Values some hwmon entries report as constants must not be believed.
    assert icecc.best_temp({"weird": 0.0, "coretemp": 55.0}) == (55.0, "coretemp")
    assert icecc.best_temp({"weird": 3000.0}) is None


def test_the_probe_is_shell_agnostic():
    """Measured across this cluster: the three nodes run zsh, fish and bash, and
    zsh ABORTS the whole command on an unmatched glob — an unguarded
    /sys/class/thermal/* returned nothing at all from the e15. Wrapping in sh -c
    lets a POSIX shell do the globbing whatever the login shell is."""
    assert "sh -c" not in icecc._TEMP_INNER, "the wrapper belongs at the call site"
    inner = icecc._TEMP_INNER
    assert "$(" not in inner and "`" not in inner, "command substitution is not portable here"
    assert "=" not in inner.replace("2>/dev/null", ""), "shell variables break under fish"


def test_a_node_without_ssh_access_simply_has_no_temperature(monkeypatch):
    """Optional throughout. A node we cannot log into shows nothing — never an
    error, never a nag. The cluster panel must not become an SSH-access prompt
    on somebody's laptop."""
    import subprocess as sp

    def refused(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(sp, "run", refused)
    assert icecc.node_temp("10.255.255.1") is None

    monkeypatch.setattr(sp, "run",
                        lambda *a, **k: type("P", (), {"returncode": 255,
                                                       "stdout": ""})())
    assert icecc.node_temp("10.255.255.1") is None


def test_temperatures_are_probed_in_parallel(monkeypatch):
    """Serially, three SSH handshakes sit inside a refresh that also talks to
    the scheduler, and one unreachable node delays every node behind it."""
    import time as _t
    monkeypatch.setattr(icecc, "node_temp",
                        lambda ip, *a, **k: (_t.sleep(1), (50.0, "coretemp"))[1])
    nodes = [{"ip": f"10.0.0.{i}"} for i in range(4)]
    start = _t.monotonic()
    got = icecc.refresh_temps(nodes)
    elapsed = _t.monotonic() - start
    assert len(got) == 4
    assert elapsed < 2.5, f"probed serially: {elapsed:.1f}s for 4 nodes"


def test_a_reading_survives_a_non_zero_exit(monkeypatch):
    """MEASURED on the e15, 2026-08-08: it has no /sys/class/thermal tree at
    all, so the probe's second grep finds nothing, exits 2, and takes the whole
    `sh -c` with it — while stdout carries a perfectly good k10temp reading.
    Gating on the exit status threw that away and the node showed no
    temperature at all. Partial success is the normal case; the output is the
    signal, not the return code."""
    import subprocess as sp
    e15 = ("/sys/class/hwmon/hwmon3/name:k10temp\n"
           "/sys/class/hwmon/hwmon3/temp1_input:45375\n")
    monkeypatch.setattr(sp, "run",
                        lambda *a, **k: type("P", (), {"returncode": 2,
                                                       "stdout": e15})())
    got = icecc.node_temp("192.168.176.164")
    assert got is not None, "a good reading was discarded because grep exited 2"
    assert round(got[0], 1) == 45.4 and got[1] == "k10temp"

    # But genuinely empty output still yields nothing rather than a fake zero.
    monkeypatch.setattr(sp, "run",
                        lambda *a, **k: type("P", (), {"returncode": 2,
                                                       "stdout": ""})())
    assert icecc.node_temp("192.168.176.164") is None
