// SPDX-FileCopyrightText: 2026 Timo Könnecke <github.com/moWerk>
// SPDX-License-Identifier: BSD-3-Clause
// Nutty Benchy — the a-d-b fleet FPS benchmark, wearing Nutty Null's layout.
//
// Layout is Nutty Null's and stays fixed: one huge glyph dead-centre, a
// travelling numeral on the inner rim, one whisper line below. What changed:
//   • the centre glyph carries the workload and is Black weight, not Thin —
//     heavier coverage means more glyph bitmap to rasterise and upload, which
//     is the point here rather than a stylistic one;
//   • the hour numeral and its fading neighbours become the FPS ROTATOR: the
//     live FPS travels the rim, and the trail FOLLOWS it, each trailing
//     numeral holding an older reading — an FPS history pushed through the
//     tail as the head advances;
//   • the whisper line names the phase.
//
// It opens with a 5→0 countdown so you can reach the rig and find the watch.
//
// Qt6 only (moWerk, 2026-07): MultiEffect, never Qt5Compat.GraphicalEffects.
// No layer.samples anywhere — a confirmed no-op on all AsteroidOS hardware
// that only logs a warning (RAG qml_patterns layer_msaa_unsupported).
// Every phase animation is gated `running: active && visible` — rendering is
// not animation, and an ungated phase would leak its cost into the next one.
//
// Font: Inter Tight (SIL OFL 1.1)

import QtQuick
import QtQuick.Effects
import QtQuick.Shapes
import org.asteroid.controls 1.0
import "benchy-mesh.js" as Mesh

Item {
    id: root

    // ── the scene version. CHANGE THIS whenever the workload changes: results
    // are only comparable within one version (see docs/FPS_BENCH.md).
    readonly property string sceneVersion: "1"

    readonly property real maxSize: Math.min(width, height)
    readonly property real rootRadius: Math.min(width, height) * 0.41
    readonly property color fg: Qt.rgba(1, 1, 1, 1)
    readonly property color dim: Qt.rgba(1, 1, 1, 0.7)
    readonly property color hot: "#f85149"

    // ── frame counting ────────────────────────────────────────────────────
    // An animation ticks once per RENDERED frame, so dropped frames simply do
    // not tick. Window-free and Qt-version agnostic; no QQuickWindow needed.
    property real frameTick: 0
    property int frameCount: 0
    property int fps: 0
    property var fpsHistory: []
    readonly property int trailCount: 5

    onFrameTickChanged: root.frameCount++

    // ── phase machine ─────────────────────────────────────────────────────
    readonly property var phases: [
        { name: "IDLE",      dur: 8 },   // the sanity floor: must be flat 60
        { name: "SCALE",     dur: 8 },   // distance-field text, scale transform
        { name: "RERASTER",  dur: 8 },   // same visual, animated pixelSize
        { name: "ORBIT",     dur: 8 },   // rim numeral + pulsing shadow
        { name: "OVERDRAW",  dur: 8 },   // stacked translucent full-screen fills
        { name: "DRAWCALLS", dur: 8 },   // unbatchable Icons in motion
        { name: "SHAPES",    dur: 8 },   // re-tessellated Shape path
        { name: "CASCADE",   dur: 8 },   // scale on an Item with many children
        { name: "BENCHY",    dur: 10 }   // the wireframe boat, projected in QML
    ]
    property int countdown: 5
    property int phase: -1                       // -1 = counting down
    property int phaseElapsed: 0
    readonly property bool running: phase >= 0 && phase < phases.length
    readonly property bool done: phase >= phases.length
    readonly property string phaseName: running ? phases[phase].name : (done ? "DONE" : "READY")
    // Per-phase results, harvested by a-d-b from the watch (and mirrored by the
    // host's own kernel-side sampling): [{phase, min, avg, samples}]
    property var results: []
    property var _cur: []

    function tickSecond() {
        if (countdown > 0) {
            countdown--;
            return;
        }
        if (countdown === 0 && phase === -1) {
            phase = 0;
            phaseElapsed = 0;
            _cur = [];
            return;
        }
        if (!running)
            return;
        phaseElapsed++;
        if (phaseElapsed >= phases[phase].dur) {
            var s = _cur.slice();
            var sum = 0, mn = 999;
            for (var i = 0; i < s.length; i++) {
                sum += s[i];
                if (s[i] < mn)
                    mn = s[i];
            }
            var r = results.slice();
            r.push({
                "phase": phases[phase].name,
                "avg": s.length ? Math.round(sum / s.length) : 0,
                "min": s.length ? mn : 0,
                "samples": s.length
            });
            results = r;
            _cur = [];
            phase++;
            phaseElapsed = 0;
        }
    }

    anchors.fill: parent

    // The frame-tick driver. Long duration, infinite loops — the value itself
    // is meaningless, only the per-frame notification matters.
    NumberAnimation on frameTick {
        from: 0
        to: 1000000
        duration: 16000000
        loops: Animation.Infinite
        running: !displayAmbient
    }

    // One second: harvest the frame count into fps + the trail history, and
    // drive the countdown / phase machine.
    Timer {
        interval: 1000
        repeat: true
        running: !displayAmbient
        onTriggered: {
            root.fps = root.frameCount;
            root.frameCount = 0;
            var h = root.fpsHistory.slice();
            h.unshift(root.fps);
            while (h.length > root.trailCount)
                h.pop();
            root.fpsHistory = h;
            if (root.running) {
                var c = root._cur.slice();
                c.push(root.fps);
                root._cur = c;
            }
            root.tickSecond();
        }
    }

    // ── centre glyph ──────────────────────────────────────────────────────
    // The workload carrier. Black weight (moWerk): more coverage per glyph, so
    // a cache miss costs more — exactly what RERASTER is meant to expose.
    // SCALE and RERASTER show the SAME visual span by two different routes:
    // SCALE animates a transform on distance-field text (the pattern the RAG
    // recommends), RERASTER animates font.pixelSize on native-rendered text
    // (the anti-pattern it warns about — every size churns the glyph cache
    // with a CPU rasterisation and a texture upload). Their ratio IS the
    // measurement.
    readonly property real baseGlyph: maxSize * 0.28
    readonly property real glyphPeak: 1.8

    Text {
        id: centreText

        readonly property bool scaling: root.phase === 1
        readonly property bool rerastering: root.phase === 2

        text: root.countdown > 0 ? root.countdown
                                 : (root.done ? "OK" : Qt.formatDateTime(wallClock.time, "mm"))
        color: root.countdown > 0 ? root.hot : root.fg
        anchors.centerIn: parent
        renderType: rerastering ? Text.NativeRendering : Text.QtRendering
        font.family: "Inter Tight"
        font.weight: Font.Black
        font.letterSpacing: -root.maxSize * 0.02
        font.pixelSize: root.baseGlyph

        SequentialAnimation on scale {
            running: centreText.scaling && centreText.visible
            loops: Animation.Infinite
            alwaysRunToEnd: false
            NumberAnimation { from: 1; to: root.glyphPeak; duration: 900; easing.type: Easing.InOutSine }
            NumberAnimation { from: root.glyphPeak; to: 1; duration: 900; easing.type: Easing.InOutSine }
        }

        SequentialAnimation on font.pixelSize {
            running: centreText.rerastering && centreText.visible
            loops: Animation.Infinite
            NumberAnimation { from: root.baseGlyph; to: root.baseGlyph * root.glyphPeak; duration: 900; easing.type: Easing.InOutSine }
            NumberAnimation { from: root.baseGlyph * root.glyphPeak; to: root.baseGlyph; duration: 900; easing.type: Easing.InOutSine }
        }

        // The countdown reads as a pulse so it is obvious across the room.
        SequentialAnimation on opacity {
            running: root.countdown > 0
            loops: Animation.Infinite
            NumberAnimation { from: 1; to: 0.35; duration: 500 }
            NumberAnimation { from: 0.35; to: 1; duration: 500 }
        }

    }

    // ── FPS rotator: head + following trail ───────────────────────────────
    // Nutty Null's hour numeral and its fading neighbours, repurposed. The
    // head shows the live FPS at the leading rim position; each trail numeral
    // sits BEHIND it holding an older reading, so values push backwards
    // through the tail as the head sweeps on.
    property real rotorAngle: 0

    NumberAnimation on rotorAngle {
        from: 0
        to: 360
        duration: root.phase === 3 ? 2200 : 6000     // ORBIT sweeps faster
        loops: Animation.Infinite
        running: !displayAmbient
    }

    Item {
        id: rotor

        anchors.fill: parent

        Repeater {
            model: root.trailCount + 1

            delegate: Text {
                readonly property bool head: index === 0
                // Trailing numerals lag the head; the gap widens down the tail.
                readonly property real ang: (root.rotorAngle - index * 13 - 90) * Math.PI / 180
                readonly property int shown: head ? root.fps
                                                  : (root.fpsHistory.length > index ? root.fpsHistory[index] : -1)

                text: shown >= 0 ? shown : ""
                visible: shown >= 0
                color: shown >= 0 && shown < 45 ? root.hot : root.fg
                opacity: head ? 1 : Math.max(0.12, 0.62 - (index - 1) * 0.13)
                x: root.width / 2 + root.rootRadius * Math.cos(ang) - width / 2
                y: root.height / 2 + root.rootRadius * Math.sin(ang) - height / 2
                font.family: "Inter Tight"
                font.weight: head ? Font.Bold : Font.Light
                font.pixelSize: root.maxSize * (head ? 0.13 : 0.1)
            }

        }

    }

    // ORBIT's cost: a pulsing shadow recomputed every frame over the rotor.
    MultiEffect {
        source: rotor
        anchors.fill: rotor
        visible: root.phase === 3
        shadowEnabled: true
        shadowColor: "#c00090ff"
        shadowHorizontalOffset: 0
        shadowVerticalOffset: 0

        SequentialAnimation on shadowBlur {
            running: root.phase === 3
            loops: Animation.Infinite
            NumberAnimation { from: 0.2; to: 1; duration: 700; easing.type: Easing.InOutSine }
            NumberAnimation { from: 1; to: 0.2; duration: 700; easing.type: Easing.InOutSine }
        }

    }

    // ── OVERDRAW: stacked translucent full-screen fills, the compositor must
    // blend every one of them every frame. Pure fill rate; scales with panel
    // pixels, which is why results are also reported per megapixel.
    Item {
        anchors.fill: parent
        visible: root.phase === 4

        Repeater {
            model: 7

            delegate: Rectangle {
                anchors.fill: parent
                color: index % 2 ? "#3000a0ff" : "#30ff5090"

                SequentialAnimation on opacity {
                    running: root.phase === 4 && parent.visible
                    loops: Animation.Infinite
                    NumberAnimation { from: 0.35; to: 0.9; duration: 600 + index * 90 }
                    NumberAnimation { from: 0.9; to: 0.35; duration: 600 + index * 90 }
                }

            }

        }

    }

    // ── DRAWCALLS: org.asteroid.controls Icon is a QQuickPaintedItem — each
    // icon is its own scene-graph texture and cannot batch with its siblings,
    // so this measures draw-call/state overhead rather than fill rate.
    Item {
        id: iconStorm

        anchors.fill: parent
        visible: root.phase === 5

        Repeater {
            model: 28

            delegate: Icon {
                readonly property real a: index / 28 * 2 * Math.PI
                readonly property real rad: root.rootRadius * (0.35 + (index % 4) * 0.2)

                name: "ios-flash"
                width: root.maxSize * 0.11
                height: width
                x: root.width / 2 + rad * Math.cos(a + iconStorm.spin) - width / 2
                y: root.height / 2 + rad * Math.sin(a + iconStorm.spin) - height / 2
            }

        }

        property real spin: 0

        NumberAnimation on spin {
            from: 0
            to: 2 * Math.PI
            duration: 3000
            loops: Animation.Infinite
            running: iconStorm.visible
        }

    }

    // ── SHAPES: a stroked path whose geometry changes every frame, so it is
    // re-tessellated continuously — the geometry pipeline, not fill or glyphs.
    Shape {
        id: spiro

        property real t: 0

        anchors.fill: parent
        visible: root.phase === 6
        preferredRendererType: Shape.CurveRenderer

        NumberAnimation on t {
            from: 0
            to: 2 * Math.PI
            duration: 4000
            loops: Animation.Infinite
            running: spiro.visible
        }

        ShapePath {
            fillColor: "transparent"
            strokeColor: "#7ee787"
            strokeWidth: root.maxSize * 0.012
            capStyle: ShapePath.RoundCap
            startX: root.width / 2
            startY: root.height / 2 - root.rootRadius

            PathCubic {
                x: root.width / 2 + root.rootRadius * Math.cos(spiro.t)
                y: root.height / 2 + root.rootRadius * Math.sin(spiro.t)
                control1X: root.width / 2 + root.rootRadius * 1.6 * Math.cos(spiro.t * 2)
                control1Y: root.height / 2 - root.rootRadius * 1.6 * Math.sin(spiro.t * 3)
                control2X: root.width / 2 - root.rootRadius * 1.6 * Math.sin(spiro.t * 3)
                control2Y: root.height / 2 + root.rootRadius * 1.6 * Math.cos(spiro.t * 2)
            }

            PathCubic {
                x: root.width / 2
                y: root.height / 2 - root.rootRadius
                control1X: root.width / 2 - root.rootRadius * 1.4 * Math.cos(spiro.t * 3)
                control1Y: root.height / 2 + root.rootRadius * 1.4 * Math.sin(spiro.t * 2)
                control2X: root.width / 2 + root.rootRadius * 1.4 * Math.sin(spiro.t * 2)
                control2Y: root.height / 2 - root.rootRadius * 1.4 * Math.cos(spiro.t * 3)
            }

        }

    }

    // ── CASCADE: scale on an Item with many children forces a transform
    // recalculation for every child on every frame (RAG expensive_operations).
    Item {
        id: cascade

        anchors.fill: parent
        visible: root.phase === 7
        transformOrigin: Item.Center

        Repeater {
            model: 40

            delegate: Rectangle {
                readonly property real a: index / 40 * 2 * Math.PI

                width: root.maxSize * 0.07
                height: width
                radius: width * 0.3
                antialiasing: true
                color: index % 3 ? "#58a6ff" : "#d29922"
                opacity: 0.75
                x: root.width / 2 + root.rootRadius * 0.75 * Math.cos(a) - width / 2
                y: root.height / 2 + root.rootRadius * 0.75 * Math.sin(a) - height / 2
                rotation: index * 9
            }

        }

        SequentialAnimation on scale {
            running: cascade.visible
            loops: Animation.Infinite
            NumberAnimation { from: 0.55; to: 1.25; duration: 800; easing.type: Easing.InOutSine }
            NumberAnimation { from: 1.25; to: 0.55; duration: 800; easing.type: Easing.InOutSine }
        }

    }

    // ── BENCHY: 3DBenchy as a rotating wireframe, projected in QML ────────
    // There is no 3D engine on these images (QtQuick3D is absent — checked on
    // catfish), so the watchface IS the renderer: every frame it rotates 1118
    // vertices, projects them with a perspective divide, and hands six point
    // arrays to six PathPolylines. The model arrives pre-welded and chained
    // into strips by tools/stl_to_qml_mesh.py, so the watch pays only for
    // rotate → project → stroke. This is deliberately the heaviest phase and
    // the finale: JS arithmetic and Shape re-tessellation at once.
    //
    // 3DBenchy is public domain (NTI Group, 2025-02-14); credit to Creative
    // Tools / NTI.
    property real benchyAngle: 0
    // How many of the six strips to draw — the dial for how brutal this is.
    // Lower it if the phase is a slideshow rather than a rotation.
    property int benchyStrips: 6

    onBenchyAngleChanged: if (benchyShape.visible) root.projectBenchy()

    function projectBenchy() {
        var V = Mesh.V, S = Mesh.S;
        var a = root.benchyAngle * Math.PI / 180;
        var ca = Math.cos(a), sa = Math.sin(a);
        var tilt = 0.42, ct = Math.cos(tilt), st = Math.sin(tilt);
        var cx = root.width / 2, cy = root.height / 2;
        var k = root.maxSize * 0.00042;          // the +/-1000 cube to screen
        var dist = 2800;                          // perspective distance
        // pl* carry the points; bp* carry the start coordinate. A ShapePath
        // starts at startX/startY, so without setting it every strip would
        // trail a stray line back to the top-left corner.
        var lines = [pl0, pl1, pl2, pl3, pl4, pl5];
        var paths = [bp0, bp1, bp2, bp3, bp4, bp5];
        for (var s = 0; s < lines.length; s++) {
            if (s >= root.benchyStrips || s >= S.length) {
                lines[s].path = [];
                continue;
            }
            var idx = S[s], pts = [];
            for (var i = 0; i < idx.length; i++) {
                var o = idx[i] * 3;
                var x = V[o], y = V[o + 1], z = V[o + 2];
                // spin about the model's own vertical axis (Z up, print-bed
                // frame), then tilt the camera down onto it
                var rx = x * ca - y * sa;
                var ry = x * sa + y * ca;
                var depth = ry * ct - z * st;
                var up = ry * st + z * ct;
                var f = dist / (dist + depth);
                pts.push(Qt.point(cx + rx * k * f, cy - up * k * f));
            }
            lines[s].path = pts;
            if (pts.length) {
                paths[s].startX = pts[0].x;
                paths[s].startY = pts[0].y;
            }
        }
    }

    Shape {
        id: benchyShape

        anchors.fill: parent
        visible: root.phase === 8
        onVisibleChanged: if (visible) root.projectBenchy()

        ShapePath { id: bp0; fillColor: "transparent"; strokeColor: "#58a6ff"; strokeWidth: 1; PathPolyline { id: pl0 } }
        ShapePath { id: bp1; fillColor: "transparent"; strokeColor: "#58a6ff"; strokeWidth: 1; PathPolyline { id: pl1 } }
        ShapePath { id: bp2; fillColor: "transparent"; strokeColor: "#79c0ff"; strokeWidth: 1; PathPolyline { id: pl2 } }
        ShapePath { id: bp3; fillColor: "transparent"; strokeColor: "#79c0ff"; strokeWidth: 1; PathPolyline { id: pl3 } }
        ShapePath { id: bp4; fillColor: "transparent"; strokeColor: "#a5d6ff"; strokeWidth: 1; PathPolyline { id: pl4 } }
        ShapePath { id: bp5; fillColor: "transparent"; strokeColor: "#a5d6ff"; strokeWidth: 1; PathPolyline { id: pl5 } }

        NumberAnimation on rotationDriver {
            from: 0
            to: 360
            duration: 6000
            loops: Animation.Infinite
            running: benchyShape.visible
        }

        property real rotationDriver: 0

        onRotationDriverChanged: root.benchyAngle = rotationDriver
    }

    // ── whisper line: phase name and progress (Nutty Null's date slot) ─────
    Text {
        text: root.countdown > 0 ? "GET TO THE RIG"
                                 : (root.done ? "BENCH COMPLETE · v" + root.sceneVersion
                                              : root.phaseName + " · " + (root.phase + 1) + "/" + root.phases.length)
        color: root.dim

        anchors {
            bottom: parent.bottom
            bottomMargin: parent.height * 0.18
            horizontalCenter: parent.horizontalCenter
        }

        font {
            family: "Inter Tight"
            weight: Font.Light
            pixelSize: root.maxSize * 0.054
            letterSpacing: root.maxSize * 0.012
        }

    }

}
