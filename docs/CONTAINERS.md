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
connection. Requests carry the token; the backend **silently drops** any
line whose token does not match (no error reply, no log spam an attacker
can use as an oracle — matching Beroset's "would only respond if the secret
token was part of the received message").

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

**[Q1]** NDJSON vs length-prefixed framing — NDJSON is trivially debuggable
(`nc` + eyeballs) and JSON cannot contain raw newlines, so the framing is
sound. Veto if you want length-prefix anyway.

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

Generated once (`secrets.token_urlsafe`), stored root-readable-only, and
injected into both containers as a podman secret; constant-time comparison.
**[Q2]** podman secrets vs a bind-mounted file vs env var — secrets is the
cleanest on Fedora/Arch podman; env vars leak into `podman inspect`.

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
  tasks dir). Runs its own adb/fastboot servers inside the container so no
  host adb socket is exposed. **[Q3]** in-container adb vs talking to a host
  adb server — in-container is more isolated but means a second adb
  authorization keyring; preference?
- systemd integration via **quadlet** units, mirroring today's user units.
  **[Q4]** quadlet vs compose file — quadlet fits the existing
  systemd-user-unit workflow; compose is more portable to docker users.

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
