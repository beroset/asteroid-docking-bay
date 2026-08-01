# AoD never renders in LPM after a fresh flash — root cause, one-line fix, demonstrated

**TL;DR:** two QML files declare different `defaultValue`s for the *same* dconf
key. On a fresh flash the key is unset, so each falls back to its own default
and they disagree. Settings shows the AoD toggle **ON** while the launcher
writes `lowPowerModeEnabled = false` to MCE. Everything downstream follows.

**Fix: one line in `asteroid-launcher`.** Built, flashed and confirmed on
hardware. Nightstand keeps every feature.

---

## The defect

| file | key | `defaultValue` |
|---|---|---|
| `asteroid-settings` `src/qml/NightstandPage.qml:59` | `/desktop/asteroid/nightstand/always-on-display` | **`true`** |
| `asteroid-launcher` `src/qml/MainScreen.qml:300` | *the same key* | **`false`** |

On entering nightstand mode the launcher does:

```qml
displaySettings.lowPowerModeEnabled = nightstandAlwaysOnDisplay.value
```

With the key unset that reads `false`, so MCE's low power mode is switched off
by the launcher itself, seconds after boot, while Settings displays the toggle
as on.

## The chain, every link measured on hardware

```
key unset on a fresh flash
  → asteroid-settings default true    →  user is shown the toggle ON
  → asteroid-launcher default false   →  writes lowPowerModeEnabled = false
  → MCE "Use low power mode" = disabled
  → MCE pushes setAmbientEnabled(FALSE) to org.nemomobile.compositor
  → lipstick discards every 60s ambient update tick
  → black screen in LPM, and nothing anywhere reports a problem
```

Compositor trace from the broken state (instrumented build, behind the existing
`LIPSTICK_COMPOSITOR_DEBUG` gate):

```
AOD setAmbientEnabled( false ) supported= true current= false
AOD setAmbientUpdatesEnabled( true ) displayState= 0 ambientEnabled= false
AOD update DISCARDED: ambient mode is not enabled
```

## The fix, and the proof

```diff
     ConfigurationValue {
         id: nightstandAlwaysOnDisplay
         key: "/desktop/asteroid/nightstand/always-on-display"
-        defaultValue: false
+        defaultValue: true
     }
```

Launcher trace, same watch, same fresh flash, same docked boot, **only that
line changed**:

```
before:  nightstandAodKey = false  →  NSD enter: will write lowPowerModeEnabled = false
after:   nightstandAodKey = true   →  NSD enter: will write lowPowerModeEnabled = true
```

MCE then reads `Use low power mode: enabled` 25 seconds into a fresh docked
boot — a value that was `disabled` on every previous fresh flash — and **the
watchface renders in LPM right after firstrun, with nothing touched.**

## Why it looked unreproducible for three releases

Every "fix" people found was really *some other code path writing the main AoD
key*, whose default is `true` in both projects:

| action | why it "fixed" it |
|---|---|
| disabling nightstand | the else branch writes `alwaysOnDisplay.value` — `true` |
| unplugging the charger | the off-charger timer writes the same |
| the quickpanel AoD toggle | writes it directly, unconditionally |
| opening Settings → Display and toggling | writes it — *unless* nightstand is on and charging, see below |

And it is **intermittent across identical reboots** because `mceChargerType`
starts at `0` and becomes `4` shortly after startup. Whether that transition
lands before or after component completion decides whether the bad value is
written at all.

## A second, smaller defect worth fixing separately

`asteroid-settings` `src/qml/DisplayPage.qml:127`:

```qml
if (!nightstandEnabled.value || mceChargerType.type == MceChargerType.None) {
    displaySettings.lowPowerModeEnabled = checked
}
```

With nightstand on **and** the watch charging, toggling AoD in Settings updates
dconf and the toggle moves, but MCE is never told — so the setting silently
does nothing. Reproduced deterministically on beluga. Independent of the bug
above and worth its own patch.

## Ruled out along the way, with evidence

| theory | killed by |
|---|---|
| framebuffer / display-driver handoff | `ambientSupported()` returns **true** over D-Bus on a broken watch |
| the Qt6 migration | present in 2.0 and 2.1 |
| the quickpanel rework (#180) | it writes both values unconditionally — it is a *workaround*, and predates 2.0 |
| cradle / USB / power state | a VBUS cycle and a usb-mode switch neither break nor fix it |
| a missing boot-time apply | a service that set MCE at boot was built and flashed; a **control image without it rendered too**, so it was not the fix |
| nightstand's uninitialised `onReadyChanged` | real (a watch booting already on charger never fires it) but **not sufficient** — patched, flashed, still black |

The last two were our own hypotheses. Both were disproved by building them and
running controls, which is the only reason they appear here as dead ends rather
than as conclusions.

## Reproduction

1. Flash any recent image, complete firstrun, leave the watch **on the charger**.
2. Let it blank. → black in LPM, while both AoD toggles read on.
3. `mcetool | grep 'Use low power mode'` → `disabled`.
4. Disable nightstand, or unplug, or hit the quickpanel AoD toggle → renders.

## Suggested patches

1. **`asteroid-launcher`** — `MainScreen.qml:300`, `defaultValue: false` →
   `true`. The whole fix.
2. **`asteroid-settings`** — `DisplayPage.qml:127`, drop or rework the
   nightstand/charger condition so the toggle always reaches MCE.
3. *Optional hardening:* two projects declaring separate defaults for one shared
   key is what made this possible. A shared schema, or the launcher reading the
   setting rather than mirroring it, would make the class of bug impossible.

---

*Diagnosed on an AsteroidOS watch rig (sturgeon, beluga, dory) using
asteroid-docking-bay, across roughly a dozen build/flash cycles. Written by
Claude (Opus) under moWerk's direction as part of an LLM-driven workflow; every
figure above is from live hardware and reproducible with the steps given.
Errors are ours to correct — please push back on anything that does not match
your reading of the code.*
