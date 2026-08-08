# Post-Machine-Room deep audit — 2026-08-08

Auditor: Claude (Fable 5), under moWerk's direction, overnight autonomous run.

## Why this audit

A large surface landed since the last audit (baseline `19f54bd`, 2026-07-25):
the Machine Room (icecc cluster monitoring), the operation lock, the mmcblk0
dump path, the rpcops transport-routing rework, the webapp request-body parsing,
the webstatus SSH-stray recovery, and a big webtemplate expansion. Several of
those touch concurrency and subprocess boundaries — where bugs hide quietly — so
mo asked for a sweep before building further toward 1.0.

## Method

Six review agents ran in parallel, one per subsystem, each told to hunt concrete
failures (inputs/state → wrong behaviour) rather than style, and to drop any
suspicion that did not survive a second look. Every surviving finding was then
verified by me against the source, and against the live rig where it actuates
hardware. Every fix carries a test **validated by planting the bug it exists to
catch** (base rule 7) — the plant/restore runs are in the commit trail. The full
suite is green on the w541 (731 passed) and the fixes are deployed and verified
(version 1.0dev, Machine Room reachable, 13 hubs, service healthy after restart).

The findings ledger is `2026-08-08-post-machineroom-audit.findings.json`. This
narrative is the readable companion: what was fixed, what is left for mo to
decide, and the dead ends.

## Headline

Two features were **silently dead in the UI** and nobody had noticed, because
the last fortnight's work (wanze, orbit, stockrom, Machine Room) never exercised
them:

- **Flash and single-port onboarding did nothing from the web UI** since
  2026-07-22. The `reconcileRows` optimisation that stopped watch thumbnails
  reloading on every poll kept only the first `<tr>` of each two-row watch
  entry, dropping the hidden log row that carries `id="log-<slot>"`. With no log
  box in the DOM, `doFl`/`doRemap`/`_openOnboard` all returned before opening the
  flash/onboard stream. The render tests never caught it because every one of
  them swaps `reconcileRows` for a plain `innerHTML=join`, so its own tree logic
  was never run.
- **The Dump mmcblk0 menu item did nothing** — an empty `function doDump(s){}`
  sat below the real implementation, and JS hoists the last declaration.

Both are fixed, each with a test that fails on the old code (the reconcile test
runs the real function under a DOM shim; the dump test is a static
no-duplicate-declaration guard).

## Fixed (11 commits)

**icecc / Machine Room**
- `3966355` — cache age measured monotonically and stamped at the *end* of the
  refresh (an NTP step-back froze the panel; a slow refresh churned).
- `0f40de0` — a failed refresh-thread spawn no longer wedges the panel forever;
  check-and-set moved under the lock so a threaded server cannot double-spawn.
- `125762d` — the scheduler protocol is framed on bytes, so a multibyte hostname
  split across a recv() boundary no longer mangles.
- `1e3273b` — the unit suite no longer SSHes into the real cluster machines (the
  fixtures carry live IPs and the temp sweep ran against them); module state is
  reset between tests; the sh -c wrapper is asserted at the call site, not just
  on the constant.

**UI**
- `6e5d0db` — `reconcileRows` keeps both rows of each watch entry, so flash and
  onboarding can stream again.
- (dodump) — the shadowing empty `doDump` stub is gone; a static test now
  rejects any function declared twice.

**oplock / dump**
- `877d67d` — `hold()` refuses to displace a live lock of a different kind, so a
  dump can no longer silently overwrite (and then delete) a running wanze run's
  14-day lock. This is the crown-jewel fix: the marker existed to protect long
  operations and was itself overwritable by the next one.
- `f481950` — the device-supplied serial and the dest path are quoted before
  reaching the dump's `bash -c` shell.
- `db931cb` — a dump is built for the link the size preflight actually answered
  over, so an orbit/WiFi watch is not dumped over a dead adb channel.
- `8502b55` — a truncated or crashed dump is renamed `.partial`, so it can never
  pass for a backup in a directory listing (the original-sprat.img failure).

**webstatus**
- `d3ca8b8` — an unreachable SSH stray is cycled ONCE again: the one-shot marker
  is re-armed only on real recovery, not on the brief absence the cycle's own
  re-enumeration creates (which had it cycling a dead watch every few minutes
  forever).

## Follow-up: the two HIGH items are now fixed (2026-08-08, later)

Both were surfaced rather than applied during the overnight run because they
actuate hardware. moWerk took them next, in order, once the session had moved
onto the rig itself:

- **Recovery cycles are serialized** (`4a22555`). One shared
  `usb.recovery_cycle_lock`, held for the whole off→on by every automatic
  recovery cycle — the fake-power heal, the SSH-stray recovery and adb's
  not-enumerating recovery — so they can never actuate together. Operator
  cycles and onboarding sweeps deliberately stay immediate.
- **The oplock now guards every remaining actuator** (`2ddceed`, `ecc9fba`,
  `7b3b04d`, `32cc3ed`, `4cd1f5c`): charge/drain/workbench via one check in
  `Operation.start`, `flash.start` (which also gained the cross-op check it
  lacked), the CLI check-charge timer, the adb wedge self-heal, and
  `wear.set` / `ssh.switch_adb` / `watch.switch_ssh`. Guarding the last of
  those also closed the aligner's own TOCTOU in `finish_ssh_relocation`.

- **The onboarding sweep steps around held watches** (`db3eba0`). This half was
  held back for moWerk because it changes a bulk operation's contract; he took
  the recommended option the same day. The sweep does not refuse — the operator
  did ask to power everything down — it leaves a held socket powered and
  untouched, sweeps the rest, and never skips silently: `sweep_prepare` reports
  what it left alone, the run stream names it at the start and in the summary,
  and the confirm dialog shows it *before* the operator starts equipping
  sockets. With this, every actuator on the rig honours the lock.

## Everything found is now fixed (2026-08-08, same day)

The ledger is empty: all 24 findings are closed, each with a test validated by
planting the bug it exists to catch. What the remaining batch fixed, beyond the
guard work above:

* **Atomic config writes + a cross-process lock** (`7044e27`). The save was a
  truncate-in-place, so a crash mid-write left invalid JSON and nothing catches
  the decode error — every op and the whole status page would break with the
  fleet's mappings and locks unreadable. And the read-modify-write cycles were
  guarded by a threading lock the CLI (a separate process) cannot see, so
  interleaving cycles could erase a live operation lock outright.
* **Escaping for values that are not ours** (`00ac15b`). A watch supplies its
  own serial, the icecc scheduler supplies hostnames, and a Bluetooth device
  supplies its advertised name over the air. `esc()` escaped neither quote, and
  an inline handler argument needs more than that anyway: two parsers run in
  order, so a quote written as `&#39;` is handed to JS as a plain quote. Added
  `jsq()` for that context and routed 20 sites through it.
* **A held watch now says so** (`a8300a6`) — the other unfinished half of the
  dump UI, next to the Dump menu item that turned out to be dead.
* **Dump integrity** (`cbd8eaf`): an atomic claim (the concurrency test found a
  second hole in the first version of the fix — the claim window itself) and a
  free-space refusal before an 8 GB write onto a disk the fleet shares.
* **A full disk no longer blanks the fleet view** (`f0d3763`), **a shelved watch
  no longer reports the OS it was reflashed away from** (`05243ab`), and **power
  actions route on live adb rather than a warmer cache that can be a cycle
  stale** (`56c2ed5`) — which had turned a working button into a 20-second hang.
* **The three test gaps that let all of this ship** (`fb538e1`): nothing
  asserted the ops CALL the guard, `fb_draining` was tested by round-trip rather
  than by evaluating the flag, and nothing exercised the request layer that
  shipped the silent-defaults bug.
* **Low-severity quiet failures** (`4e6d07c`): an expiry-less lock read as
  immortal, an unbounded ttl, a mistyped dump action starting a dump, and an
  error message claiming "not on adb" for a watch that is on adb.

Two things are worth recording as process, not findings. The audit's own
suspicion that `registry.note` could take down the status path did **not** hold —
it was already safe — and it is marked as such rather than quietly dropped. And
two existing tests had to be corrected rather than the code: both stubbed a
watch as adb-online *and* in the bootloader, a state that cannot exist on
hardware, which is exactly what the stale-cache bug looked like.

## Dead ends

Suspicions raised by the agents and dropped on a second look, kept here because
the process record is part of the deliverable (base rule 8): icecc SSH injection
via node IP (regex-constrained); the `_refreshing` event leaking after a worker
*exception* (the try/finally is correct — only the pre-*start* window leaked);
the restore gates reaching a live flash (no executor is wired yet);
`merge_op_args` redirecting a serial (URL args are authoritative and pinned);
`_ssh_delivered` miscounting a link-dropping poweroff (deliberate and pinned —
the real gap is the path that does not call it); the machine-room hidden contract
for null/[]/missing (correct and tested); and the fake-power oplock test being
decoration (it now carries a positive control).

## What was NOT done, deliberately

No hardware was actuated to test dump/flash end-to-end — the dump feature is
still unproven on a real watch (that is task #37, yours to run) and this audit
was a code sweep, not a hardware session. The concurrent-cycle serialization and
the broad oplock guard-wiring were surfaced rather than applied, per the note
above. Nothing touched `~/Git/asteroid` or the Yocto trees (owned by the porting
session).
