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
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            echo "Usage: $0 [--uninstall]"
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
    echo "  Arch:    sudo pacman -S uhubctl"
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

# ── udev rules ────────────────────────────────────────────────────────────────

echo ""
echo "Optional: install udev rules for rootless uhubctl/adb access."
echo "  sudo cp udev/70-asteroid-docking-bay.rules ${UDEV_RULES_DIR}/"
echo "  (edit the file first to uncomment the lines matching your hub's vendor ID)"
echo "  sudo udevadm control --reload-rules && sudo udevadm trigger"
echo "  sudo usermod -aG plugdev \$USER   # log out and back in after this"

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "Installation complete."
echo ""
echo "Next steps:"
echo "  1. Set up udev rules (see above) for rootless operation."
echo "  2. Map your hubs:     asteroid-docking-bay map"
echo "  3. Verify:            asteroid-docking-bay status"
echo "  4. Enable the timer:  systemctl --user enable --now asteroid-docking-bay-charge.timer"
echo "  5. Web UI (optional): pip install bottle"
echo "                        systemctl --user enable --now asteroid-docking-bay-web.service"
echo "                        # then open http://127.0.0.1:8080/"
echo ""
echo "Config will be created at ${CONFIG_DIR}/config.json on first use."
echo "See config.example.json in this repo for all available options."
