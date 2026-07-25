# USB2 migration, the onboarding evening, and the socket matrix — 2026-07-25 (evening/night)

Branch: `onboard-discovery-rework`. Continues the morning audit
(`2026-07-25-usb-brittleness-xhci-slots`), which identified xHCI device-slot
exhaustion as the root of the two-A16 breakdown. This records what happened
when the fix was applied and everything else that surfaced until ~01:00.

## Arc

1. **Both A16s moved to the w541's USB 2.0 chassis ports** (moWerk's hands).
   The SuperSpeed companion trees vanished as predicted: 10 hub chips single-
   enumerated, ~17 controller slots free for watches (32-slot xHCI). Replug
   powered all hub ports by default → boot flood → ~31/32 slots, 6 watches
   enumerated-but-unconfigured. Lesson recorded as a spec requirement: **map
   must end with every socket dark** (the sweep's prepare step already does
   this; the physical replug bypasses it).
2. **Config migrated by prefix** (`1-1→1-3`, `1-2→1-6`) using the serials of
   enumerated watches as evidence the internal chip/port structure was
   unchanged. All 15 previously-onboarded mappings, PPPS verdicts, socket
   labels and hub names survived. Topology map re-run clean.
3. **Serialized fleet drain-down**: graceful poweroff (adb/ssh/fastboot per
   state) then immediate VBUS cut, one port at a time; ~31 devices → 0, all
   leaf registers off, LEDs dark (moWerk-verified).
4. **Onboard sweep #2**: 17 watches onboarded end-to-end (identify, full CC
   read, fleet-registry write, PPPS smart ✓ on all 17, shelve HELD — the
   morning's poweroff-race fix `3661e0d` doing its job). Two belugas with a
   custom no-adb image joined via the SSH path (moWerk-driven). Later,
   device-side discovery auto-joined re-seated watches with no operator
   action — the rework behaving as designed.
5. **The rest of the night**: chasing why ~10 more watches would not join.
   Result below.

## The 17-wall, reconciled

moWerk observed a hard ceiling: 17 onboarded, and no further watch could be
added regardless of port, while those watches enumerated instantly on
another laptop. The wall was real; it decomposed into FOUR independent
causes, none of which was the morning's slot theory (during one-at-a-time
onboarding the bus is far below the limit):

- **Failed onboards leave the row blocked** (in-service state; a restart
  clears it). Open bug, high priority.
- **The `.15` SSH collision** (below) made every SSH-mode watch unreachable
  and unshelvable once two of them were up.
- **Sweep/onboard detection was blind to anything but the adb server and
  RNDIS** — watches parked in fastboot or presenting other gadget modes
  enumerate fine and are simply never seen (the ASUS units, the fastboot
  catfish). Fix queued: sysfs-first identification, truthful rows.
- **Four faulty hub sockets** (below) — watches seated there never arrived
  at the host correctly at all.

## The `.15` SSH collision — found and fixed

Watches rebooting into SSH mode return on the shared default 192.168.2.15,
not their allocated address (allocation demonstrably does not survive a
reboot). With two such watches, the host holds two RNDIS interfaces in one
/24 — the kernel routes `.15` over one of them and every watch is
effectively unreachable; each status refresh then burned a 2s ping timeout
per dead allocated address (the constant 4.25s render in the logs).

Fix (commits `b6e8977`, `a14dd3c`): identity from sysfs (`rndis_links()`:
host iface → USB path → serial, zero network I/O), reads routed to the
address the watch actually answers on (`ssh_reach_ip`: allocated, else the
default when that watch's link wins the kernel route — `ip route get`,
no privileges), `_detect_rndis` now a bounded 1s TCP :22 connect, and a
warmer step that peels one stray per minute to adb (each peel frees the
default for the next; converges). Verified end to end on catfish: live CC
over `.15`, then peeled to adb.

**Open datum**: the peel logic works when driven manually but the
in-service warmer did not fire it during the one live window. Same code,
same host; needs one observation next time a stray exists. If a docked SSH
watch's row doesn't sort itself within ~2 minutes, that's the trigger.

## The socket matrix — the night's main finding

Symptom: three ASUS watches on #1 sockets s4–s6 "would not onboard". Host-
side facts, all timestamped in the kernel journal:

- Through those sockets the watches enumerated within ~1s but presented a
  wrong gadget (`18d1:0afe`, storage-class interface) or, on s1, failed at
  the link level (`Cannot enable … error -71`, two different watches).
- The same watch units (watch + own cradle + own cable, moved as a unit)
  were **instantly correct** on: the w541's chassis ports (adb AND fastboot
  protocol), the Lenovo dock's hub, and — decisively — **other sockets of
  the same A16**.
- Two two-way swaps (sparrow↔anthias, swift↔wren): the failure followed
  the **socket** both times, in both directions. swift, previously fine on
  `1-3.4:1`, failed on the swapped socket; wren, previously wrong on s4,
  came up clean adb on `1-3.4:1`.

Eliminated along the way (each by direct test): the w541's software stack
(fastboot listed the flashed catfish on a chassis port), the host
controller wiring (USB2 vs USB3 host port — `0afe` predated the move, and
recurred after it), a wedged hub state (full PSU + uplink cold-boot changed
nothing), the A16 as a design (other sockets pass the same units), the
watches (all clean elsewhere), and the map sequence (topology-only,
switches nothing).

**Verdict: A16 #1 has four faulty sockets — s1 (electrical link failure)
and the s4–s6 cluster (enumeration-corrupting), mapping internally to root
port 1 and chip `1-3.4` ports 2–4. RMA-grade evidence.** Watch-side
interpretation of the `0afe` presentation stays with moWerk (maintainer);
this audit claims only the host-side observables.

Also logged for hub #2: `1-6.4` port 3 shows error-71 storms on a
low-speed-detected device — same failure class as s1, one cradle/socket to
examine.

## Also shipped tonight

- `a5ddbc1` sweep skips hidden hubs + excluded ports (the dock cost 4×90s).
- `8ada05f` live "skip port" button on the running sweep.
- `5518c9f` ADB-unauthorized (WearOS RSA prompt) is its own sweep outcome:
  hint during the window, port left powered, registry note, own summary
  bucket.
- Lenovo dock marked hidden; revealed via "show all ports" when its 4
  PPPS-capable sockets are wanted.
- Process corrections recorded in session memory: the revoked lenok
  battery exception (situational rules need expiry, not tenure), and the
  binding watch-side/host-side domain split.

## Open queue (priority order)

1. Failed onboard must clean up its row/state (the blocked-row bug).
2. Truthful port rows: any enumerated `18d1` device shows with serial and
   link type (adb/rndis/fastboot/storage/unknown) — never an empty row.
   Same sysfs-first identification feeds onboarding.
3. Mark s1/s4/s5/s6 excluded in config once moWerk confirms (pending).
4. Warmer-silence datum for the SSH peel (see above).
5. Post-shelve verification via uptime at next sighting; per-model halt
   behaviour in the registry (poweroff-as-reboot class).
6. Direct chassis ports (and optionally dock) as first-class UI seats.
7. xHCI slot gauge + max-concurrent-powered-ports governor.
8. Event-driven discovery (udev netlink) replacing the 15s/90s polling.

## Fleet state at close (~01:00)

19+ members in the registry (17 sweep + 2 beluga + auto-joined sparrow/
wren on borrowed seats `1-3.3:1`/`1-3.4:1`; swift and anthias unseated, in
hand, need good sockets). Fleet otherwise shelved and dark. KW88/harmony
sits offline-on-adb at `1-6.2`. The flashed catfish (`720EX8C130737`,
build refreshed on the chassis port after its flaky-build diagnosis) —
seated back on faulty s1, needs a good socket too.

## Post-audit correction (2026-07-26, ~02:00) — the deploy gap

Found after the audit was committed: the deploy procedure was violated all
evening. The w541 service loads the package from
`~/.local/share/asteroid-docking-bay/lib` (copied by `install.sh`); every
"deploy" tonight rsynced the repo and restarted the service but never ran
`install.sh` — so **the service executed 13:55-vintage code all evening**
(the rework as the previous session left it, none of tonight's commits).
Consequences for the claims above:

- **E6 is solved, cause: me.** The in-service warmer never fired the SSH
  peel because the service never had the peel. The manual run worked
  because it imported from the repo. Not a code mystery — a deploy gap.
- **Sweep #2's holding shelves are NOT evidence for the poweroff-race fix**
  — that sweep ran the OLD wait-then-cut code, and its 17 shelves held
  anyway. The fix (3661e0d) remains code-reviewed and planted-bug-tested
  but rig-unproven; the old code's race is evidently narrower than the
  worst case, or these 17 models halt cleanly regardless. Downgraded from
  "confirmed on rig" to "tested, awaiting rig evidence".
- The evening's live observations (2.02s renders, dock swept, no skip
  button, unauthorized shown as needs-charge) were all OLD code behaving
  as old code — consistent, in hindsight, to the decimal.

`install.sh` + restart executed at 01:5x; the orbit section, sweep skip,
exclusions, unauthorized outcome and the SSH/.15 machinery are live in the
service from this point on. Lesson recorded in session memory as binding:
**deploy = rsync + install.sh + restart** — the rule was written in
CLAUDE.md all along.
