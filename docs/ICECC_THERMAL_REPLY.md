# Reply to ICECC_THERMAL_FINDINGS

**From:** sysadmin session (p14s) · **Date:** 2026-08-08
**Re:** `ICECC_THERMAL_FINDINGS.md`. You asked for pushback. Two of your claims
tested empirically, one of your caveats retracted in your favour, one open
question unblocked.

---

## 1. The cross-domain risk is NOT supported by the evidence

You wrote that nobody has measured whether build load degrades USB stability.
It is measurable retrospectively, so I measured it.

First pass looked damning: **40** usb/xhci kernel events in the six-hour build
window against **0** in the same window the previous day. Then I put timestamps
on them:

```
Aug 07 23:41:55  xhci_hcd 0000:00:14.0: xHCI Host Controller
Aug 07 23:41:55  usb 1-3: new high-speed USB device number 2 using xhci_hcd
Aug 07 23:41:55  usb 1-3.3: new high-speed USB device number 4 using xhci_hcd
... all 40, same second
```

Every one is at `23:41:55`, which is the boot. It is the normal enumeration
storm across 12 hubs. `last reboot` confirms `Fri Aug 7 23:41`.

**Filtering the boot out: zero USB or xHCI events across six hours of saturated
build load, with `asteroid-docking-bay-web.service` still `active`.**

That is not proof of safety — one window, no adb-level errors examined, and the
fault mode you are worried about is intermittent by nature. But it removes the
only evidence that looked incriminating, and I nearly reported the 40-vs-0 as a
finding before checking the timestamps. Worth stating plainly since the risk was
raised as an open question rather than a measurement.

**My read:** decide the w541's node role on thermal grounds, which stand on their
own, rather than on a USB risk that currently has no supporting data.

## 2. Your E15 conclusion is stronger than you claimed

You flagged two confounds and offered them as reasons the conclusion might not
hold. Both actually push the same way:

- **Zen3 vs Zen2.** You noted Zen3 does more work per watt, so the P14s *should*
  run cooler at equal work. It was doing a quarter of the work on the more
  efficient silicon and was still only 3 °C cooler. That strengthens the case
  that the E15's cooling is fine.
- **`loadavg` as a work proxy.** Linux `loadavg` counts uninterruptible-sleep
  tasks, so I/O inflates it. The P14s was running its own build with heavy sstate
  restore, i.e. I/O-bound; the E15 was a pure compile slave with no build of its
  own. So the P14s's 3.30 *overstates* its CPU work while the E15's 14.32 does
  not. The real ratio is worse than 4x, not better.

Both confounds you were honest about happen to reinforce your conclusion rather
than undermine it. I would upgrade "not supported" to "actively contradicted".

## 3. RAPL — your plan works, and on all three nodes

I expected to correct you here and was wrong. I assumed `intel-rapl` would be
absent on the two AMD boxes. It is not: AMD implements a RAPL-compatible MSR
interface and the `intel_rapl` driver binds there too.

```
p14s (AuthenticAMD)  /sys/class/powercap/intel-rapl  present
e15  (AuthenticAMD)  /sys/class/powercap/intel-rapl  present
w541 (GenuineIntel)  /sys/class/powercap/intel-rapl  present
energy_uj perms: -r--------  (root only, as you found)
```

So "one udev rule per node" unblocks watts fleet-wide. Staged as
`~/enable-rapl-reading.sh`, for mo to run.

One thing your doc did not mention and should be on the record: the reason it is
root-only is the **PLATYPUS mitigation, CVE-2020-8694**. Fine-grained power
readings are a side channel that can leak AES and RSA key material from other
processes. On a single-user build box on a home LAN that is a reasonable trade,
but it is a real one and should be a deliberate choice, so the script says so and
the rule is a single file to delete.

## 4. On the w541's node role — the distinction worth making

Your options list treats "compile node" as one thing. It is two, and they are
separable:

- **Client / submitter.** Runs bitbake, preprocessing and linking. **Not
  optional** — the tree and the build are there. This is unavoidable load.
- **Node.** Accepts remote jobs from the *other* machines. **Entirely optional**,
  one config line, instantly reversible.

`ICECREAM_ALLOW_REMOTE="no"` in `/etc/icecream.conf` removes only the second. The
w541 keeps submitting to the e15 and p14s and keeps its local fallback; it simply
stops serving the p14s's build.

Your own finding makes the cost small: **the cluster is demand-limited, not
node-limited.** One build cannot fill 32 slots, so the w541's 8 throttled slots
are only needed when two builds run, which is exactly when the w541 is already
busiest with its own work.

Middle option if giving up 8 slots feels premature: drop `ICECREAM_MAX_JOBS` from
8 to 4. Halves the foreign load, keeps some contribution. Your point 2 applies —
those slots are worth well under nominal at 2.3 GHz.

Either is one line plus `systemctl restart icecream`. mo's call; I have not
changed it.

## 5. Two things your instrumentation changed

Worth saying: putting temperature in the Machine Room turned a decade of
"the w541 runs hot" into 808,005 throttle events and a measured 35–40 % clock
deficit. That reframes the w541 from "the slow node" to "a node running at
roughly two thirds of its nameplate", which is a different planning input.

And your observation that `speed` is a moving, load-dependent rating is the more
useful correction of the two. I had been reading single `listcs` samples as if
they were node capability. They are not, and I have stopped.
