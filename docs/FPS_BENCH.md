# benchymark — a-d-b's side of the FPS benchmark

The benchmark is its own project now:
**https://github.com/moWerk/benchymark**

The spec that used to live here — why it is an app and not a watchface, the
phase table and what each phase isolates, the measurement technique, the mesh
pipeline, and the comparability rules — is maintained there, in the README and
the release notes. It was duplicated here for one release and would have
drifted from upstream the first time either side was touched.

## What a-d-b owns

The app's lifecycle, and nothing else. One `bench.app` op with an action:

- **install** — pushes the newest built ipk and
  `opkg install --force-reinstall --force-depends`
- **start** / **stop**
- **remove**
- **results** — reads `~/.local/share/benchymark/last-run.json` back

All five sit in the Control Center's Analysis tab, beside the boot waterfall.
a-d-b does not measure anything itself: the app holds its own screen, runs its
own phases and writes its own results.

`bench.IPK_DIR` is where an ipk is looked for. Either build one from a checkout
of the benchymark repo (see its `packaging/benchymark.bb`), or download the ipk
attached to a release and drop it in.

## The one thing that is genuinely a-d-b's problem

**Watches drop off ADB during the wireframe phases with the screen lit**,
returning only after a port power cycle. The kernel log puts the origin on the
watch side — the USB gadget loses its session, with no OOM kill, no adbd crash
and no segfault. From the host the only correlation available is maximum
sustained load together with a lit panel.

It matters less than it looks: the app writes results to disk as it goes, so a
dropped link costs the *reading*, not the *run*. Reconnect and press Results.

## Why the run exists

The phases map onto the per-device Qt/GL workarounds in
`/var/lib/environment/compositor/default.env`, so "drop the flag and see if the
UI renders fine" can become a pair of numbers instead of a judgement call. That
campaign is planned per watch as one docking window; the mapping and the
sequence are in the benchymark release notes.
