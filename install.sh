#!/bin/bash
# Install script for neptune-3-klipper-screen
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_INSTANCE="neptune-screen@${USER}"
SERVICE_NAME="${SERVICE_INSTANCE}.service"
PRINTER_DATA="$HOME/printer_data"
MOONRAKER_ASVC="${PRINTER_DATA}/moonraker.asvc"

echo "Installing Neptune 3 Klipper Screen daemon..."

# Copy service file
sudo cp "${SCRIPT_DIR}/neptune-screen@.service" /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

# Register with Moonraker's allowed services
if [ -f "${MOONRAKER_ASVC}" ]; then
    if ! grep -qxF "${SERVICE_INSTANCE}" "${MOONRAKER_ASVC}"; then
        echo "${SERVICE_INSTANCE}" >> "${MOONRAKER_ASVC}"
        echo "Added ${SERVICE_INSTANCE} to ${MOONRAKER_ASVC}"

        # Restart Moonraker to pick up the change
        if systemctl is-active --quiet moonraker; then
            sudo systemctl restart moonraker
            echo "Restarted Moonraker"
        fi
    else
        echo "${SERVICE_INSTANCE} already in ${MOONRAKER_ASVC}"
    fi
else
    echo "Warning: ${MOONRAKER_ASVC} not found — skipping Moonraker integration"
    echo "  To enable, add '${SERVICE_INSTANCE}' to your moonraker.asvc file"
fi

echo ""
echo "Installed and started ${SERVICE_NAME}"
echo ""
echo "To enable Fluidd update management, add to moonraker.conf:"
echo ""
echo "  [update_manager neptune-screen]"
echo "  type: git_repo"
echo "  path: ${SCRIPT_DIR}"
echo "  origin: https://github.com/jonask-028/neptune-3-klipper-screen.git"
echo "  managed_services: ${SERVICE_INSTANCE}"
echo ""
echo "Usage:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
