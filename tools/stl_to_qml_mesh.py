#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Binary STL → a QML-embeddable wireframe.

Written for the benchymark app's Benchy phase (docs/FPS_BENCH.md). QML has
no 3D engine on these images (QtQuick3D is absent — checked), so the app
projects the model itself: this script does everything that can be done once,
on the host, so the watch only pays for rotate → project → stroke per frame.

What it emits and why:

* **Deduplicated vertices**, quantised to integers on a ±1000 cube. Integer
  literals keep the embedded payload small and parse fast, and the app
  scales them back into screen space anyway.
* **Edge STRIPS, not an edge list.** Each strip is a chain of vertex indices
  walked greedily through the mesh's edges, so the app can hand one
  strip to one `PathPolyline` instead of stroking thousands of separate
  two-point paths. Segment count is unchanged; draw-call and state overhead
  collapses to the number of strips.
* A **fixed** number of strips (`--strips`), because a `Shape` cannot hold a
  `Repeater` of `ShapePath`s — they are declared statically in the QML, so the
  count is part of the contract between this script and the app's QML.

Usage:
    tools/stl_to_qml_mesh.py in.stl -o benchymark/src/benchy-mesh.js
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import defaultdict


def read_binary_stl(path):
    """Triangles as [(v0, v1, v2), …] of raw float triples."""
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:512]:
        raise SystemExit("ASCII STL not supported — re-export as binary")
    count = struct.unpack("<I", data[80:84])[0]
    tris = []
    for i in range(count):
        off = 84 + i * 50 + 12          # skip the 12-byte facet normal
        tris.append(tuple(struct.unpack("<3f", data[off + k * 12: off + k * 12 + 12])
                          for k in range(3)))
    return tris


def dedupe(tris, quant=1e-4):
    """Weld vertices that coincide within `quant` → (verts, index triangles).
    STL stores every triangle's corners independently, so a mesh of V distinct
    points arrives as 3F loose ones; without welding, no edge is ever shared
    and the strip walk below has nothing to chain."""
    index = {}
    verts = []
    faces = []
    for tri in tris:
        idx = []
        for v in tri:
            key = tuple(round(c / quant) for c in v)
            if key not in index:
                index[key] = len(verts)
                verts.append(v)
            idx.append(index[key])
        if len(set(idx)) == 3:          # drop degenerate faces
            faces.append(tuple(idx))
    return verts, faces


def normalise(verts, scale=1000):
    """Centre on the model's bounding box and fit it in a ±`scale` cube,
    preserving aspect (one divisor for all axes, so the boat is not squashed).

    Orientation is left alone: the data stays in the STL's own frame (Z up for
    a print-bed model) and the app decides which axis faces the viewer.
    Baking a flip in here would silently mirror the model."""
    xs, ys, zs = zip(*verts)
    cx, cy, cz = ((min(a) + max(a)) / 2 for a in (xs, ys, zs))
    span = max(max(a) - min(a) for a in (xs, ys, zs)) or 1.0
    f = 2.0 * scale / span
    return [(round((x - cx) * f), round((y - cy) * f), round((z - cz) * f))
            for x, y, z in verts]


def cluster(verts, faces, target):
    """Vertex-clustering decimation to roughly `target` vertices.

    Bins vertices into a uniform grid and replaces each occupied cell with the
    average of the vertices in it, then rebuilds the faces on the survivors and
    drops the ones that collapsed. Crude next to an edge-collapse simplifier,
    but it is deterministic, dependency-free and preserves the silhouette —
    all a wireframe benchmark needs. The grid resolution is found by bisection
    because cell occupancy is not linear in resolution."""
    if not target or target >= len(verts):
        return verts, faces
    xs, ys, zs = zip(*verts)
    lo = [min(xs), min(ys), min(zs)]
    span = [max(1e-9, max(a) - min(a)) for a in (xs, ys, zs)]

    def bin_at(res):
        cells = {}
        for i, v in enumerate(verts):
            key = tuple(min(res - 1, int((v[k] - lo[k]) / span[k] * res))
                        for k in range(3))
            cells.setdefault(key, []).append(i)
        return cells

    n_lo, n_hi = 2, 256
    best = bin_at(n_hi)
    while n_lo <= n_hi:                       # smallest grid that still fits
        mid = (n_lo + n_hi) // 2
        cells = bin_at(mid)
        if len(cells) > target:
            n_hi = mid - 1
        else:
            best, n_lo = cells, mid + 1

    remap, new_verts = {}, []
    for members in best.values():
        idx = len(new_verts)
        new_verts.append(tuple(sum(verts[m][k] for m in members) / len(members)
                               for k in range(3)))
        for m in members:
            remap[m] = idx
    new_faces = []
    for a, b, c in faces:
        t = (remap[a], remap[b], remap[c])
        if len(set(t)) == 3:
            new_faces.append(t)
    return new_verts, new_faces


def edges_of(faces):
    out = set()
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            out.add((u, v) if u < v else (v, u))
    return out


def walk_strips(edges, n_strips):
    """Chain edges into continuous polylines, then pack them into exactly
    `n_strips` buckets.

    Greedy: from the current vertex, take any unused edge; when the walk dead-
    ends, close the strip and restart elsewhere. Every edge is visited exactly
    once, so the wireframe is complete — the walk only decides how the strokes
    are grouped."""
    adj = defaultdict(list)
    for u, v in edges:
        adj[u].append(v)
        adj[v].append(u)
    unused = set(edges)
    strips = []
    for start in sorted(adj):
        while True:
            nxt = next((w for w in adj[start]
                        if (min(start, w), max(start, w)) in unused), None)
            if nxt is None:
                break
            strip = [start]
            cur = start
            while nxt is not None:
                unused.discard((min(cur, nxt), max(cur, nxt)))
                strip.append(nxt)
                cur = nxt
                nxt = next((w for w in adj[cur]
                            if (min(cur, w), max(cur, w)) in unused), None)
            strips.append(strip)
    # Pack longest-first into the fixed number of buckets, joining the chains
    # in a bucket end to end. A join draws one stray segment between two
    # disconnected chains; on a dense wireframe it is invisible, and it costs
    # far less than another draw call.
    strips.sort(key=len, reverse=True)
    buckets = [[] for _ in range(n_strips)]
    for strip in strips:
        buckets.sort(key=len)
        buckets[0].extend(strip)
    return [b for b in buckets if b]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--strips", type=int, default=6,
                    help="must match the ShapePath count in the app's QML")
    ap.add_argument("--scale", type=int, default=1000)
    ap.add_argument("--target-verts", type=int, default=0,
                    help="decimate to roughly this many vertices (0 = keep all)")
    args = ap.parse_args()

    tris = read_binary_stl(args.stl)
    verts, faces = dedupe(tris)
    if args.target_verts:
        verts, faces = cluster(verts, faces, args.target_verts)
    verts = normalise(verts, args.scale)
    edges = edges_of(faces)
    strips = walk_strips(edges, args.strips)
    segments = sum(len(s) - 1 for s in strips)

    flat = [c for v in verts for c in v]
    with open(args.out, "w") as fh:
        fh.write(f"// GENERATED by tools/stl_to_qml_mesh.py from "
                 f"{args.stl.split('/')[-1]} — do not edit by hand.\n")
        fh.write("// 3DBenchy is public domain (NTI Group, 2025-02-14); "
                 "credit to Creative Tools / NTI.\n")
        fh.write(f"// {len(verts)} vertices, {len(edges)} edges, "
                 f"{len(strips)} strips, {segments} segments, "
                 f"coordinates in a +/-{args.scale} cube.\n")
        fh.write(".pragma library\n")
        fh.write(f"var SCALE = {args.scale};\n")
        fh.write("var V = [" + ",".join(str(c) for c in flat) + "];\n")
        fh.write("var S = [" + ",".join("[" + ",".join(str(i) for i in s) + "]"
                                        for s in strips) + "];\n")

    print(f"{len(tris)} triangles → {len(verts)} vertices, {len(edges)} edges, "
          f"{len(strips)} strips, {segments} segments", file=sys.stderr)


if __name__ == "__main__":
    main()
