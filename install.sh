#!/bin/bash
set -e

SERVICE_NAME="hailo-tracker"
SCRIPT_NAME="hailo_tracker.py"

echo "Installing ${SERVICE_NAME} as a system service..."

if [ "$EUID" -eq 0 ]; then
    echo "Please run as a regular user (not sudo)"
    exit 1
fi

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER=$(whoami)

echo "Install directory: ${INSTALL_DIR}"
echo "User: ${USER}"

# udev rule so /dev/hailo0 is accessible without sudo
echo "Setting up udev rule for /dev/hailo0..."
sudo tee /etc/udev/rules.d/99-hailo.rules > /dev/null << UDEV
KERNEL=="hailo*", MODE="0666"
UDEV

sudo udevadm control --reload-rules
sudo udevadm trigger

# Reload driver to apply permissions immediately
if lsmod | grep -q hailo_pci; then
    echo "Reloading Hailo PCIe driver..."
    sudo rmmod hailo_pci
    sudo modprobe hailo_pci
    echo "Driver reloaded"
else
    echo "hailo_pci not loaded — permissions will apply on next boot"
fi

if [ -e /dev/hailo0 ]; then
    echo "Device permissions: $(ls -l /dev/hailo0 | awk '{print $1}')"
else
    echo "Warning: /dev/hailo0 not found — is the Hailo kit connected?"
fi

# systemd service
echo "Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SERVICE
[Unit]
Description=Hailo Tracker — real-time object detection
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/${SCRIPT_NAME}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}

sleep 2

echo ""
echo "Service status:"
sudo systemctl status ${SERVICE_NAME} --no-pager || true

echo ""
echo "Installation complete!"
echo ""
echo "  View logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Stop:         sudo systemctl stop ${SERVICE_NAME}"
echo "  Restart:      sudo systemctl restart ${SERVICE_NAME}"
echo "  Uninstall:    ./uninstall.sh"
echo ""
echo "  Web interface: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
