# Architecture (1.0)

0.1–0.3 grew as a single 4300-line file. That was the right shape while the
feature set was being discovered on live hardware, and the wrong shape the
moment other humans wanted to review and contribute. 0.4 splits it into a
package, introduced classes where state and identity genuinely live, and added
a pytest suite for the pure logic.

Everything since has kept that shape: ~18k lines of package across 36 modules,
with ~13k lines of tests. This map went stale between 0.5 and 1.0 — twenty
modules had appeared without being described here, including the op table that
is the container split's security boundary — and was brought back level by the
pre-1.0 audit. A test now fails if a module is missing from it.

## Layout

```
bin/asteroid-docking-bay      thin launcher: finds the package, calls cli.main()
asteroid_docking_bay/
    util.py        run() subprocess wrapper, logging setup, shared logger
    transport.py   ADB/SSH transport: one watch's shell/pull/push, over adb
                   (by serial) or ssh (USB-SSH 192.168.2.15 / WiFi-SSH IP);
                   Watch routes every command through it
    adb.py         adb devices/-l parsing, per-serial state, shell, battery,
                   codename + OS detection, ADB wait loops
    config.py      config file I/O + defaults, config lock, lookup helpers
                   (codename <-> serial <-> hub port)
    usb.py         port power: direct sysfs read/write/cycle, PowerCache,
                   sysfs hub discovery (finds non-PPPS hubs uhubctl cannot
                   see) + uhubctl fallback and cross-process lock,
                   PPPS (true-VBUS) test, sysfs topology scan,
                   per-port device identification (serial + link type +
                   the unconfigured state), xHCI device-slot budget
    fastboot.py    fastboot device polling + cache, nightly download +
                   SHA512 verify, the fastboot flash sequence
    events.py      EventLog: per-watch JSONL timeline, standby-drain rate,
                   adaptive next-due projection, drain-test results/summaries
    tasks.py       in-memory operation registries + TaskStore (atomic JSON
                   persistence so running ops survive restarts), the
                   onboard lock and task_active() deadline guard
    wifi.py        connman credential adaptation: find the APs across watch
                   backups, re-key a service to the target watch's own MAC
    usbevents.py   udev-driven bus monitoring: debounced cache invalidation
                   so a docking watch shows up without waiting for the next
                   poll, and naming the enumerated-but-unconfigured state
    watchctl.py    Watch: one serial-bound handle for everything done *to*
                   a watch — Control Center data batch, WiFi/BT toggles,
                   clock sync, screenshot, notification, buzz, screen,
                   ceres-session command wrapper
    ops.py         the long-running operations: charge (with the
                   ChargeDropDetector losing-power alarm), drain test,
                   workbench band-hold, end-of-op graceful poweroff,
                   flash-one-watch orchestration, resume-after-restart
    webstatus.py   the /api/status document builder + live soft-remap
    webtemplate.py the single-page UI (HTML/CSS/JS) as a string
    webapp.py      Bottle app factory: routes, SSE streams, status cache,
                   background cache warmer
    cli.py         argparse commands + main()

    rpc.py         RPC transport for the container split: NDJSON over TCP,
                   the token gate, and the Dispatcher — the allow-list of
                   op name -> handler (see docs/CONTAINERS.md)
    rpcops.py      the backend op table itself: every /api route as a named
                   data or stream op. Adding a capability means registering
                   an op in a reviewable diff, which is why a test pins the
                   list

    oplock.py      a "leave this watch alone" marker for long operations,
                   persisted in the config so a SEPARATE process can see it,
                   and expiring so a crashed holder cannot wedge a watch.
                   The cross-process half of tasks.py — note that
                   active_op_on_slot does NOT see it
    lastseen.py    last-known-good per-watch readings, so the UI can show a
                   stale value marked as stale rather than a blank
    registry.py    the Fleet Registry: a durable per-serial record of every
                   watch the rig has ever seen, plus a change log
    flap.py        counting how often a port re-enumerates — the connection
                   shame badge

    orbit.py       the Orbit port: fleet watches reachable over the air
                   rather than on a USB socket
    bt.py          host-side Bluetooth for Orbit — scan for watches and pair

    variants.py    exact hardware codenames for watches that share one image
    watchimg.py    per-watch product images from asteroidos.org, host-cached
    watch_settings.py  the AsteroidOS settings a-d-b mirrors, and how to read
                   them off a watch
    weather.py     fetch weather (Open-Meteo, keyless) and shape it for a
                   watch's weather dconf
    stockrom.py    full-disk dump and the restore-to-stock path
    wanze.py       host side of the on-watch probe that records while the
                   watch is away
    bench.py       driving the benchymark FPS benchmark app
    boottime.py    boot-time measurement: VBUS-on to usable
    diag.py        the a-d-b-doctor dataset — kernel diagnostics no watch
                   image ships a tool for
    drainlog.py    measuring battery current on a watch running on its own
                   battery
    aodcheck.py    catching the gap between what a watch is CONFIGURED to do
                   and what it actually does
    icecc.py       the compile cluster the dock itself runs on (Machine Room)
tests/             pytest suite (pure logic only — no hardware, no adb)
```

Dependency direction (imports only point left):

```
util -> adb -> config -> usb -> fastboot/events/tasks -> watchctl -> ops
     -> webstatus -> webapp -> cli
```

One deliberate seam keeps that acyclic:

- `adb.wait_serial_online()` can power-cycle a port as enumeration recovery;
  it imports `usb` lazily inside the function rather than at module level.

`rpcops` sits above `webstatus` (it serves that document as `status.get`), and
the web layer reaches the ops only through a caller — `LocalCaller` in
monolithic mode, `RpcClient` in the split. There is **one acknowledged cycle**:
`webstatus.finish_ssh_relocation` calls back into `rpcops` for
`watch.switch_ssh`, worked around with a local import. It also reaches the op
through the private `DISPATCH._data` rather than `Dispatcher.dispatch`, which
skips the unknown-op check — as does `cli.cmd_status`. Recorded in the pre-1.0
audit rather than untangled unattended.

The background cache warmer (needs both `usb` and `fastboot`) lives in
`ops`, with the operations: whichever process runs the ops — monolithic
serve, or the split backend — starts exactly one warmer.

## Where the classes are — and where they aren't

Classes were introduced where there is real per-instance state or identity:

- **`Watch(serial)`** (watchctl) — every action bound to one watch.
- **`Operation`** (ops) — the shared lifecycle of the long-running per-slot
  operations: duplicate/conflict refusal, registry seeding, durable
  persistence, worker spawn, stop, resume-after-restart. `ChargeOp`,
  `DrainOp` and `WorkbenchOp` subclass it with their kind, registries,
  conflict rule and worker body; the web routes reduce to
  `ChargeOp.start(loc, port, cfg)` / `.stop(loc, port)`.
- **`EventLog(dir)`** (events) — the JSONL timeline; directory injectable,
  which is also what makes it testable.
- **`TaskStore(dir)`** (tasks) — durable operation state.
- **`PowerCache(ttl)`** (usb) — TTL'd port-power cache.
- **`ChargeDropDetector`** (ops) — the losing-power alarm state machine.

The operation registries themselves stay as plain dicts in `tasks.py` —
the Operation subclasses bind to them, and the status builder reads them
directly, so one source of truth serves both.

Port power switching stays as module functions (`usb.set_power(loc, port,
on)` etc.). Wrapping every call site in a `UsbPort` object would have churned
~40 hardware-critical lines for no behavioral gain; if a port abstraction
earns its keep later (issue #2's mapping rework is the likely trigger), the
functions give it a single place to grow from. The flash/remap SSE streams
also keep their own lifecycle: they are browser-connection-driven, not
resumable background ops.

## Testing

`pytest` from the repo root. The suite covers the logic that can be tested
without hardware: the `adb devices -l` and uhubctl output parsers, per-serial
state lookup, hub/port path parsing, the charge-drop detector, standby-rate
and next-due math, and EventLog round-trips in a tmpdir. Everything that
touches a bus or a watch is exercised on the real rig instead — see the
release notes for what "verified" means in this project.

## Ground rules preserved from the monolith

- Single source of truth for operation state is the server process; the
  browser only renders it.
- Every USB-touching subprocess is bounded by a timeout.
- adb evidence outranks hub status registers (PPPS test hierarchy).
- No parallel USB reads: the warmer is sequential and gently paced.
