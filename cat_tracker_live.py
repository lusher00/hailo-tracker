#!/usr/bin/env python3
import cv2
import numpy as np
import threading
import queue
from flask import Flask, Response

HTTP_PORT   = 5000
NN_WIDTH    = 640
NN_HEIGHT   = 640
CONF_THRESH = 0.40

app = Flask(__name__)
frame_q = queue.Queue(maxsize=2)

# ============================================================
# PUT YOUR EXISTING HAILO INFERENCE CODE INSIDE THIS FUNCTION
# ============================================================
def run_inference(frame):
    """
    Replace this with your working infer_pipeline call.
    Must return iterable of detections.
    Each detection expected as:
        [x1,y1,x2,y2,conf,class]
    """

    # ---- example stub ----
    detections = []
    return detections


# ============================================================
# CAMERA + INFERENCE THREAD
# ============================================================
def capture_loop():
    import subprocess

    cmd = [
        "rpicam-vid",
        "--codec", "mjpeg",
        "--inline",
        "--nopreview",
        "--width", "1920",
        "--height", "1080",
        "--framerate", "30",

        # ---- critical fixes ----
        "--denoise", "off",          # avoids ISP buffering stalls
        "--autofocus-mode", "manual",
        "--flush",                   # force frame delivery
        "--timeout", "0",            # run forever
        "--output", "-"              # stdout pipe
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)

    buffer = b""

    while True:
        chunk = proc.stdout.read(16384)
        if not chunk:
            continue
        buffer += chunk
        
        start = buffer.find(b'\xff\xd8')
        end   = buffer.find(b'\xff\xd9')

        if start != -1 and end != -1:
            jpg = buffer[start:end+2]
            buffer = buffer[end+2:]

            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)

            if frame is None:
                continue

            detections = run_inference(frame)

            if not frame_q.full():
                frame_q.put((frame, detections))


# ============================================================
# MJPEG STREAM
# ============================================================
def mjpeg_generator():
    while True:
        frame, detections = frame_q.get()

        # ---- FIX #1: rotate image 90° clockwise
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        h, w = frame.shape[:2]
        sx = w / NN_WIDTH
        sy = h / NN_HEIGHT

        # ---- FIX #2: handle Hailo tensor shape safely
        for det in detections:
            try:
                det = np.array(det).reshape(-1)

                if det.size < 6:
                    continue

                x1, y1, x2, y2, conf, cls = det[:6]

                conf = float(conf)
                cls  = int(cls)

                if conf < CONF_THRESH:
                    continue

                x1 = int(x1 * sx)
                y1 = int(y1 * sy)
                x2 = int(x2 * sx)
                y2 = int(y2 * sy)

                cv2.rectangle(frame, (x1,y1),(x2,y2),(0,255,0),2)
                cv2.putText(frame,f"{conf:.2f}",
                            (x1,y1-6),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,(0,255,0),1)

            except Exception as e:
                print("[Parse Error]", e)

        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY),80])
        if not ok:
            continue

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' +
               jpg.tobytes() +
               b'\r\n')


@app.route("/")
def index():
    return "<html><body style='margin:0;background:black;'><img src='/video' style='width:100%;'></body></html>"

@app.route("/video")
def video():
    return Response(mjpeg_generator(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == "__main__":
    threading.Thread(target=capture_loop, daemon=True).start()
    print(f"Streaming on http://0.0.0.0:{HTTP_PORT}")
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)
