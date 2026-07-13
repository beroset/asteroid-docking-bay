# asteroid-docking-bay 0.5 — the split, and the machine that reviews itself

0.4 was the review release — the point where the code became something other
humans could read. 0.5 is what that reviewability bought: a real external
contributor whose security design became an architecture, a workflow that
answers a naming remark with a pushed commit before the reviewer's next
coffee, and a deep self-audit that found its own bugs before shipping them.
The headline feature is the container split. The headline *change* is that
the project now demonstrably holds itself to a standard.

The [README](https://github.com/moWerk/asteroid-docking-bay#readme) covers
use; [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) and
[docs/CONTAINERS.md](../docs/CONTAINERS.md) are the maps; the
[docs/audits/](../docs/audits/) folder is new and holds the process record
(see below). These notes are the technical companion.

## The container split (experimental)

For a network-exposed deployment, the web UI can now run as an unprivileged
**frontend** container that reaches a separate, host-touching **backend**
over a token-gated socket — so a compromise of the exposed HTTP surface
cannot reach the USB devices, the config, or the operations. The design and
threat model are Ed Beroset's, elaborated into a concrete contract:

- `rpc.py` — newline-delimited JSON over TCP. A `TokenGate` with a
  constant-time compare gives a mismatched token **no reply on the wire**
  (no oracle for a probe) while logging per peer and escalating from a
  growing backoff to a listener shutdown, so a box under attack surfaces as
  a failed unit. It refuses an empty token outright.
- `rpcops.py` — the single op table. Every host-touching operation the web
  API offers is a named op on an allow-list; there is deliberately no generic
  "run a command" op, and **the allow-list is itself a test** — adding a
  capability must consciously touch it.
- `serve --backend host:port` turns the web server into a pure proxy through
  an RPC client instead of the in-process caller — the routes are identical
  either way, so the monolith and the split share one implementation.
- Streaming (flash, onboarding) bridges the same way: the backend yields raw
  messages, the frontend wraps them as SSE.
- `containers/` ships podman Containerfiles and quadlets: the frontend
  publishes the only host port, runs read-only, drops all capabilities, is
  non-root and has no volumes; the backend stays on the container network
  with USB/sysfs access and its own adb server.

**Honest scope:** the container split is **experimental**. The single-process
`serve` is unchanged, fully tested, and remains the default for bare-metal
installs. The split's plumbing is verified end to end (two containers, token
secret, RPC, hardened frontend), but the backend container touching *real*
USB on a target host is the one surface still to be trialled — flagged in the
quadlet for per-host tuning. Run the split only if you want the isolation and
are ready to shake out the device passthrough.

## Everything under the split changed shape

Getting to a clean split forced the web server to stop being a pile of
near-identical route functions. It is now a thin frontend: routes parse HTTP
and dispatch through a caller to the op table. Nineteen uniform routes
collapsed into an importable data table; the drift and op-coverage tests read
that table directly. The cache warmer moved to where the operations live.

## Tests: 48 → 103, and they hunt their own class of bug

New suites, each written to catch a failure class that had actually shipped:

- **Static integrity** — a symtable undefined-name sweep and an unused-import
  sweep on every module, because a mechanical move once silently truncated a
  function and refactors leave import lists lying.
- **UI wiring** — every onclick handler must be a defined function, every
  `getElementById` target must exist or be created at runtime, every JS API
  path must hit a registered route. Each check was validated by planting the
  exact historical bug it exists to catch.
- **Op-table contract** — every op the frontend dispatches is registered, and
  the registered set is pinned as the documented allow-list.
- **RPC transport** — framing across TCP fragments, unicode tokens, escalation,
  and the edge cases a good tester imagines (an empty token is *no gate at all*
  — found and fixed here).

A GitHub Actions workflow now **refuses a check-in unless the suite passes**,
on the floor and current Python — Beroset's commercial-standard rule.

## Consolidation, naming, and Beroset's review made code

Applying his guidance directly and with his name in each commit: one variable
name per concept across the package (the adb map is `devices`, hubs iterate as
`hub`); dead code deleted (a caller-less function, a vestigial JS state
machine); simplify-over-add wherever the diff allowed. The
[consolidation pass](../docs/audits/2026-07-12-consolidation-pass.md) records
what shrank and — honestly — what was deliberately *not* merged and why.

## The self-audit (new: docs/audits/)

Between build and ship, a
[deep second-pass audit](../docs/audits/2026-07-12-0.5-deep-audit.md) ran over
the branch: static caller-integrity, tri-mode live comparison against the
running fleet, and failure injection on real hardware (client disconnects,
backend killed mid-operation, token abuse to shutdown). It found — and this
release fixes — a hardened frontend that could never start (read-only rootfs
vs the default secret mount), a host-adb design that was unreachable as first
specified, PID-1 containers that ignored SIGTERM, and the empty-token hole.
The audit ships *with the code*, methodology and dead ends included, so a
reviewer can go deeper than the diff. That transparency is the point of this
project, not a footnote to it.

## Also fixed

- **Workbench end now powers the watch down to rest** (audit F8): ending a
  checkout cut the port without the graceful poweroff drain and charge
  perform, leaving the watch running on battery behind an "off" port.
- **The running version is on the page.** A fixed top bar shows the update
  stamp (left) and the version (right), so "did my upgrade land?" has an
  answer — and the varying-length stamp no longer repositions the header.

## Known limitations / on the roadmap

- Container backend on real USB: untested, experimental (above).
- A forgotten screen-force-on (`mcetool -D on`) from the Control Center can
  strand a watch's display on with no indicator that it is active — a
  demo-mode detection + release safety is the next UI-pass priority.
- Split-mode UI has no "backend unreachable" banner yet.

## Provenance (unchanged stance)

Written by an LLM coding agent (Anthropic Claude), directed, tested and
ground-truthed on hardware by the maintainer, and — central to this release —
pre-reviewed and shaped by an external human contributor, Ed Beroset, whose
findings and design are credited in the commit history, the audit records,
and above. Commit discipline follows cbea.ms at his prompting: one coherent
change per commit. GPL-3.0-only. The commit history and the audit folder are
the unedited record.

## Requirements

Unchanged: Python ≥ 3.9 (stdlib), adb; uhubctl for discovery/fallback;
fastboot + wget for flashing; bottle for the web UI; podman for the container
split. For development: pytest (the CI gate).
