# Hailo Tracker

Real-time object detection and tracking on a Raspberry Pi 5 with a Hailo-8L NPU. Streams annotated video from one or both CSI cameras to any browser over HTTP.

Labels every object it sees — all 80 COCO classes, each with its own colour, a confidence score, and a track ID that stays with the object as it moves. Narrow it to just cats, or just people, from the web UI without restarting anything.

![architecture](doc/pipeline.svg)

## Hardware

- Raspberry Pi 5 (4GB+)
- Hailo-8L AI Kit — M.2 HAT+ or AI HAT+ (13 TOPS)
- One or two Raspberry Pi camera modules, one per CSI port (tested with an IMX708 / Camera Module 3 on port 0 and an IMX477 / HQ Camera on port 1)

## Features

**Detection**

- ~30 FPS real-time inference on the Hailo-8L
- All 80 COCO classes, per-class colours, cats in green
- Persistent track IDs — "cat #3 was here for 90 seconds", not 2,700 unrelated frames
- Motion trails, confidence thresholding, minimum box size filter
- Region of interest — ignore everything outside a polygon you define

**Cameras**

- Both CSI ports at once — each camera gets its own capture thread, tracker, stats and stream
- Cameras are auto-detected at startup; sensor-specific quirks handled (no autofocus flags sent to an IMX477)
- Detection is per camera and switchable live, so a second camera can be plain video until you want the NPU on it
- Per-camera capture settings — resolution, framerate, rotation and exposure are independent
- Resolution, exposure, white balance and image controls changed live from the browser, one camera at a time
- Software effects — grayscale, invert, edge detect, heat map, sharpen — applied after inference, so what the model sees is never altered
- A camera that stops delivering frames says so on its own panel instead of disappearing

**Interface**

- Live MJPEG stream, no app required
- Control panel: confidence slider, class picker, display toggles — all applied live
- One encode shared by every connected client, so a second browser tab costs nothing
- `/snapshot`, `/stats`, `/tracks`, `/events`, `/metrics`, `/healthz`

**Recording**

- SQLite event log — one row per visit, with duration and peak confidence
- Auto-snapshot on new detection, with retention limits
- Webhook POST on detection, for Home Assistant / Node-RED / whatever
- CSV export

**Operations**

- Self-healing capture loop — a camera hiccup restarts the pipeline with backoff instead of freezing the stream
- Configuration by env file, CLI flag, or the web UI; no need to edit Python
- Prometheus metrics endpoint
- `systemd` service with clean SIGTERM shutdown
- Off-device test harness so you can check changes on a laptop

## Quick Start

```bash
git clone https://github.com/lusher00/hailo-tracker
cd hailo-tracker
./download_model.sh      # fetches yolov8s.hef (~23MB)
./install.sh
```

Open `http://<pi-ip>:8080`.

If HailoRT isn't installed yet, work through [SETUP.md](SETUP.md) first — about 90 minutes on a fresh system, mostly compile time.

## Running by hand

```bash
python3 hailo_tracker.py                          # every camera found, all classes
python3 hailo_tracker.py --classes cat,dog        # pets only
python3 hailo_tracker.py --conf 0.6 --rotate 90
python3 hailo_tracker.py --cameras 0,1            # force both CSI ports
python3 hailo_tracker.py --detect-cameras 0,1     # run the NPU on both
python3 hailo_tracker.py --cameras 0              # single camera, ignore port 1
python3 hailo_tracker.py --snapshots --webhook http://ha.local/api/webhook/cat
python3 hailo_tracker.py --list-cameras
python3 hailo_tracker.py --list-classes
```

Full flag list:

| Flag | Effect |
|------|--------|
| `--port N` | HTTP port |
| `--cameras 0,1` | Which CSI cameras to run (`auto` by default) |
| `--detect-cameras 0,1` | Which cameras run the NPU (camera 0 only by default) |
| `--classes a,b` | Class filter |
| `--conf 0.6` | Confidence threshold |
| `--model path.hef` | Use a specific model |
| `--rotate 0\|90\|180\|270` | Camera rotation |
| `--width` / `--height` / `--fps` | Camera capture settings |
| `--no-track` | Per-frame detection only, no IDs |
| `--no-events` | Don't write to the event log |
| `--snapshots` | Save a JPEG per new detection |
| `--webhook URL` | POST detections to URL |
| `--list-classes` | Print all 80 classes with IDs and exit |
| `--list-cameras` | Print what libcamera can see and exit |

`--width`, `--height`, `--fps` and `--rotate` apply to camera 0. Use the `CAM1_*` variables below for the second camera.

## Configuration

Three ways in, highest priority first: **CLI flag → environment variable → default in `hailo_tracker.py`**.

`install.sh` writes `hailo-tracker.env` next to the script and points the systemd unit at it. Edit that file for anything persistent:

```bash
nano hailo-tracker.env
sudo systemctl restart hailo-tracker
```

```bash
TRACKED_CLASSES=cat,dog,person   # empty = all 80 classes
CONF_THRESH=0.40

CAMERAS=auto                     # auto | 0 | 0,1

CAM_WIDTH=1280
CAM_HEIGHT=720
CAM_FRAMERATE=30
CAM_AUTOFOCUS=continuous
CAM_SHUTTER=20000                # microseconds; blank = auto exposure
CAM_GAIN=2
CAM_ROTATE=0                     # 0, 90, 180, 270

CAM_BRIGHTNESS=                  # -1.0..1.0   blank = leave it to the ISP
CAM_CONTRAST=                    # 1.0 is normal
CAM_SATURATION=                  # 0.0 is mono, 1.0 is normal
CAM_SHARPNESS=                   # 1.0 is normal
CAM_AWB=                         # auto|incandescent|tungsten|fluorescent|indoor|daylight|cloudy
CAM_EFFECT=none                  # none|gray|invert|edges|heat|sharpen

CAM1_FRAMERATE=15                # camera 1 inherits camera 0 unless told otherwise
CAM1_ROTATE=180
CAM1_DETECT=true                 # run the NPU on camera 1 as well

SNAPSHOT_ON_DETECT=true
WEBHOOK_URL=http://homeassistant.local:8123/api/webhook/hailo
```

The file installed by `install.sh` lists every available setting with comments. Confidence, the class filter, the display toggles and every per-camera setting can also be changed live from the web UI — those changes apply instantly but reset to the file's values on restart. This file is the only thing that survives one.

### Multiple cameras

Plug a second module into the other CSI port and it is picked up on the next restart — `CAMERAS` defaults to `auto`, which asks libcamera what is present. `--list-cameras` shows what it found:

```
$ python3 hailo_tracker.py --list-cameras
  0  imx708
  1  imx477
```

Camera 0 reads the unprefixed variables (`CAM_WIDTH`, `CAM_FRAMERATE`, …). Every other camera reads a `CAMN_` prefix and **falls back to whatever camera 0 resolved to**, so you only set what differs:

```bash
CAM_FRAMERATE=30                 # both cameras, unless overridden
CAM1_FRAMERATE=15                # ...except camera 1
CAM1_ROTATE=180                  # mounted upside down
```

Every per-camera setting works this way: `CAM1_WIDTH`, `CAM1_HEIGHT`, `CAM1_FRAMERATE`, `CAM1_ROTATE`, `CAM1_SHUTTER`, `CAM1_GAIN`, `CAM1_EV`, `CAM1_DENOISE`, `CAM1_AUTOFOCUS`, `CAM1_LENS_POSITION`, `CAM1_HFLIP`, `CAM1_VFLIP`, `CAM1_EXTRA_ARGS`, `CAM1_INFER_EVERY_N`, `CAM1_DETECT`, `CAM1_BRIGHTNESS`, `CAM1_CONTRAST`, `CAM1_SATURATION`, `CAM1_SHARPNESS`, `CAM1_AWB`, `CAM1_EFFECT`.

**Detection is opt-in per camera.** Camera 0 runs the NPU; any additional camera comes up as plain video, because two cameras sharing one accelerator halve the inference rate each one gets. Turn it on from the Cameras panel in the web UI, or pin it:

```bash
CAM1_DETECT=true
```

```bash
curl -X POST http://<pi-ip>:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"cameras": {"1": {"detect": true}}}'
```

The class filter, confidence threshold and display toggles are global — one NPU, one set of rules. What is per camera: capture settings, track IDs, stats, and whether inference runs at all. Track IDs restart at 1 for each camera, so an event is identified by the `(camera, track_id)` pair; the event log, the CSV export and the webhook payload all carry the camera index.

If a sensor has no focus actuator (IMX477, IMX219, OV5647) the autofocus flags are dropped automatically rather than handed to `rpicam-vid`, which would otherwise refuse to start and show up as a camera that restarts forever.

### Live camera controls

Each camera has its own block in the **Cameras** panel of the web UI. Expand it for resolution, framerate, rotation, flips, shutter, gain, EV, brightness, contrast, saturation, sharpness and white balance.

The **Resolution** dropdown lists the modes that sensor actually has, read from `rpicam-hello --list-cameras` at startup. Anything else is marked *(scaled)*, because the ISP has to crop and resize into it — which matters on a 4:3 sensor like the IMX477, whose smallest native mode is 1332x990 and which has no 720p mode at all.

Two different things happen when you change something:

| Setting | Effect |
|---------|--------|
| Resolution, framerate, rotation, flips, exposure, image controls, white balance | Staged until you press **Apply**, then that camera's `rpicam-vid` is relaunched — about a second of black. The other camera keeps streaming. |
| Effect, and the detect checkbox | Applied on the very next frame. No restart. |

`rpicam-vid` reads its settings once at launch and offers no runtime control channel, which is why the first group needs a relaunch. An intentional relaunch is deliberately *not* counted as a capture error and skips the restart backoff, so `capture_errors` in `/stats` still means real camera trouble.

**Effects run after inference**, on the finished frame in the render loop. Detection always sees clean video — running YOLO on an inverted or edge-detected image would wreck it. They cost CPU per frame per camera, and `edges` and `heat` are the expensive ones at 720p30.

Everything here is also settable from the env file (`CAM_EFFECT`, `CAM_AWB`, …) and over HTTP:

```bash
curl -X POST http://<pi-ip>:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"cameras": {"0": {"width": 1332, "height": 990, "effect": "heat"}}}'
```

### Camera notes

The defaults come from a build that was pointed at an actual moving cat:

- **1280x720 rather than 1080p.** The network downsamples to 640x640 regardless, so extra pixels only cost JPEG decode and encode time. Raise it if you want a prettier stream and have CPU to spare.
- **`CAM_SHUTTER=20000`** (1/50s) with **`CAM_GAIN=2`** pins exposure. Auto-exposure hunts when a subject crosses the frame, and the resulting motion blur costs real detections.
- **`CAM_AUTOFOCUS=continuous`.** If you set `manual`, you must also set `CAM_LENS_POSITION` (dioptres — `0` is infinity, `2.0` is roughly 50cm). Manual AF with no lens position leaves the lens wherever it was parked, which usually means everything is soft.
- **`CAM_DENOISE=off`** — it adds latency and the network doesn't care.

Outdoors, where light varies a lot, leave `CAM_SHUTTER` and `CAM_GAIN` blank and let the ISP handle it.

### Tracking

The tracker associates detections across frames by IoU overlap, with constant-velocity prediction to ride out brief occlusions.

| Setting | Default | Meaning |
|---------|---------|---------|
| `TRACK_MIN_HITS` | 3 | Frames an object must appear before it counts. Filters the single-frame false positives YOLO throws on textured backgrounds — rugs and blankets love to be "cat". |
| `TRACK_MAX_MISSES` | 15 | Frames to coast a lost object before dropping it. At 30fps that's half a second, enough to survive a chair leg. |
| `TRACK_IOU` | 0.30 | Overlap needed to call two boxes the same object. Lower it for fast movers. |

Set `TRACK_ENABLED=false` (or `--no-track`) for plain per-frame detection with no IDs.

### Region of interest

Ignore everything outside a polygon — a food bowl, a doorway, your own garden but not the pavement:

```bash
ROI_POLYGON=[[0.05,0.4],[0.6,0.35],[0.65,0.95],[0.1,0.95]]
SHOW_ROI=true
```

Coordinates are normalised 0–1, so they survive a resolution change. A detection counts only if its centre falls inside.

## Endpoints

| Path | Returns |
|------|---------|
| `/` | Web UI — every camera plus the control panel |
| `/video` | MJPEG stream from camera 0. Drop into Home Assistant, VLC, or an `<img>` tag |
| `/video/<n>` | MJPEG stream from camera `n` |
| `/snapshot` | Latest annotated frame from camera 0, single JPEG |
| `/snapshot/<n>` | Latest annotated frame from camera `n` |
| `/cameras` | Every per-camera setting, plus the sensor's available modes and the valid effect and white-balance values |
| `/tracks` | What's in frame right now across every camera, with IDs and boxes |
| `/tracks/<n>` | The same for camera `n` only |
| `/stats` | Totals for the whole rig, plus a `cameras` array with per-camera detail |
| `/stats/<n>` | Just camera `n` |
| `/events` | Recent detection events. `?limit=`, `?class=`, `?since=`, `?camera=` |
| `/events/summary` | Per-class visit counts and total time in frame. `?camera=` |
| `/events.csv` | Full event log as CSV. `?camera=` |
| `/snapshots` | Saved snapshot filenames |
| `/snapshots/<name>` | A specific saved snapshot |
| `/metrics` | Prometheus exposition format — every series carries a `camera` label |
| `/healthz` | 200 if a frame arrived in the last 10s, else 503 |
| `/api/config` | GET current settings; POST to change them live, globally or per camera |
| `/api/snapshot` | POST to save the current frame to disk on demand |

```bash
curl -s http://<pi-ip>:8080/stats | python3 -m json.tool
curl -s http://<pi-ip>:8080/events/summary | python3 -m json.tool
curl -o cat.jpg http://<pi-ip>:8080/snapshot

# Switch to cats-only without restarting
curl -X POST http://<pi-ip>:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"tracked_classes": ["cat"], "conf_thresh": 0.5}'

# Start running the NPU on the second camera
curl -X POST http://<pi-ip>:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"cameras": {"1": {"detect": true}}}'

# Re-resolution camera 0 and give it a false-colour view
curl -X POST http://<pi-ip>:8080/api/config \
     -H 'Content-Type: application/json' \
     -d '{"cameras": {"0": {"width": 1332, "height": 990, "effect": "heat"}}}'

# What modes does each sensor actually have?
curl -s http://<pi-ip>:8080/cameras | python3 -m json.tool
```

### Webhook payload

```json
{
  "event": "detection",
  "timestamp": 1754238401.22,
  "iso": "2026-08-03T16:26:41",
  "camera": 1,
  "track": {
    "id": 7, "class": "cat", "class_id": 15,
    "box": [822, 440, 1000, 558],
    "conf": 0.93, "max_conf": 0.94,
    "hits": 12, "duration_s": 0.4, "confirmed": true
  }
}
```

Fired once when a track is confirmed, not per frame. Delivery is fire-and-forget on a background thread — a dead endpoint can't stall the video.

## Event log

One row per track, not per frame:

```bash
sqlite3 events.db \
  "SELECT camera, class, COUNT(*), ROUND(SUM(duration_s)/60,1) AS minutes
   FROM events GROUP BY camera, class ORDER BY 3 DESC;"
```

```
cat|47|182.4
person|12|31.7
dog|3|4.2
```

Rows older than `EVENT_RETENTION_DAYS` (default 30) are pruned hourly.

## COCO Classes

Full list via `python3 hailo_tracker.py --list-classes`. Common ones:

| Class | ID | Class | ID |
|-------|----|-------|----|
| person | 0 | cat | 15 |
| bicycle | 1 | dog | 16 |
| car | 2 | horse | 17 |
| bird | 14 | bottle | 39 |

## Architecture

One `CameraPipeline` per camera, all the way from `rpicam-vid` to the published JPEG. The NPU is the only shared stage:

```
cam0 (IMX708)                      cam1 (IMX477)
    ↓ rpicam-vid --camera 0            ↓ rpicam-vid --camera 1
capture thread                     capture thread
JPEG demux → decode → rotate       JPEG demux → decode → rotate
    ↓ letterbox 640×640                ↓ letterbox 640×640
    └──────────────┬────────────────────┘
                   ↓
        Hailo-8L NPU (YOLOv8s)      ← one lock, requests serialised
                   ↓ [80][N, 5] detections
    ┌──────────────┴────────────────────┐
    ↓                                   ↓
parse → ROI → IoU tracker           parse → ROI → IoU tracker
render thread → annotate → encode   render thread → annotate → encode
    ↓                                   ↓
FramePublisher ──┬── browser 1      FramePublisher ── browser
                 ├── browser 2
                 └── Home Assistant
```

Each camera has its own capture thread, render thread, depth-2 queue, tracker, stats and publisher, so a hiccup on one camera — a restart, a slow encode, a stalled client — cannot drop frames on the other. What they share is the NPU, the event log and the detection settings.

Capture and render are separate threads with a depth-2 queue between them, so a slow client or a busy encode can't back-pressure the NPU. The render thread encodes each frame exactly once no matter how many clients are watching.

**Latency is bounded, throughput is not.** `rpicam-vid` never stops producing. If decode plus inference can't keep up with the camera — even briefly — the capture loop discards every buffered frame except the newest before decoding. Falling behind therefore costs you frames, never delay. The alternative (processing every frame in order) means a backlog that grows without limit: at 30fps captured and 20fps processed you accumulate ten frames a second, and after two minutes you're watching twenty-second-old video.

`dropped` in `/stats` counts these. A steady non-zero number is normal and healthy; it means the camera is outrunning the NPU and you're seeing live video rather than a queue. Set `CAM_FRAMERATE` to whatever you actually sustain if you'd rather not waste the encode.

## Files

| File | Purpose |
|------|---------|
| `hailo_tracker.py` | Entry point — config, camera, NPU, Flask |
| `tracker.py` | IoU tracker with velocity prediction |
| `events.py` | SQLite event log, snapshot store, webhooks |
| `webui.py` | The browser UI (single self-contained page) |
| `install.sh` / `uninstall.sh` | systemd service, udev rule, env file |
| `download_model.sh` | Fetches the `.hef` |
| `hailo-tracker.env` | Your settings — created by `install.sh`, gitignored |
| `events.db` | SQLite event log — created on first run, gitignored |
| `snapshots/` | Saved detection frames, if enabled |
| `tests/` | Off-device test harness |
| `doc/` | Diagrams |

## Testing

The harness fakes the camera and the NPU and runs everything else for real, so you can check changes on a laptop before deploying:

```bash
pip install flask opencv-python numpy
./tests/run_tests.sh
```

It starts **two** fake cameras — the stand-in answers `--list-cameras` with an IMX708 on port 0 and an IMX477 on port 1, each advertising sensor modes — so camera enumeration, mode parsing, per-camera config, live reconfiguration and switching detection on and off at runtime are all covered. It also seeds a pre-multi-camera `events.db` so the schema migration is exercised, and checks that changing a camera's resolution restarts only that camera and is not counted as a capture error. It verifies the letterbox round trip, track ID stability, live reconfiguration, multi-client streaming, the event log, and clean shutdown. It writes `tests/output_sample.jpg` so you can eyeball the annotation.

## Service Management

```bash
sudo systemctl status hailo-tracker
sudo journalctl -u hailo-tracker -f    # live logs, including detection events
sudo systemctl restart hailo-tracker
./uninstall.sh
```

## Troubleshooting

**`HAILO_OUT_OF_PHYSICAL_DEVICES` (error 74)** — "not enough free devices, requested: 1, found: 0". The count is of *free* devices, not present ones, so this means one of two different things. Check which:

```bash
ls -l /dev/hailo0
```

*No such file* — the driver isn't loaded. Usually a kernel update outran the out-of-tree module; see the next entry.

*It exists* — something already has it open. Only one process can hold the NPU:

```bash
sudo fuser -v /dev/hailo0
systemctl is-active hailo-cat-tracker    # an older service of your own?
```

A crashed instance can keep the handle. Kill the PID `fuser` reports and restart. If you have a second Hailo project installed as a service, disable it — both will start at boot and whichever wins locks out the other, which presents as an intermittent failure.

Note that `sudo hailortcli fw-control identify` succeeding does **not** rule this out. It opens the device only briefly, so it works even when a long-lived process holds it.

**Driver missing after a kernel update** — the most common Ubuntu failure. The Hailo PCIe driver is out-of-tree, so a new kernel arrives without it:

```bash
uname -r
find /lib/modules -name 'hailo_pci*'
```

If those disagree, rebuild for the running kernel:

```bash
sudo apt install -y linux-headers-$(uname -r)
sudo dkms autoinstall -k $(uname -r)
sudo modprobe hailo_pci
```

`dkms status` should list `hailo_pci` for your current kernel. If it doesn't, the driver isn't registered with DKMS and this will break again on the next `apt upgrade` — reinstall it from `hailort-drivers/linux/pcie` so DKMS picks it up.

**`/dev/hailo0` permission denied**

```bash
sudo rmmod hailo_pci && sudo modprobe hailo_pci
```

`./install.sh` writes a udev rule that fixes this permanently. If the node is recreated by something else (a DKMS rebuild, for instance) the rule may not fire:

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/hailo0      # want crw-rw-rw-
```

**Video is minutes behind reality** — you're on a build from before the stale-frame fix. The capture loop now discards all but the newest buffered frame. Confirm `dropped` in `/stats` is climbing while `fps` stays steady; that's correct behaviour. See [Architecture](#architecture).

**`hailo_platform` not found** — use the system Python (`/usr/bin/python3`), not a venv. If you built HailoRT from source the bindings land in `/usr/lib/aarch64-linux-gnu/python3.*/site-packages`; the script adds that path automatically.

**Camera not found**

```bash
rpicam-vid --list-cameras
python3 hailo_tracker.py --list-cameras
```

**Second camera never appears** — if `--list-cameras` shows only one, the Pi isn't seeing the module: check the ribbon seating and orientation, and that no `dtoverlay` in `/boot/firmware/config.txt` is pinning a single sensor. `camera_auto_detect=0` with a single explicit `dtoverlay=imx708` will hide the second port. If `--list-cameras` shows both but only one streams, force it with `CAMERAS=0,1` and read the log for that camera's `rpicam-vid` line and exit code.

**One camera restarts in a loop** — read the `[cam1] error:` lines. `rpicam-vid exited 1` immediately after start is almost always an unsupported flag for that sensor. Autofocus is handled automatically for the sensors listed above; for anything else, set `CAM1_AUTOFOCUS=` (empty) to drop it.

**A camera panel says NO SIGNAL** — that camera is configured and `rpicam-vid` is running, but it has never delivered a frame. The tracker says so after 12 seconds and dumps the last 20 lines `rpicam-vid` wrote to stderr:

```bash
journalctl -u hailo-tracker -b | grep -A22 '\[cam0\]'
```

**`Camera frontend has timed out!` / `Dequeue timer ... has expired`** — the sensor is answering on I²C (it enumerated, its modes were read, the stream was configured) but no pixels are arriving on the CSI-2 lanes. Those are two different physical paths, and this failure means the slow one works and the fast one doesn't.

Reach for a **full power-down before anything else**:

```bash
sudo poweroff
```

then pull the power for 30 seconds. A warm `reboot` never drops the 3.3V rail on the camera connector, so a sensor or CSI PHY latched in a bad state survives every reboot you throw at it — and a camera that fails this way will keep failing across any number of restarts while looking, in software, exactly like a cable fault.

If a cold boot brings it back and it lapses again later, that is a marginal connection rather than a dead one: reseat both ends of the ribbon, and swap the good camera's cable onto the bad one to tell cable from module. No resolution, mode or framerate setting will help — nothing above the physical layer can conjure data that isn't on the wire. Changing the request only *looks* like it helps, because the fault is intermittent.

**No `.hef`** — run `./download_model.sh`, or set `HEF_PATH`.

**Boxes in the wrong place** — the model's input size doesn't match `NN_SIZE`. The startup log warns when it can detect this.

**Boxes mirrored across the diagonal** — your model emits `[y1,x1,y2,x2]` rather than `[x1,y1,x2,y2]`. Set `BOX_ORDER=yxyx`.

**Everything's blurry** — see the camera notes; `CAM_AUTOFOCUS=manual` without `CAM_LENS_POSITION` is the usual cause.

**Boxes flicker on and off** — raise `CONF_THRESH`, or raise `TRACK_MAX_MISSES` so the tracker coasts longer.

**False positives on rugs and blankets** — raise `TRACK_MIN_HITS` to 5, and/or `MIN_BOX_AREA_FRAC`.

**Stream stutters with several viewers** — set `STREAM_MAX_FPS=15`, or lower `JPEG_QUALITY`.

**Port 8080 in use** — set `HTTP_PORT`.

More detail in [INSTALL.md](INSTALL.md) and [SETUP.md](SETUP.md).

## Performance

- ~30ms inference per frame on Hailo-8L — this is the floor for yolov8s
- 20–30 FPS end to end at 720p, depending on scene complexity and JPEG size
- ~50–100ms latency, camera to browser
- ~3W NPU power draw
- Tracker overhead is well under 1ms for typical object counts

Where the time goes, in rough order: NPU inference (~30ms, fixed), JPEG decode on the CPU (scales with capture resolution), annotation (scales with object count), JPEG encode (scales with `JPEG_QUALITY`). `/stats` reports `inference_ms` and `encode_ms` separately; whatever's left over between them and your frame time is decode.

To buy frames back: lower `CAM_WIDTH`/`CAM_HEIGHT` first (decode dominates above 720p), then `JPEG_QUALITY`, then `INFER_EVERY_N` as a last resort — the tracker coasts between inferences, so 2 or 3 is usually invisible for slow-moving subjects.

**With detection on two cameras** the ~30ms of inference is spent alternately, so each camera sees roughly half the inference rate while both keep streaming video at full framerate. If that isn't enough, `CAM1_INFER_EVERY_N=2` gives camera 0 the larger share rather than splitting evenly.

## Roadmap

- [ ] Line-crossing and dwell-time rules
- [ ] Per-class confidence thresholds
- [x] Multi-camera support
- [ ] Send detection position to a robot controller (BeagleBone Blue)

## License

Copyright (c) 2025 Ryan Lush. Free for personal, educational, and open-source use. Commercial use requires written permission — ryan.lush@gmail.com

## Acknowledgments

Hailo for the accelerator and HailoRT SDK, Ultralytics for YOLOv8, and the Raspberry Pi Foundation.
