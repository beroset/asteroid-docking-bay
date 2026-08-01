# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
"""Catching the gap between what a watch is CONFIGURED to do and what it does.

Always-on display is toggled in userland (dconf) and performed by MCE. Those
are two different pieces of state, and nothing has ever compared them: a-d-b
recorded the dconf toggle as a standby "consumer" and took it as fact. If the
toggle is on while MCE was never told, every drain figure attributed to AoD was
attributed to something that was not happening.

So capture BOTH sides and diff them across an action. The bug this exists for
is reported as: after a fresh flash, AoD does not render in low-power mode
until the settings app is opened and closed — after which it works, with the
settings themselves unchanged. Two captures either side of that action say
which layer moved, and therefore which project owns the bug:

  * dconf identical, MCE changed  -> the setting was never PUSHED to MCE at
    boot; opening the page pushes it. A configuration-plumbing bug.
  * both identical                -> not a config discrepancy at all; the
    thing that draws was simply never instantiated. A different bug entirely.

Reading is deliberately side-effect free. The evidence for a boot is destroyed
the moment anything opens the settings app, so this must be safe to run first.
"""

from __future__ import annotations

import shlex

# One round trip. Both sources, clearly fenced, so a partial read is obvious
# rather than silently short.
# The fences are '@@@' rather than '---' because mcetool underlines its own
# header with a row of dashes. A '---...---' fence matched that underline, and
# everything after it was discarded as belonging to a bogus section: the
# capture came back with ONE MCE key and looked merely sparse rather than
# broken. Fences must not be able to occur in the output they fence.
CAPTURE_CMD = (
    "echo '@@@MCE@@@'; mcetool 2>/dev/null; "
    "echo '@@@DCONF@@@'; "
    "su ceres -c 'XDG_RUNTIME_DIR=/run/user/1000 "
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
    "HOME=/home/ceres dconf dump /' 2>/dev/null; "
    "echo '@@@UPTIME@@@'; cut -d' ' -f1 /proc/uptime"
)


_FENCES = {"@@@MCE@@@": "mce", "@@@DCONF@@@": "dconf", "@@@UPTIME@@@": "uptime"}


def parse(text: str) -> dict:
    """Capture text -> {"mce": {...}, "dconf": {...}, "uptime": float|None}.

    Both halves flatten to key->value maps so they can be diffed key by key.
    mcetool prints `Key: value`; dconf dump prints INI sections, whose keys are
    qualified with their path so two settings of the same name in different
    schemas cannot collide. Pure — see tests.
    """
    section = None
    mce: dict[str, str] = {}
    dconf: dict[str, str] = {}
    path = ""
    uptime = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        # Exact match against the known fences, never a pattern: mcetool's own
        # dashed underline once matched a pattern-based fence and silently ate
        # the rest of the capture.
        if line.strip() in _FENCES:
            section = _FENCES[line.strip()]
            continue
        if section == "mce":
            # Sub-values are indented continuations of the previous key; keep
            # them attached rather than dropping them, since the interesting
            # display settings live there.
            if ":" in line:
                k, _, v = line.partition(":")
                mce[k.strip()] = v.strip()
        elif section == "dconf":
            if line.startswith("[") and line.endswith("]"):
                path = line[1:-1]
            elif "=" in line:
                k, _, v = line.partition("=")
                dconf[f"{path}/{k.strip()}"] = v.strip()
        elif section == "uptime" and line.strip():
            try:
                uptime = float(line.split()[0])
            except ValueError:
                uptime = None
    return {"mce": mce, "dconf": dconf, "uptime": uptime}


def diff(before: dict, after: dict) -> dict:
    """What moved between two captures, per layer.

    The VERDICT is the point: which side changed decides which project owns
    the bug, and that is exactly the question two blobs of text do not answer
    on their own. Pure — see tests.
    """
    def _delta(a: dict, b: dict) -> dict:
        out = {}
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                out[k] = [a.get(k), b.get(k)]
        return out

    mce_d = _delta(before.get("mce", {}), after.get("mce", {}))
    dconf_d = _delta(before.get("dconf", {}), after.get("dconf", {}))
    if mce_d and not dconf_d:
        verdict = ("MCE changed while the userland settings did not — the "
                   "setting was never pushed to MCE at boot, and the action "
                   "pushed it. Configuration plumbing, not the toggle.")
    elif dconf_d and not mce_d:
        verdict = ("The userland settings changed but MCE did not — the action "
                   "wrote a setting that never reached its consumer.")
    elif mce_d and dconf_d:
        verdict = ("Both layers changed — the action wrote a setting AND it "
                   "reached MCE, so compare the keys to see which write "
                   "mattered.")
    else:
        verdict = ("NEITHER layer changed. Whatever the action fixed is not "
                   "configuration state at all — look at what draws (was it "
                   "ever instantiated?) rather than at what is configured.")
    return {"mce": mce_d, "dconf": dconf_d,
            "mce_changed": len(mce_d), "dconf_changed": len(dconf_d),
            "verdict": verdict}


# The two sides of the same intent. dconf is what the user asked for; MCE's
# "Use low power mode" is whether the thing that draws was ever told. Found by
# reading a real watch: MCE exposes 111 keys and this is the counterpart.
AOD_DCONF_KEY = "org/asteroidos/settings/always-on-display"
MCE_LPM_KEY = "Use low power mode"


def consistency(cap: dict) -> dict:
    """Does the AoD toggle agree with MCE, in ONE capture?

    This is the payoff. The reported bug — toggle on, nothing drawn in
    low-power mode until the settings app is opened — shows up here as
    `consistent: False` without needing a before/after pair, a fresh flash, or
    anyone watching the screen at the right moment. Pure — see tests.
    """
    want = (cap.get("dconf") or {}).get(AOD_DCONF_KEY)
    got = (cap.get("mce") or {}).get(MCE_LPM_KEY)
    if want is None or got is None:
        return {"known": False,
                "note": ("could not read both sides — "
                         f"dconf {AOD_DCONF_KEY}={want!r}, "
                         f"MCE {MCE_LPM_KEY!r}={got!r}")}
    asked = want.strip().lower() == "true"
    doing = got.strip().lower().startswith("enabled")
    out = {"known": True, "aod_requested": asked, "mce_lpm_enabled": doing,
           "consistent": asked == doing}
    if asked and not doing:
        out["note"] = ("AoD is switched ON in userland but MCE's low power "
                       "mode is DISABLED — the setting never reached the thing "
                       "that draws. Nothing will render in LPM, and any drain "
                       "attributed to AoD is attributed to something that is "
                       "not happening.")
    elif doing and not asked:
        out["note"] = ("MCE is in low power mode while the AoD toggle is OFF — "
                       "the reverse mismatch; something enabled it behind the "
                       "toggle.")
    return out


def capture(watch) -> dict:
    """One side-effect-free capture from a watch."""
    rc, out, err = watch.t.shell(shlex.quote(CAPTURE_CMD), timeout=40)
    if rc != 0 or "@@@MCE@@@" not in out:
        return {"ok": False, "error": (err or out).strip()[:200] or "no output"}
    parsed = parse(out)
    if not parsed["mce"]:
        return {"ok": False, "error": "mcetool returned nothing — is MCE up?",
                **parsed}
    return {"ok": True, **parsed, "aod": consistency(parsed)}
