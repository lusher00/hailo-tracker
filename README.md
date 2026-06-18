# Hailo Tracker

Real-time object detection on Raspberry Pi 5 with Hailo-8L NPU. Streams annotated video to a browser over HTTP.

Detects all 80 COCO classes by default. Edit `TRACKED_CLASSES` at the top of `hailo_tracker.py` to limit tracking to specific objects.

## Hardware

- Raspberry Pi 5
- Hailo-8L AI Kit (M.2 HAT+)
- Raspberry Pi Camera Module 3 (IMX708)

## Features

- 30 FPS real-time inference via Hailo-8L NPU
- All 80 COCO classes with per-class color coding
- Configurable class filter — track everything or just what you care about
- Live MJPEG stream in any browser, no app needed
- `systemd` service for headless auto-start

## Quick Start

```bash
git clone https://github.com/lusher00/hailo-tracker
cd hailo-tracker
./install.sh
```

Open `http://<pi-ip>:8080` in a browser.

## Configuration

Open `hailo_tracker.py` and edit the CONFIG block near the top:

```python
# Track everything (default)
TRACKED_CLASSES = []

# Or pick specific classes from the 80-class COCO list
TRACKED_CLASSES = ["cat", "dog", "person"]

# Confidence threshold (0.0 – 1.0)
CONF_THRESH = 0.40

# Camera rotation
ROTATE = cv2.ROTATE_90_CLOCKWISE   # or None to disable
```

After editing, restart the service:

```bash
sudo systemctl restart hailo-tracker
```

## COCO Classes

The full 80-class list is in `hailo_tracker.py`. A few useful ones:

| Class | ID | Class | ID |
|-------|----|-------|----|
| person | 0 | cat | 15 |
| bicycle | 1 | dog | 16 |
| car | 2 | horse | 17 |
| bird | 14 | bottle | 39 |

## Installation Details

### Prerequisites

The Hailo AI Kit needs the PCIe driver and HailoRT runtime installed. The easiest path on Pi 5 is the official Hailo install script:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install hailo-all -y
sudo reboot
```

Verify the kit is detected after reboot:

```bash
hailortcli fw-control identify
```

You should see `Hailo-8L` in the output. If not, check that the M.2 HAT is seated and the PCIe cable is connected.

### Python Dependencies

```bash
sudo apt install -y python3-opencv python3-flask python3-numpy
```

The `hailo_platform` Python package is installed as part of `hailo-all` above.

### Model File

The YOLOv8s `.hef` file compiled for Hailo-8L is not included in this repo (it's ~30MB). Download it from the Hailo model zoo:

```bash
pip install huggingface_hub --break-system-packages
python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    repo_id='hailo/Model-Zoo',
    filename='hailo8l/yolov8s.hef',
    local_dir='.'
)
"
```

Or find it in `/usr/share/hailo-models/` if `hailo-all` installed it.

### Service Install

```bash
./install.sh
```

This sets up a `systemd` service that starts automatically on boot.

**Useful commands:**

```bash
sudo systemctl status hailo-tracker
sudo journalctl -u hailo-tracker -f    # live logs
sudo systemctl restart hailo-tracker
sudo systemctl stop hailo-tracker
```

To uninstall:

```bash
./uninstall.sh
```

## Architecture

```
Camera (IMX708)
    ↓ rpicam-vid MJPEG
Python JPEG decoder
    ↓ NumPy array
Letterbox → 640×640
    ↓
Hailo-8L NPU (YOLOv8s)
    ↓ [80][N, 5] detections
Class filter + OpenCV annotation
    ↓ annotated JPEG
Flask MJPEG stream → Browser
```

## Performance

- ~30ms inference per frame on Hailo-8L
- ~50–100ms end-to-end latency (camera to browser)
- ~3W NPU power draw

## Troubleshooting

**`/dev/hailo0` permission denied**
```bash
sudo rmmod hailo_pci && sudo modprobe hailo_pci
```
Or run `./install.sh` which sets up a udev rule to fix this permanently.

**Camera not found**
```bash
rpicam-vid --list-cameras
```

**Port 8080 in use** — change `HTTP_PORT` in `hailo_tracker.py`.

**`hailo_platform` not found** — make sure you're using the system Python 3 (`/usr/bin/python3`), not a venv. The Hailo package installs into the system site-packages.

## License

MIT
