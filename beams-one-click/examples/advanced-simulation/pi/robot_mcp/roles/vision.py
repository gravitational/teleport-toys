"""Vision node: returns a camera frame as MCP image content so the agent can look.

Real mode tries, in order: fswebcam, ffmpeg (USB webcam at $CAMERA_DEV, default /dev/video0).
USB webcams are far less painful than the Pi Camera on 32-bit Arch.
"""
import base64
import os
import random
import shutil
import subprocess
import tempfile

from ..hw.png import sim_scene
from ..registry import ToolError, tool

SIM = os.environ.get("ROBOT_SIM", "1") == "1"
DEV = os.environ.get("CAMERA_DEV", "/dev/video0")


def _capture_real(w, h):
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        if shutil.which("fswebcam"):
            cmd = ["fswebcam", "-q", "-d", DEV, "-r", f"{w}x{h}", "--no-banner", "-S", "5", f.name]
        elif shutil.which("ffmpeg"):
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "v4l2", "-video_size", f"{w}x{h}",
                   "-i", DEV, "-frames:v", "1", "-q:v", "5", f.name]
        else:
            raise ToolError("no capture tool: pacman -S ffmpeg (or install fswebcam)")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise ToolError(f"capture failed: {r.stderr.strip()[:300]}")
        return f.read(), "image/jpeg"


@tool("sense_capture_image",
      "Take a photo with the robot's camera and return it as an image you can look at.",
      {"width": {"type": "integer", "minimum": 64, "maximum": 1280, "default": 640},
       "height": {"type": "integer", "minimum": 48, "maximum": 960, "default": 480}})
def capture(width=640, height=480):
    if SIM:
        w, h = min(width, 320), min(height, 240)  # keep the pure-python encoder fast on a Pi 3
        tx, ty = random.randint(0, w - w // 10), random.randint(h // 3, h - h // 10)
        data, mime = sim_scene(w, h, (tx, ty)), "image/png"
        note = f"SIMULATED frame {w}x{h}: red target block near pixel ({tx},{ty})."
    else:
        data, mime = _capture_real(width, height)
        note = f"Camera frame {width}x{height} from {DEV}."
    return [
        {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": mime},
        {"type": "text", "text": note},
    ]
