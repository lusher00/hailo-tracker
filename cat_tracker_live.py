#!/usr/bin/env python3
import sys
sys.path.insert(0, '/usr/lib/aarch64-linux-gnu/python3.12/site-packages')

import cv2
import numpy as np
import threading
import queue
import subprocess
from flask import Flask, Response, render_template_string

from hailo_platform import (VDevice, HEF, ConfigureParams, InferVStreams,
                             InputVStreamParams, OutputVStreamParams,
                             HailoStreamInterface)

# ============================================================
# CONFIG
# ============================================================
HTTP_PORT   = 8080
NN_SIZE     = 640       # YOLOv8 input is 640×640
CONF_THRESH = 0.40

# Set to None to disable, or cv2.ROTATE_90_CLOCKWISE /
# cv2.ROTATE_90_COUNTERCLOCKWISE / cv2.ROTATE_180
ROTATE = cv2.ROTATE_90_CLOCKWISE

app = Flask(__name__)
frame_q = queue.Queue(maxsize=2)

HTML = '''<!DOCTYPE html><html><head><title>Live Cat Tracker</title>
<style>body{margin:0;background:#000;display:flex;justify-content:center;align-items:center;height:100vh;}
img{max-width:95vw;max-height:95vh;border:3px solid #0f0;}</style></head>
<body><img src="/video"/></body></html>'''

# ============================================================
# HAILO INIT
# ============================================================
hef_obj              = None
target               = None
network_group        = None
input_vstreams_params  = None
output_vstreams_params = None
network_group_params = None
activated_network    = None
infer_pipeline_ctx   = None
input_name           = None

def init_hailo():
    global hef_obj, target, network_group
    global input_vstreams_params, output_vstreams_params, network_group_params
    global activated_network, infer_pipeline_ctx, input_name

    hef_obj = HEF("yolov8s.hef")
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
    print(f"✅ Hailo ready!  HEF input shape: {info.shape}")


def run_inference(nn_frame):
    """Run YOLOv8 on a letterboxed 640×640 frame."""
    img_array = np.expand_dims(nn_frame, axis=0)
    output = infer_pipeline_ctx.infer({input_name: img_array})
    return output['yolov8s/yolov8_nms_postprocess'][0]   # [num_classes][N, 5]


# ============================================================
# LETTERBOX  — fit frame into 640×640 with padding, no squish
# ============================================================
def letterbox(frame, target=NN_SIZE):
    h, w    = frame.shape[:2]
    scale   = target / max(h, w)
    nh, nw  = int(h * scale), int(w * scale)
    resized = cv2.resize(frame, (nw, nh))
    canvas  = np.zeros((target, target, 3), dtype=np.uint8)
    pad_y   = (target - nh) // 2
    pad_x   = (target - nw) // 2
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
        "--output",         "-"
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

        # Software rotation — adjust ROTATE at the top of the file if needed
        if ROTATE is not None:
            frame = cv2.rotate(frame, ROTATE)

        nn_frame, scale, pad_x, pad_y = letterbox(frame)
        detections = run_inference(nn_frame)

        if not frame_q.full():
            frame_q.put((frame, detections, scale, pad_x, pad_y))


# ============================================================
# MJPEG STREAM
# ============================================================
def mjpeg_generator():
    while True:
        frame, detections, scale, pad_x, pad_y = frame_q.get()

        # detections: [num_classes][N, 5]  — coords are 0..1 relative to 640×640 canvas
        for class_id, class_dets in enumerate(detections):
            for det in class_dets:
                try:
                    det  = np.array(det).reshape(-1)
                    if det.size < 5:
                        continue

                    x1, y1, x2, y2, conf = det[:5]
                    conf = float(conf)

                    if conf < CONF_THRESH:
                        continue

                    # Convert from 0..1 canvas coords -> pixel coords in original frame
                    x1 = int((x1 * NN_SIZE - pad_x) / scale)
                    y1 = int((y1 * NN_SIZE - pad_y) / scale)
                    x2 = int((x2 * NN_SIZE - pad_x) / scale)
                    y2 = int((y2 * NN_SIZE - pad_y) / scale)

                    color = (0, 255, 0) if class_id == 15 else (100, 100, 255)
                    label = "CAT" if class_id == 15 else "obj"

                    cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
                    cv2.putText(frame, f"{label} {conf:.0%}",
                                (x1, y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

                except Exception as e:
                    print("[Parse Error]", e)

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
    return render_template_string(HTML)

@app.route("/video")
def video():
    return Response(mjpeg_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == "__main__":
    init_hailo()
    threading.Thread(target=capture_loop, daemon=True).start()
    print("\n🎉 Hailo Cat Tracker is live!")
    print(f"   http://192.168.1.139:{HTTP_PORT}\n")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
