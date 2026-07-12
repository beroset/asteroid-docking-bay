#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Build the frontend and backend images. Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."
podman build -f containers/Containerfile.backend  -t adb-backend  .
podman build -f containers/Containerfile.frontend -t adb-frontend .
echo
echo "Built adb-backend and adb-frontend. Next (see docs/CONTAINERS.md):"
echo "  1. token secret:  python3 -c 'import secrets;print(secrets.token_urlsafe(32))' \\"
echo "                      | podman secret create adb-token -"
echo "  2. quadlets:      cp containers/adb-*.container containers/adb-*.network \\"
echo "                      ~/.config/containers/systemd/"
echo "  3. start:         systemctl --user daemon-reload && systemctl --user start adb-frontend"
