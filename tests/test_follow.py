#!/usr/bin/env python3
"""Unit tests for follow.py: steering/drive maths, target locking, stale-data
handling and the robot-linkd socket client. No camera or NPU needed.

Run from the project root:  python3 tests/test_follow.py
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from follow import Follower, FollowConfig, LinkClient, compute_command  # noqa: E402

W, H = 1280, 720


class T:
    def __init__(self, id, box, cls="cat", confirmed=True):
        self.id, self.box, self.class_name, self.confirmed = id, box, cls, confirmed


def box_at(cx, size_frac, w=W, h=H):
    """Square-ish box centred at normalised cx with the given area fraction."""
    side_w = (size_frac * w * h) ** 0.5
    x = cx * w
    return (x - side_w / 2, h / 2 - side_w / 2, x + side_w / 2, h / 2 + side_w / 2)


class FakeClient:
    connected = True
    def __init__(self): self.sent = []
    def send(self, obj): self.sent.append(obj); return True
    def close(self): pass


class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t


def cfg(**kw):
    c = FollowConfig(enabled=True, deadband=0.0, max_steer=1.0, max_drive=1.0)
    for k, v in kw.items():
        setattr(c, k, v)
    c.validate()
    return c


class ComputeTest(unittest.TestCase):
    def test_signs(self):
        c = cfg(target_size=0.10)
        x, y, *_ = compute_command(box_at(0.8, 0.10), W, H, c)
        self.assertGreater(x, 0, "target on the right must steer right")
        x, y, *_ = compute_command(box_at(0.2, 0.10), W, H, c)
        self.assertLess(x, 0, "target on the left must steer left")
        _, y, *_ = compute_command(box_at(0.5, 0.02), W, H, c)
        self.assertGreater(y, 0, "small (far) target must drive forward")
        _, y, *_ = compute_command(box_at(0.5, 0.40), W, H, c)
        self.assertLess(y, 0, "large (close) target must back off")
        x, y, *_ = compute_command(box_at(0.5, 0.10), W, H, c)
        self.assertAlmostEqual(x, 0, places=2); self.assertAlmostEqual(y, 0, places=2)

    def test_limits_and_turn_first(self):
        c = cfg(target_size=0.10, steer_gain=5, drive_gain=5, max_steer=0.4, max_drive=0.3)
        x, y, *_ = compute_command(box_at(0.99, 0.001), W, H, c)
        self.assertLessEqual(abs(x), 0.4)
        self.assertLess(y, 0.3 * 0.1, "drive is scaled down while the target is at the edge")


class FollowerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(); self.client = FakeClient()
        self.f = Follower(cfg(classes=("cat",)), client=self.client, clock=self.clock, log=lambda m: None)

    def test_locks_largest_then_keeps_lock(self):
        big, small = T(1, box_at(0.3, 0.05)), T(2, box_at(0.7, 0.01))
        self.f.update(0, [small, big], W, H)
        self.assertEqual(self.f.locked_id, 1)
        # Track 2 grows past track 1: stay on the locked one.
        self.f.update(0, [T(1, box_at(0.3, 0.05)), T(2, box_at(0.7, 0.20))], W, H)
        self.assertEqual(self.f.locked_id, 1)
        # Locked track gone: pick again.
        self.f.update(0, [T(2, box_at(0.7, 0.20))], W, H)
        self.assertEqual(self.f.locked_id, 2)

    def test_ignores_other_classes_unconfirmed_and_other_cameras(self):
        self.f.update(0, [T(1, box_at(0.5, 0.05), cls="person"),
                          T(2, box_at(0.5, 0.05), confirmed=False)], W, H)
        self.assertIsNone(self.f.target)
        self.f.update(1, [T(3, box_at(0.5, 0.05))], W, H)
        self.assertIsNone(self.f.target)

    def test_stale_detections_hold_still(self):
        self.f.update(0, [T(1, box_at(0.9, 0.01))], W, H)
        self.assertNotEqual(self.f.current(), (0.0, 0.0))
        self.clock.t += 1.0                         # camera stalled
        self.assertEqual(self.f.current(), (0.0, 0.0))

    def test_disable_sends_one_stop_then_nothing(self):
        self.f.update(0, [T(1, box_at(0.9, 0.01))], W, H)
        self.f.tick()
        self.f.apply({"enabled": False})
        self.f.tick(); self.f.tick()
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual((self.client.sent[1]["x"], self.client.sent[1]["y"]), (0.0, 0.0))
        self.assertEqual(self.client.sent[0]["op"], "drive")

    def test_apply_validates(self):
        changed = self.f.apply({"max_drive": "0.25", "classes": "Dog, cat", "ttl_ms": 99999,
                                "socket_path": "/etc/passwd", "bogus": 1})
        self.assertEqual(self.f.cfg.max_drive, 0.25)
        self.assertEqual(self.f.cfg.classes, ("dog", "cat"))
        self.assertEqual(self.f.cfg.ttl_ms, 1000)
        self.assertNotIn("socket_path", changed)


class LinkClientTest(unittest.TestCase):
    def test_sends_lines_and_never_blocks_without_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pi.sock")
            client = LinkClient(path, retry_s=0)
            t0 = time.monotonic()
            self.assertFalse(client.send({"op": "drive", "x": 0, "y": 0}))
            self.assertLess(time.monotonic() - t0, 0.5)

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path); srv.listen(1)
            got = []
            def serve():
                conn, _ = srv.accept()
                buf = b""
                while len(got) < 2:
                    buf += conn.recv(4096)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        got.append(json.loads(line))
                conn.close()
            th = threading.Thread(target=serve); th.start()
            self.assertTrue(client.send({"op": "drive", "x": 0.1, "y": 0.2}))
            self.assertTrue(client.send({"op": "drive", "x": 0.3, "y": 0.4}))
            th.join(2); client.close(); srv.close()
            self.assertEqual([g["x"] for g in got], [0.1, 0.3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
