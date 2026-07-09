# Architecture (0.4)

0.1–0.3 grew as a single 4300-line file. That was the right shape while the
feature set was being discovered on live hardware, and the wrong shape the
moment other humans wanted to review and contribute. 0.4 splits it into a
package, introduces classes where state and identity genuinely live, and adds
a pytest suite for the pure logic.

## Layout

```
bin/asteroid-docking-bay      thin launcher: finds the package, calls cli.main()
asteroid_docking_bay/
    util.py        run() subprocess wrapper, logging setup, shared logger
    adb.py         adb devices/-l parsing, per-serial state, shell, battery,
                   codename + OS detection, ADB wait loops
    config.py      config file I/O + defaults, config lock, lookup helpers
                   (codename <-> serial <-> hub port)
    usb.py         port power: direct sysfs read/write/cycle, PowerCache,
                   uhubctl discovery/fallback + cross-process lock,
                   PPPS (true-VBUS) test, sysfs topology scan
    fastboot.py    fastboot device polling + cache, nightly download +
                   SHA512 verify, the fastboot flash sequence
    events.py      EventLog: per-watch JSONL timeline, standby-drain rate,
                   adaptive next-due projection, drain-test results/summaries
    tasks.py       in-memory operation registries + TaskStore (atomic JSON
                   persistence so running ops survive restarts)
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
tests/             pytest suite (pure logic only — no hardware, no adb)
```

Dependency direction (imports only point left):

```
util -> adb -> config -> usb -> fastboot/events/tasks -> watchctl -> ops
     -> webstatus -> webapp -> cli
```

Two deliberate seams keep that acyclic:

- `adb.wait_serial_online()` can power-cycle a port as enumeration recovery;
  it imports `usb` lazily inside the function rather than at module level.
- The background cache warmer needs both `usb` (port power cache) and
  `fastboot` (device poll), so it lives in `webapp`, the only place it is
  started.

## Where the classes are — and where they aren't

Classes were introduced where there is real per-instance state or identity:

- **`Watch(serial)`** (watchctl) — every action bound to one watch.
- **`EventLog(dir)`** (events) — the JSONL timeline; directory injectable,
  which is also what makes it testable.
- **`TaskStore(dir)`** (tasks) — durable operation state.
- **`PowerCache(ttl)`** (usb) — TTL'd port-power cache.
- **`ChargeDropDetector`** (ops) — the losing-power alarm state machine.

Port power switching stays as module functions (`usb.set_power(loc, port,
on)` etc.). Wrapping every call site in a `UsbPort` object would have churned
~40 hardware-critical lines for no behavioral gain; if a port abstraction
earns its keep later (issue #2's mapping rework is the likely trigger), the
functions give it a single place to grow from.

The operation workers (charge/drain/workbench) remain functions around a
shared task-dict/stop-event pattern. They are candidates for an `Operation`
base class, but converting them without hardware-verifying every path is how
regressions happen; they moved verbatim.

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
