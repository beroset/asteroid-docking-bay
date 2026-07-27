# a-d-b-bench — the fleet FPS benchmark watchface (spec)

Status: **spec, nothing built yet.** Drafted 2026-07-27 by moWerk (concept,
visual direction, phase ideas) + Claude (phase design against the RAG,
measurement and comparability rules). Watchface authoring is moWerk's domain;
this spec is the proposal he reviews before any QML is written.

## Why a watchface

The watches ship **no standalone QML runtime** — probed on catfish
(2.2-nightly): no `qml`, no `qmlscene`, only the QML *modules*
(`/usr/lib/qml/QtQuick`, `org/asteroid/controls`). But the launcher is a QML
runtime that is already running, and a watchface is just a QML file it loads.
So the benchmark ships as a watchface: our own fixed scene, injected into the
renderer that actually matters, on every watch, with no tooling to install.
Same recipe as a-d-b-analyze — don't ship a tool, use the engine that is there.

Scrolling the launcher or a list is explicitly **rejected** as the workload:
each watch has different content, so the numbers would not be comparable, and
"it scrolled badly" is not a diagnosis. Fixed scene, fixed phases, per-phase
numbers.

## What it looks like

It is a real watchface — it shows the time — that runs a **timed phase
sequence** like a desktop GPU benchmark: phase name and progress, the live FPS
in large type, and a rolling frame-time graph (the last ~120 frames) so drops
are visible as spikes, not just a lower average. moWerk's visual direction:
striking to watch, not a test pattern.

## Phases

Fixed order, fixed duration each (proposal: 8 s), deterministic. Each targets
ONE cost path so a result diagnoses rather than scores. Cost claims cite the
RAG (`qml_patterns.json`) where they are already confirmed knowledge.

| # | phase | what it stresses | expected on the fastest watch |
|---|---|---|---|
| P0 | **Idle clock** — static time, one 16 ms timer, no effects | nothing; the sanity floor | flat 60 |
| P1 | **Glyph scale** — time scaled 1.0→3.0 via a `scale` transform on a base-size distance-field `Text` | transform only | flat 60 |
| P2 | **Glyph re-raster** — the *same visual*, animating `font.pixelSize` instead | glyph-cache churn: CPU rasterization + texture upload per new size | **frame drops** |
| P3 | **Orbit + pulsing shadow** — the FPS number travels the screen border, shadow blur pulsing | per-frame effect re-render, fill rate | drops (version-scoped, see below) |
| P4 | **Overdraw** — N semi-transparent full-screen layers blending | pure GPU fill rate; scales with panel pixels | drops at high N |
| P5 | **Draw-call storm** — many `org.asteroid.controls` `Icon`s in motion | batching/draw-call overhead: `Icon` is a `QQuickPaintedItem`, each its own texture, cannot batch | drops at high count |
| P6 | **Shapes stroke** — an animated Qt Quick `Shape` path (spirograph), re-tessellated each frame | geometry/tessellation path | drops |
| P7 | **Transform cascade** — `scale` animation on an Item with many children | transform recalculation on every child every frame | drops |

**P1 vs P2 is the centrepiece.** The RAG's `text_glyph_cache_cpu_cost` states
that on Adreno/Mali "text amount and text-size CHANGES dominate UI cost" and
that animating `font.pixelSize` churns the glyph cache while animating a
`scale` transform does not. So the two phases look **identical on screen** and
differ only in which code path they take: their FPS ratio measures the glyph
path directly. That pair alone gives both poles moWerk asked for — a task the
fastest watch holds at 60, and a task that drops frames on every watch — and
it is the single most transferable finding for UI authors.

Optional, NOT part of the comparable core score:

- **P8 GC pressure** — `createObject`/`destroy` churn (RAG lists it as
  expensive). Jittery by nature; run it only when explicitly asked.
- **P9 Shader** — a `ShaderEffect` pass. Cross-version hazard: Qt6 requires a
  pre-compiled `.qsb` and **crashes on inline GLSL**, Qt5 needs the inline
  GLSL. Two different artefacts = not the same test. Ship both, report
  separately, never fold into the score.

## Measuring the frames

Version-agnostic and window-free: an infinite `NumberAnimation` drives a dummy
property, and its change handler counts ticks — an animation ticks once per
**rendered** frame, so dropped frames simply do not tick. A 1 s `Timer`
converts that to FPS and pushes it into the rolling graph. Works identically on
the Qt 5.15 and Qt 6.11 halves of the fleet, with no access to the
`QQuickWindow`.

In parallel, a-d-b samples **`/sys/class/graphics/fb0/measured_fps`** (the MSM
MDSS driver's own counter, present on catfish) and `vsync_event` from the host
while the phases run. Two independent instruments on one run — app-side and
kernel-side — so a disagreement between them is itself informative.

## Comparability rules

These are what make a number mean something a week later on another watch:

1. **Version-stamp everything.** Scene version (a hash of the .qml) shown on
   screen and stored with every result, plus the watch's OS build. Change the
   scene → old numbers are void. Same discipline as the feature-matrix audit.
2. **Resolution is not fairness.** The fleet spans 320×360 to 476×402. Fill-rate
   phases (P3, P4) do proportionally more work on a bigger panel — that is real
   hardware truth, so report **raw FPS and Mpix/s side by side** and state which
   phases are pixel-normalized.
3. **Qt5 ≠ Qt6 on effects.** `Qt5Compat.GraphicalEffects` `DropShadow` and Qt6's
   `MultiEffect` are different code paths, so P3 is **version-scoped**: compare
   it only within a Qt generation.
4. **Never `layer.samples`** — a confirmed no-op on all AsteroidOS hardware that
   only logs a warning.
5. **Gate every animation** with `running: <phase active> && visible`. Rendering
   is not animation: a hidden item stops rendering but a running animation keeps
   consuming, which would leak one phase's cost into the next.
6. **Screen forced on** for the whole run (a-d-b already owns that toggle) and
   released afterwards; AoD/nightstand must not fire mid-run.
7. Report **p50 and worst-5% frame times plus the share of frames ≥16.7 ms**, not
   just an average — an average hides exactly the stutter users feel.

## a-d-b side

- Push the .qml, save the current watchface dconf value, switch to the bench,
  run, **switch back**. This is the one case where a-d-b writes the watchface
  key (Settings keeps it display-only).
- Capture the kernel counters during the run; store per-phase results in the
  fleet registry keyed by serial + OS build + scene version, next to boot times
  and flash wear.
- Present it in Analysis, beside the boot waterfall: same shape of artefact —
  per-phase bars, worst-case highlighted.

## Open questions for moWerk

- Phase duration (8 s → ~1 min for the full run) and whether the run should be
  interruptible from the watch.
- Whether P8/P9 ship at all, given they cannot join the comparable score.
- Whether the bench watchface lives in this repo, in unofficial-watchfaces, or
  ships as an a-d-b asset pushed on demand.
