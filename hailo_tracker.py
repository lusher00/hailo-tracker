#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
hailo_tracker.py — Real-time object detection and tracking on Raspberry Pi 5
with a Hailo-8/8L NPU. Streams annotated MJPEG over HTTP.

Labels every detected object by default (all 80 COCO classes, each with its own
colour and a stable track ID). Narrow it down live from the web UI, or pin a
class list in the config.

Pipeline — one of these per camera, sharing the one NPU:

    rpicam-vid ──> capture thread ──> NPU ──> tracker ──> render thread ──┐
       --camera N                      │                                  │
                                       │      all browser clients read ────┘
                    serialised across  │      the same published frame
                    cameras by a lock ─┘      (no per-client re-encode)

Every CONFIG constant can be overridden by an environment variable of the same
name, or a --flag on the command line. Precedence: CLI > env > file default.
"""

import os
import re
import sys
import select
import csv
import io
import json
import time
import queue
import signal
import socket
import argparse
import threading
import subprocess
from collections import deque, defaultdict

# HailoRT built from source drops its Python bindings outside the default path
# on some distros (notably Ubuntu on Pi). Harmless when hailo_platform came
# from `apt install hailo-all`.
for _p in (
    "/usr/lib/aarch64-linux-gnu/python3.12/site-packages",
    "/usr/lib/aarch64-linux-gnu/python3.11/site-packages",
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, send_from_directory

from tracker import Tracker
from events import EventLog, SnapshotStore, Webhook
import webui

HERE = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# CONFIG
# ============================================================
def _env(name, default, cast=str):
    raw = os.environ.get(name)
    if raw is None:
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log(f"[WARN] Bad value for {name}={raw!r} — using default {default!r}")
        return default


_print_lock = threading.Lock()


def log(msg):
    """print() from a thread is two writes — the text, then the newline — so
    with a capture and a render thread per camera all logging at once, lines
    end up spliced into each other. Serialise them."""
    with _print_lock:
        print(msg, flush=True)


# ---------- server ----------
HTTP_PORT = _env("HTTP_PORT", 8080, int)
HTTP_HOST = _env("HTTP_HOST", "0.0.0.0")

# ---------- model ----------
NN_SIZE = _env("NN_SIZE", 640, int)
HEF_CANDIDATES = [
    _env("HEF_PATH", ""),
    os.path.join(HERE, "yolov8s.hef"),
    "/usr/share/hailo-models/yolov8s_h8l.hef",
    "/usr/share/hailo-models/yolov8s.hef",
]

# Hailo's on-chip NMS post-process (the *_nms_postprocess output of the model-zoo
# YOLO HEFs) emits each box as [y_min, x_min, y_max, x_max, score], normalised
# 0..1. Reading it as x-first swaps the axes: confident detections drawn in the
# wrong place. Override with BOX_ORDER=xyxy only for a model known to differ.
BOX_ORDER = _env("BOX_ORDER", "yxyx")

# ---------- detection ----------
CONF_THRESH = _env("CONF_THRESH", 0.40, float)
TRACKED_CLASSES = [c.strip().lower() for c in _env("TRACKED_CLASSES", "").split(",") if c.strip()]

# Run the NPU on every Nth frame; the tracker coasts through the gaps. Leave at 1
# unless you're running a heavier model and would rather have smooth video than
# a detection on every single frame.
INFER_EVERY_N = max(1, _env("INFER_EVERY_N", 1, int))

# Ignore boxes smaller than this fraction of the frame — kills the speckle of
# tiny false positives on textured backgrounds.
MIN_BOX_AREA_FRAC = _env("MIN_BOX_AREA_FRAC", 0.0005, float)

# ---------- tracking ----------
TRACK_ENABLED    = _env("TRACK_ENABLED", True, bool)
TRACK_IOU        = _env("TRACK_IOU", 0.30, float)
TRACK_MAX_MISSES = _env("TRACK_MAX_MISSES", 15, int)
TRACK_MIN_HITS   = _env("TRACK_MIN_HITS", 3, int)
TRAIL_LENGTH     = _env("TRAIL_LENGTH", 48, int)

# ---------- cameras ----------
# Every camera on the CSI bus gets its own capture thread, tracker, stats and
# MJPEG publisher — a second camera is another CameraPipeline instance, not a
# second copy of the pipeline code. The NPU is the one shared resource, and
# run_inference() serialises access to it.
#
# Which cameras to run:
#     CAMERAS=auto        ask libcamera what is plugged in (default)
#     CAMERAS=0           force a single camera
#     CAMERAS=0,1         force both CSI ports
#
# Camera 0 reads the historical unprefixed names, so existing .env files and
# systemd units keep working untouched. Camera N reads CAMN_* and falls back to
# whatever camera 0 resolved to:
#
#     CAM_WIDTH=1280               CAM1_WIDTH=1280
#     CAM_FRAMERATE=30             CAM1_FRAMERATE=15
#     CAM_ROTATE=0                 CAM1_ROTATE=180
#                                  CAM1_DETECT=1     run the NPU on camera 1 too
#
# Defaults are carried over from the cat-tracker build, which was aimed at a
# moving animal indoors. 720p keeps latency down (the net downsamples to 640
# anyway), and a pinned shutter/gain stops auto-exposure hunting from smearing
# motion.
CAMERAS_SPEC = _env("CAMERAS", "auto")

# key -> (default for camera 0, cast)
_CAM_DEFAULTS = {
    "width":         (1280, int),
    "height":        (720, int),
    "framerate":     (30, int),
    "autofocus":     ("continuous", str),   # continuous|manual|auto|"" for none
    "lens_position": ("", str),             # dioptres, manual AF only
    "shutter":       ("20000", str),        # µs; "" = auto
    "gain":          ("2", str),            # "" = auto
    "ev":            ("0", str),
    "denoise":       ("off", str),
    # Camera 0 is mounted inverted on this build, so both flips default on --
    # 180 degrees, done by the ISP via rpicam-vid rather than by cv2.rotate on
    # every frame. Override per install with CAM_HFLIP/CAM_VFLIP if a given
    # machine has it the right way up.
    "hflip":         (True, bool),
    "vflip":         (True, bool),
    # Image controls. Blank means "don't pass the flag", which leaves the ISP
    # on its own default — not the same as passing a neutral value.
    "brightness":    ("", str),             # -1.0 .. 1.0
    "contrast":      ("", str),             # 0.0 .. ~2.0, 1.0 is normal
    "saturation":    ("", str),             # 0.0 = mono, 1.0 is normal
    "sharpness":     ("", str),             # 0.0 = none, 1.0 is normal
    "awb":           ("", str),             # auto|incandescent|tungsten|...
    # Software effect, applied to the finished frame in the render loop. Costs
    # CPU per frame but needs no capture restart, and never touches what the
    # NPU sees — inference runs on the clean frame before any of this.
    "effect":        ("none", str),
    "extra_args":    ("", str),             # raw passthrough to rpicam-vid
    "rotate":        (0, int),              # 0|90|180|270
    "infer_every_n": (INFER_EVERY_N, int),
    "detect":        (True, bool),          # run the NPU on this camera
}

# Pre-multi-camera variable names, still honoured for camera 0.
_CAM0_ALIASES = {
    "lens_position": "CAM_LENS_POSITION",
    "rotate":        "ROTATE_DEGREES",
    "infer_every_n": "INFER_EVERY_N",
}

# Secondary cameras come up as plain video. Turn detection on per camera from
# the web UI, or pin it with CAMN_DETECT=1.
# hflip/vflip are here so the inverted mounting of camera 0 does NOT propagate.
# Everything a secondary camera leaves unset inherits camera 0's resolved value,
# which is right for exposure and framerate and wrong for orientation: how one
# module is bolted in says nothing about the other. Set CAM1_HFLIP/CAM1_VFLIP if
# the second one is inverted too.
_CAM_SECONDARY_DEFAULTS = {"detect": False, "hflip": False, "vflip": False}

# Sensors with no focus actuator. Handing --autofocus-mode to rpicam-vid on one
# of these makes it exit immediately, which would otherwise surface as an
# endless capture-restart loop rather than an obvious error.
NO_AUTOFOCUS_SENSORS = {"imx477", "imx219", "imx296", "imx290", "imx462", "ov5647"}

_ROTATE_MAP = {0: None, 90: cv2.ROTATE_90_CLOCKWISE,
               180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}

AWB_MODES = ("", "auto", "incandescent", "tungsten", "fluorescent",
             "indoor", "daylight", "cloudy")

# Software effects. Order matters only for the UI; "none" must stay first.
EFFECTS = ("none", "gray", "invert", "edges", "heat", "sharpen")

# Settings that live in the rpicam-vid command line, so changing one means
# relaunching capture for that camera. Everything else applies on the next
# rendered frame.
CAPTURE_KEYS = frozenset((
    "width", "height", "framerate", "autofocus", "lens_position", "shutter",
    "gain", "ev", "denoise", "hflip", "vflip", "brightness", "contrast",
    "saturation", "sharpness", "awb", "extra_args", "rotate",
))


class CameraConfig:
    """Resolved capture settings for one camera."""

    def __init__(self, index, sensor="", base=None, modes=()):
        self.index = int(index)
        self.sensor = (sensor or "").lower()
        # What libcamera says this sensor can actually produce. Used to offer
        # real choices in the UI instead of arbitrary numbers the ISP would
        # have to crop and scale into.
        self.modes = [tuple(m) for m in modes]
        prefix = "CAM_" if self.index == 0 else f"CAM{self.index}_"
        self._explicit = set()

        for key, (root_default, cast) in _CAM_DEFAULTS.items():
            if base is None:
                fallback = root_default
            elif key in _CAM_SECONDARY_DEFAULTS:
                fallback = _CAM_SECONDARY_DEFAULTS[key]
            else:
                fallback = base._inheritable[key]

            names = [prefix + key.upper()]
            if self.index == 0 and key in _CAM0_ALIASES:
                names.append(_CAM0_ALIASES[key])

            value = fallback
            for name in names:
                if name in os.environ:
                    value = _env(name, fallback, cast)
                    self._explicit.add(key)
                    break
            setattr(self, key, value)

        # What later cameras inherit: the values as resolved from config, before
        # any sensor-specific fixups below. Inheriting the *fixed up* values
        # would leak one camera's hardware limits onto another — an IMX477 on
        # port 0 would strip autofocus from an IMX708 on port 1, parking its
        # lens wherever it happened to be.
        self._inheritable = {k: getattr(self, k) for k in _CAM_DEFAULTS}

        if self.rotate not in _ROTATE_MAP:
            log(f"[WARN] cam{self.index}: rotate={self.rotate} invalid "
                  f"(use 0/90/180/270) — ignoring")
            self.rotate = 0

        self.infer_every_n = max(1, int(self.infer_every_n))

        # An IMX477 has no autofocus. Drop the flag rather than let rpicam-vid
        # refuse to start, even when it was asked for explicitly: there is no
        # useful way to honour it on hardware with no focus actuator, and
        # CAM_AUTOFOCUS doubles as the default for every other camera, so an
        # explicit value there is not a statement about this sensor. Say so
        # rather than silently discarding what the operator wrote.
        if self.autofocus and self.sensor in NO_AUTOFOCUS_SENSORS:
            if "autofocus" in self._explicit:
                print(f"[WARN] cam{self.index}: {self.sensor} has no focus "
                      f"actuator — ignoring autofocus={self.autofocus!r} "
                      f"(rpicam-vid would refuse to start)")
            self.autofocus = ""

    @property
    def name(self):
        return f"cam{self.index}"

    @property
    def rotate_flag(self):
        return _ROTATE_MAP.get(self.rotate)

    def describe(self):
        bits = [f"{self.width}x{self.height}@{self.framerate}"]
        if self.sensor:
            bits.append(self.sensor)
        if self.rotate:
            bits.append(f"rotate={self.rotate}deg")
        if self.autofocus:
            bits.append(f"AF={self.autofocus}")
        bits.append("detecting" if self.detect else "stream only")
        return ", ".join(bits)

    def to_dict(self):
        d = {"index": self.index, "name": self.name, "sensor": self.sensor,
             "modes": [{"width": w, "height": h} for w, h in self.modes],
             "effects": list(EFFECTS), "awb_modes": list(AWB_MODES)}
        for key in _CAM_DEFAULTS:
            d[key] = getattr(self, key)
        return d

    def apply_runtime(self, data):
        """Change settings on a live camera. Returns (changed, needs_restart)."""
        changed = []
        for key, value in (data or {}).items():
            if key not in _CAM_DEFAULTS:
                continue
            try:
                new = self._coerce(key, value)
            except (TypeError, ValueError):
                log(f"[WARN] {self.name}: ignoring {key}={value!r}")
                continue
            if new != getattr(self, key):
                setattr(self, key, new)
                changed.append(key)
        return changed, any(k in CAPTURE_KEYS for k in changed)

    def _coerce(self, key, value):
        default, cast = _CAM_DEFAULTS[key]
        if key == "rotate":
            v = int(value)
            if v not in _ROTATE_MAP:
                raise ValueError(key)
            return v
        if key == "effect":
            v = str(value).lower()
            if v not in EFFECTS:
                raise ValueError(key)
            return v
        if key == "awb":
            v = str(value).lower()
            if v not in AWB_MODES:
                raise ValueError(key)
            return v
        if key in ("width", "height"):
            v = int(value)
            if not 64 <= v <= 8192:
                raise ValueError(key)
            return v
        if key == "framerate":
            v = int(value)
            if not 1 <= v <= 120:
                raise ValueError(key)
            return v
        if key == "infer_every_n":
            return max(1, int(value))
        if cast is bool:
            return bool(value)
        if cast is str:
            v = str(value).strip()
            # Numeric controls accept a blank ("leave it to the ISP") or a
            # number; anything else would reach rpicam-vid as a bad flag and
            # take the camera down.
            if key in ("shutter", "gain", "ev", "brightness", "contrast",
                       "saturation", "sharpness", "lens_position") and v:
                float(v)
            return v
        return cast(value)

    def rpicam_cmd(self):
        cmd = [
            "rpicam-vid",
            "--camera", str(self.index),
            "--codec", "mjpeg",
            "--inline", "--nopreview", "--flush",
            "--width", str(self.width),
            "--height", str(self.height),
            "--framerate", str(self.framerate),
            "--timeout", "0",
            "--output", "-",
        ]
        if self.denoise:
            cmd += ["--denoise", self.denoise]
        if self.autofocus:
            cmd += ["--autofocus-mode", self.autofocus]
            if self.autofocus == "manual" and self.lens_position:
                cmd += ["--lens-position", self.lens_position]
        if self.ev != "":
            cmd += ["--ev", self.ev]
        if self.shutter != "":
            cmd += ["--shutter", self.shutter]
        if self.gain != "":
            cmd += ["--gain", self.gain]
        for flag, value in (("--brightness", self.brightness),
                            ("--contrast", self.contrast),
                            ("--saturation", self.saturation),
                            ("--sharpness", self.sharpness)):
            if value != "":
                cmd += [flag, value]
        if self.awb:
            cmd += ["--awb", self.awb]
        if self.hflip:
            cmd += ["--hflip"]
        if self.vflip:
            cmd += ["--vflip"]
        if self.extra_args:
            cmd += self.extra_args.split()
        return cmd


#   0 : imx708 [4608x2592 10-bit RGGB] (/base/axi/pcie@120000/rp1/i2c@88000/...)
_CAM_LIST_RE = re.compile(r"^\s*(\d+)\s*:\s*(\S+)")
#     'SBGGR10_CSI2P' : 1332x990 [30.00 fps - (0, 0)/0x0 crop]
#                       2028x1080 [50.03 fps - ...]
_CAM_MODE_RE = re.compile(r"(\d{2,5})x(\d{2,5})\s*\[")


def detect_cameras(timeout=8.0):
    """Ask libcamera what is on the CSI ports.

    Returns [(index, sensor, [(w, h), ...]), ...] — the modes come from the
    same listing, so the UI can offer resolutions the sensor actually has
    rather than numbers the ISP would have to crop and scale into.
    """
    for exe in ("rpicam-hello", "libcamera-hello", "rpicam-vid"):
        try:
            proc = subprocess.run([exe, "--list-cameras"], capture_output=True,
                                  text=True, timeout=timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        if "Available cameras" not in text:
            continue

        found, current = [], None
        for line in text.splitlines():
            m = _CAM_LIST_RE.match(line)
            if m:
                current = [int(m.group(1)), m.group(2).lower(), []]
                found.append(current)
                continue            # the header's own [4056x3040 12-bit] is
                                    # the sensor array, not a selectable mode
            if current is not None:
                for w, h in _CAM_MODE_RE.findall(line):
                    wh = (int(w), int(h))
                    if wh not in current[2]:
                        current[2].append(wh)

        if found:
            for entry in found:
                entry[2].sort()
            return [(i, sensor, modes) for i, sensor, modes in sorted(found)]
    return []


def build_camera_configs(spec=None):
    """Turn a CAMERAS= spec into the list of CameraConfig to run."""
    spec = (CAMERAS_SPEC if spec is None else spec).strip()
    detected = detect_cameras()
    sensors = {i: sensor for i, sensor, _ in detected}
    modes = {i: m for i, _, m in detected}

    if spec.lower() in ("", "auto"):
        indices = sorted(sensors)
        if not indices:
            log("[camera] could not enumerate cameras — assuming one on port 0")
            indices = [0]
    else:
        indices = []
        for part in spec.split(","):
            part = part.strip()
            if part.isdigit():
                indices.append(int(part))
            elif part:
                log(f"[WARN] CAMERAS entry {part!r} is not an index — ignored")
        indices = sorted(dict.fromkeys(indices)) or [0]
        missing = [i for i in indices if sensors and i not in sensors]
        if missing:
            log(f"[WARN] camera(s) {missing} were not reported by libcamera — "
                  f"starting them anyway")

    configs, base = [], None
    for i in indices:
        cfg = CameraConfig(i, sensors.get(i, ""), base=base,
                           modes=modes.get(i, ()))
        if base is None:
            base = cfg
        configs.append(cfg)
    return configs


# ---------- output ----------
JPEG_QUALITY   = _env("JPEG_QUALITY", 85, int)
SHOW_HUD       = _env("SHOW_HUD", True, bool)
SHOW_LABELS    = _env("SHOW_LABELS", True, bool)
SHOW_IDS       = _env("SHOW_IDS", True, bool)
SHOW_TRAILS    = _env("SHOW_TRAILS", False, bool)
SHOW_ROI       = _env("SHOW_ROI", False, bool)
CONFIRMED_ONLY = _env("CONFIRMED_ONLY", True, bool)
STREAM_MAX_FPS = _env("STREAM_MAX_FPS", 0, float)   # 0 = as fast as frames arrive

# ---------- region of interest ----------
# Normalised polygon, JSON: ROI_POLYGON='[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]]'
# Detections whose centre falls outside are ignored entirely.
ROI_POLYGON = _env("ROI_POLYGON", "")

# ---------- events / snapshots / webhook ----------
EVENT_LOG          = _env("EVENT_LOG", True, bool)
EVENT_DB           = _env("EVENT_DB", os.path.join(HERE, "events.db"))
EVENT_RETENTION_D  = _env("EVENT_RETENTION_DAYS", 30, int)
SNAPSHOT_ON_DETECT = _env("SNAPSHOT_ON_DETECT", False, bool)
SNAPSHOT_DIR       = _env("SNAPSHOT_DIR", os.path.join(HERE, "snapshots"))
SNAPSHOT_MAX_FILES = _env("SNAPSHOT_MAX_FILES", 500, int)
SNAPSHOT_COOLDOWN  = _env("SNAPSHOT_COOLDOWN", 30.0, float)
WEBHOOK_URL        = _env("WEBHOOK_URL", "")

# How long a camera may sit open without producing a frame before saying so.
# Long enough to cover a slow sensor coming up, short enough that a camera
# which is never going to work announces itself rather than staying blank.
CAPTURE_STALL_S = _env("CAPTURE_STALL_S", 12.0, float)

DETECTION_LOG          = _env("DETECTION_LOG", True, bool)
DETECTION_LOG_COOLDOWN = _env("DETECTION_LOG_COOLDOWN", 10.0, float)


# ============================================================
# COCO CLASSES
# ============================================================
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]
CLASS_NAME_TO_ID = {name: i for i, name in enumerate(COCO_CLASSES)}


def _class_color(class_id):
    """Stable, reasonably separated BGR colour per class."""
    rng = np.random.default_rng(class_id + 42)
    return tuple(int(x) for x in rng.integers(80, 255, size=3))


CLASS_COLORS = [_class_color(i) for i in range(len(COCO_CLASSES))]
CLASS_COLORS[CLASS_NAME_TO_ID["cat"]] = (0, 255, 0)      # cats stay green
CLASS_COLORS[CLASS_NAME_TO_ID["dog"]] = (0, 200, 255)
CLASS_COLORS[CLASS_NAME_TO_ID["person"]] = (255, 180, 60)

CSS_COLORS = [f"rgb({r},{g},{b})" for (b, g, r) in CLASS_COLORS]


def resolve_class_ids(names):
    if not names:
        return set(range(len(COCO_CLASSES)))
    ids = set()
    for n in names:
        key = str(n).strip().lower()
        if key in CLASS_NAME_TO_ID:
            ids.add(CLASS_NAME_TO_ID[key])
        elif key.isdigit() and 0 <= int(key) < len(COCO_CLASSES):
            ids.add(int(key))
        else:
            log(f"[WARN] Unknown class {n!r} — ignored")
    return ids or set(range(len(COCO_CLASSES)))


# ============================================================
# RUNTIME SETTINGS (mutable from the web UI, no restart)
# ============================================================
class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self.conf_thresh = CONF_THRESH
        self.tracked_names = list(TRACKED_CLASSES)
        self.tracked_ids = resolve_class_ids(TRACKED_CLASSES)
        self.show_labels = SHOW_LABELS
        self.show_ids = SHOW_IDS
        self.show_trails = SHOW_TRAILS
        self.show_hud = SHOW_HUD
        self.show_roi = SHOW_ROI
        self.confirmed_only = CONFIRMED_ONLY
        self.jpeg_quality = JPEG_QUALITY

    def snapshot(self):
        with self._lock:
            return {
                "conf_thresh": self.conf_thresh,
                "tracked_classes": sorted(COCO_CLASSES[i] for i in self.tracked_ids)
                                   if self.tracked_names else [],
                "show_labels": self.show_labels,
                "show_ids": self.show_ids,
                "show_trails": self.show_trails,
                "show_hud": self.show_hud,
                "show_roi": self.show_roi,
                "confirmed_only": self.confirmed_only,
                "jpeg_quality": self.jpeg_quality,
            }

    def apply(self, data):
        changed = []
        with self._lock:
            if "conf_thresh" in data:
                v = float(data["conf_thresh"])
                if 0.01 <= v <= 0.99:
                    self.conf_thresh = v
                    changed.append("conf_thresh")
            if "tracked_classes" in data:
                names = [str(n).lower() for n in data["tracked_classes"] or []]
                self.tracked_names = names
                self.tracked_ids = resolve_class_ids(names)
                changed.append("tracked_classes")
            for key in ("show_labels", "show_ids", "show_trails", "show_hud",
                        "show_roi", "confirmed_only"):
                if key in data:
                    setattr(self, key, bool(data[key]))
                    changed.append(key)
            if "jpeg_quality" in data:
                q = int(data["jpeg_quality"])
                if 10 <= q <= 100:
                    self.jpeg_quality = q
                    changed.append("jpeg_quality")
        return changed

    # Cheap reads for the hot path
    def draw_opts(self):
        with self._lock:
            return (self.show_labels, self.show_ids, self.show_trails,
                    self.show_hud, self.show_roi, self.confirmed_only,
                    self.jpeg_quality)

    def detect_opts(self):
        with self._lock:
            return self.conf_thresh, self.tracked_ids


SETTINGS = Settings()


# ============================================================
# STATS
# ============================================================
class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.frames = 0
        self.dropped = 0
        self.capture_errors = 0
        self.total_tracks = 0
        self.last_frame_at = 0.0
        self._frame_times = deque(maxlen=90)
        self._infer_times = deque(maxlen=90)
        self._encode_times = deque(maxlen=90)
        self.class_counts = defaultdict(int)

    def frame(self, infer_s):
        with self._lock:
            now = time.time()
            self.frames += 1
            self.last_frame_at = now
            self._frame_times.append(now)
            self._infer_times.append(infer_s)

    def encode(self, sec):
        with self._lock:
            self._encode_times.append(sec)

    def fps(self):
        with self._lock:
            if len(self._frame_times) < 2:
                return 0.0
            span = self._frame_times[-1] - self._frame_times[0]
            return (len(self._frame_times) - 1) / span if span > 0 else 0.0

    def _mean_ms(self, dq):
        return (sum(dq) / len(dq) * 1000.0) if dq else 0.0

    def infer_ms(self):
        with self._lock:
            return self._mean_ms(self._infer_times)

    def encode_ms(self):
        with self._lock:
            return self._mean_ms(self._encode_times)

    def healthy(self):
        with self._lock:
            return (time.time() - self.last_frame_at) < 10.0 if self.last_frame_at else False


# ============================================================
# FRAME PUBLISHER — one encode, many clients
# ============================================================
class FramePublisher:
    """Single-writer / many-reader latest-frame holder.

    The originals called frame_q.get() inside the MJPEG generator, so two open
    browser tabs each got half the frames and annotated them twice. Here the
    render thread publishes once and every client reads the same bytes.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._jpeg = None
        self._seq = 0

    def publish(self, jpeg_bytes):
        with self._cv:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cv.notify_all()

    def latest(self):
        with self._cv:
            return self._jpeg

    def wait_for(self, last_seq, timeout=5.0):
        """Block until a frame newer than last_seq. Returns (seq, jpeg) or (last_seq, None)."""
        with self._cv:
            if self._seq == last_seq:
                self._cv.wait(timeout)
            if self._seq == last_seq:
                return last_seq, None
            return self._seq, self._jpeg

    @property
    def seq(self):
        with self._cv:
            return self._seq


# index -> CameraPipeline, in the order the cameras were configured.
CAMS = {}
STARTED_AT = time.time()
_shutdown = threading.Event()


# ============================================================
# ROI
# ============================================================
def _parse_roi(raw):
    if not raw:
        return None
    try:
        pts = json.loads(raw)
        if not isinstance(pts, list) or len(pts) < 3:
            raise ValueError("need at least 3 points")
        return np.array([[float(x), float(y)] for x, y in pts], dtype=np.float32)
    except Exception as e:
        log(f"[WARN] ROI_POLYGON ignored ({e}). "
              f'Expected JSON like [[0.1,0.1],[0.9,0.1],[0.9,0.9]]')
        return None


ROI_NORM = _parse_roi(ROI_POLYGON)


def roi_pixels(w, h):
    if ROI_NORM is None:
        return None
    return (ROI_NORM * np.array([w, h], dtype=np.float32)).astype(np.int32)


def in_roi(cx, cy, poly_px):
    if poly_px is None:
        return True
    return cv2.pointPolygonTest(poly_px, (float(cx), float(cy)), False) >= 0


# ============================================================
# HAILO
# ============================================================
from hailo_platform import (VDevice, HEF, ConfigureParams, InferVStreams,   # noqa: E402
                            InputVStreamParams, OutputVStreamParams,
                            HailoStreamInterface)

_hailo = {"hef": None, "target": None, "ng": None, "pipe": None,
          "activated": None, "in": None, "out": None, "path": None}
_infer_lock = threading.Lock()


def find_hef():
    for path in HEF_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "No .hef model found. Looked in:\n  " +
        "\n  ".join(p for p in HEF_CANDIDATES if p) +
        "\n\nRun ./download_model.sh, or set HEF_PATH."
    )


def init_hailo():
    path = find_hef()
    log(f"[hailo] Loading {path} ...")

    hef = HEF(path)
    target = VDevice()
    cfg = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
    ng = target.configure(hef, cfg)[0]
    ng_params = ng.create_params()

    in_params = InputVStreamParams.make_from_network_group(ng)
    out_params = OutputVStreamParams.make_from_network_group(ng)

    activated = ng.activate(ng_params)
    activated.__enter__()
    pipe = InferVStreams(ng, in_params, out_params)
    pipe.__enter__()

    in_info = ng.get_input_vstream_infos()[0]
    out_info = ng.get_output_vstream_infos()[0]

    _hailo.update({"hef": hef, "target": target, "ng": ng, "pipe": pipe,
                   "activated": activated, "in": in_info.name,
                   "out": out_info.name, "path": path})

    log(f"[hailo] Ready — in {in_info.name} {in_info.shape}, out {out_info.name}")

    # Reading the output name off the HEF instead of hardcoding
    # 'yolov8s/yolov8_nms_postprocess' means swapping models just works.
    try:
        model_side = int(in_info.shape[0])
        if model_side != NN_SIZE:
            log(f"[WARN] Model wants {model_side}px input but NN_SIZE={NN_SIZE}. "
                  f"Set NN_SIZE={model_side} or every box will be misplaced.")
    except Exception:
        pass


def shutdown_hailo():
    try:
        if _hailo["pipe"] is not None:
            _hailo["pipe"].__exit__(None, None, None)
        if _hailo["activated"] is not None:
            _hailo["activated"].__exit__(None, None, None)
    except Exception as e:
        log(f"[hailo] shutdown warning: {e}")


def run_inference(nn_frame):
    arr = np.expand_dims(nn_frame, axis=0)
    with _infer_lock:
        out = _hailo["pipe"].infer({_hailo["in"]: arr})
    return out[_hailo["out"]][0]        # [num_classes][N, 5]


# ============================================================
# GEOMETRY
# ============================================================
def letterbox(frame, size=NN_SIZE):
    h, w = frame.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    pad_y = (size - nh) // 2
    pad_x = (size - nw) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def parse_detections(raw, scale, pad_x, pad_y, frame_w, frame_h, conf_thresh, tracked_ids):
    """Hailo NMS output -> [(class_id, name, (x1,y1,x2,y2), conf)] in frame pixels."""
    out = []
    min_area = MIN_BOX_AREA_FRAC * frame_w * frame_h

    for class_id, class_dets in enumerate(raw):
        if class_id not in tracked_ids:
            continue
        if class_id >= len(COCO_CLASSES):
            continue
        name = COCO_CLASSES[class_id]

        for det in class_dets:
            try:
                d = np.asarray(det, dtype=np.float32).reshape(-1)
                if d.size < 5:
                    continue

                conf = float(d[4])
                if conf < conf_thresh:
                    continue

                if BOX_ORDER == "yxyx":
                    y1, x1, y2, x2 = d[0], d[1], d[2], d[3]
                else:
                    x1, y1, x2, y2 = d[0], d[1], d[2], d[3]

                # 0..1 on the NN_SIZE letterbox canvas -> original frame pixels
                x1 = (x1 * NN_SIZE - pad_x) / scale
                y1 = (y1 * NN_SIZE - pad_y) / scale
                x2 = (x2 * NN_SIZE - pad_x) / scale
                y2 = (y2 * NN_SIZE - pad_y) / scale

                x1 = max(0.0, min(x1, frame_w - 1))
                x2 = max(0.0, min(x2, frame_w - 1))
                y1 = max(0.0, min(y1, frame_h - 1))
                y2 = max(0.0, min(y2, frame_h - 1))

                if x2 <= x1 or y2 <= y1:
                    continue
                if (x2 - x1) * (y2 - y1) < min_area:
                    continue

                out.append((class_id, name, (x1, y1, x2, y2), conf))

            except Exception as e:
                log(f"[parse] {e}")

    return out


# ============================================================
# ANNOTATION
# ============================================================
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _label_scale(box_w):
    """Shrink text for small boxes so labels don't swamp what they describe."""
    return max(0.4, min(0.7, box_w / 320.0))


def draw_track(frame, track, show_labels, show_ids, show_trails):
    x1, y1, x2, y2 = (int(v) for v in track.box)
    h, w = frame.shape[:2]
    x1, x2 = max(0, min(x1, w - 1)), max(0, min(x2, w - 1))
    y1, y2 = max(0, min(y1, h - 1)), max(0, min(y2, h - 1))
    if x2 <= x1 or y2 <= y1:
        return

    color = CLASS_COLORS[track.class_id] if track.class_id < len(CLASS_COLORS) else (200, 200, 200)
    thickness = 2 if track.confirmed else 1

    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    if show_trails and len(track.trail) > 1:
        pts = np.array([[int(px), int(py)] for px, py in track.trail], np.int32)
        cv2.polylines(frame, [pts], False, color, 2)
        cx, cy = track.trail[-1]
        cv2.circle(frame, (int(cx), int(cy)), 3, color, -1)

    if not show_labels:
        return

    parts = [track.class_name]
    if show_ids:
        parts.append(f"#{track.id}")
    parts.append(f"{track.conf:.0%}")
    text = " ".join(parts)

    scale = _label_scale(x2 - x1)
    (tw, th), base = cv2.getTextSize(text, FONT, scale, 1)

    # Prefer above the box; drop inside it when we're against the top edge.
    ty = y1 - 6
    if ty - th - 4 < 0:
        ty = y1 + th + 8

    bx1, by1 = x1, ty - th - 5
    bx2, by2 = x1 + tw + 8, ty + base
    bx2 = min(bx2, w - 1)

    cv2.rectangle(frame, (bx1, by1), (bx2, by2), color, -1)
    cv2.putText(frame, text, (x1 + 4, ty - 1), FONT, scale, (10, 10, 10), 1, cv2.LINE_AA)


def apply_effect(frame, effect):
    """Visual-only transform of a finished frame.

    Deliberately applied in the render loop, after inference has already run on
    the clean frame in the capture loop. Running the NPU on an inverted or
    edge-detected image would wreck detection, and the point of these is to
    change what you see, not what the model sees.
    """
    if effect in ("", "none"):
        return frame
    if effect == "gray":
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    if effect == "invert":
        return cv2.bitwise_not(frame)
    if effect == "edges":
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.cvtColor(cv2.Canny(g, 80, 160), cv2.COLOR_GRAY2BGR)
    if effect == "heat":
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.applyColorMap(g, cv2.COLORMAP_INFERNO)
    if effect == "sharpen":
        kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
        return cv2.filter2D(frame, -1, kernel)
    return frame


def draw_hud(frame, stats, live_count, label=None, detecting=True):
    bits = []
    if label:
        bits.append(label)
    bits.append(f"{stats.fps():4.1f} fps")
    if detecting:
        bits.append(f"{stats.infer_ms():4.1f} ms")
        bits.append(f"{live_count} obj")
    else:
        bits.append("no detection")
    text = " | ".join(bits)
    cv2.putText(frame, text, (10, 24), FONT, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(frame, text, (10, 24), FONT, 0.6, (255, 255, 255), 1, cv2.LINE_AA)


def draw_roi(frame, poly_px):
    if poly_px is None:
        return
    overlay = frame.copy()
    cv2.fillPoly(overlay, [poly_px], (60, 60, 60))
    cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)
    cv2.polylines(frame, [poly_px], True, (0, 200, 255), 2)


# ============================================================
# EVENTS / SNAPSHOTS / WEBHOOK  — one set, shared by every camera
# ============================================================
EVENTS = EventLog(EVENT_DB, EVENT_RETENTION_D, enabled=EVENT_LOG)
SNAPSHOTS = SnapshotStore(SNAPSHOT_DIR, SNAPSHOT_MAX_FILES, SNAPSHOT_COOLDOWN,
                          enabled=SNAPSHOT_ON_DETECT)
HOOK = Webhook(WEBHOOK_URL)


class _FakeTrack:
    """Duck-typed stand-in so draw_track works with tracking disabled."""

    __slots__ = ("id", "class_id", "class_name", "box", "conf", "confirmed", "trail")

    def __init__(self, class_id, class_name, box, conf):
        self.id = 0
        self.class_id = class_id
        self.class_name = class_name
        self.box = box
        self.conf = conf
        self.confirmed = True
        self.trail = ()

    def to_dict(self):
        x1, y1, x2, y2 = (int(v) for v in self.box)
        return {"id": 0, "class": self.class_name, "class_id": self.class_id,
                "box": [x1, y1, x2, y2], "conf": round(self.conf, 3),
                "confirmed": True}


# ============================================================
# CAMERA PIPELINE — capture, infer, track, annotate, publish
# ============================================================
class CameraPipeline:
    """Everything that used to be a module global, scoped to one camera.

    Two of these run side by side. They share the NPU (run_inference holds a
    lock), the event log, the snapshot store and the detection Settings; they do
    not share frames, queues, trackers, stats or publishers, so a stall on one
    camera cannot drop frames on the other.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.index = cfg.index
        self.name = cfg.name
        self.detect = bool(cfg.detect)
        self.stats = Stats()
        self.publisher = FramePublisher()
        self.queue = queue.Queue(maxsize=2)
        self.tracker = Tracker(iou_threshold=TRACK_IOU,
                               max_misses=TRACK_MAX_MISSES,
                               min_hits=TRACK_MIN_HITS,
                               trail_len=TRAIL_LENGTH)
        self._live_tracks = []
        self._live_lock = threading.Lock()
        self._last_logged = {}
        self._stderr_tail = deque(maxlen=20)
        self._reconfigure = threading.Event()
        self._proc = None
        self._proc_lock = threading.Lock()

    def __repr__(self):
        return f"<CameraPipeline {self.name} {self.cfg.describe()}>"

    # ------------------------------------------------------------------

    def start(self):
        for target, role in ((self.capture_loop, "capture"),
                             (self.render_loop, "render")):
            threading.Thread(target=target, daemon=True,
                             name=f"{self.name}-{role}").start()

    def request_reconfigure(self):
        """Relaunch capture so new rpicam-vid arguments take effect.

        rpicam-vid reads its settings once at startup, so there is no way to
        change resolution or exposure on a running camera — it has to come back
        up. Killing it here rather than waiting for the capture loop to notice
        keeps the gap to about a second, and only this camera is touched.
        """
        self._reconfigure.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def set_detect(self, enabled):
        """Flip inference on or off for this camera without a restart."""
        enabled = bool(enabled)
        if enabled == self.detect:
            return False
        self.detect = enabled
        self.cfg.detect = enabled
        log(f"[{self.name}] detection {'on' if enabled else 'off'}")
        return True

    # ------------------------------------------------------------------
    # CAPTURE
    # ------------------------------------------------------------------

    def _drain_stderr(self, proc):
        """Keep rpicam-vid's own account of itself.

        This used to be sent to DEVNULL, which made the two failure modes
        indistinguishable: a camera that opens and streams, and a camera that
        opens, says exactly why it cannot stream, and then sits there silently
        forever. The second one presents as a panel that never fills in and a
        log with nothing wrong in it.

        Also has to be drained rather than merely redirected — a full stderr
        pipe would block rpicam-vid mid-frame.
        """
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                low = line.lower()
                if "error" in low or "fail" in low or "not supported" in low:
                    log(f"[{self.name}] rpicam-vid: {line}")
        except Exception:
            pass

    def _log_stderr_tail(self, why):
        if not self._stderr_tail:
            log(f"[{self.name}] {why}, and rpicam-vid printed nothing at all")
            return
        log(f"[{self.name}] {why}. Last words from rpicam-vid:")
        for line in list(self._stderr_tail):
            log(f"[{self.name}]   {line}")

    def capture_loop(self):
        """rpicam-vid -> decode -> NPU -> self.queue.

        Wrapped so a camera hiccup restarts this camera's pipeline with backoff
        instead of killing the thread and leaving a live page serving a frozen
        image. One camera restarting does not disturb the other.
        """
        backoff = 1.0
        frame_index = 0
        last_raw = None

        while not _shutdown.is_set():
            proc = None
            try:
                # Re-read per-restart: a reconfigure may have changed any of it.
                rotate = self.cfg.rotate_flag
                cmd = self.cfg.rpicam_cmd()
                log(f"[{self.name}] {' '.join(cmd)}")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
                with self._proc_lock:
                    self._proc = proc
                self._stderr_tail.clear()
                threading.Thread(target=self._drain_stderr, args=(proc,),
                                 daemon=True,
                                 name=f"{self.name}-stderr").start()
                log(f"[{self.name}] capture started")
                backoff = 1.0
                buf = b""
                opened_at = time.time()
                saw_frame = False
                stalled = False

                while not _shutdown.is_set():
                    if self._reconfigure.is_set():
                        self._reconfigure.clear()
                        log(f"[{self.name}] applying new camera settings")
                        break

                    # Wait with a timeout rather than blocking in read(). A
                    # camera that opens but never sends anything would
                    # otherwise park this thread forever: no frames, no error,
                    # nothing in the log, and no way to notice except by
                    # spotting that the stream never fills in.
                    if not select.select([proc.stdout], [], [], 2.0)[0]:
                        if proc.poll() is not None:
                            raise RuntimeError(f"rpicam-vid exited {proc.returncode}")
                        if not saw_frame and not stalled and \
                                time.time() - opened_at > CAPTURE_STALL_S:
                            stalled = True
                            self._log_stderr_tail(
                                f"no frames {CAPTURE_STALL_S:.0f}s after "
                                f"rpicam-vid started")
                        continue

                    chunk = proc.stdout.read(1 << 20)
                    if not chunk:
                        if proc.poll() is not None:
                            raise RuntimeError(f"rpicam-vid exited {proc.returncode}")
                        continue
                    buf += chunk

                    # Drain to the NEWEST complete JPEG sitting in the buffer.
                    #
                    # rpicam-vid never stops producing. If decode + inference
                    # ever falls behind — and with two cameras sharing one NPU
                    # it will, briefly — taking the oldest frame each pass means
                    # rendering an ever-growing backlog of stale video that
                    # never catches up. Anything older than the last complete
                    # frame is worthless, so throw it away.
                    jpg = None
                    stale = 0
                    while True:
                        start = buf.find(b"\xff\xd8")
                        if start == -1:
                            break
                        end = buf.find(b"\xff\xd9", start + 2)
                        if end == -1:
                            break
                        if jpg is not None:
                            stale += 1
                        jpg = buf[start:end + 2]
                        buf = buf[end + 2:]

                    if jpg is None:
                        if len(buf) > 8 << 20:
                            log(f"[{self.name}] resyncing — no complete JPEG in 8MB")
                            buf = b""
                        continue

                    if not saw_frame:
                        saw_frame = True
                        if stalled:
                            log(f"[{self.name}] frames arrived after all")

                    if stale:
                        with self.stats._lock:
                            self.stats.dropped += stale

                    frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    if rotate is not None:
                        frame = cv2.rotate(frame, rotate)

                    frame_index += 1
                    infer_s = 0.0

                    if not self.detect:
                        # Stream only. Drop whatever the last inference said so
                        # stale boxes can't outlive the switch.
                        last_raw = None
                    elif frame_index % self.cfg.infer_every_n == 0:
                        nn_frame, scale, pad_x, pad_y = letterbox(frame)
                        t0 = time.perf_counter()
                        raw = run_inference(nn_frame)
                        infer_s = time.perf_counter() - t0
                        last_raw = (raw, scale, pad_x, pad_y)

                    self.stats.frame(infer_s)

                    if self.detect and last_raw is None:
                        continue        # nothing to draw until the first inference

                    if self.queue.full():
                        with self.stats._lock:
                            self.stats.dropped += 1
                        try:
                            self.queue.get_nowait()     # drop stale, keep latest
                        except queue.Empty:
                            pass
                    self.queue.put((frame, last_raw))

            except Exception as e:
                # A reconfigure kills rpicam-vid on purpose; the read failing
                # is the expected consequence, not a fault. Restart clean with
                # no error counted and no backoff.
                if self._reconfigure.is_set():
                    self._reconfigure.clear()
                    log(f"[{self.name}] applying new camera settings")
                    continue

                with self.stats._lock:
                    self.stats.capture_errors += 1
                log(f"[{self.name}] error: {e} — restarting in {backoff:.0f}s")
                self._log_stderr_tail("capture failed")
                _shutdown.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                with self._proc_lock:
                    self._proc = None
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    # ------------------------------------------------------------------
    # RENDER
    # ------------------------------------------------------------------

    def render_loop(self):
        while not _shutdown.is_set():
            try:
                frame, payload = self.queue.get(timeout=2.0)
            except queue.Empty:
                continue

            h, w = frame.shape[:2]
            conf_thresh, tracked_ids = SETTINGS.detect_opts()
            (show_labels, show_ids, show_trails,
             show_hud, show_roi, confirmed_only, quality) = SETTINGS.draw_opts()

            poly_px = roi_pixels(w, h)

            if payload is None:
                # Detection is off for this camera. Retire anything the tracker
                # is still holding so the event log closes those rows out
                # instead of leaving them open forever.
                if self.tracker.tracks:
                    self.tracker.reset()
                    for t in self.tracker.drain_finished():
                        EVENTS.track_ended(t, camera=self.index)
                visible = []
            else:
                raw, scale, pad_x, pad_y = payload
                dets = parse_detections(raw, scale, pad_x, pad_y, w, h,
                                        conf_thresh, tracked_ids)

                if poly_px is not None:
                    dets = [d for d in dets
                            if in_roi((d[2][0] + d[2][2]) / 2,
                                      (d[2][1] + d[2][3]) / 2, poly_px)]

                if TRACK_ENABLED:
                    tracks = self.tracker.update(dets)
                    visible = [t for t in tracks
                               if (t.confirmed or not confirmed_only)]
                    self._handle_track_events(tracks)
                else:
                    # Tracking off: synthesise throwaway objects so drawing is
                    # uniform.
                    visible = [_FakeTrack(cid, name, box, conf)
                               for cid, name, box, conf in dets]

            effect = self.cfg.effect
            if effect and effect != "none":
                frame = apply_effect(frame, effect)

            if show_roi and payload is not None:
                draw_roi(frame, poly_px)

            for t in visible:
                draw_track(frame, t, show_labels, show_ids, show_trails)

            if show_hud:
                draw_hud(frame, self.stats, len(visible),
                         label=self.name if len(CAMS) > 1 else None,
                         detecting=self.detect)

            t0 = time.perf_counter()
            ok, jpg = cv2.imencode(".jpg", frame,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            self.stats.encode(time.perf_counter() - t0)
            if not ok:
                continue

            data = jpg.tobytes()
            self.publisher.publish(data)

            with self._live_lock:
                self._live_tracks = [dict(t.to_dict(), camera=self.index)
                                     for t in visible]

            if payload is not None and TRACK_ENABLED:
                self._handle_snapshots(visible, data)

    # ------------------------------------------------------------------
    # EVENTS
    # ------------------------------------------------------------------

    def _handle_track_events(self, tracks):
        now = time.time()

        for t in tracks:
            if t.confirmed and not t.notified:
                t.notified = True
                with self.stats._lock:
                    self.stats.total_tracks += 1
                    self.stats.class_counts[t.class_name] += 1
                EVENTS.track_started(t, camera=self.index)
                HOOK.notify(t, extra={"camera": self.index})
                if (DETECTION_LOG and now - self._last_logged.get(t.class_id, 0)
                        > DETECTION_LOG_COOLDOWN):
                    self._last_logged[t.class_id] = now
                    x1, y1, x2, y2 = (int(v) for v in t.box)
                    log(f"[detect] {self.name} {t.class_name} #{t.id} "
                          f"{t.conf:.0%} at ({x1},{y1})-({x2},{y2})")

        for t in self.tracker.drain_finished():
            EVENTS.track_ended(t, camera=self.index)
            if DETECTION_LOG:
                log(f"[detect] {self.name} {t.class_name} #{t.id} left after "
                      f"{t.duration:.1f}s ({t.hits} frames, peak {t.max_conf:.0%})")

    def _handle_snapshots(self, tracks, jpeg_bytes):
        if not SNAPSHOTS.enabled:
            return
        for t in tracks:
            if getattr(t, "confirmed", False) and not getattr(t, "snapshot_taken", True):
                t.snapshot_taken = True
                path = SNAPSHOTS.maybe_save(t.class_name, t.id, jpeg_bytes,
                                            camera=self.index)
                if path:
                    EVENTS.attach_snapshot(t.id, path, camera=self.index)
                    log(f"[snapshot] {os.path.basename(path)}")

    # ------------------------------------------------------------------
    # READERS
    # ------------------------------------------------------------------

    def live_tracks(self):
        with self._live_lock:
            return list(self._live_tracks)

    def stats_dict(self):
        s = self.stats
        with s._lock:
            frames, dropped = s.frames, s.dropped
            errors, total_tracks = s.capture_errors, s.total_tracks
            class_counts = dict(s.class_counts)

        return {
            "index": self.index,
            "name": self.name,
            "sensor": self.cfg.sensor,
            "detect": self.detect,
            "resolution": f"{self.cfg.width}x{self.cfg.height}",
            "framerate": self.cfg.framerate,
            "rotate": self.cfg.rotate,
            "infer_every_n": self.cfg.infer_every_n,
            "fps": round(s.fps(), 2),
            "inference_ms": round(s.infer_ms(), 2),
            "encode_ms": round(s.encode_ms(), 2),
            "frames": frames,
            "dropped": dropped,
            "capture_errors": errors,
            "live_tracks": len(self.live_tracks()),
            "total_tracks": total_tracks,
            "class_counts": class_counts,
            "healthy": s.healthy(),
        }

    def config_dict(self):
        return {**self.cfg.to_dict(), "detect": self.detect}

    def shutdown(self):
        try:
            self.tracker.reset()
            for t in self.tracker.drain_finished():
                EVENTS.track_ended(t, camera=self.index)
        except Exception:
            pass
        # Wake any client blocked on wait_for so /video generators can exit.
        self.publisher.publish(self.publisher.latest() or b"")


# ---------------- registry ----------------

def start_cameras(configs):
    for cfg in configs:
        CAMS[cfg.index] = CameraPipeline(cfg)
    for cam in CAMS.values():
        cam.start()
    return CAMS


def primary_cam():
    """Camera 0 if it exists, otherwise the lowest-numbered one configured."""
    return next(iter(CAMS.values()), None)


def get_cam(index):
    try:
        return CAMS.get(int(index))
    except (TypeError, ValueError):
        return None


# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


def _pick_cam(index):
    """None/blank means the primary camera; anything else must resolve."""
    if index is None or index == "":
        return primary_cam()
    return get_cam(index)


def _config_snapshot():
    cfg = SETTINGS.snapshot()
    cfg["cameras"] = [c.config_dict() for c in CAMS.values()]
    return cfg


@app.route("/")
def index():
    _, tracked_ids = SETTINGS.detect_opts()
    return webui.render(COCO_CLASSES, CSS_COLORS, tracked_ids,
                        [c.config_dict() for c in CAMS.values()])


# ---------------- video ----------------

def mjpeg_stream(cam):
    seq = 0
    min_interval = (1.0 / STREAM_MAX_FPS) if STREAM_MAX_FPS > 0 else 0.0
    last_sent = 0.0

    while not _shutdown.is_set():
        seq, data = cam.publisher.wait_for(seq, timeout=5.0)
        if data is None:
            continue
        if min_interval:
            now = time.time()
            if now - last_sent < min_interval:
                continue
            last_sent = now
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n"
               b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" +
               data + b"\r\n")


@app.route("/video")
@app.route("/video/<int:index>")
def video(index=None):
    cam = _pick_cam(index)
    if cam is None:
        return "No such camera", 404
    return Response(mjpeg_stream(cam),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/snapshot")
@app.route("/snapshot/<int:index>")
def snapshot(index=None):
    cam = _pick_cam(index)
    if cam is None:
        return "No such camera", 404
    data = cam.publisher.latest()
    if data is None:
        return "No frame yet", 503
    return Response(data, mimetype="image/jpeg")


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    body = request.get_json(silent=True) or {}
    cam = _pick_cam(body.get("camera", request.args.get("camera")))
    if cam is None:
        return jsonify({"saved": None, "error": "no such camera"}), 404

    data = cam.publisher.latest()
    if data is None:
        return jsonify({"saved": None, "error": "no frame"}), 503

    store = SNAPSHOTS if SNAPSHOTS.enabled else SnapshotStore(
        SNAPSHOT_DIR, SNAPSHOT_MAX_FILES, cooldown_s=0.0, enabled=True)
    path = store.maybe_save("manual", 0, data, camera=cam.index)
    return jsonify({"saved": os.path.basename(path) if path else None,
                    "camera": cam.index})


@app.route("/snapshots")
def snapshots_list():
    return jsonify(SNAPSHOTS.list_recent(limit=int(request.args.get("limit", 50))))


@app.route("/snapshots/<path:name>")
def snapshots_file(name):
    return send_from_directory(SNAPSHOT_DIR, name)


# ---------------- config ----------------

def _apply_camera_config(spec):
    """Per-camera settings from the UI.

    Accepts either {"1": {"detect": true}} or [{"index": 1, "detect": true}].
    """
    if not spec:
        return []

    if isinstance(spec, dict):
        items = list(spec.items())
    elif isinstance(spec, list):
        items = [(item.get("index"), item) for item in spec
                 if isinstance(item, dict)]
    else:
        return []

    changed = []
    for key, value in items:
        cam = get_cam(key)
        if cam is None or not isinstance(value, dict):
            continue
        if "detect" in value and cam.set_detect(value["detect"]):
            changed.append(f"{cam.name}.detect")

        rest = {k: v for k, v in value.items() if k != "detect"}
        if rest:
            keys, needs_restart = cam.cfg.apply_runtime(rest)
            changed += [f"{cam.name}.{k}" for k in keys]
            if needs_restart:
                cam.request_reconfigure()
    return changed


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(_config_snapshot())

    data = request.get_json(silent=True) or {}
    changed = SETTINGS.apply(data)
    changed += _apply_camera_config(data.get("cameras"))
    if changed:
        log(f"[config] updated: {', '.join(sorted(set(changed)))}")
    return jsonify({"ok": True, "changed": sorted(set(changed)),
                    "config": _config_snapshot()})


@app.route("/cameras")
def cameras():
    return jsonify([c.config_dict() for c in CAMS.values()])


# ---------------- tracks + stats ----------------

@app.route("/tracks")
@app.route("/tracks/<int:index>")
def tracks(index=None):
    if index is None:
        out = []
        for cam in CAMS.values():
            out.extend(cam.live_tracks())
        return jsonify(out)

    cam = get_cam(index)
    if cam is None:
        return jsonify({"error": "no such camera"}), 404
    return jsonify(cam.live_tracks())


@app.route("/stats")
@app.route("/stats/<int:index>")
def stats(index=None):
    if index is not None:
        cam = get_cam(index)
        if cam is None:
            return jsonify({"error": "no such camera"}), 404
        return jsonify(cam.stats_dict())

    per_cam = [c.stats_dict() for c in CAMS.values()]
    detecting = [c for c in per_cam if c["detect"]]

    class_counts = defaultdict(int)
    for c in per_cam:
        for name, n in c["class_counts"].items():
            class_counts[name] += n

    # Top-level numbers are the whole rig; "cameras" has the per-camera detail.
    return jsonify({
        **SETTINGS.snapshot(),
        "fps": round(sum(c["fps"] for c in per_cam), 2),
        "inference_ms": round(
            sum(c["inference_ms"] for c in detecting) / len(detecting), 2)
            if detecting else 0.0,
        "encode_ms": round(
            sum(c["encode_ms"] for c in per_cam) / len(per_cam), 2)
            if per_cam else 0.0,
        "frames": sum(c["frames"] for c in per_cam),
        "dropped": sum(c["dropped"] for c in per_cam),
        "capture_errors": sum(c["capture_errors"] for c in per_cam),
        "live_tracks": sum(c["live_tracks"] for c in per_cam),
        "total_tracks": sum(c["total_tracks"] for c in per_cam),
        "class_counts": dict(class_counts),
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "healthy": bool(per_cam) and all(c["healthy"] for c in per_cam),
        "model": os.path.basename(_hailo["path"] or "?"),
        "camera": ", ".join(f'{c["name"]} {c["resolution"]}@{c["framerate"]}'
                            for c in per_cam),
        "cameras": per_cam,
        "webhook": {"sent": HOOK.sent, "failed": HOOK.failed} if HOOK.enabled else None,
    })


# ---------------- events ----------------

@app.route("/events")
def events():
    return jsonify(EVENTS.recent(
        limit=int(request.args.get("limit", 50)),
        class_name=request.args.get("class"),
        since=request.args.get("since"),
        camera=request.args.get("camera"),
    ))


@app.route("/events/summary")
def events_summary():
    return jsonify(EVENTS.summary(since=request.args.get("since"),
                                  camera=request.args.get("camera")))


@app.route("/events.csv")
def events_csv():
    buf = io.StringIO()
    EVENTS.write_csv(buf, camera=request.args.get("camera"))
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=hailo-events.csv"})


# ---------------- monitoring ----------------

@app.route("/metrics")
def metrics():
    """Plain-text Prometheus exposition — point a scraper at it if you like.

    Every series carries a camera label. `sum by (class) (hailo_detections_total)`
    gets you the old single-camera number back.
    """
    per_cam = [c.stats_dict() for c in CAMS.values()]
    lines = []

    def block(name, help_text, mtype, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.extend(samples)

    def series(name, help_text, mtype, key, fmt="{:.3f}"):
        block(name, help_text, mtype,
              [f'{name}{{camera="{c["index"]}"}} ' + fmt.format(c[key])
               for c in per_cam])

    series("hailo_fps", "Frames per second through the pipeline", "gauge", "fps")
    series("hailo_inference_ms", "Mean NPU inference time", "gauge", "inference_ms")
    series("hailo_frames_total", "Frames processed", "counter", "frames", "{:d}")
    series("hailo_frames_dropped_total", "Frames dropped under backpressure",
           "counter", "dropped", "{:d}")
    series("hailo_capture_errors_total", "Camera pipeline restarts",
           "counter", "capture_errors", "{:d}")
    series("hailo_live_tracks", "Objects currently tracked", "gauge",
           "live_tracks", "{:d}")
    series("hailo_tracks_total", "Tracks confirmed since start", "counter",
           "total_tracks", "{:d}")
    series("hailo_camera_detecting", "1 when the NPU is running on this camera",
           "gauge", "detect", "{:d}")

    block("hailo_detections_total", "Confirmed tracks by class", "counter",
          [f'hailo_detections_total{{class="{name}",camera="{c["index"]}"}} {n}'
           for c in per_cam
           for name, n in sorted(c["class_counts"].items())])

    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.route("/healthz")
def healthz():
    per_cam = [c.stats_dict() for c in CAMS.values()]
    ok = bool(per_cam) and all(c["healthy"] for c in per_cam)
    return jsonify({
        "ok": ok,
        "fps": round(sum(c["fps"] for c in per_cam), 2),
        "cameras": {c["name"]: {"healthy": c["healthy"], "fps": c["fps"]}
                    for c in per_cam},
    }), (200 if ok else 503)


# ============================================================
# CLI
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(
        description="Real-time object detection and tracking on Hailo-8L.",
        epilog="Any CONFIG constant can also be set via an env var of the same name.")
    p.add_argument("--port", type=int, help="HTTP port (default 8080)")
    p.add_argument("--cameras",
                   help='which CSI cameras to run: "auto" (default), "0", "0,1"')
    p.add_argument("--detect-cameras",
                   help="comma-separated cameras to run the NPU on, e.g. 0,1 "
                        "(default: camera 0 only)")
    p.add_argument("--classes", help="comma-separated class filter, e.g. cat,dog")
    p.add_argument("--conf", type=float, help="confidence threshold 0-1")
    p.add_argument("--model", help="path to .hef")
    p.add_argument("--rotate", type=int, choices=[0, 90, 180, 270],
                   help="rotation for camera 0 (use CAMN_ROTATE for the rest)")
    p.add_argument("--width", type=int, help="capture width for camera 0")
    p.add_argument("--height", type=int, help="capture height for camera 0")
    p.add_argument("--fps", type=int, dest="framerate",
                   help="capture framerate for camera 0")
    p.add_argument("--no-track", action="store_true", help="disable ID tracking")
    p.add_argument("--no-events", action="store_true", help="disable the event log")
    p.add_argument("--snapshots", action="store_true", help="save a JPEG per new detection")
    p.add_argument("--webhook", help="POST detections to this URL")
    p.add_argument("--list-classes", action="store_true")
    p.add_argument("--list-cameras", action="store_true",
                   help="show what libcamera can see, then exit")
    return p.parse_args()


def apply_args(a):
    """CLI flags win over env, which wins over the defaults in this file.

    Camera flags are pushed back into os.environ rather than applied directly,
    because CameraConfig resolves from the environment — one precedence rule
    to reason about instead of two.
    """
    global HTTP_PORT, TRACK_ENABLED, CAMERAS_SPEC

    if a.port:
        HTTP_PORT = a.port
    if a.model:
        HEF_CANDIDATES.insert(0, a.model)
    if a.cameras:
        CAMERAS_SPEC = a.cameras
    if a.rotate is not None:
        os.environ["CAM_ROTATE"] = str(a.rotate)
    if a.width:
        os.environ["CAM_WIDTH"] = str(a.width)
    if a.height:
        os.environ["CAM_HEIGHT"] = str(a.height)
    if a.framerate:
        os.environ["CAM_FRAMERATE"] = str(a.framerate)
    if a.detect_cameras is not None:
        wanted = {c.strip() for c in a.detect_cameras.split(",") if c.strip()}
        for i in range(8):
            prefix = "CAM_" if i == 0 else f"CAM{i}_"
            os.environ[prefix + "DETECT"] = "1" if str(i) in wanted else "0"
    if a.no_track:
        TRACK_ENABLED = False
    if a.no_events:
        EVENTS.enabled = False
    if a.snapshots:
        SNAPSHOTS.enabled = True
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    if a.webhook:
        HOOK.url = a.webhook
        HOOK.enabled = True

    patch = {}
    if a.conf is not None:
        patch["conf_thresh"] = a.conf
    if a.classes is not None:
        patch["tracked_classes"] = [c.strip() for c in a.classes.split(",") if c.strip()]
    if patch:
        SETTINGS.apply(patch)


# ============================================================
# MAIN
# ============================================================
def local_ip():
    """gethostbyname returns 127.0.1.1 on Debian — ask the routing table instead."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


_shutdown_done = threading.Event()


def _shutdown_all(signum=None, _frame=None):
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()

    if signum:
        print(f"\n[main] signal {signum} — shutting down")
    _shutdown.set()

    for cam in CAMS.values():
        try:
            cam.shutdown()
        except Exception:
            pass

    EVENTS.close()          # flushes pending rows before we go
    HOOK.close()
    shutdown_hailo()

    if signum:
        # Flask's dev server has no clean stop from a signal handler, and
        # streaming /video responses keep worker threads alive indefinitely.
        # Everything that needed flushing is flushed, so leave hard — otherwise
        # systemd waits out its 90s TimeoutStopSec before SIGKILL on every
        # `systemctl restart`.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def main():
    args = parse_args()

    if args.list_classes:
        for i, name in enumerate(COCO_CLASSES):
            print(f"{i:3d}  {name}")
        return

    if args.list_cameras:
        found = detect_cameras()
        if not found:
            print("No cameras reported by libcamera.")
            return
        for i, sensor, modes in found:
            sizes = ", ".join(f"{w}x{h}" for w, h in modes) or "no modes listed"
            print(f"{i:3d}  {sensor:10s} {sizes}")
        return

    apply_args(args)

    signal.signal(signal.SIGTERM, _shutdown_all)
    signal.signal(signal.SIGINT, _shutdown_all)

    configs = build_camera_configs()
    init_hailo()
    start_cameras(configs)

    cfg = SETTINGS.snapshot()
    tracking = ", ".join(cfg["tracked_classes"]) if cfg["tracked_classes"] else "all 80 classes"

    print("\n  Hailo Tracker")
    print(f"  Model:    {os.path.basename(_hailo['path'])}")
    for c in configs:
        print(f"  {c.name}:     {c.describe()}")
    print(f"  Tracking: {tracking}  (conf >= {cfg['conf_thresh']:.0%})")
    print(f"  IDs:      {'on' if TRACK_ENABLED else 'off'}   "
          f"Events: {'on' if EVENTS.enabled else 'off'}   "
          f"Snapshots: {'on' if SNAPSHOTS.enabled else 'off'}")
    if ROI_NORM is not None:
        print(f"  ROI:      {len(ROI_NORM)}-point polygon active")
    print(f"\n  http://{local_ip()}:{HTTP_PORT}\n")

    try:
        app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True,
                use_reloader=False)
    finally:
        _shutdown_all()


if __name__ == "__main__":
    main()
