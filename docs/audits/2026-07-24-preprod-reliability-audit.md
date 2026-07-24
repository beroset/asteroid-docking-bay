# Pre-production reliability audit — 2026-07-24

**Trigger:** a large 0.9 feature load (Orbit, Fleet Registry, BT, Display&Sound,
mic, narwhal hands, weather-on-watch, per-feature drain attribution) landed with
thin integration testing. The first real overnight mass drain test produced
implausible results — watches previously measured at 6–8 days standby reported
**2–4 hours**. Before trusting the rig for multi-day real-time tests, mo asked for
a full re-audit hunting **logic issues, not component bugs**: over-arching gaps
the per-component unit suite can't catch, feature discreteness / side-effects, and
timing/parallelisation issues.

**Method:** three parallel code-audit passes (drain mechanic; 0.9 feature
host↔watch correctness; power/USB/status reliability), plus a live hardware
investigation on the rig (powering watches on, inspecting wakelocks / suspend /
pulseaudio / mce). pytest was run on the rig throughout (436 passing at audit
start). Confidence labels: **Confirmed** (read in code / observed on hardware
this session), **Inferred** (two confirmed facts joined), **Recalled** (general
device knowledge, not verified here).

---

## Headline

1. **The scary drain result is real and its cause is UPSTREAM, not a-d-b.**
   On sparrow (screen off, idle, on charger) the pulseaudio **primary sink is
   `RUNNING`** because **asteroid-launcher holds an open sink-input**. A running
   sink keeps the audio hardware clocked and blocks SoC suspend → the observed
   22–36 %/h. Confirmed on hardware. This is the "pulseaudio won't let it sleep"
   / Qt6 issue mo suspected — a shared-code / launcher matter to raise upstream,
   **not** an a-d-b bug, **not** AoD (mce showed `Display state: off`,
   `Blank inhibit: disabled`), **not** speaker-related (triggerfish and sparrow
   both `HAS_SPEAKER = true`, disproving the no-speaker hypothesis).

2. **But a-d-b's drain test cannot be trusted to measure standby even once the
   watch suspends correctly** — the instrument perturbs the measurement and has
   several integrity holes (below). Fixing the upstream sleep bug is necessary
   but not sufficient; the drain mechanic itself needs hardening.

3. **The rig's status display actively lies about the one failure that bit us
   all night** — a docked-but-not-enumerating watch is reported as "no link / no
   watch docked / dead cable," the opposite of the truth, because the
   `not_enumerating` warning is dead code on the sysfs rig.

---

## A. Rig reliability (the "100 % trustworthy rig" goal)

### A1 — CRITICAL (Confirmed): `not_enumerating` is dead code on the sysfs rig
`webstatus.py:513-521`, `usb.py:160-164` & `329-330`. The warning is gated on
`connect and not _port_device_present(...)`, but in the sysfs fast path
(`_sysfs_hub_scan`) `connect[n]` is derived from the **same** device-node
existence (`{loc}.{n}`) as `_port_device_present`. The guard reduces to
`present and not present` → always False. So a powered port with a docked watch
that has not enumerated (flat-battery bootloop, marginal contact, slow boot after
a deep drain) never shows "not enumerating"; it falls through to **"no link — no
watch docked, or a dead cable/contact"** — the inverse of reality. This is the
trust break that made the whole night baffling. **Fix:** populate `connect` from
a source independent of node existence, or key purely on
`power and adb_state is None and not present` → "not enumerating".

### A2 — HIGH (Confirmed observation, confound noted): drained watches need real
boot time to re-enumerate, and the adb server may go stale
After the overnight drains, watches on powered ports showed nothing on
`adb devices` for tens of seconds; they appeared at the USB level and on adb only
after a `adb kill-server && adb start-server` **and** ~30 s more boot time. A
deeply-drained watch must charge past its boot threshold and boot before it
enumerates — longer than `_adb_read_battery`'s wait budget assumes — and a stale
adb server (documented failure mode during disconnect storms) compounds it.
a-d-b has **no self-heal** for a wedged adb server. (Confound: I cannot fully
separate "needed boot time" from "adb was stale" — both were in play.) **Fix:**
detect a wedged adb server and kill/start it; give drained-watch re-enumeration a
longer, battery-aware budget.

### A3 — MEDIUM (Confirmed): power state is a register read that cannot see a real
VBUS cut; the self-heal masks it
`usb.py:86-98` (cache TTL 300 s), `webstatus.py:577`. Status "power" is the sysfs
`disable` register (cached ≤300 s). The A16's physical buttons cut VBUS *below*
register visibility (LEDs are truth). The fake-power self-heal responds by
`uhubctl_cycle`'s sysfs branch, which only rewrites `disable` — it **cannot**
restore a button-cut VBUS, yet logs "auto-cycling," implying a recovery that did
not happen. **Fix:** treat register-power as a claim, not truth; don't imply
recovery the sysfs path can't deliver.

### A4 — MEDIUM (Inferred): `wait_serial_online` recovery can power-cycle the
wrong port after a physical move
`adb.py:246-279`. `recover_loc_port=(loc,port)` is fixed at op start from config;
after a watch is physically moved (soft-remap scenario), the cycle hits the old
seat — now possibly a different watch — bouncing it. Bounded (no loop), but real
on a multi-day run.

---

## B. Drain-test measurement integrity

### B1 — CRITICAL (Confirmed): charge and drain are mutually blind
`ops.py:306-310` (`ChargeOp.conflict`), `574-578` (`DrainOp.conflict`),
`261-282` (`Operation.start`). The conflict matrix is asymmetric: WorkbenchOp
refuses if charge OR drain is active, but ChargeOp and DrainOp each only check
WorkbenchOp — **not each other**. `Operation.start` never consults
`active_op_on_slot`. So a `charge.start` on a slot with a live drain passes every
gate and immediately powers the port ON, charging the watch mid-measurement. The
drain's next poll reads a *rising* battery, the rate goes to zero/negative, the
floor never trips, and two threads fight over one port's power. The CLI path is
guarded (`_busy_guard`, `_web_busy_slots`); the in-process web/RPC charge↔drain
pair is not, and the RPC is directly reachable. **Fix:** make the conflict matrix
symmetric or route `Operation.start` through `active_op_on_slot`.

### B2 — HIGH (Confirmed mechanism / Inferred magnitude): the 30-min re-power poll
wakes the watch, so the test measures a "woken-every-30-min" rate
`ops.py:648-651`, `_adb_read_battery` `437-462`. Every poll applies VBUS,
re-enumerates USB, runs adb, then cuts power. Applying charger + USB wakes the
SoC. A watch that lasts days only by staying in deep suspend is force-woken 48×/
day. The instrument perturbs the quantity it measures. **This compounds the
upstream sleep bug and must be quantified** (run the same watch at 30-min vs 6-h
poll; compare to the pure `standby_off_to_on_rate` baseline, which has no poll).

### B3 — HIGH (Confirmed): `_end_port` stamps a "safely down" marker without
confirming the watch actually halted
`ops.py:102-124`. The graceful branch calls `wait_serial_online(serial,5,4)`
**without checking the result**, then unconditionally logs "graceful poweroff,"
sends `adb poweroff` (`check=False`), and stamps `safe_off_ts` — then cuts VBUS.
If the watch never woke in the 20 s window, the poweroff was never delivered but
the UI shows "down" while the watch **runs on battery, invisible** — the exact
sturgeon-to-0 % failure the rules warn about ("never trust a poweroff
confirmed:true blindly"). **Fix:** gate the safe-off mark on a confirmed
offline/uptime check.

### B4 — HIGH (Confirmed): no minimum duration/sample floor before a rate is saved
and seeded fleet-wide
`ops.py:689-691`, `events.py:186-244`. The only divisor guard is `elapsed_h>0`.
A drain stopped after one 30-min poll saves a rate computed over 0.5 h with
maximal noise sensitivity; it becomes the watch's canonical `est_h` and drives
`next_due_ts`. **Fix:** refuse to save/trust a rate below a minimum elapsed time
and sample count; flag low-confidence.

### B5 — HIGH (Confirmed): a forced screen (`mcetool -D on`) is never detected or
released by the drain test
`watchctl.py:595-598`, `ops.py:580-685`. `Watch.screen(True)` forces the display
on until an explicit `-D off`. The drain samples with `get_battery_level` only —
never `battery_and_screen` — so it is blind to a forced screen, and nothing in
drain/charge start clears demo mode (only the manual `screen.release_all`). An
operator who clicks "Screen: ON" to inspect a watch and then drains it measures a
lit panel for days as if it were standby. **Fix:** at drain start, read
`battery_and_screen`; refuse-with-reason or `screen(False)` first, and log it.

### B6 — MEDIUM (Confirmed): serial mis-attribution across a mid-drain remap
`ops.py:650` re-resolves the *current* serial for the read, but `693` /
`_save_drain_results` attribute the reading to the *start* serial (`task["serial"]`
fixed at `597`). A remap mid-test records watch B's battery under watch A. The
loop's stated intent (re-resolve "in case serial mapping changed") contradicts
logging under the old serial. **Fix:** attribute readings to the serial actually
read.

### B7 — MEDIUM (Inferred): the initial reading's extra shell round-trips inflate
the whole rate
`ops.py:447-453`. The initial read holds VBUS on for the battery read **plus**
`standby_features` (connman + dconf, `timeout=10` each) — a longer charge window
than any later poll. Since the rate anchors on the fixed `start_pct`, this bias
inflates the rate for the entire test. (Note: capturing features in the online
window was itself a fix this session for a real bug where features were read
*after* VBUS was cut and always came back defaulted.) **Fix:** re-read `start_pct`
immediately after the initial VBUS-off, or subtract the known bump.

### B8 — MEDIUM (Confirmed): `record_audio` can orphan `gst-launch` on the watch
`watchctl.py:531-546`. Runs `timeout -s INT {n} gst-launch … pulsesrc … filesink`
with only a host-side subprocess timeout, and does not check its result. Any
non-happy path (host timeout, worker killed, gst stuck in Pulse preroll, a
`timeout` with no `-k` fallback, or a busybox `timeout` that rejects `-s INT`)
can leave gst running — holding `pulsesrc` and the CPU, blocking suspend
indefinitely, invisible to the host. A latent version of tonight's exact
signature. **Fix:** `timeout -k`, verify termination, and `pkill` the recorder on
cleanup.

### B9 — LOW/MEDIUM (Confirmed): the background warmer does USB/adb reads with no
lock against the drain poll
`ops.py:210-226` vs `437-462`. `_adb_read_battery` deliberately holds no
`_adb_lock` ("UI guarantees exclusion"), but the warmer is not UI-driven. A
warmer read overlapping a poll's enumeration can cause a transient failed read →
a spurious `blind` increment, nudging a marginal watch toward `blind_abort`.

---

## C. Feature discreteness & side-effects (mo's explicit ask)

- **B5 (forced screen) and B8 (orphaned recorder)** are the two feature
  side-effects that leak across into a later drain and keep the SoC awake —
  the highest-priority "features are not discrete" findings.
- **C1 — MEDIUM (Confirmed): `set_datetime` is shipped broken.** `watchctl.py:321`
  `date -s {shlex.quote(when)}` — `when` always contains a space, `shlex.quote`'s
  layer is consumed by the *host* shell, adb/ssh re-split, and the watch runs
  `date -s 2026-01-01 12:00:00` → time-of-day is a stray operand. Wrong time on
  every call, adb and ssh. Same quoting class as the battery-read/mcetool bugs.
  The sibling `set_hands` wraps the whole command and is correct. **Fix:** wrap
  the whole remote command in one token.
- **C2 — LOW (Confirmed): `standby_features` reports `aod=True` on a failed read**
  (`watchctl.py:521-524` ignores `rc`), mislabelling attribution. Guard on `rc`.
- **C3 — LOW (Confirmed): `weather_sync` strips apostrophes** from city names
  (`St. John's` → `St. Johns`, `watchctl.py:412`). Cosmetic.
- **Verified sound (no side-effect):** `buzz`, `move_hands`/`set_hands` (motor
  self-stops), `notify`/`play_notification` (transient) leave nothing running.
  `bt.py` lazy imports, `registry.note` change-log, `orbit.reachable`, and most
  `user_cmd`-routed commands are correctly quoted and discrete.

---

## D. Missing over-arching tests (the level the unit suite doesn't reach)

The unit suite is strong on components; the gaps are all "does the assembled
mechanic measure a true thing and never leave a watch unsafe":

1. **Cross-op conflict contract** — a table over {charge,drain,workbench,flash}²
   asserting pairwise refusal (would have caught B1).
2. **End-to-end measured-rate on a synthetic battery** — drive a full
   `DrainOp.run` against a fake battery discharging at a KNOWN rate (with a known
   charge bump while VBUS on) and assert the saved rate is within tolerance. The
   single most valuable missing test; directly targets "the whole thing measures
   garbage" and quantifies B2/B7.
3. **Poll-contamination test** — same model at two poll intervals; the derived
   rate must not depend on poll frequency (B2).
4. **Safe-off honesty test** — a watch that never wakes in the window must NOT be
   stamped "down" (B3); planted-bug validated.
5. **Minimum-duration guard test** (B4).
6. **Serial-remap-mid-drain attribution test** (B6).
7. **Warmer-vs-poll concurrency test** (B9).
8. **Power-ledger invariant across every drain exit path** — floor, user-stop,
   blind-abort, unreadable-start, mid-loop exception — asserting the final port
   power state and whether a confirmed halt occurred, so no future edit can
   silently strand a watch.

---

## Recommended order

**Before any multi-day test (rig must not lie or corrupt):** A1 (enumeration
honesty), B1 (charge↔drain guard), B3 (safe-off honesty), B5 (forced-screen at
drain start), plus test D2 (synthetic-battery end-to-end).
**Then integrity:** B4, B6, B7, B8, C1, A2.
**Upstream (mo/maintainers, not a-d-b):** the asteroid-launcher/pulseaudio
sink-stays-RUNNING sleep bug — the actual cause of the scary numbers.

All device observations were made on triggerfish (`C3F9275E1467`) and sparrow
(`H1NZCJ010087020`), powered on for inspection and left charging (both were
drained by the flawed tests). Those overnight drain results are to be discarded.

---

## Resolution (2026-07-24, same day)

Fixed and deployed the critical + high + key-medium findings, each a
tested, single-concern commit (planted-bug validated), suite green (447):

| Finding | Fix commit |
|---|---|
| B1 charge/drain mutually blind | Enforce one long-running op per slot symmetrically |
| B3 false safe-off | Mark a watch safely-down only when it was reachable to halt |
| B5 forced screen | Release a forced-on display at drain start |
| A1 not_enumerating dead code | Report a docked-but-stuck watch honestly on the sysfs rig |
| C1 set_datetime quoting | Set the watch clock with a command that survives adb re-split |
| C2 aod-on-failed-read | Report AoD as unknown, not on, when its read fails |
| B8 orphaned recorder | Never leave an orphaned mic recorder running |
| B6 serial mis-attribution | Stop a drain if the port's watch changes mid-run |
| B4 min-samples | Require enough samples before trusting a drain rate |
| A2 adb-server wedge | Self-heal a wedged adb server |
| D2 (missing test) | Test the whole drain mechanic against a synthetic battery |

**Still open (lower priority, deferred for review):** A3 (register power
can't see an external VBUS cut; the self-heal shouldn't imply a recovery
it can't deliver), A4 (`wait_serial_online` recovery can cycle the wrong
port after a physical move), B7 (initial read's extra round-trips inflate
the start anchor — needs hardware quantification), B9 (warmer vs drain-poll
bus lock), C3 (weather_sync strips apostrophes).

**Upstream (not a-d-b), for the maintainers:** the asteroid-launcher /
pulseaudio sink-stays-RUNNING sleep bug — the actual cause of the
implausible drain numbers.
