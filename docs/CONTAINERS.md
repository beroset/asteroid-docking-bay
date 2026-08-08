# 0.5 — container split (design, and what shipped)

Status: **implemented, experimental.** This began as a proposal written
before any code existed, so the shape could be reviewed and corrected first;
it shipped in 0.5 and the header said "nothing below is implemented" long
after that stopped being true. What exists now: `serve-backend` in the CLI,
two Containerfiles, podman quadlets and a network unit under `containers/`,
and the run-it recipe in the README under
[Running in a container](../README.md#running-in-a-container).

What "experimental" means concretely: the entrypoints are kept in step with
the CLI, but this path is not exercised by CI and is not part of the hardware
testing everything touching a hub or a watch gets. The rootless-podman
device/group directives in the quadlets are the least-proven part and should
be expected to need per-host work.

The architecture is Beroset's (two containers, JSON over TCP, shared secret);
this document elaborates it into a concrete contract and a commit-sized
migration plan. Open questions from the review stage are still marked **[Q]** —
they are questions about the design, not a claim that the code is absent.

## Goal and threat model

The web UI is the exposed surface: it parses untrusted HTTP from whatever
can reach the port, and today it runs in the same process that writes sysfs
attributes, drives adb/fastboot, and owns the config. A bottle or parsing
compromise is therefore a host compromise.

The split puts the HTTP surface in a **frontend** container with no volume
mounts and no privileges, and everything that touches the host in a
**backend** container that exposes nothing except a token-authenticated TCP
socket on an internal virtual network. The frontend can be fully compromised
and yields: the ability to send the same JSON requests it could already
send. The backend accepts only explicitly allowed operations from the one
peer that knows the token.

Non-goals for 0.5: multi-user auth, TLS between containers (the network is
host-internal), sandboxing adb itself.

## Processes

```
browser ──HTTP──▶ frontend container            backend container
                  bottle + webtemplate ──TCP──▶ rpc server (token gated)
                  no mounts, no devices,        /dev/bus/usb, sysfs port
                  read-only rootfs,             attrs, adb + fastboot,
                  non-root                      config + state volumes
```

Single-process `serve` remains the default for bare-metal installs; the
container mode is opt-in. Both modes share the same code — the frontend
calls the same functions either directly (monolithic) or through the RPC
client (split), so the contract has one implementation, not two.

## Wire protocol

Newline-delimited JSON (one object per line, UTF-8) over a persistent TCP
connection. Requests carry the token; a line whose token does not match gets
**no reply on the wire** (no oracle for a probe — matching Beroset's "would
only respond if the secret token was part of the received message"). Silent
to the sender is not silent to the operator, though: every rejection is
logged host-side with the peer address, and the backend escalates on repeat
(see Abuse response below).

Request:

```json
{"token": "…", "id": 42, "op": "port.set", "args": {"loc": "1-2.3", "port": 1, "on": true}}
```

Response (`id` echoes the request):

```json
{"id": 42, "ok": true, "data": {"confirmed": true}}
{"id": 42, "ok": false, "error": "non-smart port — power cannot be switched"}
```

Streaming ops (flash, onboarding) reply with multiple frames sharing the
`id`, terminated by a final frame:

```json
{"id": 43, "stream": "Powering on 1-2.4 p4…"}
{"id": 43, "stream": "ADB: M6600TB1Z300"}
{"id": 43, "ok": true, "done": true}
```

The frontend bridges stream frames 1:1 onto the browser SSE channel it
already serves today.

Binary payloads (the screenshot JPEG) are base64 in `data` — at ~60 KB per
screenshot the overhead is irrelevant and keeps the protocol single-channel.

Framing is NDJSON, no length prefix (confirmed in review) — trivially
debuggable with `nc` and eyeballs, and JSON cannot contain a raw newline.

## Operation namespace

Deliberately mirrors the existing `/api/*` routes and module seams — the
`webstatus` document is already the status contract:

| op | maps to |
|---|---|
| `status.get` | webstatus document + thresholds |
| `port.set / port.cycle` | usb.set_power / cycle |
| `watch.poweroff / reboot / bootloader` | existing endpoints |
| `port.recovery / port.continue` | boot recovery / resume boot chain (fastboot) |
| `watch.cc / toggle / settime / notify / buzz / screen / screenshot` | Watch methods |
| `ssh.switch_adb / watch.switch_ssh` | toggle a watch between adb and SSH/developer USB mode |
| `op.charge.start / stop` (same for drain, workbench) | Operation classes |
| `wear.set` | arm/release the off-rig wear marker (holds the port, marks worn) |
| `prefs.set_usb_mode` | set the fleet adb/ssh USB-mode preference (top-bar toggle) |
| `drain.history` | drain results |
| `watch.timeline` | per-watch battery timeline (event log) |
| `watch.hands` | physical hand position (HH:MM) for a hands watch (narwhal sysfs), or null |
| `watch.set_hands` | move a hands watch's physical hands to a validated YYYY-MM-DD HH:MM:SS (narwhal) |
| `weather.get` | current forecast for the stored location (host-side Open-Meteo fetch, no watch) |
| `weather.set_location` | geocode a city name and store the fleet-wide weather location |
| `watch.weather_sync` | fetch the forecast and write it to a watch's weather dconf |
| `watch.settings_read` | mirrored watch settings (dconf appearance/display/nightstand), read-only |
| `watch.settings_write` | write one togglable mirrored setting over dconf (catalog-gated boolean keys only) |
| `watch.set_datetime` | set the watch clock to an explicit validated YYYY-MM-DD HH:MM:SS |
| `watch.quickpanel_set` | enable/disable one quick-panel toggle (rewrites the dconf enabled dict) |
| `watch.fbreport` | fastboot getvar all -> downloadable text report |
| `config.hide / hide_hub` | config mutations |
| `hub.rename` | set a physical hub's friendly name (by address prefix) |
| `socket.set` | set/clear a port's physical socket number (ports sort by it) |
| `flash.start`, `onboard.start` | streaming ops |
| `onboard.sweep_prepare` | power all sockets down before an onboard sweep |
| `onboard.sweep_run` | streaming: onboard every equipped socket, one at a time |
| `onboard.sweep_skip` | skip the running sweep's current port (boot-wait aborts) |
| `watch.bootchart` | systemd boot accounting: summary + per-service activation spans |
| `watch.diag` | kernel diagnostics: suspend blockers, freq residency, eMMC wear, failed units |
| `watch.wake_set` | MCE wake gestures: doubletap policy, wrist/tilt detection |
| `watch.locale_set` | system locale via localectl, gated on what the watch carries |
| `bench.push` | install the nutty-benchy benchmark watchface |
| `bench.run` | streaming: switch to the benchmark, sample FPS, switch back |
| `bench.restore` | put a watch's own watchface back after an interrupted run |
| `bench.app` | benchymark app lifecycle: install/start/stop/remove/results |
| `aod.check` | capture a watch's AoD state (MCE + dconf) and diff two captures; read-only, safe to run before anything touches the UI |
| `watch.dump` | take a full-disk backup of a watch in the background. Held under an oplock for the duration so a-d-b's own housekeeping cannot switch its USB mode mid-copy; the result is size-checked against the watch's own reported disk size and a short file is reported FAILED, never as a backup; a manifest beside it records the date taken, the method and whether it completed |
| `oplock.set` | claim or release a watch for a long operation (dump, flash). While held, a-d-b's own housekeeping — the stray-SSH peeler, the USB-mode aligner, the fake-power self-heal — leaves it alone, and per-port ops refuse. Persisted so it survives a service restart, and EXPIRING so a crashed holder cannot exempt a watch forever |
| `wanze.probe` | wanze telemetry probe: install/stop/harvest. Reads a trace the watch recorded while undocked; `harvest` only clears the on-watch buffer when explicitly asked, since it is the only copy |

The dispatch table is an allow-list; unknown ops get `ok:false`. Nothing
generic (no eval-style "run this shell command" op) — adding a capability
means adding a named op in a reviewable diff.

## Token

Generated once (`secrets.token_urlsafe(32)`), delivered to both containers as
a **podman secret** (confirmed in review — env vars leak into `podman
inspect`, and a bind-mounted file needs its own permission handling). Compared
with `hmac.compare_digest` (constant time).

**Rotation** (after a suspected compromise, or routinely): the token lives in
exactly one place, so rotation is three commands and a restart —

```sh
podman secret rm adb-token
printf %s "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  | podman secret create adb-token -
systemctl --user restart adb-backend adb-frontend
```

Both containers read the secret at start, so the restart is the cutover; any
connection still holding the old token is dropped when the backend restarts.
The secret is never written to a volume, a log, or the config file.

**Abuse response.** A mismatched token is dropped without a reply (above) but
counted per peer. The backend logs each rejection, and on a burst from one
peer it (1) rate-limits that peer — a short accept backoff that grows with the
failure count — and (2) past a hard threshold, closes the listener entirely
and exits non-zero, so systemd surfaces a failed unit rather than leaving a
box quietly under attack. Thresholds are config, defaulting conservative
(rate-limit at a handful, shut down at dozens) because the only legitimate
peer holds the token and never trips this.

## Containers

Rootless **podman** (daemonless, FOSS, native on both reference distros),
one internal network, only the frontend port published.

- **frontend**: `python + bottle + webtemplate + rpc client`. Read-only
  rootfs, `--cap-drop=ALL`, no volumes, non-root user.
- **backend**: the rest of the package. Needs `/dev/bus/usb` passthrough
  (the udev rules already scope device access to the `users` group; the
  container runs as that gid), the hub ports' sysfs `disable` attrs
  (bind-mounted from `/sys/bus/usb/devices` — writes work because the udev
  RUN rule chgrps the attrs on the host), and volumes for config + state
  (`~/.config/asteroid-docking-bay`, `~/.local/share/asteroid-docking-bay`,
  tasks dir). It runs **its own adb server** against the passed-through
  devices. The reviewed preference was the host's adb server, but trial
  showed it unreachable: adb binds 127.0.0.1, invisible from the container
  network, and exposing it with `adb -a` would listen on the LAN — against
  the point of the split. AsteroidOS watches are auth-less so the second
  keyring costs nothing; a WearOS watch needs one re-authorization. Hosts
  that already run `adb -a` behind a firewall can flip this back with
  `Environment=ADB_SERVER_SOCKET=tcp:host.containers.internal:5037`.
- systemd integration via **quadlet** units (confirmed in review), mirroring
  today's user units and fitting the existing systemd-user workflow.

## Nightly image download and verification

Raised in review: which container fetches and verifies the nightly images?
Reasoning to a decision, because the tension is real — the backend is the
privileged container, and giving it outbound internet widens the surface that
matters most.

The deciding constraint is that **verification must happen in the trust domain
that flashes.** Whoever downloads, the backend must SHA512-check the image
against the pinned `SHA512SUMS` before writing it to a watch — it can never
trust bytes handed to it. So verification is backend regardless; the only
question is who fetches.

If the frontend fetched, it would need either a writable volume (breaking its
no-mounts, no-privileges cleanliness) or to stream a ~300 MB image over the
RPC channel (absurd) — and it would still be touching the internet. Both trade
the frontend's most valuable property, its pristine isolation, for nothing.

So **the backend downloads, verifies, and flashes** — one coherent operation
(`flash.start`) in one trust domain, reusing the existing `_download_nightly`
+ `_flash_watch` flow unchanged. The backend's new outbound need is bounded and
auditable: HTTPS to exactly the release host, which the quadlet restricts at
the network level. That bounded egress is a smaller, clearer risk than an
untrusted frontend writing files the backend flashes, and the SHA512-against-
pinned-sums check is the integrity boundary either way.

## Frontend, longer term

For 0.5 the frontend stays Python + bottle serving the existing template —
it is the smallest change that achieves the isolation, and bottle in a
no-privilege, no-mount, read-only container is a contained risk. Longer term
(noted in review) a JavaScript frontend talking to the RPC backend directly
is the more maintainable end state; the container boundary and the RPC
contract are exactly what make that a later swap of one container rather than
a rewrite.

## Migration plan — one reviewable commit each

1. **this document** (correct it before anything below exists)
2. `rpc.py`: framing, token gate, dispatch table — pure logic, fully
   pytest-covered (framing round-trips, auth rejection, unknown ops)
3. backend entry point (`serve-backend`): dispatch wired to the existing
   modules; no behavior change to `serve`
4. frontend rpc client + `serve` gains `--backend host:port` mode; the
   bottle routes become thin proxies when it is set
5. streaming bridge (flash/onboard over RPC → SSE)
6. Containerfiles + quadlet units + README section
7. hardening pass (read-only rootfs, cap drops, secret handling) with the
   verification steps documented per item

Each step keeps the monolithic `serve` green; containers only become the
recommended deployment at the end, and only for network-exposed installs.

## What this does not fix

bottle still parses the HTTP; a frontend compromise still sees every fleet
action the UI offers (it just cannot escalate past the allow-list). The
protocol deliberately does not carry arbitrary shell — the diagnostics-
bundle and similar future features must be named ops, which is a feature.
