# asteroid-docking-bay 0.4 — the review release

0.3 ended with a first outside contributor; 0.4 is what happened when he was
taken seriously. Ed Beroset asked for a modular, object-oriented,
pytest-testable codebase — reasonable things to want before investing review
effort in a 4300-line single file. This release is that refactor, plus
everything his pre-review and a two-machine fresh-install campaign shook out
of it: nine bugs and stale docs found by fresh eyes on hardware and habits
the original rig never exercised. Nothing here adds a feature. Everything
here makes the next feature reviewable.

The [README](https://github.com/moWerk/asteroid-docking-bay#readme) covers
use; [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) is the map reviewers
should start from. These notes are the technical companion.

## The package split

`bin/asteroid-docking-bay` is now a thin launcher; the implementation lives
in the `asteroid_docking_bay/` package — 13 modules with a documented
dependency direction and two deliberate seams (see ARCHITECTURE.md).
Code moved verbatim, and that claim was proven rather than assumed: an AST
sweep compared every function body against the monolith and caught the one
silent truncation the split had produced (a dropped `return` that compiled
fine and would have broken OS detection). Everything else is byte-identical
modulo the deliberate renames.

`install.sh` installs the launcher plus the package under
`~/.local/share/asteroid-docking-bay/lib` and removes both on `--uninstall`.
Repo-checkout use is unchanged: `./bin/asteroid-docking-bay` just works.

## Classes where state actually lives

- **`Watch(serial)`** — every action bound to one watch: the Control Center
  data batch, connman toggles, clock sync, ceres-session screenshot/
  notification, buzz, screen.
- **`Operation`** with `ChargeOp` / `DrainOp` / `WorkbenchOp` — the shared
  lifecycle (duplicate/conflict refusal, registry seed, durable persist,
  worker spawn, stop, resume-after-restart) written once; the web routes
  collapse to `Op.start()` / `Op.stop()`. Verified end-to-end on hardware,
  including kill-the-service-mid-op resume.
- **`ConfigManager` + `ChargeConfig`/`FlashConfig` dataclasses** — Beroset's
  design, implemented as proposed: file I/O and the read-modify-write lock in
  one place, settings defaults declared once, ~30 scattered
  `.get("key", default)` fallbacks replaced by typed attribute access. The
  config file format is unchanged.
- **`EventLog(dir)`**, **`TaskStore(dir)`** (both directory-injectable — that
  is what makes them testable), **`PowerCache(ttl)`**, and
  **`ChargeDropDetector`** (the losing-power alarm as a state machine).

Port power switching deliberately stays functional — ARCHITECTURE.md explains
why, and names the mapping rework (#2) as the point where a port abstraction
would earn its keep.

## Tests

35 pytest tests over the pure logic: the `adb devices -l` and uhubctl output
parsers, per-serial state lookup, hub/port path math, charge-drop alarm,
standby-rate and next-due projection, EventLog and ConfigManager round-trips,
the foreign-device guard, and a mocked-hardware run of the whole charge
worker. Writing them immediately paid out: the parser tests exposed a
long-standing latent bug where adb daemon-restart notices were parsed as
bogus devices.

## What review and fresh installs found (all fixed here)

From Beroset's pre-review, on his Fedora rig:

- **`map` powered off non-watch devices** — including a keyboard-integrated
  hub and its mouse. A foreign-device guard now classifies every occupied
  port before anything switches; adb/fastboot-visible serials count as
  watches even under non-Google vendor IDs (a hacked Ticwatch found the
  refinement).
- **`fastboot oem unlock` aborted the flash on already-unlocked bootloaders**,
  stranding the watch in fastboot. Now best-effort; a truly locked bootloader
  still fails loudly at the flash itself.
- **A resumed blind-mode charge fed the UI a countdown already in the past**,
  closing a refresh loop that hammered `/api/status` at 30+ req/s. Fixed at
  all three layers (worker, status builder, frontend), with a regression test.
- **Every web menu action silently died on a missing toast element** (the
  screenshot button's mysterious no-op), and the battery-current arrow
  rendered as a literal HTML entity.

From fresh installs on x86_64 (EndeavourOS) and aarch64 (Pinebook Pro):

- install.sh's printed advice still referenced the `plugdev` group its own
  rules had abandoned, claimed uhubctl is in the Arch repos (it is AUR-only),
  and said `pip install bottle` on systems that ship no pip. All hints now
  name working commands per distro.
- A port conflict (:8080 already owned by a container forward) crash-looped
  the service with a raw traceback; it now exits with the `--port` and
  systemd-override instructions spelled out.
- Firewalld silently blocking LAN access to the web UI is now documented.
- `--uninstall` got its first real execution (on the Pinebook) and removes
  the package directory the new layout introduced.

## Known notes

- Group membership: the udev rules grant sysfs port-write access via the
  `users` group. A user added to it mid-session gets fast switching in new
  logins immediately, but long-running systemd user services keep the old
  groups until the next full logout/login; the tool logs which mode it is in
  at startup and falls back to uhubctl transparently.
- `map`'s detection window is sized for AsteroidOS boot times; watches that
  need a long cold boot are onboarded from the web UI instead (30 s window
  plus one automatic power-cycle retry), or raise
  `charge.onboard_wait_seconds`.

## Provenance (unchanged stance)

Written by an LLM coding agent (Anthropic Claude) directed, tested and
reviewed from the user/tester side by the maintainer — and, new in this
release, pre-reviewed by an external human contributor whose findings are
credited above and in the commit history. Commit discipline follows
cbea.ms/git-commit at Beroset's prompting: one coherent change per commit.
GPL-3.0-only. The commit history remains the unedited record.

## Requirements

Unchanged: Python ≥ 3.9 (stdlib), adb; uhubctl for discovery/fallback;
fastboot + wget for flashing; bottle for the web UI. For development:
pytest.
