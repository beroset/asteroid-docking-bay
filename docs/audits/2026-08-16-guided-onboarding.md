# Guided onboarding — built and proven on the rig (2026-08-16)

A full-day session with moWerk at the rig. The trigger was an accident: a
plant-bug validation run overwrote the live config, leaving the w541 with an
empty mapping — 12 hubs and 27 ports gone. Rather than restore it, moWerk
declared the wreckage a test fixture: *"we will use this incident as welcome
test for the user onboarding."*

That turned out to be the most productive decision of the day. Every finding
below was found by **standing in the user's position with a genuinely empty
config**, not by reading code. Several of them were invisible from the inside.

The guided setup existed before this session. It could not onboard anything.

## What was wrong at the start

The guide was six fixed steps behind a small text link in the meta row, and it
**wrote nothing at all**: no hub map, no port, no watch. Its final screen
offered a single *Close* button. A user who followed it to the end had exactly
the config they started with.

It also opened with *"Empty the bus"* — which asks the commonest user of all,
the one who already plugged their watch in, to undo the one thing they did
right.

## Findings

Numbered as in `2026-08-16-guided-onboarding.findings.json`.

### Fixed

**F1 — the guide could not see a dumb hub.** It listed hubs from
`uhubctl_list()`, which only reports hubs uhubctl can *switch*. Measured on the
rig with the config emptied: `uhubctl_list()` found 6 hubs, `discover_hubs()`
found 12 — the difference being the Sabrent box and the Lenovo dock, both
carrying watches. A user with a plain hub was told their hardware did not
exist, on the first screen, and that user is precisely who the guide is for.

**F2 — a watch on an unmapped port was invisible.** Every status row is built
by walking the *configured* hubs, so a watch on a port no hub owns had no row:
a-d-b could see it on ADB, talk to it, and flash it, while showing an empty
table. That is the first thing a new user does — one watch, no hub, straight
into the machine. Fixed with a `direct` view beside Orbit, deliberately light
(no charge/drain/PPPS cells, because those belong to a port that can switch its
own power).

**F3 — Finish wrote nothing.** See above. Now the guide names every watch it
saw and stores serial → codename.

**F4 — the guide was unreachable.** Hidden behind a `guided setup` text link. It
now opens by itself whenever nothing is mapped, and is not summonable at all —
which also means it returns after a config loss, on the next page load. The
link is gone.

**F5 — naming was ADB-only.** The guide tells users SSH works. Reading the
codename went through ADB only, so an SSH watch answered *"did not answer"* —
blaming the watch for a link the reader never tried. Now routed through
whatever `_reachable_transport` returns.

**F6 — fastboot and Wear OS could not be named.** A watch in its bootloader has
no shell at all; a Wear OS watch has no `/etc/asteroid-release` and a hostname
of `localhost` that is correctly refused as an identity. Fastboot now answers
from `getvar product`, Wear OS from `getprop ro.product.device` — which works
because AsteroidOS codenames *are* the vendor codenames. `getprop` is tried
before `hostname`, or the search ends on the refused `localhost`.

**F7 — the watch-side power check was dead on the entire fleet.**
`adb_external_power` ran `dumpsys battery`, an *Android* command. AsteroidOS is
plain Linux and has none: measured on sturgeon, `dumpsys: command not found`,
and the function returned `None` every time. This is the corroborating half of
the PPPS verdict — the branch that catches a hub which did *not* cut VBUS — so
it existed, answered "unknown" always, and verified nothing. It now reads the
kernel's `power_supply` class.

**F8 — `adb_shell` expands globs on the HOST.** Found while fixing F7. A bare
`*` in a command expands against the laptop's own `/sys` and ships the host's
supply names (`AC`, `BAT0`) to the watch, which answers "No such file". Silent:
indistinguishable from a watch with no `power_supply` at all. The sibling
charge-status read carries the same-looking glob but wraps its pipeline in
double quotes, which suppresses host expansion — **checked against the watch
rather than assumed**, and correct as it stands.

**F9 — a-d-b reshaped watches mid-onboarding.** Staging a live SSH test needed
the fleet USB preference flipped first, or the rig kept switching the watch
back. *The workaround was the bug report*: a real user never flips the
preference, so a first-timer connecting in SSH mode would watch a-d-b silently
undo it. Fleet-wide corrections are now gated on the guide panel being **open**
(heartbeat while shown, explicit release on close), not on the last request —
a screen that polls nothing would otherwise drop out of an activity window
while being read.

**F10 — the gate was added at one call site and assumed at the others.** Within
minutes the rig proved it: a watch deliberately switched to SSH was peeled back
by the **warmer**, a sibling of the path that had been gated. Same shape as the
pre-1.0 audit's F1/F8 pair. Every caller that switches a watch's USB mode was
then classified: status aligner and warmer are automatic → gated; the flash
path, the onboard sweep and `watch.switch_*` are deliberate → untouched. The
window moved to `util`, the leaf both layers import, because keeping the state
next to one of them is what made it easy to miss the other.

**F11 — a restart re-opened the hole.** The quiet window lives in memory, so a
deploy cleared it, and the warmer's first pass runs ~1s into startup — long
before an open guide's next heartbeat. Measured exactly: the gate refused
twice, a deploy restarted the service, and one second later the same watch was
switched anyway. It now starts armed, which is also what a-d-b actually knows
at that moment: nothing.

**F12 — a reachable SSH watch was labelled "not reachable".** The row carried
no address, because a watch on the shared default has no *allocated* one. The
badge was correctly reporting what it was handed. Resolved with `ssh_reach_ip`
— the function already used on that path, not a second detection — and the row
now reads `SSH connected 192.168.2.15`.

**F13 — "add another watch" was a wizard loop.** It restarted the scan, which
re-found the watch just onboarded (still docked, still powered) and announced
*"found 2 watches already connected"*. Adding watches is a physical sequence,
not a loop: dock the next and it appears. Replaced with what to do instead, and
with what *shelved* means — shut down first, port power off second.

**F14 — the PPPS claim was stronger than the evidence.** The registration step
reported hubs as *"switches its own power"*. All a descriptor says is that the
hub **announces** per-port switching, and a hub can acknowledge a power command
with the power still on. moWerk caught this. Now: announced, and explicitly
unconfirmed until a port is used with a watch on it.

**F15 — wording.** The empty-bus screen ran state and instruction together, so
the sentence that matters (a watch may already be attached and simply invisible
until ADB or SSH is on) read as a footnote. The WiFi button said "by IP" while
the box accepts a hostname just as happily — moWerk onboarded beluga *by
hostname* through that route. Clearing the bus now names the physical per-port
buttons and explains the enumeration storm. The panel anchors below the Orbit
row by measurement rather than a guessed pixel offset, because during
onboarding the layout moves constantly.

### Reported, not fixed

**F16 — sol keeps its USB gadget up with VBUS cut.** With its port's physical
button off (LED dark, `usb/online = 0` on every supply, confirmed from the watch
itself), sol stayed enumerated and fully reachable over ADB. This has never
been observed on this fleet in two months of shelving watches — which is strong
evidence that it is **sol specifically**, not the hub: sol is the one watch
being actively ported. A gadget that asserts its pull-up unconditionally
instead of following VBUS would produce exactly this. Belongs to the porting
session; the control test (switch another watch's button and see whether it
vanishes) was not run.

**F17 — `seed_hub_names` has no entry for the Sabrent.** It came out as
`Hub @ 1-6` while the A16 and the dock got real names. Cosmetic.

**F18 — an Orbit member's `address` field is called `ip` and holds a hostname.**
Works; the name says the wrong thing.

## Dead ends and corrections — the part worth reading

**A test script reported a verdict it had not measured.** Investigating whether
a laptop root port cuts VBUS, a staged script left a sampler running on the
watch, cut the port, and read the log back. The sampler died with the adb
session, the log never existed — and the script's own logic counted "zero
samples showing no power" as *proof of data-only*. moWerk read it as confirmed.
It was retracted. **Absence of evidence was rendered as evidence**, by a script
written to avoid exactly that.

**A claim about our own PPPS verdict was overstated.** It looked as though
a-d-b concluded "VBUS cut confirmed" from a watch merely dropping off ADB, and
that the Sabrent could therefore be mis-classified as switchable. moWerk
challenged it. Investigation showed the test has three layers — a capability
gate from the hub descriptor, a register gate that returns *dumb* if the port
does not confirm both off and on, and the watch-side corroboration — and that
the Sabrent never reaches the test at all, because it is not flagged PPPS. The
example was invented from adjacency. What the challenge *did* surface was F7,
which is real and worse: the corroborating layer had been dead fleet-wide.

**A time skew nearly produced a confident wrong answer.** Grepping the watch's
journal for a charger event used host time against a watch clock **6h10m**
behind. The window was in the watch's future and came back empty, which reads
exactly like "no event was logged".

**The "phantom" was a live watch.** A row that persisted on a port whose LED was
dark looked like a stale kernel node. It was sol, awake, on battery, answering
adb — see F16. The instinct to chase a ghost was reasonable and wrong, and the
only thing that settled it was asking the watch about its own power, using the
function fixed that morning in F7.

## What was proven on hardware

| path | evidence |
|---|---|
| watch already connected → keep on this port | sturgeon, over **ADB** and over **SSH**, auto-named both times |
| nothing connected → hub → register → watches one at a time | 11 hub entries across 3 boxes; A16 announces PPPS, Sabrent does not; sol and aurora mapped and named |
| nothing connected → WiFi by **hostname** → Orbit | beluga, with nothing plugged in |
| welcome opens itself; skip → developer, finish → user | verified |
| naming from fastboot / Wear OS | tests only — no hardware yet |

Suite 819 → 832. Every new test was validated by planting the bug it exists to
catch; the plants are listed in the findings JSON.

## Notes for the next session

- Naming covers ADB, SSH, fastboot and Wear OS. A user is never asked for a
  codename, and should not be: the watch knows it.
- The guide is not summonable. It appears when nothing is mapped and that is
  the only trigger — deliberately, so a rig that loses its config gets it back
  without anyone remembering where it lives.
- A PPPS announcement is a claim, not a verdict. The verdict needs a watch on
  the port, and its corroborating half only started working today (F7).
