# AoD does not render in low-power mode — investigation plan

**Date:** 2026-08-01 · **Status:** PLAN, no tests run yet
**Reported by:** moWerk; first noticed by dodoradio
**Scope:** upstream AsteroidOS. a-d-b's part (wrong drain attribution) is
already fixed — see `Ask MCE whether AoD runs, not the toggle`.

## The symptom, as reported

After a fresh flash and completing firstrun/tutorial, the AoD toggle reads
**on**, but nothing renders during LPM sleep — the screen stays black. The
homescreen draws fine. **Opening the settings app (display page) and closing it
makes AoD work**, with the settings themselves unchanged.

Long-standing: present in **2.0, 2.1 and 2.2**, so it predates the Qt6
migration and there is no point bisecting recent work.

## What is already established

Read from a live watch (sturgeon), 2026-08-01:

- MCE keeps its own setting, **`Use low power mode`**, and exposes a setter:
  `mcetool -E, --set-low-power-mode=<enabled|disabled>`.
- There is **no gsettings schema** for `org.asteroidos.settings` — only the
  weather schemas are installed. So `always-on-display` has **no default**; the
  "on" shown in the UI is declared in QML. An unset key means *nobody ever
  wrote it*.
- There are **two** AoD toggles in different schemas:
  `/org/asteroidos/settings/always-on-display` and
  `/desktop/asteroid/nightstand/always-on-display`. dodoradio suspects the
  nightstand implementation and its "mixed up states".

## Hypotheses

**H-A — MCE is never told at boot.** MCE persists its own value; nothing
applies the userland toggle at startup; only the settings page writes it. A
fresh flash leaves MCE at its factory default while the UI shows on.
*Predicts: a reboot does NOT reproduce, because MCE already holds the value.*

**H-B — the two toggles fight.** Both AoD toggles drive the same MCE setting;
last writer wins, and nightstand's state machine can leave it disabled.
*Predicts: behaviour depends on the nightstand toggle, and may be
non-deterministic.*

**H-C — nothing was instantiated.** MCE is correctly enabled and the thing that
draws was simply never created. *Predicts: capture reads `consistent: true`
while the screen is still black.*

## THE TRAP — read before touching a watch

**Opening the settings app destroys the evidence for that boot**, and checking
the AoD toggle is the natural first instinct. On any watch under test, run the
capture FIRST:

```sh
curl -s -X POST http://localhost:8080/api/watch/<serial>/aod/capture
```

It is side-effect free by design. a-d-b's ordinary polling is read-only too.

## Ground truth: is it actually rendering?

Configuration state is not rendering. Two observables:

| | how |
|---|---|
| **MCE `Display state`** | should read `lpm-on` (or similar) while AoD renders, `off` when not — in the same capture |
| **backlight during LPM** | `/sys/class/leds/lcd-backlight/brightness`, sampled by **wanze** while the watch is undocked |

wanze matters here because the plain-AoD path only exists **off USB**. On USB
the watch takes the *nightstand* path instead (hence the charging ring), which
is a different code path and a different toggle. **Any test on a docked watch
tests nightstand, not AoD.**

## The plan, cheapest and most discriminating first

### Test 1 — does a reboot reproduce it? (minutes)

On a watch where AoD is **confirmed rendering**:

1. `aod/capture` → expect `consistent: true`, MCE `enabled`.
2. Reboot.
3. `aod/capture` **immediately, before opening anything**.

| result | meaning |
|---|---|
| MCE still `enabled` | **H-A supported.** MCE persists; the bug is once-per-flash. |
| MCE now `disabled` | **H-A dead.** Something resets it at boot — far more severe, and no flash is needed to iterate. |

### Test 2 — reproduce on demand, no flash (the key test)

Same watch, still working:

1. `mcetool --set-low-power-mode=disabled` over adb. **Do not open settings.**
2. Undock, let it sleep, observe (eyes, or wanze's backlight column).

| result | meaning |
|---|---|
| AoD stops rendering | The MCE setting **is** the gate. The bug is fully reproducible without flashing, and `--set-low-power-mode=enabled` is a one-line workaround. |
| AoD still renders | The gate is elsewhere — go to H-C, and look at what draws. |

Then restore with `--set-low-power-mode=enabled` and confirm it returns.

### Test 3 — what does opening settings actually write?

From the broken state produced by Test 2:

1. `aod/capture`
2. Open settings → display page → close.
3. `aod/diff`

The verdict field says which layer moved. `MCE changed, dconf did not` confirms
H-A's plumbing. `Neither changed` means H-C.

Optional and higher yield if the diff is ambiguous: `dbus-monitor` on both
buses across the open/close, which names the exact call that flips it.

### Test 4 — the nightstand angle (dodoradio's suspicion)

1. Capture. Toggle **nightstand** AoD. Capture. Diff.
2. Repeat for the **display** AoD toggle.

If both write `Use low power mode`, they are fighting over one setting and the
last writer wins — which is the "mixed up states" dodoradio suspects, and would
explain non-deterministic reports.

### Test 5 — fresh flash (LAST, and only if 1–4 do not explain it)

Build from upstream source — **not** a nightly; 2.0/2.1 are pre-migration Qt5
and the nightlies run weeks behind. Flash, then capture **before** firstrun
completes and again after, without ever opening settings.

## What each outcome means for filing

- **H-A** → the bug is that nothing applies the toggle to MCE at boot. Files
  against whatever owns that plumbing, with Test 3's diff as evidence.
- **H-B** → files against the nightstand implementation, with Test 4 showing
  both toggles writing one setting.
- **H-C** → not configuration at all; files against whatever should have
  instantiated the AoD surface, with a capture showing `consistent: true` and a
  black screen.

## Note for the drain data

Every past drain result recording `aod: true` took it from the userland toggle,
which this bug proves is not evidence that AoD ran. Those attributions should
be treated as unreliable rather than re-interpreted — a-d-b now records MCE's
state alongside, so future runs carry the answer with them.
