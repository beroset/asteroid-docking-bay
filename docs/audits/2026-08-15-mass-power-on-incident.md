# Incident: the whole A16 cascade powered itself on — 2026-08-15

**Severity: worst rig incident since a-d-b began.** Every watch on the `1-3`
cascade went from a deliberate shelved/powered-off state to booting, at once,
unattended. Nobody pressed a button and no software issued a power-on.

Written by the a-d-b session that caused it. Recorded here rather than only in
the chat because the mechanism is a standing property of this rig, not a
one-off: **it will happen again under the same provocation.**

## What mo observed (the ground truth)

Not in view of the rig; heard every vibrator fire at once. Moved over and saw
**all blue port LEDs lit and powered**, with every watch booting up out of a
shelved power-off state. No physical port buttons were touched at any time.
Afterwards the UI power-down brought them all back to `shelved` cleanly and the
UI state again matches the hardware.

## Root cause

The kernel reset the root port carrying the entire A16 cascade, and the hub
came back at its power-on default: **all ports on**.

```
18:57:48 usb usb1-port3: disabled by hub (EMI?), re-enabling...
18:57:48 usb 1-3:     USB disconnect, device number 2
18:57:48 usb 1-3.2 / 1-3.3 / 1-3.3.3 / 1-3.3.4 / 1-3.4 / 1-3.4.3: USB disconnect
18:57:48 usb 1-3:     new high-speed USB device number 76 using xhci_hcd
                      Product: USB2.1 Hub  (0bda:5411)
                      hub 1-3:1.0: 4 ports detected
18:58:10 usb usb1-port3: disabled by hub (EMI?), re-enabling...   <-- again
```

It happened **three times** — 18:57:48, 18:58:10 and 18:58:38 — inside 50
seconds. Device numbers ran from 82 past 127 across the event: the whole
cascade re-enumerated each time.

**The third one is also what killed the a-d-b dump.** At 18:58:38 the dump was
not aborted by a watch-side drop; `usb1-port3` was disabled, taking the whole
tree — hoki included — with it. So the 85 MB truncation and the mass power-on
are the SAME event, not two.

**The mechanism (inferred, but the only reading consistent with all the
evidence):** a-d-b sets per-port power over PPPS, and PPPS state lives in hub
firmware. It is **volatile** — a hub that is reset and re-enumerated comes back
at its descriptor default, which on these Realtek 0bda:5411 hubs is
ports-powered. So the shelved ports were not switched on by anybody; the hub
simply forgot they were off. That is consistent with every fact: mo's LEDs,
the absence of any power-on call, and no button presses.

Falsifiable prediction, and the reason this is worth recording: **any future
re-enumeration of the `1-3` tree will again power every port on it back on**,
regardless of what a-d-b believes.

## What provoked the port reset — the a-d-b session, i.e. me

hoki sits at `1-3:2`, on that same cascade. I was diagnosing a dump failure and
ran **four aborted bulk transfers against it in under five minutes** — three
`adb pull /dev/mmcblk0` attempts and one a-d-b dump. Each abort dropped hoki's
link:

```
18:55:22 usb 1-3.2: new high-speed USB device number 73 ... Product: Hoki
18:55:23 usb 1-3.2: USB disconnect, device number 73
18:55:26 usb 1-3.2: new full-speed USB device number 74
18:55:27 usb 1-3.2: new high-speed USB device number 75
18:57:48 usb usb1-port3: disabled by hub (EMI?), re-enabling...   <-- mass event
```

Both mass re-enumerations (18:57:48, 18:58:10) fall **during the `adb pull`
attempts and before the a-d-b dump started at 18:58:27**. Repeatedly aborting a
7.65 GB read on a port is exactly the electrical disturbance the kernel labels
`disabled by hub (EMI?)`.

The judgement error was mine and it was not subtle: after the first abort I
retried, then retried again, then started a 7.65 GB dump on top — on a rig
whose documented failure mode is precisely this. The correct move after abort
number one was to stop and reason about blast radius.

## What a-d-b got RIGHT

Worth stating, because none of it failed:

- **No power-on was ever issued.** The full day's power-affecting API calls are
  ten `poweroff`/`off` requests, all from mo's browser. Zero `on`, zero cycles,
  zero `uhubctl` actuation.
- **The oplock guard (task #41) worked exactly as designed:**
  `18:58:28 WARNING: adb server looks wedged, but K6F1041337B1510 held for dump
  — NOT restarting the server, which would sever a transfer in flight`
- **The dump refused to lie.** It compared against the size preflight, called
  the 85 MB result truncated, and renamed it `.partial` rather than presenting
  it as a backup.

## An initial wrong conclusion, and why

My first pass concluded "nothing powered on" from the absence of power-on calls
in the a-d-b log. mo's physical observation — LEDs lit, vibrators, watches
booting — overrode it. **Log absence proved only that a-d-b never issued a
per-port power-on; it said nothing about VBUS being re-asserted below that
layer.** The kernel log was the source that could settle it, and it should have
been the first place looked, not the second. Corrected before any action was
taken on the wrong conclusion.

## Second finding: the hoki dump is broken on this rig, both ways

Separate from the incident, and the reason beroset cannot back up his hoki.

beroset reported `watch-image` failing partway with
`[   ?] /dev/mmcblk0: 461111296/?`, never writing an image. The tool is
`asteroid-hosttools/watch-image`, and its `saveImage` takes the adb path:

```bash
function saveImage {
    if [ "${ADB}" == true ] ; then
        adb pull /dev/mmcblk0 "${imagefile}"          # beroset's path
    else
        ssh ... "dd if=/dev/mmcblk0" | dd of=... bs=4096 status=progress
    fi
}
```

`adb pull` on a raw block device cannot work properly: the sync protocol stats
the file first, a block device reports size 0 — hence the `?` total — so there
is no completeness check even on a run that appears to succeed.

Reproduced on the rig three times: aborted at **75,431,936**, **85,196,800**,
and beroset's **461,111,296** bytes — different every time, exit 1, **no file
written and no error message printed**. All three of his symptoms.

**The a-d-b dump then also stopped, at 85,413,888 bytes — but NOT for the same
reason, and an earlier version of this document got that wrong.** Its abort at
18:58:38 coincides exactly with the third `usb1-port3: disabled by hub` event:
the transport did not fail, the whole cascade was pulled out from under it.

That distinction matters, because it is the difference between "our dump
command is broken" and "this rig's cascade cannot sustain a bulk read".
**`adb exec-out dd` is NOT disproven** — it was never given an uninterrupted
run. Task #37 therefore remains *unproven*, not *failing*.

Two distinct failure modes were seen, and conflating them is what produced the
wrong conclusion:

| time | what the kernel logged | scope |
|---|---|---|
| 18:55:23 | `1-3.2: USB disconnect` → re-enumerates full-speed then high-speed | **hoki alone** — a device-side drop |
| 18:57:48 / 18:58:10 / 18:58:38 | `usb1-port3: disabled by hub (EMI?), re-enabling...` | **the whole `1-3` cascade** |

The second mode is the dangerous one and it is **electrical, not
software**: a root port tripping under sustained bulk transfer, with the hub
losing its per-port power state on the way back. Per the standing
hardware-scoping rule, that is handed over rather than debugged in code — no
amount of a-d-b logic prevents a hub from forgetting its port state when the
kernel resets it.

Expected size is known and confirmed: `/sys/class/block/mmcblk0/size` reads
14,942,208 sectors → **7,650,410,496 bytes**, exactly matching the archived
`original-hoki.img` from 2025-10-30. Root is available (`uid=0`). So neither
size-read nor privilege is the blocker — the **link** is.

Untested and the most promising lead: `watch-image`'s own **SSH path**, which
is the same single-pipeline `dd | dd` form a-d-b uses over SSH, and which is
how the verified `original-sol` dump was taken
(`transport: adb exec-out dd, single pipeline`, two dumps, matching sha256).

## Open questions — not answered here

1. Can a hoki dump complete at all over USB on this rig, or does the `1-3` root
   port trip under sustained bulk read regardless of transport? **Untested, and
   deliberately not tested on 2026-08-15**: the porting session is working sol
   live at `1-3.4:3`, on the same cascade, so a retry would risk resetting the
   tree under someone else's work as well as powering watches.
2. Does the SSH/RNDIS path survive where adb does not?
3. Should a-d-b re-assert its intended per-port power state after it detects a
   hub re-enumeration — or is silently "fixing" VBUS after a hub reset worse
   than leaving it wrong and visible? This one is mo's call; it powers watches.
4. Is the `1-3` root port tripping specific to this A16 box, this host port, or
   this cable? Hardware question, for mo.

## If a dump is retried on this rig

Conditions that would make a retry defensible, none of which held tonight:

- Nothing else live on the same cascade (sol's port is on `1-3`).
- One attempt. On abort, **stop** and read the kernel log before anything else.
- `journalctl -kf` watched during the run, so a `disabled by hub` event is seen
  as it happens rather than reconstructed afterwards.
- Ideally the target watch on a cascade of its own, so a trip cannot reach the
  rest of the fleet.

## Rules this incident argues for

- **Never retry a failed bulk transfer on the rig without stopping to reason.**
  One abort is data; three is a provocation.
- **A dump is a rig-wide event, not a per-watch one.** It sustains maximum
  bandwidth on a shared cascade, and this rig answers that with port resets.
- Treat `disabled by hub (EMI?)` in the kernel log as the signature of a
  fleet-wide power-state loss, not a cosmetic USB hiccup.
