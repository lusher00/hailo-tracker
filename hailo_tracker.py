#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com
"""
hailo_tracker.py — Real-time object detection on Raspberry Pi 5 + Hailo-8L
Streams annotated MJPEG video over HTTP. Tracks all 80 COCO classes by default;
edit TRACKED_CLASSES to limit to specific objects.
"""

import cv2
import numpy as np
import threading
import queue
import subprocess
import socket
from flask import Flask, Response, render_template_string

from hailo_platform import (VDevice, HEF, ConfigureParams, InferVStreams,
                             InputVStreamParams, OutputVStreamParams,
                             HailoStreamInterface)

# ============================================================
# CONFIG — edit these to taste
# ============================================================
HTTP_PORT   = 8080
HEF_PATH    = "yolov8s.hef"
CONF_THRESH = 0.40
NN_SIZE     = 640       # YOLOv8 input size (don't change unless you swap models)

# Which classes to draw boxes around.
# Empty list = show everything (all 80 COCO classes).
# Example: TRACKED_CLASSES = ["cat", "dog", "person"]
TRACKED_CLASSES = []

# Camera rotation. Set to None to disable, or one of:
#   cv2.ROTATE_90_CLOCKWISE
#   cv2.ROTATE_90_COUNTERCLOCKWISE
#   cv2.ROTATE_180
ROTATE = cv2.ROTATE_90_CLOCKWISE

# ============================================================
# COCO CLASS LIST (80 classes, index = class_id)
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

# Build lookup: class name -> id, and the active set of ids to draw
_CLASS_NAME_TO_ID = {name: i for i, name in enumerate(COCO_CLASSES)}

def _resolve_tracked_ids():
    if not TRACKED_CLASSES:
        return set(range(len(COCO_CLASSES)))  # all classes
    ids = set()
    for name in TRACKED_CLASSES:
        if name in _CLASS_NAME_TO_ID:
            ids.add(_CLASS_NAME_TO_ID[name])
        else:
            print(f"[WARN] Unknown class '{name}' — ignored. Check COCO_CLASSES list.")
    return ids

TRACKED_IDS = _resolve_tracked_ids()

# Assign a consistent BGR color to each class via hashing
def _class_color(class_id):
    np.random.seed(class_id + 42)
    return tuple(int(x) for x in np.random.randint(80, 255, size=3))

CLASS_COLORS = [_class_color(i) for i in range(len(COCO_CLASSES))]

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)
frame_q: queue.Queue = queue.Queue(maxsize=2)

def _tracked_class_list_html():
    if not TRACKED_CLASSES:
        return "<em>All 80 COCO classes</em>"
    return ", ".join(
        f'<span style="color:{_css_color(i)}">{name}</span>'
        for name, i in _CLASS_NAME_TO_ID.items()
        if i in TRACKED_IDS
    )

def _css_color(class_id):
    b, g, r = CLASS_COLORS[class_id]
    return f"rgb({r},{g},{b})"

HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Hailo Tracker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #111; color: #eee; font-family: monospace; }
    header { padding: 10px 16px; background: #1a1a1a; border-bottom: 1px solid #333;
             display: flex; align-items: center; gap: 16px; }
    header h1 { font-size: 1rem; color: #0f0; }
    header .meta { font-size: 0.75rem; color: #888; }
    .stream-wrap { display: flex; justify-content: center; align-items: flex-start;
                   padding: 12px; }
    img { max-width: 100%; max-height: 90vh; border: 2px solid #333; border-radius: 4px; }
    footer { padding: 8px 16px; font-size: 0.7rem; color: #555; text-align: center; }
  </style>
</head>
<body>
  <header>
    <h1>&#x1F4F9; Hailo Tracker</h1>
    <span class="meta">Tracking: """ + "{{ tracked }}" + """</span>
  </header>
  <div class="stream-wrap">
    <img src="/video" alt="Live feed"/>
  </div>
  <footer>YOLOv8s &bull; Hailo-8L &bull; """ + "{{ port }}" + """</footer>
</body>
</html>"""

# ============================================================
# HAILO INIT
# ============================================================
hef_obj = target = network_group = None
input_vstreams_params = output_vstreams_params = network_group_params = None
activated_network = infer_pipeline_ctx = input_name = None

def init_hailo():
    global hef_obj, target, network_group
    global input_vstreams_params, output_vstreams_params, network_group_params
    global activated_network, infer_pipeline_ctx, input_name

    print(f"[hailo] Loading {HEF_PATH} ...")
    hef_obj = HEF(HEF_PATH)
    target  = VDevice()
    configure_params = ConfigureParams.create_from_hef(hef_obj, HailoStreamInterface.PCIe)
    network_group    = target.configure(hef_obj, configure_params)[0]
    network_group_params = network_group.create_params()

    input_vstreams_params  = InputVStreamParams.make_from_network_group(network_group)
    output_vstreams_params = OutputVStreamParams.make_from_network_group(network_group)

    activated_network  = network_group.activate(network_group_params)
    activated_network.__enter__()
    infer_pipeline_ctx = InferVStreams(network_group, input_vstreams_params, output_vstreams_params)
    infer_pipeline_ctx.__enter__()

    input_name = network_group.get_input_vstream_infos()[0].name
    info = network_group.get_input_vstream_infos()[0]
    print(f"[hailo] Ready — input shape: {info.shape}")


def run_inference(nn_frame):
    img_array = np.expand_dims(nn_frame, axis=0)
    output = infer_pipeline_ctx.infer({input_name: img_array})
    return output['yolov8s/yolov8_nms_postprocess'][0]  # [num_classes][N, 5]


# ============================================================
# LETTERBOX — fit frame into NN_SIZE square with padding
# ============================================================
def letterbox(frame, target_size=NN_SIZE):
    h, w   = frame.shape[:2]
    scale  = target_size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(frame, (nw, nh))
    canvas  = np.zeros((target_size, target_size, 3), dtype=np.uint8)
    pad_y   = (target_size - nh) // 2
    pad_x   = (target_size - nw) // 2
    canvas[pad_y:pad_y+nh, pad_x:pad_x+nw] = resized
    return canvas, scale, pad_x, pad_y


# ============================================================
# CAMERA + INFERENCE THREAD
# ============================================================
def capture_loop():
    cmd = [
        "rpicam-vid",
        "--codec",          "mjpeg",
        "--inline",
        "--nopreview",
        "--width",          "1920",
        "--height",         "1080",
        "--framerate",      "30",
        "--denoise",        "off",
        "--autofocus-mode", "manual",
        "--flush",
        "--timeout",        "0",
        "--output",         "-",
    ]

    proc   = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    buffer = b""

    while True:
        chunk = proc.stdout.read(16384)
        if not chunk:
            continue
        buffer += chunk

        start = buffer.find(b'\xff\xd8')
        end   = buffer.find(b'\xff\xd9')

        if start == -1 or end == -1 or end <= start:
            continue

        jpg    = buffer[start:end+2]
        buffer = buffer[end+2:]

        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            continue

        if ROTATE is not None:
            frame = cv2.rotate(frame, ROTATE)

        nn_frame, scale, pad_x, pad_y = letterbox(frame)
        detections = run_inference(nn_frame)

        if not frame_q.full():
            frame_q.put((frame, detections, scale, pad_x, pad_y))


# ============================================================
# MJPEG GENERATOR
# ============================================================
def mjpeg_generator():
    while True:
        frame, detections, scale, pad_x, pad_y = frame_q.get()

        for class_id, class_dets in enumerate(detections):
            if class_id not in TRACKED_IDS:
                continue

            label = COCO_CLASSES[class_id] if class_id < len(COCO_CLASSES) else str(class_id)
            color = CLASS_COLORS[class_id]

            for det in class_dets:
                try:
                    det = np.array(det).reshape(-1)
                    if det.size < 5:
                        continue

                    x1, y1, x2, y2, conf = det[:5]
                    conf = float(conf)
                    if conf < CONF_THRESH:
                        continue

                    # Canvas coords (0..1) → original frame pixels
                    x1 = int((x1 * NN_SIZE - pad_x) / scale)
                    y1 = int((y1 * NN_SIZE - pad_y) / scale)
                    x2 = int((x2 * NN_SIZE - pad_x) / scale)
                    y2 = int((y2 * NN_SIZE - pad_y) / scale)

                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                    cv2.putText(frame, f"{label} {conf:.0%}",
                                (x1, max(y1 - 8, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

                except Exception as e:
                    print(f"[parse error] {e}")

        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' +
               jpg.tobytes() +
               b'\r\n')


# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template_string(
        HTML,
        tracked=_tracked_class_list_html(),
        port=HTTP_PORT,
    )

@app.route("/video")
def video():
    return Response(mjpeg_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    init_hailo()
    threading.Thread(target=capture_loop, daemon=True).start()

    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "localhost"

    tracking = ", ".join(TRACKED_CLASSES) if TRACKED_CLASSES else "all classes"
    print(f"\n  Hailo Tracker running")
    print(f"  Tracking: {tracking}")
    print(f"  http://{local_ip}:{HTTP_PORT}\n")

    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
