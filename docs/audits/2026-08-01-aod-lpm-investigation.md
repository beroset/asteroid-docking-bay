# AoD does not render in low-power mode — investigation plan

**Date:** 2026-08-01 · **Status:** tests 1-3 RUN — see Results at the end.
The plan below is kept as written, including the hypotheses it got wrong,
because which ones died and how is the useful part.
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
while the screen is still black.* **← THIS ONE. Confirmed; see Results.**

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

---

# Results — 2026-08-01

Run on **sturgeon** (`MQB7N15C09000847`), AoD confirmed rendering by eye
before starting. Visual state is moWerk's; everything else is captured.

## What was established

**1. The failure is DOWNSTREAM of MCE.** With the screen black in LPM, all
three upstream layers were correct *and active*:

| layer | state while black |
|---|---|
| `org/asteroidos/settings/always-on-display` | `true` |
| MCE `Use low power mode` | `enabled` |
| `setAmbientUpdatesEnabled(true)` → `org.nemomobile.compositor` | **firing every 60 s** |
| actually drawing | **no** |

The compositor was being told to do ambient updates, once a minute, and was
not drawing. No amount of settings or dconf inspection could have found this —
`aod/diff` across the repair reported **`mce changed: 0, dconf changed: 0`**.

**2. Opening the settings app repairs it WITHOUT opening the display page.**
moWerk launched it and closed it immediately. So it is not the page's
bindings; merely starting an app is enough.

**3. It is INTERMITTENT across boots — a startup race.** Two reboots, nothing
else different: the first came up black, the second came up rendering. A race
also explains why a fresh flash (slow boot, firstrun, tutorial) loses far more
often, and why the bug has survived unnoticed across 2.0/2.1/2.2.

**4. A VBUS cycle and a usb_moded mode switch do NOT break it.** Tested
directly, because both had been applied to the black boot and not to the
working one — a confounder introduced by the investigation itself. After the
cycle and switch, AoD kept rendering. Hypothesis refuted.

## What was ruled out

- **Configuration**, in either layer — identical across the repair.
- **The nightstand implementation** as the direct cause: nightstand was
  `enabled=false` throughout, while ambient calls flowed normally. (This was
  dodoradio's suspicion; it may still be involved in the race, but it is not
  the gate.)
- **The Qt6 migration** — present in 2.0 and 2.1, per moWerk.
- **MCE** — it does its job, on time, every minute.

## No remote oracle exists (negative result worth keeping)

`Display state` reads `off` **while the watchface is visibly rendering**, and
`backlight` read `70` (normal brightness, not dim) — almost certainly because
the adb query itself woke the watch. **AoD rendering cannot be reliably
detected over adb: the observation disturbs the state.** Ground truth remains
a human looking at the watch, or wanze sampling locally with no USB
interaction. That rules out a scripted reboot-loop with an automated verdict,
which was the obvious way to get a failure rate.

## Open, and what to do next

- **The trigger inside the race is not identified.** Is it the app *launch* or
  the *close*/return-to-homescreen? Launch an app and leave it open: if the
  next LPM renders, the launch is enough.
- **Is it settings-specific?** Repeat with any other app (flashlight,
  stopwatch). If any app repairs it, the report becomes "the ambient surface is
  not live until the first app launch/close cycle", which points squarely at
  asteroid-launcher rather than at settings.
- **A failure rate** needs repeated reboots with a human check each time, since
  there is no oracle.
- Filing target on current evidence: **the compositor/launcher side**, not MCE
  and not asteroid-settings.

## Consequence for a-d-b (already fixed)

Every past drain result recording `aod: true` took it from the userland
toggle. This investigation proves the toggle is not evidence that AoD ran —
the compositor may simply not be drawing. a-d-b now reads MCE's state as the
authority and records the toggle separately as intent. Past attributions should
be treated as unreliable rather than re-interpreted.

---

# Results, part 2 — the nightstand guard, measured

## CONFIRMED: two distinct bugs, not one

**Bug 1 — the nightstand guard (asteroid-settings).** `DisplayPage.qml`:

```qml
onCheckedChanged: {
    if (alwaysOnDisplay.value === checked) return          // guard 1
    alwaysOnDisplay.value = checked
    if (!nightstandEnabled.value || mceChargerType.type == MceChargerType.None) {
        displaySettings.lowPowerModeEnabled = checked      // guard 2
    }
}
```

With nightstand ON and the watch on a charger, guard 2 is false and the display
layer is **never told**. Reproduced deterministically on beluga (`225791c5`) and
measured, not inferred:

```
dconf org/asteroidos/settings/always-on-display = true
dconf desktop/asteroid/nightstand/enabled       = true
MCE   Use low power mode                        = disabled
verdict: consistent = FALSE
```

moWerk's quickpanel (`QuickPanel.qml:969`) writes both the dconf key and
`displaySettings.lowPowerModeEnabled` **unconditionally**, which is why it is
the only control that repairs the state. The quickpanel is the workaround, not
the cause — despite `lowPowerModeEnabled` first appearing there in #180
(2026-01-27, before 2.0 shipped on 2026-02-01), so it is present in every
release where the bug was seen and is a constant, not a variable.

**Bug 2 — sturgeon's case, still unexplained.** MCE `enabled`,
`setAmbientUpdatesEnabled(true)` firing every 60 s, screen black, nightstand
`false`. Guard 2 cannot explain it. This is the one that matches the
fresh-flash reports.

## The screenshot instrument: built, calibrated, and defeated

Idea: `screenshottool <path> <delay>` sleeps then captures via lipstick's own
`saveScreenshot`, so a delayed capture could photograph LPM and answer
dodoradio's challenge — *is the compositor actually not drawing?*

- **Docked calibration failed.** Frames at 31 s and 75 s were identical to a
  known-awake frame (mean luma 43.4 vs 43.4, diff 0.25). A docked watch takes
  the nightstand path and never reaches true LPM — a caveat this very document
  states, and which was ignored anyway.
- **Undocked capture worked once** (sturgeon, working state): armed detached
  with `systemd-run --collect`, VBUS cut, captured at `Display state: off` with
  0.21 s between the state read and the grab. It returned a **full-colour**
  watchface, which is itself suspicious: `saveScreenshot` may re-render on
  demand rather than capture what is presented.
- **Undocked capture on the broken watch never fired.** The unit was still
  alive minutes later with `sleep 150` unfinished:

```
├─11364 /bin/sh /tmp/lpm-onwatch.sh 150
└─11365 sleep 150          # ~4 min of wall clock later, still sleeping
```

**A plain `sleep` does not advance while the watch is suspended.** This is the
exact principle wanze is built on, now demonstrated on hardware — and it is a
hard limit on this instrument: **photographing a suspended watch requires
waking it, and a woken watch is no longer in the state under test.**

## Consequence

dodoradio's original suggestion — put logging where the compositor is suspected
of not drawing — is now the only remaining route for Bug 2. That is a local
debug build of lipstick or asteroid-launcher: shared upstream code, moWerk's
call, not to be patched from here.

Bug 1 needs no further investigation and can be filed as-is.
