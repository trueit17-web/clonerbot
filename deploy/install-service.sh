#!/usr/bin/env bash
# Install ClonerBot as a systemd service so it keeps running after you log out
# of SSH and restarts on crash/reboot.
#
# Prerequisites (do these first, once):
#   cd ~/clonerbot
#   python3 -m venv .venv && source .venv/bin/activate && pip install -e .
#   cp .env.example .env && nano .env      # fill in your keys
#   clonerbot login                        # one-time interactive Telegram login
#
# Then run this script:
#   bash deploy/install-service.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
PY="${APP_DIR}/.venv/bin/python"
UNIT=/etc/systemd/system/clonerbot.service

if [[ ! -x "${PY}" ]]; then
  echo "ERROR: ${PY} not found. Create the venv first:" >&2
  echo "  cd ${APP_DIR} && python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "ERROR: ${APP_DIR}/.env not found. Copy .env.example to .env and fill it in first." >&2
  exit 1
fi

# Warn (don't block) if the Telegram session hasn't been created yet.
if ! ls "${APP_DIR}"/*.session >/dev/null 2>&1 && ! ls "${APP_DIR}"/sessions/*.session >/dev/null 2>&1; then
  echo "WARNING: no Telegram *.session file found. Run 'clonerbot login' once before starting," >&2
  echo "         otherwise the service will fail waiting for an interactive login." >&2
fi

echo "Writing ${UNIT} (user=${USER_NAME}, dir=${APP_DIR})…"
sudo tee "${UNIT}" >/dev/null <<EOF
[Unit]
Description=ClonerBot autonomous copy-trading bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${APP_DIR}
ExecStart=${PY} -m clonerbot.main run
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now clonerbot

echo
echo "✅ Installed and started. Useful commands:"
echo "  sudo systemctl status clonerbot     # is it running?"
echo "  journalctl -u clonerbot -f          # live logs"
echo "  sudo systemctl restart clonerbot    # after 'git pull'"
echo "  sudo systemctl stop clonerbot       # stop"
