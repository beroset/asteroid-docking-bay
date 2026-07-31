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


def face_normals(verts, faces):
    """Unit normal per triangle."""
    out = []
    for a, b, c in faces:
        ax, ay, az = verts[a]
        bx, by, bz = verts[b]
        cx, cy, cz = verts[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        m = (nx * nx + ny * ny + nz * nz) ** 0.5 or 1.0
        out.append((nx / m, ny / m, nz / m))
    return out


def feature_edges(verts, faces, angle_deg, min_len_pct=0.0):
    """The edges a person would DRAW: creases and boundaries.

    A wireframe does not want a simplified mesh's edges — it wants the model's
    features. Every interior edge is shared by two triangles; the angle between
    their normals says whether it is a crease (the hull chine, the deck edge,
    a cabin corner) or merely interior tessellation across a smooth panel. Keep
    the creases, drop the rest, and the result reads as a drawing rather than
    as triangle soup.

    This replaces DECIMATION, and that is the point. Both a slicer's reduction
    and the vertex clustering below move or merge vertices, destroying the very
    creases that make a boat look like a boat — which is why the decimated hull
    lost its deck lines. Selecting instead of simplifying runs on the original
    geometry and cannot introduce an artefact: every edge kept is an edge that
    was really there.

    Boundary edges (one adjacent face) are always kept — on a closed model like
    3DBenchy there are none, but an open mesh would fall apart without them.

    ANGLE ALONE IS NOT ENOUGH, and the reason is worth recording. 3DBenchy's
    dihedral distribution is bimodal: 90% of its edges lie under 11 degrees
    (the smooth hull), and the rest sit near 90. But that rest is ~20 000
    edges, because the embossed #3DBenchy text, the nameplate and the overhang
    features are all hard-edged detail and they dominate by COUNT. Raising the
    threshold from 40 to 80 degrees only drops 20 497 to 18 812 — the knob does
    nothing useful.
    LENGTH is the discriminator that works. Those detail creases are tiny: the
    median is 0.06% of the model, while the hull chine, deck edge and cabin
    corners run 1-5%. Keeping long creases keeps the lines a person would draw
    and discards the engraving.
    """
    import math

    normals = face_normals(verts, faces)
    adj = defaultdict(list)
    for fi, (a, b, c) in enumerate(faces):
        for u, v in ((a, b), (b, c), (c, a)):
            adj[(min(u, v), max(u, v))].append(fi)

    cos_limit = math.cos(math.radians(angle_deg))
    kept = []
    for edge, fs in adj.items():
        if len(fs) == 1:
            kept.append(edge)
            continue
        if len(fs) != 2:
            continue                      # non-manifold: leave it out
        n1, n2 = normals[fs[0]], normals[fs[1]]
        dot = n1[0] * n2[0] + n1[1] * n2[1] + n1[2] * n2[2]
        if dot < cos_limit:               # normals diverge -> a crease
            kept.append(edge)

    if min_len_pct > 0:
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        span = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
        floor = span * min_len_pct / 100.0

        def _len(e):
            a, b = verts[e[0]], verts[e[1]]
            return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2
                    + (a[2] - b[2]) ** 2) ** 0.5

        kept = [e for e in kept if _len(e) >= floor]
    return kept


def compact(verts, edges):
    """Drop every vertex no kept edge touches, and renumber."""
    used = sorted({i for e in edges for i in e})
    remap = {old: new for new, old in enumerate(used)}
    return ([verts[i] for i in used],
            [(remap[a], remap[b]) for a, b in edges])


def _join_cost(verts, a_end, b_start):
    """Squared distance between two chain endpoints — the length of the stray
    segment a join would draw."""
    ax, ay, az = verts[a_end]
    bx, by, bz = verts[b_start]
    return (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2


def order_bucket(verts, chains):
    """Order the chains within one bucket so the joins between them are as
    short as possible, reversing a chain when that helps.

    Packing several disconnected chains into one PathPolyline means the stroke
    runs straight from the end of one to the start of the next. Those joins are
    unavoidable at a fixed strip count — but their LENGTH is not. Ordered
    arbitrarily they leap across the model, which is what drew the lines from
    the roof to the chimney and from the roof corners down to the hull
    (moWerk). Nearest-endpoint-first with reversal makes them short enough to
    disappear into the wireframe.

    Greedy rather than optimal: this is a travelling-salesman shape, and a
    conservative nearest-neighbour pass removes the visible offenders without
    the cost or the risk of anything cleverer.
    """
    if not chains:
        return []
    remaining = list(chains)
    ordered = [remaining.pop(0)]
    while remaining:
        end = ordered[-1][-1]
        best, best_cost, best_rev = 0, None, False
        for i, c in enumerate(remaining):
            fwd = _join_cost(verts, end, c[0])
            rev = _join_cost(verts, end, c[-1])
            if best_cost is None or min(fwd, rev) < best_cost:
                best, best_cost, best_rev = i, min(fwd, rev), rev < fwd
        c = remaining.pop(best)
        ordered.append(c[::-1] if best_rev else c)
    return ordered


def walk_strips(edges, n_strips, verts=None):
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
    # Pack SPATIALLY, not by length. Each bucket is grown from one chain by
    # repeatedly taking the nearest remaining chain (reversing it when that end
    # is closer) until the bucket holds its share of the segments.
    #
    # This is what removes the stray lines. Joins between chains in a bucket
    # are unavoidable at a fixed strip count — a PathPolyline strokes straight
    # from the end of one chain to the start of the next — but packing
    # longest-first put chains from opposite ends of the model in the same
    # bucket, so those joins leapt across it: roof to chimney, roof corners
    # down to the hull (moWerk). Growing each bucket outward from a seed keeps
    # its chains neighbours, and the joins shrink into the wireframe.
    #
    # Measured on the 2500-vertex Benchy, joins longer than a fifth of the
    # model (the ones that actually read as wrong lines):
    #     longest-first            313
    #     + nearest-neighbour      126
    #     + spatial packing         27
    # with total join length down 91% and the buckets still even.
    total = sum(len(c) for c in strips)
    share = total / float(n_strips) if n_strips else total
    remaining = list(strips)
    buckets = []
    for b in range(n_strips):
        if not remaining:
            break
        seed = remaining.pop(0)
        bucket, size = [seed], len(seed)
        # The last bucket takes whatever is left, so no chain is ever dropped.
        while remaining and (size < share or b == n_strips - 1):
            end = bucket[-1][-1]
            best, best_cost, best_rev = 0, None, False
            for i, c in enumerate(remaining):
                fwd = _join_cost(verts, end, c[0]) if verts else 0
                rev = _join_cost(verts, end, c[-1]) if verts else 0
                if best_cost is None or min(fwd, rev) < best_cost:
                    best, best_cost, best_rev = i, min(fwd, rev), rev < fwd
            c = remaining.pop(best)
            bucket.append(c[::-1] if best_rev else c)
            size += len(c)
        buckets.append(bucket)
    # Chains stay SEPARATE. Concatenating them into one polyline forced a
    # stroke from the end of each to the start of the next, and on sparse
    # feature-edge geometry those joins cross the whole model. PathMultiline
    # draws a list of independent polylines in one path with no strokes
    # between them, so the joins simply do not exist.
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
    ap.add_argument("--min-edge-pct", type=float, default=0.0,
                    help="with --feature-angle: drop creases shorter than this "
                         "percent of the model's size. This is what separates "
                         "structure from engraving; angle alone cannot.")
    ap.add_argument("--feature-angle", type=float, default=0.0,
                    help="keep only edges whose faces diverge by more than this "
                         "many degrees (0 = every edge). Selecting features "
                         "beats decimating: it runs on the original geometry "
                         "and cannot invent an artefact.")
    args = ap.parse_args()

    tris = read_binary_stl(args.stl)
    verts, faces = dedupe(tris)
    if args.feature_angle:
        # No decimation at all: pick the edges that matter from the real model.
        edges = feature_edges(verts, faces, args.feature_angle,
                              args.min_edge_pct)
        verts, edges = compact(verts, edges)
        verts = normalise(verts, args.scale)
    else:
        if args.target_verts:
            verts, faces = cluster(verts, faces, args.target_verts)
        verts = normalise(verts, args.scale)
        edges = edges_of(faces)
    strips = walk_strips(edges, args.strips, verts)
    segments = sum(len(ch) - 1 for b in strips for ch in b)

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
        # S[bucket][chain][vertex-index] — one bucket per ShapePath (colour),
        # each holding independent chains for a PathMultiline.
        fh.write("var S = [" + ",".join(
            "[" + ",".join("[" + ",".join(str(i) for i in ch) + "]"
                           for ch in b) + "]" for b in strips) + "];\n")

    print(f"{len(tris)} triangles → {len(verts)} vertices, {len(edges)} edges, "
          f"{len(strips)} strips, {segments} segments", file=sys.stderr)


if __name__ == "__main__":
    main()
