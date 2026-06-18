#!/bin/bash
set -e

SERVICE_NAME="hailo-tracker"

echo "Uninstalling ${SERVICE_NAME}..."

sudo systemctl stop ${SERVICE_NAME} 2>/dev/null || true
sudo systemctl disable ${SERVICE_NAME} 2>/dev/null || true
sudo rm -f /etc/systemd/system/${SERVICE_NAME}.service
sudo systemctl daemon-reload

echo ""
echo "Service removed."
echo "Note: /etc/udev/rules.d/99-hailo.rules was kept (may be used by other Hailo apps)."
echo "Remove manually if no longer needed: sudo rm /etc/udev/rules.d/99-hailo.rules"
echo ""
