# AoD never renders in LPM until you touch a toggle — full diagnosis

**TL;DR — this is two separate defects, not one.** One is small and local
(nightstand). The other is architectural and is the reason the bug survived
2.0, 2.1 and 2.2. Fixing only the first will look like a fix and leave the
fresh-flash case broken.

---

## The symptom

After a fresh flash, the AoD toggle reads **on** — in Settings *and* in the
quickpanel — and nothing renders in low-power mode. The screen is simply black.
The homescreen draws fine.

It "fixes itself" if you open an app, or toggle AoD in the quickpanel. On some
watches opening Settings alone is enough; on others it is not. No setting
changes in the process, which is why it never looked reproducible.

---

## Defect 1 — `DisplayPage` refuses to apply AoD while nightstand is on

`asteroid-settings/src/qml/DisplayPage.qml`:

```qml
onCheckedChanged: {
    if (alwaysOnDisplay.value === checked)
        return                                          // (a)
    alwaysOnDisplay.value = checked
    // alter displaySettings unless NightStand mode is active
    // and we are on a charger
    if (!nightstandEnabled.value || mceChargerType.type == MceChargerType.None) {
        displaySettings.lowPowerModeEnabled = checked    // (b)
    }
}
```

With **nightstand enabled and the watch on a charger**, `(b)` is skipped. dconf
updates, the toggle moves, and **MCE is never told**. The user sees AoD on and
nothing renders, with no way to fix it from that page.

`QuickPanel.qml` has no such condition — it writes both unconditionally — which
is exactly why the quickpanel toggle is the only control that reliably repairs
the state.

**Measured on beluga, reproduced on demand:**

```
dconf org/asteroidos/settings/always-on-display = true
dconf desktop/asteroid/nightstand/enabled       = true
MCE   Use low power mode                        = disabled
```

Controlled sequence on one variable, with no UI touched — `mcetool` only:

| MCE low power mode | blank cycle result |
|---|---|
| `enabled`  | watchface renders |
| `disabled` | black |
| `enabled`  | watchface renders |

This one is small and local. **It is not the cause of the fresh-flash case.**

---

## Defect 2 — nothing owns the invariant "MCE agrees with the user's setting"

The AoD state is **stored twice**: in dconf (what the user asked for) and in
MCE (which performs it, and persists its own copy). The **only** code that
keeps them in agreement is UI event handlers — `DisplayPage`,
`NightstandPage` and `QuickPanel` each write `displaySettings.lowPowerModeEnabled`
when the user touches them.

Nothing establishes that invariant at startup. So on a fresh flash MCE holds
its factory default (`disabled`) while the UI shows **on**, because
`DisplayPage.qml` declares:

```qml
ConfigurationValue {
    key: "/org/asteroidos/settings/always-on-display"
    defaultValue: true
}
```

An unset key therefore *displays* as enabled while nobody has ever written it —
and writing it is the only thing that would have told MCE.

### Where it dies, in the compositor

`lipstick/src/compositor/lipstickcompositor.cpp`:

```cpp
void LipstickCompositor::setAmbientUpdatesEnabled(bool enabled)
{
    if (enabled) {
        if (m_currentDisplayState == QMceDisplay::DisplayOn) return;
        if (!ambientEnabled()) return;          // ← every MCE tick lands here
    }
    setUpdatesEnabled(enabled, true);
    if (enabled) emit displayAmbientUpdate();
}
```

Observed on sturgeon in the black state: MCE was calling
`setAmbientUpdatesEnabled(true)` on `org.nemomobile.compositor` **every 60
seconds**, and the compositor discarded every one of them because ambient mode
had never been enabled. Nothing logged it. Nothing in Settings could reveal it.

`setAmbientEnabled()` has **no callers in lipstick or asteroid-launcher** — it
arrives over D-Bus, from MCE, and MCE sends it on *change*. After boot the value
never changes again, so it is never re-sent.

Also relevant: `reactOnDisplayStateChanges()` only emits `displayAmbientEntered`
when `ambientEnabled()` is **already true at the moment the display blanks** —
so ordering at startup decides the outcome, which matches the observed
intermittency (two identical reboots: one black, one fine).

---

## Ruled out, with the evidence

| theory | killed by |
|---|---|
| framebuffer / display-driver handoff | `ambientSupported()` returns **true** over D-Bus on a broken watch |
| the Qt6 migration | present in 2.0 and 2.1 |
| the quickpanel rework (#180) | it writes both values unconditionally — it is the *workaround*, and it predates 2.0 anyway |
| cradle / USB / power state | a VBUS cycle and a usb-mode switch do not break or fix it |
| configuration drift being the whole story | with the screen black, a before/after diff across the repair showed **zero** changes in both dconf and MCE |
| enabling ambient in the compositor alone | `setAmbientEnabled(true)` over D-Bus did **not** restore rendering — MCE's ticks are needed too |

---

## The experiment

A sturgeon test image was built with:

1. `asteroid-aod-sync` — a ceres user service that applies the dconf AoD value
   to MCE once per session (treating unset as `true`, matching the QML default).
   **Explicitly a probe, not a proposed fix** — see below.
2. Diagnostic logging in the four ambient decision points, behind the existing
   `LIPSTICK_COMPOSITOR_DEBUG` gate, so every early return says why it returned.

**Result: the sync was NOT the fix. Retracted.**

AoD did render on that run — but a **control image, identical except with the
sync package removed, rendered too.** So being off-charger was doing the work,
not the boot-time apply. The control is what saved this from being a wrong
claim in front of maintainers.

What the compositor logging then established, on a docked watch:

```
AOD setAmbientEnabled( false ) supported= true current= false
AOD setAmbientUpdatesEnabled( true ) displayState= 0 ambientEnabled= false
AOD update DISCARDED: ambient mode is not enabled
```

**MCE is not failing to talk to the compositor — it actively pushes
`setAmbientEnabled(FALSE)`,** because its own `Use low power mode` is
`disabled`. Every 60-second update tick is then discarded and nothing draws.

Setting MCE's LPM to `enabled` by hand (`mcetool -E enabled`), changing nothing
else, flips the same log to accepted ticks and **the watchface renders** —
docked, nightstand on. That part is confirmed.

### Still unexplained — do not present this as complete

1. **Undocked with MCE `disabled`, AoD renders anyway.** By the mechanism above
   it should be black. No `AOD` trace lines appear at all while undocked, so the
   off-charger path may not go through this code.
2. **Undocking RESETS MCE's LPM to `disabled`.** Observed directly: set to
   `enabled`, undocked, and it came back `disabled`. The obvious suspect is
   nightstand's off-charger timer writing `lowPowerModeEnabled =
   alwaysOnDisplay.value` — but that key is unset and BOTH the launcher and the
   settings app declare `defaultValue: true`, so it should have written `true`.
3. A `Component.onCompleted` fix for nightstand's uninitialised state
   (`onReadyChanged` never fires when a watch boots already on the charger) was
   built and flashed. It did **not** fix it on its own.

---

## The decision we need

The sync service is a **workaround** — it adds a fourth party to paper over a
missing owner. We are not proposing it. Options, worst to best:

**1. Fix only the nightstand guard.** Small, local, correct as far as it goes —
and **insufficient**: it does nothing for first boot. Tempting to stop here.

**2. Give an existing always-running component the ownership.** The launcher
already talks to the compositor and runs all session; it applies the setting at
startup and on change. Still duplicated state, but one owner instead of three
UI handlers, and no new service.

**3. Remove the duplication.** MCE or the compositor reads the AoD state from
config directly rather than being pushed it. Nothing to desynchronise, no
boot-time apply needed. Largest change, and the only one that makes the class of
bug impossible.

Options 2 and 3 both make the nightstand conditional obviously wrong, which we
take as a sign they sit at the right layer.

**Note on attribution:** the nightstand guard is a small, easily fixed defect
and is *not* what has been breaking fresh flashes. Defect 2 is independent of
nightstand — the sturgeon case had `nightstand/enabled = false` throughout.

---

## Reproduction

Deterministic, no flash required:

1. Enable nightstand, keep the watch on the charger.
2. Toggle AoD **off** in Settings → Display, confirm nothing renders in LPM.
3. Toggle AoD **on** again. → still black, both toggles read on.
4. Toggle the quickpanel AoD toggle. → renders.

Check the disagreement at any time:

```sh
mcetool | grep '^Use low power mode'
dconf read /org/asteroidos/settings/always-on-display
dbus-send --system --print-reply --dest=org.nemomobile.compositor / \
    org.nemomobile.compositor.ambientSupported
```

---

*Diagnosis performed on an AsteroidOS watch rig (sturgeon, beluga, dory) using
asteroid-docking-bay. Written by Claude (Opus) under moWerk's direction as part
of an LLM-driven workflow; all measurements are from live hardware and are
reproducible with the steps above. Errors are ours to correct — please push
back on anything that does not match your reading of the code.*
