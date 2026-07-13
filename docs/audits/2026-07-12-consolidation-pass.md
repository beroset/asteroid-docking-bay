# Consolidation pass — beroset's "less code" principle applied

**Date:** 2026-07-12 · **Branch:** `0.5-containers` · **Trigger:** review
guidance from Ed Beroset: LLMs reflexively add code when the better answer
is to simplify and consolidate; use one variable name per concept; tests
should hunt edge cases; gate check-ins on regression tests.

## Method

A cross-module reference sweep (which functions are used only in their own
module, or nowhere), a naming inventory (counted variants per concept), a
repetition hunt in the route and op layers, and an edge-case hunt over the
security-relevant primitives. Each change landed as a single-concern
commit with tests green before and after; the table-driven routes were
exercised live against the fleet after deployment.

## What got smaller

- **Dead code removed**: `config.find_port_for_serial` (no caller
  anywhere) and the page's vestigial `halting`/`_haltClear` family (state
  never set since the power-off rewiring; four handlers calling a no-op).
- **One name per concept** (earlier commit `eb1639f`): the adb map is
  `devices` everywhere, hubs iterate as `hub`, serials as `serial`, port
  keys as `port_str` — a 70-insertions/70-deletions diff, net zero.
- **Nineteen uniform routes → one data table** plus a twelve-line factory;
  webapp shrank by a third, and the route⇄op contract became importable
  data that the drift tests read directly instead of regex-scraping.
- **Five lifecycle op handlers → one registrar** over the Operation
  subclasses (charge.start stays explicit for its special case).

## What got safer

- **TokenGate refused an empty token** — `compare_digest("", "")` is true,
  so an empty configured token meant *no gate at all*. Found by the
  edge-case hunt, not by any prior test.
- Eight edge tests added: TCP fragment reassembly, unicode tokens,
  threshold-one alarms, duplicate adb serials, multi-colon tokens, corrupt
  config behavior (pinned as a conscious decision point for 0.6), and the
  headroom-zero cadence boundary.
- **CI gate**: GitHub Actions runs the suite on Python 3.9 and 3.14 for
  every push and PR — beroset's "refuse a check-in unless all regression
  tests pass".

## Deliberately not consolidated (inventory for 0.6)

- `charge_to_target` (CLI/timer) vs `ChargeOp.run` (web) are two charge
  loops with real behavioral differences (stop events, task state, the
  losing-power alarm). Merging them changes behavior for the CLI path —
  maintainer decision, not a mechanical pass.
- `uhubctl_get_power`'s inline port-line parse could reuse
  `parse_uhubctl_status`; low value (single port, different shape), noted.
- webstatus's `cfg_hub` naming stays: a hub's *config entry* and its
  *physical scan entry* are genuinely different things in the same scope.
