# Asteroid nodes — the icecream compile cluster in the dock (design)

Status: **proposed, nothing implemented** (2026-08-08). The cluster itself is
**built, live and verified**; only the a-d-b feature is outstanding. Handover
from the sysadmin session that stood the cluster up.

> mo's framing: *"in a-d-b context, these node displays would be nodes on the
> asteroid the watches are docked to."*
>
> The watches dock to the asteroid. The asteroid has its own compute. Showing
> the compile nodes here rather than in a separate tool is the same instinct as
> the Orbit port: one place that knows the whole fleet, docked or not.

This is deliberately NOT a new dashboard. An AsteroidOS infrastructure monitor
was considered and rejected as reinventing a-d-b, which already runs at constant
uptime on the w541, is already the AsteroidOS surface, and is already where the
web UI work happens.

## The idea in one line

If the host a-d-b runs on is part of an icecream cluster, show each compile node
as a panel strip below the Orbit area, live, whether or not a build is running.

## Why it earns its place

icecream fails **silently**. Yocto's `icecc.bbclass` reports a non-distributing
build at `bb.note` level, which scrolls past unread, and the cluster still looks
perfectly healthy from the outside: scheduler active, all nodes registered,
zero errors. A build can run for hours entirely local while every indicator says
the cluster is fine.

The only reliable tell is job counts moving on the nodes. That makes this panel a
correctness instrument, not decoration.

## The cluster as built

Netname **`asteroid`**. Scheduler on the **e15 at 192.168.176.164**, pinned by IP
because firewalld is active on all three hosts and UDP broadcast discovery
through it is unreliable.

| node | address | slots | role |
|---|---|---|---|
| mo-e15-eos | 192.168.176.164 | 14 | scheduler + dedicated slave |
| mo-p14s-omarchy | 192.168.176.117 | 10 | node + client |
| mo-w541-eos | 192.168.176.21 | 8 | node + client, **runs a-d-b** |

32 slots total. Ports: scheduler 8765/tcp + 8765/udp + 8766/tcp, daemons
10245/tcp. firewalld rich rules scope all of it to `192.168.176.0/24`.

## Detection contract

a-d-b must degrade to nothing at all when icecream is absent. Never assume the
host is a cluster member.

1. `/etc/icecream.conf` exists. It is shell-syntax `KEY="value"`. Read
   `ICECREAM_SCHEDULER_HOST`, `ICECREAM_NETNAME`, `ICECREAM_MAX_JOBS`.
2. An `icecc` binary exists. **Check both** `/usr/lib/icecream/bin/icecc` and
   `/usr/bin/icecc`. The Arch package installs under `/usr/lib/icecream/` and
   puts nothing in PATH; the symlink into `/usr/bin` was added by hand and may
   not exist on another host.
3. `ICECREAM_SCHEDULER_HOST` is non-empty. If it is empty the daemon relies on
   broadcast discovery and there is no address to query. Treat as not-configured.

Missing any of these means the feature renders **nothing**. No empty panel, no
error strip.

## Data source: the scheduler command port

TCP **8766** on the scheduler host. A line-based command protocol, **not HTTP**.
Pointing a browser at it yields `Invalid command 'GET / HTTP/1.1'` once per
header line, which looks alarming and means nothing.

Use a plain Python socket. Do not shell out to `nc`, it is not guaranteed
present and a-d-b has no other dependency like it.

The port is live **always**, not only during builds. The banner alone carries
host count and queue depth.

### Session shape

On connect, two banner lines arrive unprompted:

```
200-ICECC 1.4.0: 3602s uptime, 3 hosts, 0 jobs in queue (0 total).
200 Use 'help' for help and 'quit' to quit.
```

Then write commands, each newline-terminated. Every command's output is
terminated by a line `200 done`. Finish with `quit`, which answers
`200 Good Bye!`.

Full command set, confirmed against the running 1.4.0 scheduler:

```
listcs  listblocks  listjobs  removecs  blockcs  unblockcs  internals  help  quit
```

**Only `listcs` and `listjobs` should ever be sent.** `removecs`, `blockcs` and
`unblockcs` mutate cluster state and have no business behind a status poll.

### `listcs` output, verbatim

```
 mo-e15-eos (192.168.176.164:10245) [x86_64] speed=0.00 jobs=0/14 load=118
 mo-p14s-omarchy (192.168.176.117:10245) [x86_64] speed=0.00 jobs=0/10 load=571
 mo-w541-eos (192.168.176.21:10245) [x86_64] speed=0.00 jobs=0/8 load=54
200 done
```

Note the **leading space** on each node line. Fields:

- hostname, then `(ip:port)` where port is the daemon's 10245
- `[arch]`
- `speed=` a scheduler-calibrated throughput score. **`0.00` until the node has
  completed real jobs**, so a fresh cluster shows all zeros. Do not render this
  as "node is broken".
- `jobs=used/max` — the number that matters
- `load=` 0..1000 scale, not a Unix load average. 1000 is saturated.

### `listjobs`

Empty when idle, one line per in-flight job during a build. This is the
"distribution is actually happening" signal.

## Suggested shape

Mirror `orbit.py`. One module, heavy docstring, SPDX header, no framework.

- `asteroid_docking_bay/icecc.py`
  - `configured()` → the detection contract above, returns the parsed conf or None
  - `query(host, commands)` → socket, banner, commands, `200 done` framing
  - `nodes()` → `[{host, ip, arch, jobs_used, jobs_max, load, speed}]`
  - `summary()` → `{netname, scheduler, uptime_s, hosts, queue, nodes: [...]}`
- `tests/test_icecc.py` — parse the verbatim samples above, including the
  leading-space quirk, `speed=0.00`, an empty `listjobs`, a scheduler that
  accepts the connection then never answers, and connection refused.
- Wire into `webstatus.py` so it reaches `/api/status`, and render in
  `webtemplate.py` below the Orbit block.

### Do not block the status path

`/api/status` is polled continuously and the scheduler is on **another machine**.
A dead e15 or a dropped LAN link must not stall the watch fleet UI.

Follow the pattern `orbit.py` already sets, where the expensive probe is
deliberate and the status path stays cheap: cache the scheduler read behind a
short TTL of a few seconds, refresh off the poll thread, hard socket timeout
around 2s, and serve the last known value with a staleness marker when a refresh
fails. The watch fleet is the primary function; compile nodes are secondary and
must never degrade it.

### UI states worth distinguishing

- not configured → render nothing
- configured, scheduler unreachable → nodes shown greyed with a "scheduler
  unreachable" note. This is a real and useful state: it means builds are
  running local right now.
- configured and reachable, all `jobs=0` → idle cluster, healthy
- jobs non-zero → building, and worth making visually obvious, since this is the
  answer to "is my build actually distributing"

The three-state colouring already used for the moSushi fleet panels is a decent
reference: live and working, deliberately idle, and unreachable are three
different things and should not collapse into two.

## Context the implementer may want

The cluster exists to unblock the sol/nemo porting builds, which were bound by
the w541's 8 threads. Build config lives in
`w541:/home/mo/Git/asteroid/build/conf/local.conf` under a marked
`--- icecc distributed compiling ---` block: `INHERIT += "icecc"`,
`ICECC_PATH = "/usr/lib/icecream/bin/icecc"`, `ICECC_PARALLEL_MAKE = "-j 24"`,
`PARALLEL_MAKE = "-j 6"`, `BB_NUMBER_THREADS = "4"`.

Under oe-core yocto-5.2.4 the kernel is **not** excluded from distribution, and
`icecc_get_tool()` resolves kernel recipes through `KERNEL_CC`, so meta-clang's
`LLVM=1` kernel ships clang correctly. The known exposure is the reverse case:
clang-built **userland** recipes get no wrapper, because the class hardcodes
`${HOST_PREFIX}gcc`/`g++` for non-kernel targets.

Fuller notes, including a suspected upstream symlink bug with absolute
`KERNEL_CC` paths, are in the sysadmin session memory at
`~/.claude/projects/-home-mo-Sysadmin/memory/reference-icecc-build-cluster.md`.

Cluster state can be checked by hand at any time with:

```
printf 'listcs\nquit\n' | nc 192.168.176.164 8766
```

---

# Observed in practice (2026-08-08)

Added after implementing the panel and watching it against real builds. Two of
these correct claims made from a single snapshot, which is the lesson worth
keeping.

## The cluster is demand-limited, not node-limited

Measured by sampling the scheduler once a second:

| | one build (p14s) | two builds (w541 + p14s) |
|---|---|---|
| total jobs | 24 | **42** — 131% of the 32 advertised slots |
| mo-e15-eos | 17–18 / 14 | 18 / 14 |
| mo-w541-eos | **0–1 / 8**, alternating | **11 / 8**, rock steady |
| mo-p14s-omarchy | 5–6 / 10 | 13 / 10 |
| stability | w541 flickering | 30/30 samples identical |

With a single build there is simply not enough parallel work to fill the
cluster, so the scheduler feeds the fastest nodes and the others look starved. A
second client saturates everything and **every node runs over its advertised
maximum**.

An earlier note in commit 7aad62f concluded from the one-build case that "the
scheduler gives the w541 ~0-1 of its 8 slots" and that the 32 slots therefore
"overstate what a build actually gets". Both are wrong. Under real demand the
cluster runs *above* its nominal slot count, and the w541 takes a full share.
moWerk caught it by looking at the panel with two builds running.

## Nodes are routinely over-subscribed, and that is normal

`jobs=15/14`, `jobs=11/8`. The scheduler hands out more jobs than a node
advertises. Any rendering that computes a percentage from `used/max` must clamp
the bar and keep the raw count, or an over-subscribed node becomes
indistinguishable from a merely full one.

## `speed` is a moving, load-dependent rating

Not a fixed capability score. Observed on the same three nodes within an hour:

```
idle-ish cluster : e15 70.22   w541 33.86   p14s 37.69
under two builds : e15 43.28   w541 37.61   p14s 38.23
```

The ratings converge as everything loads up, so a single reading is not evidence
that one node is inherently slower than another.

## An intermittently-fed node strobes

In the demand-limited case a node alternates 0/1 jobs from poll to poll while
the cluster is never idle for a second. Rendering each sample literally makes a
working node flicker green/idle, which reads as a fault. a-d-b holds a node
"busy" for a short grace after its last seen job; the slot count beside it stays
the current sample.
