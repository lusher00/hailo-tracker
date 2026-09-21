"""Target following: turn confirmed tracks into a drive command for the Bone.

The Pi only proposes. Commands go to robot-linkd's local socket, which sends
them to the Bone as DRIVE_COMMAND. balance_bot keeps balancing regardless,
applies them only while its Pi-drive gate is open and the SBUS stick is
centred, and treats an expired command as a centred stick. So you can drive
up to something by hand, let go of the stick, and the Pi carries on; and this
module can stop, crash or restart at any time without leaving the robot moving.

Conventions (shared with robot-link and balance_bot):
    x  steer, -1..1, positive turns right
    y  drive, -1..1, positive is forward

Target choice: the locked track while it lasts, otherwise the largest
confirmed track of an allowed class (largest ~ nearest, which is the one you
just drove up to). Following is off until enabled.
"""

import json
import math
import socket
import threading
import time
from dataclasses import asdict, dataclass, fields


@dataclass
class FollowConfig:
    enabled: bool = False
    classes: tuple = ()           # empty = any class
    camera: int = 0
    target_size: float = 0.10     # box area / frame area to hold station at
    steer_gain: float = 1.0
    drive_gain: float = 1.0
    max_steer: float = 0.5
    max_drive: float = 0.3
    deadband: float = 0.05
    rate_hz: float = 10.0
    ttl_ms: int = 300
    lost_s: float = 0.5           # no fresh detections for this long = no target
    socket_path: str = "/run/robot-link/pi.sock"

    @classmethod
    def from_env(cls, env):
        c = cls()
        c.enabled = env("FOLLOW_ENABLED", c.enabled, bool)
        c.classes = _classes(env("FOLLOW_CLASSES", ""))
        c.camera = env("FOLLOW_CAMERA", c.camera, int)
        c.target_size = env("FOLLOW_TARGET_SIZE", c.target_size, float)
        c.steer_gain = env("FOLLOW_STEER_GAIN", c.steer_gain, float)
        c.drive_gain = env("FOLLOW_DRIVE_GAIN", c.drive_gain, float)
        c.max_steer = env("FOLLOW_MAX_STEER", c.max_steer, float)
        c.max_drive = env("FOLLOW_MAX_DRIVE", c.max_drive, float)
        c.deadband = env("FOLLOW_DEADBAND", c.deadband, float)
        c.rate_hz = env("FOLLOW_RATE_HZ", c.rate_hz, float)
        c.ttl_ms = env("FOLLOW_TTL_MS", c.ttl_ms, int)
        c.lost_s = env("FOLLOW_LOST_S", c.lost_s, float)
        c.socket_path = env("ROBOT_LINK_SOCKET", c.socket_path)
        c.validate()
        return c

    def validate(self):
        self.camera = int(self.camera)
        self.target_size = min(max(float(self.target_size), 0.001), 1.0)
        self.max_steer = min(max(float(self.max_steer), 0.0), 1.0)
        self.max_drive = min(max(float(self.max_drive), 0.0), 1.0)
        self.deadband = min(max(float(self.deadband), 0.0), 0.5)
        self.rate_hz = min(max(float(self.rate_hz), 1.0), 50.0)
        self.ttl_ms = min(max(int(self.ttl_ms), 50), 1000)
        self.lost_s = min(max(float(self.lost_s), 0.1), 5.0)
        self.steer_gain = float(self.steer_gain)
        self.drive_gain = float(self.drive_gain)

    def public(self):
        d = asdict(self)
        d["classes"] = list(self.classes)
        return d


def _classes(value):
    if isinstance(value, str):
        value = value.split(",")
    return tuple(c.strip().lower() for c in value if str(c).strip())


def _clamp(v, lim):
    return max(-lim, min(lim, v))


def _deadband(v, band):
    return 0.0 if abs(v) < band else v


def compute_command(box, frame_w, frame_h, cfg):
    """Normalised (x, y) for one target box (pixels, x1 y1 x2 y2)."""
    x1, y1, x2, y2 = box
    # Horizontal offset of the box centre: -1 at the left edge, +1 at the right.
    err_x = ((x1 + x2) / 2.0) / frame_w * 2.0 - 1.0
    # Apparent size falls off as 1/distance, so compare linear size (sqrt of
    # area): 0 at the target size, +1 when tiny/far, negative when too close.
    area = max(0.0, (x2 - x1) * (y2 - y1)) / float(frame_w * frame_h)
    err_d = 1.0 - math.sqrt(area / cfg.target_size)
    steer = _clamp(cfg.steer_gain * _deadband(err_x, cfg.deadband), cfg.max_steer)
    drive = _clamp(cfg.drive_gain * _deadband(err_d, cfg.deadband), cfg.max_drive)
    # Turn toward it before driving at it: full drive when centred, none at the edge.
    drive *= max(0.0, 1.0 - abs(err_x))
    return round(steer, 4), round(drive, 4), err_x, area


class LinkClient:
    """Fire-and-forget line writer to robot-linkd's local socket. Never blocks
    for long: a slow or partial write drops the connection (keeping the line
    framing intact) and reconnects are limited to one attempt a second."""

    def __init__(self, path, timeout_s=0.05, retry_s=1.0):
        self.path = path
        self.timeout_s = timeout_s
        self.retry_s = retry_s
        self.sock = None
        self.next_try = 0.0

    @property
    def connected(self):
        return self.sock is not None

    def send(self, obj):
        if self.sock is None:
            now = time.monotonic()
            if now < self.next_try:
                return False
            self.next_try = now + self.retry_s
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout_s)
            try:
                s.connect(self.path)
            except OSError:
                s.close()
                return False
            self.sock = s
        try:
            self.sock.sendall(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
            return True
        except OSError:
            self.close()
            return False

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class Follower:
    def __init__(self, cfg, client=None, clock=time.monotonic, log=print):
        self.cfg = cfg
        self.client = client or LinkClient(cfg.socket_path)
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.locked_id = None
        self.target = None          # dict describing the current target
        self.command = (0.0, 0.0)
        self.updated_at = None      # clock() of the last detection update
        self.sent = 0
        self.dropped = 0
        self._was_enabled = False

    # -- called from the camera render loop, once per processed frame --------
    def update(self, camera, tracks, frame_w, frame_h):
        cfg = self.cfg
        if camera != cfg.camera or not cfg.enabled:
            return
        candidates = [t for t in tracks
                      if t.confirmed and (not cfg.classes or t.class_name.lower() in cfg.classes)]
        with self._lock:
            chosen = next((t for t in candidates if t.id == self.locked_id), None)
            if chosen is None and candidates:
                chosen = max(candidates, key=lambda t: (t.box[2] - t.box[0]) * (t.box[3] - t.box[1]))
                if chosen.id != self.locked_id:
                    self.log(f"[follow] locked on {chosen.class_name} #{chosen.id}")
                self.locked_id = chosen.id
            if chosen is None:
                self.target = None
                self.command = (0.0, 0.0)
            else:
                x, y, err_x, area = compute_command(chosen.box, frame_w, frame_h, cfg)
                self.target = {"id": chosen.id, "class": chosen.class_name,
                               "offset": round(err_x, 3), "size": round(area, 4)}
                self.command = (x, y)
            self.updated_at = self.clock()

    # -- what to send right now ------------------------------------------------
    def current(self):
        """(x, y) to send now, or None to send nothing."""
        with self._lock:
            if not self.cfg.enabled:
                if self._was_enabled:
                    self._was_enabled = False
                    return (0.0, 0.0)       # one explicit stop when switched off
                return None
            self._was_enabled = True
            fresh = self.updated_at is not None and self.clock() - self.updated_at <= self.cfg.lost_s
            if not fresh:
                self.target = None
                self.locked_id = None
                return (0.0, 0.0)           # camera stalled or detection off: hold
            return self.command

    def tick(self):
        cmd = self.current()
        if cmd is None:
            return
        ok = self.client.send({"op": "drive", "x": cmd[0], "y": cmd[1], "ttl_ms": self.cfg.ttl_ms})
        with self._lock:
            if ok:
                self.sent += 1
            else:
                self.dropped += 1

    def _run(self):
        while not self._stop.wait(1.0 / self.cfg.rate_hz):
            try:
                self.tick()
            except Exception as e:      # never let the sender thread die
                self.log(f"[follow] {e}")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="follow", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self.cfg.enabled:
            self.client.send({"op": "drive", "x": 0.0, "y": 0.0, "ttl_ms": self.cfg.ttl_ms})
        self.client.close()

    # -- web API -------------------------------------------------------------
    def apply(self, data):
        """Update config from a dict (POST /api/follow). Returns changed keys."""
        changed = []
        with self._lock:
            names = {f.name for f in fields(FollowConfig)} - {"socket_path"}
            for key, value in (data or {}).items():
                if key not in names:
                    continue
                if key == "classes":
                    value = _classes(value)
                elif key == "enabled":
                    value = value in (True, 1, "1", "true", "on", "yes")
                if getattr(self.cfg, key) != value:
                    setattr(self.cfg, key, value)
                    changed.append(key)
            self.cfg.validate()
            if "classes" in changed or "camera" in changed or "enabled" in changed:
                self.locked_id = None
        return changed

    def status(self):
        with self._lock:
            age = None if self.updated_at is None else round(self.clock() - self.updated_at, 3)
            return {"enabled": self.cfg.enabled, "link_connected": self.client.connected,
                    "target": self.target, "command": {"x": self.command[0], "y": self.command[1]},
                    "detection_age_s": age, "sent": self.sent, "dropped": self.dropped,
                    "config": self.cfg.public()}
