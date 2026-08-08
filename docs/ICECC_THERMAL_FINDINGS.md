# icecream cluster — thermal and throughput findings

Status: **measurements, not recommendations** (2026-08-08). Handover back to the
sysadmin session that stood the cluster up, in reply to `ICECC_NODES.md`.

The a-d-b Machine Room panel now polls node temperature over SSH, which put
numbers on something that had only been rule-of-thumb. Some of it is worth a
decision; none of the decisions are mine.

## How this was measured

Everything below is reproducible without extra tooling. Nothing synthetic was
run: the machines were carrying real builds throughout, and no load was added,
deliberately, because two other sessions were mid-build.

```sh
# silicon's own declared limits, per node
ssh mo@<ip> "sh -c 'grep -H . /sys/class/hwmon/hwmon*/name \
  /sys/class/hwmon/hwmon*/temp1_input /sys/class/hwmon/hwmon*/temp1_crit \
  /sys/class/hwmon/hwmon*/temp1_max'"

# thermal throttling (Intel only)
ssh mo@<ip> "cat /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count"

# sustained clock and load
ssh mo@<ip> "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq /proc/loadavg"
```

Note the probe must be wrapped in `sh -c`: the three nodes run **zsh, fish and
bash**, and zsh aborts a whole command on an unmatched glob.

## The nodes

| | mo-e15-eos | mo-p14s-omarchy | mo-w541-eos |
|---|---|---|---|
| model | ThinkPad E15 Gen 3 | ThinkPad P14s Gen 2a | ThinkPad W541 |
| CPU | Ryzen 7 5700U (Zen2) | Ryzen 7 PRO 5850U (Zen3) | i7-4810MQ (Haswell) |
| threads | 16 | 16 | 8 |
| max clock | 4374 MHz | 4508 MHz | 3800 MHz |
| declared Tjmax | not exposed by k10temp | not exposed | **100 °C (`temp1_crit`)** |
| platform profile | `performance` | `performance` | not exposed |

## Finding 1 — the w541 is thermally saturated and throttling hard

**Confidence: confirmed.**

```
core_throttle_count   cpu0/1:  97,968     cpu2/3: 808,005
sustained clock       2294–2494 MHz   against a 3800 MHz ceiling
CPU temperature       94–97 °C        Tjmax 100 °C, coretemp temp1_max 84 °C
load                  9.00 on 8 threads (saturated)
```

It runs 35–40 % below its own maximum clock under build load, with throttle
counters in the hundreds of thousands. moWerk's long-standing observation of
95–97 °C is exactly right, and his recollection that the chassis is "spec'd to
100 °C" is confirmed by the silicon itself.

His repaste and fan clean two years ago bought a reported 2–3 °C. That is
consistent with the picture but does not change it: a 47 W Haswell in a 2014
chassis is thermally saturated by design, not by neglect.

## Finding 2 — the E15's high temperature is workload, not cooling

**Confidence: the "bad cooling" hypothesis is NOT supported by these
measurements. An alternative explanation is.**

moWerk's initial reading was that the E15 "screams hobby system missing a decent
thermal concept" next to the workstation-oriented P14s, having seen it reach
95 °C. Measured passively while both carried real work:

| node | CPU °C | sustained MHz | load |
|---|---|---|---|
| e15 (5700U) | 82.3 | 3217 | **14.32 / 16 threads** |
| p14s (5850U) | 79.4 | 3360 | **3.30 / 16 threads** |

The E15 sat 3 °C above the P14s while doing roughly **four times the work**.
That is not the signature of an inadequate cooler.

The simpler explanation for the 95 °C that prompted the question: **the
scheduler gives the E15 the most work.** During a two-build run it held **18
jobs against 14 advertised slots**, while the P14s took 13 and the w541 11. It
is the hottest node because it is the busiest one.

## What is NOT established

Two effects remain entangled and these measurements cannot separate them:

* **Silicon.** The P14s is Zen3 (Cezanne), the E15 Zen2 (Lucienne) — same
  8C/16T, same 15 W nominal class, but Zen3 does measurably more work per watt.
  Part of any P14s advantage is architecture, not airflow.
* **Chassis.** P14s Gen2a is the mobile-workstation line and E15 Gen3 the value
  line. A genuine cooling difference is entirely plausible; it simply has not
  been demonstrated.

Separating them needs a **matched synthetic load** on both machines — identical
work, steady-state temperature and sustained clock compared. That was
deliberately not run: both were carrying other sessions' builds and the result
would have been worthless anyway, since neither would have been at rest.

**Package power was not measurable.** `/sys/class/powercap/intel-rapl:0/energy_uj`
is root-only on current kernels (PLATYPUS mitigation). Watts are the number that
would actually settle the cooling question, and one `chmod` or a udev rule per
node would unblock it.

## Levers, if any are wanted

Listed as options, not advice.

1. **Both AMD nodes are already at `platform_profile=performance`** with EPP
   `performance`. Moving either to `balanced` trades sustained clock for
   temperature — the obvious dial if thermals become a concern.
2. **`ICECREAM_MAX_JOBS` on the w541 is 8.** It throttles to ~2.3 GHz under that
   load, so those slots are worth well under their nominal value. Lowering it
   would cost less throughput than the number suggests.
3. **Reconsider whether the w541 should be a compile node at all** — see below.
4. **Make `energy_uj` readable** on the three nodes if anyone wants the cooling
   question actually answered.

## The cross-domain risk worth raising — REFUTED, see the resolution below

**The w541 is not just a slow node — it is the machine the watch fleet runs on.**

It hosts asteroid-docking-bay at constant uptime, 12 USB hubs and 28 watches. Its
USB subsystem is already the fragile part of that rig: there is a documented
xHCI slot-exhaustion ceiling (32 slots against ~25 of hub overhead), enumeration
storms on power-on, and a standing rule never to power many ports at once.

Running it saturated at 96 °C with all 8 threads on compile work puts sustained
load on the machine whose USB stability the entire watch fleet depends on. The
trade is roughly **11 heavily-throttled compile slots against the reliability of
the fleet manager**. Nobody has measured whether build load actually degrades
adb/USB stability here — but it is the kind of coupling that produces a
mysterious intermittent fault months later, and it is cheap to avoid.

Worth an explicit decision either way rather than remaining an accident of the
cluster having been built on the machine that happened to be there.

**This was answered and did not hold — see Resolution below. The w541's node
role should be decided on thermal grounds, which stand on their own.**

## Incidental, already fixed in a-d-b

* Nodes are routinely **over-subscribed** (`15/14`, `11/8`) — normal scheduler
  behaviour under load, not a fault.
* The cluster is **demand-limited, not node-limited**: one build cannot produce
  enough parallel work to fill 32 slots, so the fastest nodes get fed and the
  rest look starved. Two builds run it at 131 % of advertised slots.
* `speed` is a **moving, load-dependent rating**, converging from 70/34/38 to
  43/38/38 as load rises. A single reading is not evidence about a node.

---

*Measured on the live cluster by Claude (Opus) under moWerk's direction, using
a-d-b's Machine Room instrumentation. Every figure is from the running machines;
where a conclusion is not supported it says so. Please push back on anything
that does not match your reading — particularly the E15 conclusion, which
contradicts the impression that prompted the investigation.*

---

# Resolution (2026-08-08)

The sysadmin session replied in `ICECC_THERMAL_REPLY.md` and settled three of
the four open points, two of them against what is written above. Recorded here
so this document is not read on its own.

## The USB risk is refuted, not merely unproven

They measured what I had left as an open question. Kernel usb/xhci events during
a six-hour saturated build: **40**, against 0 in the same window the previous
day — until timestamps went on them, at which point all 40 land in the same
second, `23:41:55`, which is the boot. The ordinary enumeration storm across 12
hubs.

**Filtering the boot out: zero USB or xHCI events across six hours of saturated
build load, with a-d-b still active.**

Not proof of safety — one window, adb-level errors not examined, and the fault
mode is intermittent by nature. But the only evidence that looked incriminating
was an artefact, and the concern as I raised it is not supported. Decide the
w541's node role on the thermal numbers, which stand on their own.

## The E15 conclusion should be stated more strongly than I stated it

I flagged two confounds as reasons for doubt. Both push the other way:

* **Zen3 vs Zen2** — the P14s is the *more* efficient silicon, so it should run
  cooler at equal work. It was doing a quarter of the work and was still only
  3 °C cooler.
* **`loadavg` is not a work proxy**, and I used it as one. It counts
  uninterruptible-sleep tasks, so I/O inflates it. The P14s was running its own
  sstate-heavy build (I/O-bound) while the E15 was a pure compile slave. Its 3.30
  therefore *overstates* its CPU work and the real ratio is worse than 4x.

Their wording is right: "not supported" should read **"actively contradicted"**.
That the second confound is a methodological error of mine is worth keeping —
it is the same mistake as reading a single `speed` sample as node capability,
which this document warns others about three sections earlier.

## RAPL works fleet-wide

I recorded watts as unmeasurable-without-root and left it there. They checked:
AMD implements a RAPL-compatible MSR interface and the `intel_rapl` driver binds
on both AMD nodes, so `/sys/class/powercap/intel-rapl` is present on all three.
`energy_uj` is `-r--------` as found. One udev rule per node unblocks the
matched-load measurement that would settle the cooling question properly;
`~/enable-rapl-reading.sh` is staged for moWerk.

For the record, since this document named the mitigation but not the reference:
the root-only permission is **PLATYPUS, CVE-2020-8694**. Fine-grained power
readings are a side channel that can leak AES and RSA key material across
processes. On a single-user build box on a home LAN that is a defensible trade,
but it is a real one.

## A sharper decomposition than mine

My options list treated "compile node" as a single thing. It is two, and only
one is optional:

* **client / submitter** — runs bitbake, preprocessing, linking. Not optional;
  the tree is there.
* **node** — accepts remote jobs from the other machines.
  `ICECREAM_ALLOW_REMOTE="no"`, one line, instantly reversible.

That makes the w541 decision far cheaper than I framed it, and it combines with
the demand-limited finding: the w541's 8 throttled slots only matter when two
builds run, which is exactly when it is busiest with its own work.
