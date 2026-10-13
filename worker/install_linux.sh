#!/usr/bin/env bash
# Install the Live AI Agent worker as a systemd user service on Linux.
#
#   bash worker/install_linux.sh            # install, enable, start
#   bash worker/install_linux.sh --uninstall
#
# Runs as the invoking user from this checkout; state and logs go to
# ~/.local/state/live_ai_agent. `Restart=always` recovers from crashes and
# `loginctl enable-linger` keeps it running with nobody logged in and starts it
# at boot (that one step may ask for sudo). Git must already be able to push
# from this checkout (an SSH key or credential helper); nothing is stored here.
set -euo pipefail
UNIT=live-ai-worker.service
DIR="$HOME/.config/systemd/user"
if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$DIR/$UNIT"; systemctl --user daemon-reload
  echo "removed $UNIT (state in ~/.local/state/live_ai_agent kept)"; exit 0
fi
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
[ -d "$REPO/.git" ] || { echo "$REPO is not a git checkout" >&2; exit 2; }
mkdir -p "$DIR"
cat > "$DIR/$UNIT" <<EOF
[Unit]
Description=Live AI Agent background worker (no AI)
After=network-online.target

[Service]
ExecStart=$PY $REPO/worker/live_ai_worker.py run --repo $REPO
WorkingDirectory=$REPO
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
loginctl show-user "$USER" -p Linger | grep -q yes || loginctl enable-linger "$USER" || \
  echo "run 'sudo loginctl enable-linger $USER' so it survives logout and starts at boot"
systemctl --user --no-pager status "$UNIT" | head -5
