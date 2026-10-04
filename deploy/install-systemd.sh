#!/usr/bin/env bash
# Install the Prospector daily run as systemd USER units.
# Run from the checkout: bash deploy/install-systemd.sh
# It copies the two unit files, reloads systemd and enables the timer.
# It does not start a run. Start one by hand with:
#   systemctl --user start prospector-sweep.service
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$HOME/prospector-data"
install -m 0644 "$HERE/systemd/prospector-sweep.service" "$UNIT_DIR/prospector-sweep.service"
install -m 0644 "$HERE/systemd/prospector-sweep.timer" "$UNIT_DIR/prospector-sweep.timer"
systemctl --user daemon-reload
systemctl --user enable --now prospector-sweep.timer
systemctl --user list-timers prospector-sweep.timer --no-pager
