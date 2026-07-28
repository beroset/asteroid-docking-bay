# benchymark — the fleet FPS benchmark app (spec)

Status: **shipping as an app; installed and run on medaka from the a-d-b web
UI, results read back** (moWerk, 2026-07-28). Drafted 2026-07-27 by moWerk
(concept, visual direction, phase ideas, the Nutty Null layout) + Claude (phase
design against the RAG, measurement and comparability rules, the QML). Source
lives in `benchymark/`. Watchface and app authoring is moWerk's domain; he
reviews it before it ships anywhere.

**Qt6 only** (moWerk, standing decision): the migration is near complete, so
no Qt5 path is written and any Qt5 remainder found on the way is refactored
out rather than accommodated.

## Why an app, and why it was a watchface first

The watches ship **no standalone QML runtime** — probed on catfish
(2.2-nightly): no `qml`, no `qmlscene`, only the QML *modules*. The first
version therefore shipped as a **watchface** (`nutty-benchy`), on the logic
that the launcher is a QML runtime already running and a watchface is just a
file it loads: no tooling to install, same recipe as a-d-b-analyze.

That failed for two reasons, both structural rather than fixable:

1. **A watchface cannot hold the screen.** `Nemo.KeepAlive`'s declarative
   `DisplayBlanking` is ignored inside the compositor — the identical code
   works in `asteroid-flashlight`, which is a client app (moWerk, 2026-07-28).
   The panel blanked mid-run, and forcing it on from the host meant `mcetool`
   fighting mce for the whole run.
2. **A watchface has nowhere to put results.** Everything had to be read off
   the screen, or inferred from the host's kernel sampling.

The app fixes both: it holds its own screen, and it writes
`~/.local/share/benchymark/last-run.json`, which a-d-b reads back. The
watchface and all its host-side driving machinery were **withdrawn on
2026-07-28**; the history is in git, and nothing in the running code refers to
it any more.

Scrolling the launcher or a list stays explicitly **rejected** as the
workload: each watch has different content, so the numbers would not be
comparable, and "it scrolled badly" is not a diagnosis. Fixed scene, fixed
phases, per-phase numbers.

## What it looks like

It wears **Nutty Null**'s layout (moWerk's own watchface, unofficial-watchfaces
`digital-nutty-null`), which stays fixed: one huge glyph dead-centre, a
travelling numeral on the inner rim, one whisper line below. Three changes make
it a benchmark:

- the **centre glyph carries the workload** and is Black weight rather than
  Thin — heavier coverage means a bigger glyph bitmap to rasterise and upload,
  which raises the worst case instead of merely looking different;
- the hour numeral and its fading neighbours become the **FPS rotator**: the
  live FPS travels the rim and its trail *follows* it, each trailing numeral
  holding an older reading, so values push backwards through the tail as the
  head sweeps on. The trail IS the history display — no separate graph needed;
- the whisper line names the phase and its position in the run.

It opens with a **5→0 countdown** carrying the scene version, so there is time
to reach the rig and find the watch among the others, then runs the phases back
to back. Trail numerals turn red below 45 fps, so a bad phase is visible from
across the room.

The FPS rotator is declared **last** in the QML so it paints on top of
everything (declaration order is paint order — never `z:`).

## Phases

Fixed order, fixed duration each, deterministic. Each targets ONE cost path so
a result diagnoses rather than scores. Cost claims cite the RAG
(`qml_patterns.json`) where they are already confirmed knowledge.

| # | phase | what it stresses | expected on the fastest watch |
|---|---|---|---|
| P0 | **Idle clock** — static time, one 16 ms timer, no effects | nothing; the sanity floor | flat 60 |
| P1 | **Glyph scale** — time scaled 1.0→3.0 via a `scale` transform on a base-size distance-field `Text` | transform only | flat 60 |
| P2 | **Glyph re-raster** — the *same visual*, animating `font.pixelSize` instead | glyph-cache churn: CPU rasterization + texture upload per new size | **frame drops** |
| P3 | **Orbit + pulsing shadow** — the FPS number travels the screen border, shadow blur pulsing | per-frame effect re-render, fill rate | drops (version-scoped) |
| P4 | **Overdraw** — N semi-transparent full-screen layers blending | pure GPU fill rate; scales with panel pixels | drops at high N |
| P5 | **Draw-call storm** — many `org.asteroid.controls` `Icon`s in motion | batching/draw-call overhead: `Icon` is a `QQuickPaintedItem`, each its own texture, cannot batch | drops at high count |
| P6 | **Shapes stroke** — an animated Qt Quick `Shape` path (spirograph), re-tessellated each frame | geometry/tessellation path | drops |
| P7 | **Transform cascade** — `scale` animation on an Item with many children | transform recalculation on every child every frame | drops |
| P8 | **Shader** — a doubly domain-warped fBm `ShaderEffect` | honestly GPU-bound, scales with PIXELS not scene complexity | drops; the Mpix/s phase |
| P9 | **Benchy lite** — the 438-vertex wireframe | as below, at a weight most watches can hold | ~14 fps target |
| P10 | **Benchy** — the full 1118-vertex wireframe, the finale | JS arithmetic + `Shape` re-tessellation at once | the heaviest phase |

**P1 vs P2 is the centrepiece.** The RAG's `text_glyph_cache_cpu_cost` states
that on Adreno/Mali "text amount and text-size CHANGES dominate UI cost" and
that animating `font.pixelSize` churns the glyph cache while animating a
`scale` transform does not. So the two phases look **identical on screen** and
differ only in which code path they take: their FPS ratio measures the glyph
path directly. That pair alone gives both poles moWerk asked for — a task the
fastest watch holds at 60, and a task that drops frames on every watch — and
it is the single most transferable finding for UI authors.

## Measuring the frames

Version-agnostic and window-free: an infinite `NumberAnimation` drives a dummy
property, and its change handler counts ticks — an animation ticks once per
**rendered** frame, so dropped frames simply do not tick. A 1 s `Timer`
converts that to FPS and pushes it into the rotator. Works with no access to
the `QQuickWindow`.

The app is the only instrument now. The host used to sample
`/sys/class/graphics/fb0/measured_fps` in parallel as a second, independent
reading; that went with the watchface, because the app records its own
per-phase numbers to disk and no longer needs the screen read for it. The
kernel counter remains available if a disagreement ever needs investigating.

## Known side effect: watches dropping off ADB mid-run

Observed repeatedly on beluga (moWerk, 2026-07-28) while the wireframe phases
run with the screen on: the watch disappears from `adb devices` and returns
only after a port power cycle. The kernel log puts the origin on the watch
side —

```
android_work: sent uevent USB_STATE=DISCONNECTED
msm_otg 78d9000.usb: USB in low power mode
... phy_reset: success -> USB_STATE=CONNECTED -> CONFIGURED   (the port cycle)
```

— with **no OOM kill, no adbd crash and no segfault** in the journal, so this is
not a process dying; the gadget loses its session. What causes that is a
watch-side question (moWerk's domain); from the host the only correlation
available is maximum sustained load together with a lit panel.

This is much less disruptive now than it was: the app writes its results to
disk as it goes, so a link that drops mid-run costs the *reading*, not the
*run*. Reconnect the watch and press Results. Under the old watchface flow the
same drop could also strand the watch on the benchmark face, which is why a
separate restore op had to exist — that whole failure mode is gone.

## Settling, and an idea it suggests

Watches enter a phase carrying the previous one's backlog: the frame rate
starts low and is still falling when a short phase ends, so a number recorded
over the whole window is a blend of two workloads rather than a measurement of
one (moWerk, observed on nemo). Two mitigations are in place — a **quiet
second between phases** where nothing animates and the next phase's name is
already on screen, and **phases lengthened to 10 s** (12 s for the full boat).

**Parked idea worth building:** make that settling its own phase. How long a
watch takes to shed one workload and reach a steady rate under the next is a
real property of the SoC and the scheduler, it differs between watches, and
nothing else here measures it. A phase that alternates two known workloads and
reports the time-to-stabilise would turn today's nuisance into a number.

## Comparability rules

These are what make a number mean something a week later on another watch:

1. **Version-stamp everything.** Scene version shown on screen during the
   countdown and stored with every result, plus the watch's OS build. Change
   the scene → old numbers are void. Version increments in 0.01 steps for
   every change (moWerk), starting at 0.1.
2. **Resolution is not fairness.** The fleet spans 320×360 to 476×402. Fill-rate
   phases (P3, P4, P8) do proportionally more work on a bigger panel — that is
   real hardware truth, so report **raw FPS and Mpix/s side by side** and state
   which phases are pixel-normalized.
3. **One Qt.** Qt6 only, fleet-wide — `MultiEffect`, never
   `Qt5Compat.GraphicalEffects`. This removes what would otherwise have been
   the ugliest comparability caveat: two effect code paths producing numbers
   that could not be compared to each other.
4. **Never `layer.samples`** — a confirmed no-op on all AsteroidOS hardware that
   only logs a warning.
5. **Gate every animation** with `running: <phase active> && visible`. Rendering
   is not animation: a hidden item stops rendering but a running animation keeps
   consuming, which would leak one phase's cost into the next.
6. **The app holds the screen itself** for the whole run and releases it after;
   AoD/nightstand must not fire mid-run.
7. Report **p50 and worst-5% frame times plus the share of frames ≥16.7 ms**, not
   just an average — an average hides exactly the stutter users feel.

## a-d-b side

a-d-b owns the app's lifecycle and nothing else: **install** (pushes the newest
built ipk and `opkg install --force-reinstall --force-depends`), **start**,
**stop**, **remove**, and **results** (reads `last-run.json` back). All five sit
behind one `bench.app` op with an action, in the Control Center's Analysis tab
beside the boot waterfall.

It no longer writes the watchface dconf key, pushes assets, forces the screen,
or samples frame counters — the app does all of that itself. Settings keeps the
watchface key display-only again.

## The Benchy phase — the finale

3DBenchy, 3D printing's own benchmark, rendered as a rotating wireframe by the
app itself. There is no 3D engine to lean on (QtQuick3D absent, see below), so
the QML IS the renderer: every frame it rotates 1118 vertices about the model's
vertical axis, projects them through a perspective divide, and hands six point
arrays to six `PathPolyline`s. Deliberately the heaviest phase — JS arithmetic
and `Shape` re-tessellation at once — and the last one, because it is the one
worth watching.

Pipeline: moWerk decimated the 11.3 MB original to a 2500-vertex variant;
`tools/stl_to_qml_mesh.py` welds it (STL stores every triangle's corners
loose, so without welding no edge is shared and nothing can be chained),
normalises it into a ±1000 cube **without touching orientation**, walks the
edges into continuous strips, and packs them into exactly six buckets — one per
`ShapePath`, because a `Shape` cannot hold a `Repeater` of them. Output: 1118
vertices, 3720 segments, plus a 438-vertex / 1545-segment **lite** variant for
P9.

`benchyStrips` (default 6) is the dial: drawing fewer strips is how to pull the
phase back from a slideshow to a rotation once hardware says which it is.

## Findings behind it

**QtQuick3D: absent — checked.** `ls /usr/lib/qml` on catfish (2.2-nightly)
lists Amber, Connman, Nemo, QML, Qt, Qt5Compat, QtCore, QtFeedback,
QtMultimedia, QtNetwork, QtQml, QtQuick, QtSensors, QtTest, QtWayland, org —
no QtQuick3D, and no Qt6 3D libraries. So there is no engine-level 3D path and
the wireframe stands on its own. What IS there and unused so far:
`QtQuick/Particles` (a particle-system phase, if we want one) and
`QtQuick/Shapes` + `QtQuick/Effects`, which the wireframe and shader phases
already rely on.

**Shaders need a baked `.qsb`.** Inline GLSL crashes Qt6, so P8 ships the
compiled artefact and its source side by side, the latter so the phase can be
rebuilt and audited rather than trusted as a binary blob:

```
qsb --glsl "100 es,120,150" --hlsl 50 --msl 12 \
    -o benchy-shader.frag.qsb benchy-shader.frag
```

**Licence: clear — verified 2026-07-28.** 3DBenchy entered the **public
domain** on 2025-02-14 (NTI Group press release, after the January 2025
enforcement controversy over the original CC BY-ND terms). Attribution is
**not required** and derivative works are **explicitly permitted** — remixes
are encouraged by the current custodians. The earlier NoDerivatives concern is
resolved; we can decimate, wireframe, shade and ship it. Crediting Creative
Tools / NTI stays good manners, not an obligation.
Sources: https://www.3dbenchy.com/3dbenchy-sets-sail-into-the-public-domain/ ,
https://www.nti-group.com/home/news/3dbenchy/

**Build gotchas** for anyone rebuilding the app are in
`docs/audits/2026-07-28-benchymark-build-upstream-findings.md` — three of them
are upstream issues, not benchymark's.
