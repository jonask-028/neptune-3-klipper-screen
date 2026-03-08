#!/bin/bash
# Install script for neptune-3-klipper-screen
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="neptune-screen.service"
PRINTER_DATA="$HOME/printer_data"
MOONRAKER_ASVC="${PRINTER_DATA}/moonraker.asvc"

echo "Installing Neptune 3 Klipper Screen daemon..."

# Copy service file
# Copy service file with user and path substituted
sed -e "s|NEPTUNE_USER|${USER}|g" \
    -e "s|NEPTUNE_INSTALL_DIR|${SCRIPT_DIR}|g" \
    "${SCRIPT_DIR}/neptune-screen.service" | sudo tee /etc/systemd/system/neptune-screen.service > /dev/null
sudo systemctl daemon-reload

# Enable and start
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

# Register with Moonraker's allowed services
if [ -f "${MOONRAKER_ASVC}" ]; then
    if ! grep -qxF "neptune-screen" "${MOONRAKER_ASVC}"; then
        echo "neptune-screen" >> "${MOONRAKER_ASVC}"
        echo "Added neptune-screen to ${MOONRAKER_ASVC}"

        # Restart Moonraker to pick up the change
        if systemctl is-active --quiet moonraker; then
            sudo systemctl restart moonraker
            echo "Restarted Moonraker"
        fi
    else
        echo "neptune-screen already in ${MOONRAKER_ASVC}"
    fi
else
    echo "Warning: ${MOONRAKER_ASVC} not found — skipping Moonraker integration"
    echo "  To enable, add 'neptune-screen' to your moonraker.asvc file"
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
echo "  managed_services: neptune-screen"
echo ""
echo "Usage:"
echo "  sudo systemctl status ${SERVICE_NAME}"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
