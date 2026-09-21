"""Stand-in for hailo_platform so the pipeline can be exercised off-device.

The fake 'NPU' genuinely locates the synthetic objects in the letterboxed frame
it is handed and returns normalised boxes, so the letterbox -> normalise ->
un-letterbox round trip is actually under test. If that maths is wrong, the
drawn boxes land in the wrong place in the output JPEG and the test fails.
"""

import numpy as np


class HailoStreamInterface:
    PCIe = "pcie"


class _Info:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape


class HEF:
    def __init__(self, path):
        self.path = path


class ConfigureParams:
    @staticmethod
    def create_from_hef(hef, interface):
        return {"fake": True}


class _Activated:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class NetworkGroup:
    def create_params(self):
        return {}

    def activate(self, params):
        return _Activated()

    def get_input_vstream_infos(self):
        return [_Info("yolov8s/input_layer1", (640, 640, 3))]

    def get_output_vstream_infos(self):
        return [_Info("yolov8s/yolov8_nms_postprocess", (80, 5))]


class VDevice:
    def configure(self, hef, params):
        return [NetworkGroup()]


class InputVStreamParams:
    @staticmethod
    def make_from_network_group(ng):
        return {}


class OutputVStreamParams:
    @staticmethod
    def make_from_network_group(ng):
        return {}


# Synthetic objects are drawn in these exact BGR colours by fake_rpicam_vid.py
TARGETS = {
    15: (0, 0, 220),      # "cat"    — red block
    0:  (220, 0, 0),      # "person" — blue block
}


class InferVStreams:
    def __init__(self, ng, in_params, out_params):
        self.ng = ng
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def infer(self, feed):
        self.calls += 1
        frame = list(feed.values())[0][0]        # (640, 640, 3) uint8 BGR
        size = frame.shape[0]

        per_class = [np.zeros((0, 5), dtype=np.float32) for _ in range(80)]

        for class_id, bgr in TARGETS.items():
            diff = np.abs(frame.astype(np.int16) - np.array(bgr, dtype=np.int16))
            mask = diff.sum(axis=2) < 110
            ys, xs = np.nonzero(mask)
            if xs.size < 30:
                continue

            x1, x2 = xs.min() / size, xs.max() / size
            y1, y2 = ys.min() / size, ys.max() / size
            conf = 0.93 if class_id == 15 else 0.81
            # Same layout as the real HailoRT NMS output: y first.
            per_class[class_id] = np.array([[y1, x1, y2, x2, conf]], dtype=np.float32)

        return {"yolov8s/yolov8_nms_postprocess": [per_class]}
