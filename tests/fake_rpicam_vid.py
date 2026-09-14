#!/usr/bin/env python3
"""Stand-in for rpicam-vid: emits an MJPEG stream of synthetic frames on stdout.

Camera 0 is the original scene: a red block ("cat") following a slow path and a
blue block ("person") sitting still, so the tracker has both a moving and a
static object to hold IDs on. Deliberately non-square (1280x720) so letterbox
padding is exercised.

Camera 1 is a *different* scene — one red block on its own path, no person — so
a frame from cam1 is distinguishable from a frame from cam0 both visually and
in what the fake NPU reports.

Also answers --list-cameras the way libcamera does, so camera enumeration is
under test rather than stubbed out.
"""

import sys
import time
import math
import argparse

import cv2
import numpy as np

p = argparse.ArgumentParser()
for flag in ("--codec", "--width", "--height", "--framerate", "--timeout",
             "--output", "--denoise", "--autofocus-mode", "--ev", "--shutter",
             "--gain", "--lens-position", "--camera"):
    p.add_argument(flag)
for flag in ("--inline", "--nopreview", "--flush", "--hflip", "--vflip",
             "--list-cameras"):
    p.add_argument(flag, action="store_true")
args, _unknown = p.parse_known_args()

# Two sensors: an IMX708 (has autofocus) on port 0, an IMX477 (does not) on 1.
SENSORS = ["imx708", "imx477"]

if args.list_cameras:
    print("Available cameras")
    print("-----------------")
    for i, sensor in enumerate(SENSORS):
        print(f"{i} : {sensor} [4608x2592 10-bit RGGB] "
              f"(/base/axi/pcie@120000/rp1/i2c@88000/{sensor}@1a)")
        # Real rpicam-hello lists the selectable sensor modes under each
        # camera; the tracker parses these to offer resolutions that exist.
        print("    Modes: 'SBGGR10_CSI2P' : 640x480 [60.00 fps - (0, 0)/0x0 crop]")
        print("                            1280x720 [30.00 fps - (0, 0)/0x0 crop]")
        print("                            2304x1296 [30.00 fps - (0, 0)/0x0 crop]")
    sys.exit(0)

CAM = int(args.camera or 0)
W = int(args.width or 1280)
H = int(args.height or 720)
FPS = int(args.framerate or 30)

out = sys.stdout.buffer
t0 = time.time()
i = 0

while True:
    frame = np.full((H, W, 3), 30, dtype=np.uint8)
    cv2.putText(frame, f"SYNTHETIC CAM{CAM}", (W // 2 - 250, H - 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (70, 70, 70), 3)

    if CAM == 0:
        # Moving "cat" — red block
        phase = i / 60.0
        cx = int(W * (0.5 + 0.30 * math.sin(phase)))
        cy = int(H * (0.5 + 0.24 * math.cos(phase * 0.8)))
        cv2.rectangle(frame, (cx - 90, cy - 60), (cx + 90, cy + 60), (0, 0, 220), -1)

        # Stationary "person" — blue block, upper left
        cv2.rectangle(frame, (120, 90), (260, 400), (220, 0, 0), -1)
    else:
        # A second scene entirely: one "cat", no "person", so tracks coming
        # from this camera are unmistakable.
        phase = i / 45.0
        cx = int(W * (0.5 + 0.22 * math.cos(phase)))
        cy = int(H * (0.45 + 0.18 * math.sin(phase * 1.3)))
        cv2.rectangle(frame, (cx - 70, cy - 70), (cx + 70, cy + 70), (0, 0, 220), -1)

    ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if ok:
        out.write(jpg.tobytes())
        out.flush()

    i += 1
    delay = t0 + i / FPS - time.time()
    if delay > 0:
        time.sleep(delay)
