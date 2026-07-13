# 0.5 — container split (design for review)

Status: **proposal**. Nothing below is implemented; this document is the
first commit of 0.5 so the shape can be reviewed — and corrected — before
any code exists. The architecture is Beroset's (two containers, JSON over
TCP, shared secret); this elaborates it into a concrete contract and a
commit-sized migration plan. Open questions for review are marked **[Q]**.

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
| `watch.cc / toggle / settime / notify / buzz / screen / screenshot` | Watch methods |
| `op.charge.start / stop` (same for drain, workbench) | Operation classes |
| `drain.history` | drain results |
| `config.hide / hide_hub` | config mutations |
| `flash.start`, `onboard.start` | streaming ops |

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
