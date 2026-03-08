#!/bin/bash
# Install script for neptune-3-klipper-screen
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="neptune-screen@${USER}.service"

echo "Installing Neptune 3 Klipper Screen daemon..."

# Copy service file
sudo cp "${SCRIPT_DIR}/neptune-screen@.service" /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo "Installed and started ${SERVICE_NAME}"
echo ""
echo "Usage:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
