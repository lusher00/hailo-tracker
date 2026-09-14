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
RUN_USER=$(whoami)

echo "Install directory: ${INSTALL_DIR}"
echo "User: ${RUN_USER}"

# ------------------------------------------------------------
# Preflight
# ------------------------------------------------------------
if ! command -v rpicam-vid >/dev/null 2>&1; then
    echo "Warning: rpicam-vid not found. Install with: sudo apt install -y rpicam-apps"
fi

if ! /usr/bin/python3 -c "import hailo_platform" >/dev/null 2>&1; then
    echo "Warning: hailo_platform not importable by /usr/bin/python3."
    echo "         See SETUP.md — you need either 'sudo apt install hailo-all' (Pi OS)"
    echo "         or the HailoRT source build (Ubuntu)."
fi

if ! ls "${INSTALL_DIR}"/*.hef >/dev/null 2>&1 && \
   ! ls /usr/share/hailo-models/*.hef >/dev/null 2>&1; then
    echo "Warning: no .hef model found. Run ./download_model.sh first."
fi

# ------------------------------------------------------------
# udev rule so /dev/hailo0 is accessible without sudo
# ------------------------------------------------------------
echo "Setting up udev rule for /dev/hailo0..."
sudo tee /etc/udev/rules.d/99-hailo.rules > /dev/null << 'UDEV'
KERNEL=="hailo*", MODE="0666"
UDEV

sudo udevadm control --reload-rules
sudo udevadm trigger

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

# ------------------------------------------------------------
# Config file — edit this instead of the .py to retune
# ------------------------------------------------------------
CONF_FILE="${INSTALL_DIR}/hailo-tracker.env"
if [ ! -f "${CONF_FILE}" ]; then
    echo "Creating ${CONF_FILE}..."
    cat > "${CONF_FILE}" << 'ENVCONF'
# Every setting here overrides the CONFIG block in hailo_tracker.py.
# Uncomment, edit, then: sudo systemctl restart hailo-tracker
#
# Confidence, class filter and the display toggles are also adjustable live
# from the web UI without a restart — this file just sets the boot defaults.

# ---------- server ----------
#HTTP_PORT=8080
#HTTP_HOST=0.0.0.0          # 127.0.0.1 to expose only to the Pi itself

# ---------- detection ----------
#CONF_THRESH=0.40
# Comma-separated; empty or absent means all 80 COCO classes
#TRACKED_CLASSES=cat,dog,person
# Ignore boxes smaller than this fraction of the frame
#MIN_BOX_AREA_FRAC=0.0005
# Run the NPU every Nth frame; the tracker coasts between
#INFER_EVERY_N=1

# ---------- tracking ----------
#TRACK_ENABLED=true
#TRACK_IOU=0.30
#TRACK_MAX_MISSES=15     # frames to coast a lost object (15 = 0.5s at 30fps)
#TRACK_MIN_HITS=3        # frames before an object counts as real
#TRAIL_LENGTH=48

# ---------- cameras ----------
# Which CSI cameras to run: auto (whatever libcamera reports), or a list.
#CAMERAS=auto               # auto | 0 | 0,1

# Camera 0. These are also the fallback for every other camera, so set the
# common values here and only override what differs below.
#CAM_WIDTH=1280
#CAM_HEIGHT=720
#CAM_FRAMERATE=30
#CAM_AUTOFOCUS=continuous   # continuous | manual | auto | blank for none
#CAM_LENS_POSITION=         # dioptres, required if AF is manual (0 = infinity)
#CAM_SHUTTER=20000          # microseconds; blank = auto exposure
#CAM_GAIN=2                 # blank = auto
#CAM_EV=0
#CAM_DENOISE=off
# Both flips together = 180 degrees, and they are the cheap way to do it:
# they go to rpicam-vid as --hflip/--vflip and the ISP applies them. CAM_ROTATE
# below is an OpenCV rotate on every frame in Python, so keep that one for 90
# and 270, which flips cannot express.
#CAM_HFLIP=false
#CAM_VFLIP=false
#CAM_EXTRA_ARGS=            # anything else to pass to rpicam-vid
#CAM_ROTATE=0               # 0, 90, 180 or 270 degrees clockwise

# Image controls. Blank means "don't pass the flag at all", which is not the
# same as passing a neutral value -- blank leaves the ISP on its own default.
#CAM_BRIGHTNESS=            # -1.0 .. 1.0
#CAM_CONTRAST=              # 1.0 is normal
#CAM_SATURATION=            # 0.0 is mono, 1.0 is normal
#CAM_SHARPNESS=             # 1.0 is normal
#CAM_AWB=                   # auto|incandescent|tungsten|fluorescent|indoor|daylight|cloudy

# Software effect, applied to the finished frame after inference -- detection
# always runs on clean video. Costs CPU per frame; edges and heat are the
# expensive ones at 720p30.
#CAM_EFFECT=none            # none|gray|invert|edges|heat|sharpen
#CAM_DETECT=true            # run the NPU on this camera
#CAM_INFER_EVERY_N=1

# Camera 1, if you have a second module on the other CSI port. Anything left
# unset here inherits camera 0's value. Detection is the exception: additional
# cameras come up as plain video, because two cameras sharing one NPU halve the
# inference rate each one gets. Flip it here or from the web UI.
#CAM1_WIDTH=1280
#CAM1_HEIGHT=720
#CAM1_FRAMERATE=30
#CAM1_AUTOFOCUS=            # dropped automatically for sensors with no AF
#CAM1_SHUTTER=20000
#CAM1_GAIN=2
#CAM1_HFLIP=false
#CAM1_VFLIP=false
#CAM1_ROTATE=0
#CAM1_DETECT=false
#CAM1_INFER_EVERY_N=1

# Pre-multi-camera name for CAM_ROTATE, still honoured.
#ROTATE_DEGREES=0

# ---------- display ----------
#JPEG_QUALITY=85
#SHOW_HUD=true
#SHOW_LABELS=true
#SHOW_IDS=true
#SHOW_TRAILS=false
#CONFIRMED_ONLY=true
#STREAM_MAX_FPS=0           # 0 = unthrottled

# ---------- region of interest ----------
# Normalised polygon. Only detections whose centre falls inside are counted.
#ROI_POLYGON=[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]
#SHOW_ROI=false

# ---------- events / snapshots / webhook ----------
#EVENT_LOG=true
#EVENT_DB=/home/pi/hailo-tracker/events.db
#EVENT_RETENTION_DAYS=30
#SNAPSHOT_ON_DETECT=false
#SNAPSHOT_DIR=/home/pi/hailo-tracker/snapshots
#SNAPSHOT_MAX_FILES=500
#SNAPSHOT_COOLDOWN=30
#WEBHOOK_URL=http://homeassistant.local:8123/api/webhook/hailo
#DETECTION_LOG=true
#DETECTION_LOG_COOLDOWN=10

# ---------- model ----------
#HEF_PATH=/usr/share/hailo-models/yolov8s_h8l.hef
#NN_SIZE=640
#BOX_ORDER=xyxy             # flip to yxyx if boxes look mirrored diagonally
ENVCONF
else
    echo "Keeping existing ${CONF_FILE}"
fi

# ------------------------------------------------------------
# systemd service
# ------------------------------------------------------------
echo "Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << SERVICE
[Unit]
Description=Hailo Tracker — real-time object detection
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=-${CONF_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/${SCRIPT_NAME}
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

sleep 3

echo ""
echo "Service status:"
sudo systemctl status ${SERVICE_NAME} --no-pager || true

echo ""
echo "Installation complete."
echo ""
echo "  Configure:    edit ${CONF_FILE}, then sudo systemctl restart ${SERVICE_NAME}"
echo "  View logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Stop:         sudo systemctl stop ${SERVICE_NAME}"
echo "  Restart:      sudo systemctl restart ${SERVICE_NAME}"
echo "  Uninstall:    ./uninstall.sh"
echo ""
echo "  Web interface: http://$(hostname -I | awk '{print $1}'):8080"
echo ""
