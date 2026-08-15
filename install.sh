#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
# install.sh — install asteroid-docking-bay for the current user.
#
# Usage:
#   ./install.sh            # installs to ~/.local/bin + ~/.config/systemd/user
#   ./install.sh --uninstall

set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
CONFIG_DIR="${HOME}/.config/asteroid-docking-bay"
UDEV_RULES_DIR="/etc/udev/rules.d"

# ── Argument handling ─────────────────────────────────────────────────────────

UNINSTALL=0
SKIP_ROOT=0
SKIP_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --uninstall)   UNINSTALL=1 ;;
        --no-root)     SKIP_ROOT=1 ;;
        --no-service)  SKIP_SERVICE=1 ;;
        -h|--help)
            echo "Usage: $0 [--uninstall] [--no-root] [--no-service]"
            echo ""
            echo "  --no-root     skip the privileged step (udev rules + group);"
            echo "                prints the commands to run by hand instead"
            echo "  --no-service  do not enable/start the web UI"
            exit 0
            ;;
    esac
done

# ── Uninstall ─────────────────────────────────────────────────────────────────

if [[ $UNINSTALL -eq 1 ]]; then
    echo "Stopping and disabling systemd units…"
    systemctl --user stop  asteroid-docking-bay-charge.timer  2>/dev/null || true
    systemctl --user disable asteroid-docking-bay-charge.timer 2>/dev/null || true
    systemctl --user stop  asteroid-docking-bay-charge.service 2>/dev/null || true
    systemctl --user stop  asteroid-docking-bay-web.service    2>/dev/null || true
    systemctl --user disable asteroid-docking-bay-web.service  2>/dev/null || true

    echo "Removing installed files…"
    rm -f "${BIN_DIR}/asteroid-docking-bay"
    rm -rf "${HOME}/.local/share/asteroid-docking-bay/lib"
    rm -f "${SYSTEMD_USER_DIR}/asteroid-docking-bay-charge.service"
    rm -f "${SYSTEMD_USER_DIR}/asteroid-docking-bay-charge.timer"
    rm -f "${SYSTEMD_USER_DIR}/asteroid-docking-bay-web.service"

    systemctl --user daemon-reload
    echo "Uninstall complete. Config and serial mapping preserved in ${CONFIG_DIR}"
    echo "Remove manually with: rm -rf ${CONFIG_DIR}"
    exit 0
fi

# ── Preflight checks ──────────────────────────────────────────────────────────

echo "Checking dependencies…"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found — install Python 3.9 or later." >&2
    exit 1
fi

PYTHON_VER=$(python3 -c 'import sys; print(sys.version_info[:2] >= (3, 9))')
if [[ "$PYTHON_VER" != "True" ]]; then
    echo "ERROR: Python 3.9 or later required." >&2
    exit 1
fi

if ! command -v adb &>/dev/null; then
    echo "WARNING: adb not found. Install android-tools or android-sdk-platform-tools."
fi

if ! command -v uhubctl &>/dev/null; then
    echo "WARNING: uhubctl not found."
    echo "  Arch:    yay -S uhubctl   (AUR)"
    echo "  Debian:  sudo apt install uhubctl"
    echo "  Source:  https://github.com/mvp/uhubctl"
fi

# ── Install launcher + package ────────────────────────────────────────────────

LIB_DIR="${HOME}/.local/share/asteroid-docking-bay/lib"
echo "Installing launcher to ${BIN_DIR} and package to ${LIB_DIR}…"
mkdir -p "${BIN_DIR}" "${LIB_DIR}"
install -m 755 bin/asteroid-docking-bay "${BIN_DIR}/asteroid-docking-bay"
rm -rf "${LIB_DIR}/asteroid_docking_bay"
cp -r asteroid_docking_bay "${LIB_DIR}/asteroid_docking_bay"
find "${LIB_DIR}/asteroid_docking_bay" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Older versions installed an assets/ tree (the withdrawn nutty-benchy
# benchmark watchface). Clear it on upgrade so an install tree does not keep
# carrying files nothing reads any more.
rm -rf "${LIB_DIR}/assets"

# Ensure ~/.local/bin is on PATH.
if [[ ":$PATH:" != *":${HOME}/.local/bin:"* ]]; then
    echo ""
    echo "NOTE: ${HOME}/.local/bin is not on your PATH."
    echo "Add this to your ~/.bashrc or ~/.zshrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# ── Install systemd user units ────────────────────────────────────────────────

echo "Installing systemd user units to ${SYSTEMD_USER_DIR}…"
mkdir -p "${SYSTEMD_USER_DIR}"
install -m 644 systemd/asteroid-docking-bay-charge.service "${SYSTEMD_USER_DIR}/"
install -m 644 systemd/asteroid-docking-bay-charge.timer   "${SYSTEMD_USER_DIR}/"
install -m 644 systemd/asteroid-docking-bay-web.service    "${SYSTEMD_USER_DIR}/"
systemctl --user daemon-reload

# ── Privileged setup: ONE sudo block, or none ─────────────────────────────────
#
# Everything needing root happens here, in a single `sudo bash -s` — so the
# password is asked exactly once, at a predictable moment, instead of being
# demanded again halfway through by a step the user did not expect. Work out
# WHAT needs doing first (unprivileged), then do it all in one go.

NEED_RULES=0
NEED_GROUP=0
RULES_SRC="udev/70-asteroid-docking-bay.rules"
RULES_DST="${UDEV_RULES_DIR}/70-asteroid-docking-bay.rules"

if [[ -f "$RULES_SRC" ]] && ! cmp -s "$RULES_SRC" "$RULES_DST" 2>/dev/null; then
    NEED_RULES=1
fi
# The rules grant access through the 'users' group; without membership they
# have no effect and a-d-b silently falls back to slow, root-requiring paths.
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx users; then
    NEED_GROUP=1
fi

if [[ $SKIP_ROOT -eq 1 ]]; then
    if [[ $NEED_RULES -eq 1 || $NEED_GROUP -eq 1 ]]; then
        echo ""
        echo "Skipping privileged setup (--no-root). Still to do by hand:"
        if [[ $NEED_RULES -eq 1 ]]; then
            echo "  sudo install -m 644 ${RULES_SRC} ${RULES_DST}"
            echo "  sudo udevadm control --reload-rules && sudo udevadm trigger --action=add"
        fi
        if [[ $NEED_GROUP -eq 1 ]]; then
            echo "  sudo usermod -aG users ${USER}   # then log out and back in"
        fi
    fi
elif [[ $NEED_RULES -eq 0 && $NEED_GROUP -eq 0 ]]; then
    echo "Privileged setup already in place (udev rules current, group membership ok)."
elif ! command -v sudo &>/dev/null; then
    echo ""
    echo "NOTE: sudo not found — run these as root yourself:"
    if [[ $NEED_RULES -eq 1 ]]; then
        echo "  install -m 644 ${RULES_SRC} ${RULES_DST} && udevadm control --reload-rules && udevadm trigger --action=add"
    fi
    if [[ $NEED_GROUP -eq 1 ]]; then
        echo "  usermod -aG users ${USER}"
    fi
else
    echo ""
    echo "Privileged setup — sudo will ask for your password ONCE:"
    if [[ $NEED_RULES -eq 1 ]]; then
        echo "  • udev rules for rootless hub power switching and adb"
    fi
    if [[ $NEED_GROUP -eq 1 ]]; then
        echo "  • add ${USER} to the 'users' group (the rules grant access through it)"
    fi
    if sudo bash -s -- "$NEED_RULES" "$NEED_GROUP" "$RULES_SRC" "$RULES_DST" "$USER" <<'ROOT_BLOCK'
set -euo pipefail
need_rules="$1"; need_group="$2"; src="$3"; dst="$4"; who="$5"
if [[ "$need_rules" == "1" ]]; then
    install -m 644 "$src" "$dst"
    udevadm control --reload-rules
    udevadm trigger --action=add
    echo "  ✓ udev rules installed and applied"
fi
if [[ "$need_group" == "1" ]]; then
    usermod -aG users "$who"
    echo "  ✓ ${who} added to the 'users' group"
fi
ROOT_BLOCK
    then
        if [[ $NEED_GROUP -eq 1 ]]; then GROUP_RELOGIN=1; fi
    else
        echo "  ! privileged setup did not complete — a-d-b will fall back to" >&2
        echo "    slower paths that need root. Re-run install.sh to retry." >&2
    fi
fi

# ── Optional: start the web UI ────────────────────────────────────────────────

if [[ $SKIP_SERVICE -eq 0 ]]; then
    if python3 -c 'import bottle' 2>/dev/null; then
        systemctl --user enable --now asteroid-docking-bay-web.service 2>/dev/null \
            && echo "Web UI enabled at http://127.0.0.1:8080/" \
            || echo "NOTE: could not enable the web service (no user systemd session?)."
    else
        echo ""
        echo "NOTE: python 'bottle' is missing, so the web UI was not started."
        echo "  Arch:   sudo pacman -S python-bottle"
        echo "  Debian: sudo apt install python3-bottle"
        echo "  or:     pip install --user bottle"
        echo "  then:   systemctl --user enable --now asteroid-docking-bay-web.service"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "Installation complete."
if [[ ${GROUP_RELOGIN:-0} -eq 1 ]]; then
    echo ""
    echo "IMPORTANT: you were just added to the 'users' group. Group membership"
    echo "only applies to NEW logins — log out and back in (or reboot) before"
    echo "expecting rootless port switching to work."
fi
echo ""
echo "Next steps:"
echo "  1. Map your hubs:     asteroid-docking-bay map"
echo "  2. Verify:            asteroid-docking-bay status"
echo "  3. Charge timer:      systemctl --user enable --now asteroid-docking-bay-charge.timer"
echo ""
echo "Config will be created at ${CONFIG_DIR}/config.json on first use."
echo "See config.example.json in this repo for all available options."
echo ""
echo "Hubs other than Lenovo and Realtek RTS5411 may need their vendor ID"
echo "uncommented in ${RULES_SRC} — then re-run this script."
