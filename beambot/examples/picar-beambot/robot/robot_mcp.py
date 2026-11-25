"""
BeamBot robot server — runs ON the PiCar-X (Raspberry Pi).

One process, two ports on 127.0.0.1, so Teleport can grant them separately:
  :8765  /mcp          Streamable-HTTP MCP server (drive, look, stop, resume, snapshot, get_telemetry, say)
                       published as the MCP app `picar-mcp`; Teleport filters its tools per role
  :8766  /video.mjpg   Live MJPEG camera stream          (for the Beam app's VIDEO panel)
         /snapshot.jpg  One JPEG frame
         /telemetry    JSON: pose, ultrasonic, grayscale  (for the DATA + OFFICE MAP panels)
         /stop         POST: emergency stop. Stops the motors AND latches: `drive` is refused until /resume
         /resume       POST: clear the latch (a deliberate act: give this app only to the dashboard's beam)
         /release      POST: plain stop that does not latch (drive-pad release, end of an auto-map run)
                       published as the plain HTTP app `picar-stream`. There is deliberately no /mcp here:
                       `mcp.tools` in a Teleport role only filters MCP apps, so anything on this port is
                       reachable by whoever may open the app. It can watch and stop, never drive.

Teleport's Application Service on the Pi publishes both ports into the cluster.
Nothing listens on a public interface.

The emergency stop is a latch that lives HERE, on the robot, not in whichever client pressed STOP: a drive request
that was already on the wire, or one from a different client (Claude Code in another beam), is refused too. Only
/resume or the `resume` tool clears it, and the default viewer/driver roles do not grant that tool.

Safety on the robot, whoever calls: speed and duration are clamped, forward motion needs a fresh valid
ultrasonic reading, every drive stops at its own deadline, the control loop survives hardware errors
and retries the stop, and the motors are zeroed at startup and on SIGTERM / exit.

Run on the robot:      python3 robot_mcp.py            (SunFounder picarx + picamera2)
                       HW=hat python3 robot_mcp.py     (robot_hat_lite.py + libcamera/GStreamer, no SunFounder libs)
Run without hardware:  SIMULATE=1 python3 robot_mcp.py
"""
import asyncio
import atexit
import io
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP, Image
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

SIMULATE = os.environ.get("SIMULATE") == "1"
HW = "sim" if SIMULATE else os.environ.get("HW", "picarx")   # picarx | hat
HOST = os.environ.get("ROBOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ROBOT_PORT", "8765"))                # /mcp only
STREAM_PORT = int(os.environ.get("STREAM_PORT", "8766"))        # video, telemetry, stop: no /mcp

# ---- Safety limits (enforced here, on the robot, regardless of who calls) ----
MAX_SPEED = int(os.environ.get("MAX_SPEED", "40"))          # picarx speed units (0-100)
MAX_DRIVE_SECONDS = float(os.environ.get("MAX_DRIVE_SECONDS", "3"))
OBSTACLE_STOP_CM = float(os.environ.get("OBSTACLE_STOP_CM", "20"))
MAX_STEER_DEG = 30
# The emergency-stop latch is saved to this file so a restart (systemd Restart=on-failure after a crash, a pod
# restart) comes back STILL latched. HALT_FILE="" turns it off. Default on hardware: $STATE_DIRECTORY/halted
# (systemd StateDirectory=picar-beambot). The simulator keeps it in memory unless HALT_FILE is set.
def _halt_file():
    path = os.environ.get("HALT_FILE")
    if path is not None:
        return path or None
    if SIMULATE:
        return None
    base = os.environ.get("STATE_DIRECTORY", "").split(":")[0] or os.path.expanduser("~/.local/state/picar-beambot")
    return os.path.join(base, "halted")


HALT_FILE = _halt_file()


class StopFailed(RuntimeError):
    """The latch is set but the hardware stop call failed (it keeps being retried)."""
# Forward motion needs a valid ultrasonic echo no older than this. A -1 / None reading (no echo, or a dead
# sensor) is "unknown", not "clear". Set ALLOW_BLIND_FORWARD=1 only if you accept driving without the sensor.
SENSOR_MAX_AGE_S = float(os.environ.get("SENSOR_MAX_AGE_S", "0.5"))
ALLOW_BLIND_FORWARD = os.environ.get("ALLOW_BLIND_FORWARD") == "1"
# Speaker: text is appended as the last argument. espeak-ng plays straight to ALSA (the Robot HAT speaker).
SAY_CMD = os.environ.get("SAY_CMD", "espeak-ng -a 200 -s 150 -v en")

# ---- Dead-reckoning calibration (PiCar-X has no wheel encoders: CALIBRATE THESE) ----
# Drive forward 2s at speed 30, measure distance (cm), set CM_PER_SEC_PER_SPEED = cm / (2*30).
CM_PER_SEC_PER_SPEED = float(os.environ.get("CM_PER_SEC_PER_SPEED", "0.6"))
WHEELBASE_CM = float(os.environ.get("WHEELBASE_CM", "9.5"))


# =============================================================================
# Hardware layer (real PiCar-X or simulator)
# =============================================================================
class SimCar:
    """A fake PiCar-X in a 4m x 3m room with a couple of desks, for demos without hardware."""
    WALLS = [((-50, -150), (350, -150)), ((350, -150), (350, 150)),
             ((350, 150), (-50, 150)), ((-50, 150), (-50, -150)),
             ((120, -60), (180, -60)), ((180, -60), (180, 0)), ((180, 0), (120, 0)), ((120, 0), (120, -60)),
             ((230, 60), (290, 60)), ((290, 60), (290, 110)), ((290, 110), (230, 110)), ((230, 110), (230, 60))]

    def __init__(self):
        self.speed = 0
        self.steer = 0
        self.pose_ref = None  # set by Robot

    def forward(self, s): self.speed = s
    def backward(self, s): self.speed = -s
    def stop(self): self.speed = 0
    def set_dir_servo_angle(self, a): self.steer = a
    def set_cam_pan_angle(self, a): pass
    def set_cam_tilt_angle(self, a): pass
    def get_grayscale_data(self): return [800, 810, 805]

    def read_distance(self):
        x, y, th = self.pose_ref()
        best = 300.0
        dx, dy = math.cos(th), math.sin(th)
        for (x1, y1), (x2, y2) in self.WALLS:
            ex, ey = x2 - x1, y2 - y1
            den = dx * ey - dy * ex
            if abs(den) < 1e-9:
                continue
            t = ((x1 - x) * ey - (y1 - y) * ex) / den
            u = ((x1 - x) * dy - (y1 - y) * dx) / den
            if t > 0 and 0 <= u <= 1:
                best = min(best, t)
        return round(best, 1)


class Robot:
    def __init__(self):
        self.lock = threading.RLock()
        self.x = 0.0       # cm, start position = origin
        self.y = 0.0
        self.th = 0.0      # radians, 0 = facing +x
        self.path = [(0.0, 0.0)]
        self.distance = -1.0
        self.grayscale = [0, 0, 0]
        self.pan = 0
        self.tilt = 0
        self.speed = 0     # signed
        self.steer = 0
        self.drive_until = 0.0
        self.drive_gen = 0  # bumped per drive() so an older call doesn't straighten a newer one's steering
        self.last_event = "ready"
        self.distance_ok_at = 0.0   # when the ultrasonic last gave a valid (> 0) reading
        self.need_stop = False      # a stop was requested but the hardware call failed: retry every pass
        self.halted = self._load_halt()   # emergency-stop latch: drive is refused until resume()

        if SIMULATE:
            self.car = SimCar()
            self.car.pose_ref = lambda: (self.x, self.y, self.th)
            self.camera = None
        elif HW == "hat":
            from robot_hat_lite import GstCamera, HatCar
            self.car = HatCar()
            self.camera = GstCamera()
        else:
            from picarx import Picarx
            self.car = Picarx()
            from picamera2 import Picamera2
            self.camera = Picamera2()
            self.camera.configure(self.camera.create_video_configuration(main={"size": (640, 480), "format": "RGB888"}))
            self.camera.start()

        self.frame = b""
        try:                        # a previous run may have left the motors spinning
            self.car.stop()
            self.car.set_dir_servo_angle(0)
        except Exception as e:  # noqa: BLE001
            self.need_stop = True   # the control loop keeps retrying
            self.last_event = f"startup stop failed: {e}"
        threading.Thread(target=self._control_loop, daemon=True).start()
        threading.Thread(target=self._camera_loop, daemon=True).start()

    # ---- sensors ----
    def _read_distance(self):
        if SIMULATE:
            return self.car.read_distance()
        d = self.car.ultrasonic.read()
        return round(float(d), 1) if d is not None else -1.0

    def _forward_blocked(self, now):
        """Why forward motion is not allowed right now, or None. Fails closed: no valid reading = blocked."""
        if 0 < self.distance < OBSTACLE_STOP_CM:
            return f"obstacle at {self.distance} cm"
        if not ALLOW_BLIND_FORWARD and (self.distance <= 0 or now - self.distance_ok_at > SENSOR_MAX_AGE_S):
            return "no valid distance reading (ultrasonic unavailable)"
        return None

    # ---- 20 Hz loop: integrate pose, read sensors, enforce safety ----
    def _control_loop(self):
        last = time.time()
        while True:
            time.sleep(0.05)
            now = time.time()
            dt = now - last
            last = now
            try:
                self._tick(now, dt)
            except Exception as e:  # noqa: BLE001  one bad pass (an I2C error, say) must never end this loop
                self.last_event = f"control loop error: {e}"
                self.need_stop = True
                try:
                    with self.lock:
                        self._stop("control loop error")
                except Exception:  # noqa: BLE001  need_stop is set: the next pass tries again
                    pass

    def _tick(self, now, dt):
        with self.lock:
            try:
                self.distance = self._read_distance()
                if self.distance > 0:
                    self.distance_ok_at = now
            except Exception as e:  # noqa: BLE001
                self.distance = -1.0        # never leave the last good value in place: it would look like a clear path
                self.last_event = f"sensor error: {e}"
            try:
                self.grayscale = list(self.car.get_grayscale_data())
            except Exception as e:  # noqa: BLE001
                self.last_event = f"grayscale error: {e}"

            if self.need_stop:
                self._stop("retrying stop")
            if self.speed != 0 and now >= self.drive_until:
                self._stop("drive time elapsed")
            if self.speed > 0:
                why = self._forward_blocked(now)
                if why:
                    self._stop(why)

            if self.speed != 0:
                v = self.speed * CM_PER_SEC_PER_SPEED
                self.th += (v / WHEELBASE_CM) * math.tan(math.radians(self.steer)) * dt
                self.x += v * math.cos(self.th) * dt
                self.y += v * math.sin(self.th) * dt
                if math.dist(self.path[-1], (self.x, self.y)) > 3:
                    self.path.append((round(self.x, 1), round(self.y, 1)))
                    self.path = self.path[-2000:]

    def _camera_loop(self):
        while True:
            try:
                if SIMULATE:
                    self.frame = self._sim_frame()
                    time.sleep(0.2)
                elif HW == "hat":
                    self.frame = self.camera.frame or self.frame
                    if self.camera.error and not self.camera.frame:
                        self.last_event = self.camera.error
                    time.sleep(0.066)
                else:
                    import cv2
                    arr = self.camera.capture_array()
                    ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                    if ok:
                        self.frame = buf.tobytes()
                    time.sleep(0.066)
            except Exception as e:  # noqa: BLE001
                self.last_event = f"camera error: {e}"
                time.sleep(1)

    def _sim_frame(self):
        from PIL import Image as PILImage, ImageDraw
        img = PILImage.new("RGB", (640, 480), (40, 44, 52))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 300, 640, 480], fill=(70, 70, 80))
        d.text((20, 20), "SIMULATED CAMERA", fill=(200, 200, 200))
        d.text((20, 40), f"pose x={self.x:.0f} y={self.y:.0f} th={math.degrees(self.th):.0f}", fill=(200, 200, 200))
        d.text((20, 60), f"ultrasonic {self.distance} cm", fill=(200, 200, 200))
        for i in range(3):  # three "chairs"
            cx = 140 + i * 160
            d.rectangle([cx - 30, 230, cx + 30, 300], fill=(120, 80, 160))
            d.rectangle([cx - 30, 300, cx - 24, 340], fill=(120, 80, 160))
            d.rectangle([cx + 24, 300, cx + 30, 340], fill=(120, 80, 160))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()

    # ---- actuators ----
    def _stop(self, why):
        """Stop the motors. If the hardware call fails the exception propagates and need_stop stays set,
        so the control loop retries on every pass until it works."""
        self.need_stop = True
        self.car.stop()
        self.car.set_dir_servo_angle(0)
        self.speed = 0
        self.steer = 0
        self.need_stop = False
        self.last_event = f"stopped: {why}"

    def shutdown(self):
        """Zero the motors at exit / SIGTERM."""
        for _ in range(5):
            try:
                with self.lock:
                    self._stop("shutting down")
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)

    def _deadline(self, gen):
        """Stop at this drive's own deadline, whether or not the control loop is healthy."""
        with self.lock:
            if gen == self.drive_gen and (self.speed != 0 or self.need_stop):
                self._stop("drive time elapsed")

    def drive(self, speed, seconds, steer, wait=True):
        speed = max(-MAX_SPEED, min(MAX_SPEED, int(speed)))
        seconds = max(0.1, min(MAX_DRIVE_SECONDS, float(seconds)))
        steer = max(-MAX_STEER_DEG, min(MAX_STEER_DEG, int(steer)))
        with self.lock:
            if self.halted:   # an error, not a string: callers must not see a refused drive as a success
                raise RuntimeError("refused: emergency stop is latched; resume it (dashboard Resume) before driving")
            if speed > 0:
                why = self._forward_blocked(time.time())
                if why:
                    return f"refused: {why}"
            try:
                self.car.set_dir_servo_angle(steer)
                self.steer = steer
                if speed >= 0:
                    self.car.forward(speed)
                else:
                    self.car.backward(-speed)
            except Exception:
                self.need_stop = True       # a half-applied command (one motor on) must not stay on
                try:
                    self._stop("drive failed")
                except Exception:  # noqa: BLE001
                    pass
                raise
            self.speed = speed
            self.drive_until = time.time() + seconds
            self.drive_gen += 1
            gen = self.drive_gen
            self.last_event = f"driving speed={speed} steer={steer} for {seconds}s"
            timer = threading.Timer(seconds, self._deadline_safely, (gen,))
            timer.daemon = True
            timer.start()
            if not wait:   # manual control: return now, the deadline timer / control loop stops it
                return self.last_event
        time.sleep(seconds + 0.1)
        self._deadline(gen)
        with self.lock:
            if gen == self.drive_gen:
                self.car.set_dir_servo_angle(0)
                self.steer = 0
            return self.last_event

    def _deadline_safely(self, gen):
        try:
            self._deadline(gen)
        except Exception as e:  # noqa: BLE001  need_stop is set, the control loop retries
            self.last_event = f"deadline stop failed: {e}"

    def look(self, pan, tilt):
        pan = max(-90, min(90, int(pan)))
        tilt = max(-35, min(65, int(tilt)))
        with self.lock:
            self.car.set_cam_pan_angle(pan)
            self.car.set_cam_tilt_angle(tilt)
            self.pan, self.tilt = pan, tilt
        time.sleep(0.4)
        return f"camera pan={pan} tilt={tilt}"

    @staticmethod
    def _load_halt():
        """A latch saved by an earlier run stays set. If the file can't even be checked, assume latched."""
        if not HALT_FILE:
            return False
        try:
            return os.path.exists(HALT_FILE)
        except OSError:
            return True

    @staticmethod
    def _save_halt(on):
        """Write or remove the saved latch. Returns an error text, or "" on success."""
        if not HALT_FILE:
            return ""
        try:
            if on:
                os.makedirs(os.path.dirname(HALT_FILE) or ".", exist_ok=True)
                fd = os.open(HALT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    os.write(fd, f"{time.time():.0f}\n".encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
            else:
                try:
                    os.unlink(HALT_FILE)
                except (FileNotFoundError, NotADirectoryError):   # nothing saved, so nothing to clear
                    pass
        except OSError as e:
            return str(e)
        return ""

    def emergency_stop(self):
        """Stop the motors and latch. The latch is set (and saved) first, so it holds even if the hardware stop
        fails (the control loop keeps retrying the stop) and across a restart."""
        with self.lock:
            self.halted = True
            not_saved = self._save_halt(True)
            try:
                self._stop("emergency stop")
            except Exception as e:  # noqa: BLE001
                self.last_event = f"emergency stop: hardware stop FAILED ({e}); latched, retrying"
                raise StopFailed(self.last_event) from e
        return "stopped" + (f" (WARNING: the latch could not be saved, a restart would clear it: {not_saved})"
                            if not_saved else "")

    def release(self):
        """A plain stop that does not latch: letting go of the drive pad, the end of an auto-map run."""
        with self.lock:
            self._stop("released")
        return "stopped"

    def resume(self):
        with self.lock:
            failed = self._save_halt(False)
            if failed:   # the saved latch is still there: stay latched, or a restart would bring it back
                raise RuntimeError(f"still latched: could not clear the saved latch ({failed})")
            self.halted = False
            self.last_event = "resumed after emergency stop"
        return "resumed"

    def say(self, text):
        text = re.sub(r"[^\w ,.!?'-]", "", str(text))[:120].strip() or "hello"
        if SIMULATE:
            self.last_event = f"said: {text}"
            return self.last_event
        # Robot HAT V4: audio is I2S (dtoverlay=hifiberry-dac) and the amp is switched by GPIO 20. The amp is
        # only enabled while speaking: SunFounder's library notes it heats up when enabled without an I2S clock.
        def amp(on):
            if HW == "hat":
                self.car.speaker(on)
            else:
                from robot_hat import utils                   # SunFounder lib does the same with pinctrl
                (utils.enable_speaker if on else utils.disable_speaker)()
        try:
            amp(True)
        except Exception as e:  # noqa: BLE001
            self.last_event = f"speaker enable failed: {e}"
        try:
            r = subprocess.run([*shlex.split(SAY_CMD), text], capture_output=True, text=True, timeout=20)
        finally:
            try:
                amp(False)
            except Exception:  # noqa: BLE001
                pass
        if r.returncode != 0:
            raise RuntimeError(f"say failed ({r.returncode}): {r.stderr.strip()[-160:]}")
        self.last_event = f"said: {text}"
        return self.last_event

    def telemetry(self):
        with self.lock:
            return {
                "ts": time.time(),
                "pose": {"x_cm": round(self.x, 1), "y_cm": round(self.y, 1),
                         "heading_deg": round(math.degrees(self.th) % 360, 1)},
                "ultrasonic_cm": self.distance,
                "grayscale": self.grayscale,
                "camera": {"pan": self.pan, "tilt": self.tilt},
                "moving": self.speed != 0,
                "halted": self.halted,
                "speed": self.speed,
                "steer": self.steer,
                "last_event": self.last_event,
                "path": self.path[-300:],
                "simulated": SIMULATE,
                "hw": HW,
            }


robot = Robot()

# =============================================================================
# MCP server — every tool call is proxied and audited by Teleport MCP access
# =============================================================================
mcp = FastMCP("picar-beambot", host=HOST, port=PORT, stateless_http=True)


@mcp.tool()
async def drive(speed: int, seconds: float, steer_deg: int = 0, wait: bool = True) -> str:
    """Drive the robot. speed: -40..40 (negative = reverse). seconds: 0.1..3.
    steer_deg: -30 (left) .. 30 (right). Stops automatically if an obstacle is closer than 20 cm.
    wait: return after the move finishes (default) or immediately (for hold-to-drive controls)."""
    return await asyncio.to_thread(robot.drive, speed, seconds, steer_deg, wait)


@mcp.tool()
async def look(pan_deg: int = 0, tilt_deg: int = 0) -> str:
    """Point the camera. pan_deg: -90 (left) .. 90 (right). tilt_deg: -35 (down) .. 65 (up)."""
    return await asyncio.to_thread(robot.look, pan_deg, tilt_deg)


@mcp.tool()
def stop() -> str:
    """Emergency stop: stop all motion now and refuse `drive` until a human resumes. `drive` already ends by
    itself, so only call this to abort a move, not to finish one."""
    return robot.emergency_stop() + " (latched: drive is refused until resumed)"


@mcp.tool()
def resume() -> str:
    """Clear the emergency-stop latch so `drive` works again. Not part of the default viewer or driver roles."""
    return robot.resume()


@mcp.tool()
async def say(text: str = "Hello Beams, I love you") -> str:
    """Speak a short sentence (up to 120 characters) through the robot's speaker."""
    return await asyncio.to_thread(robot.say, text)


@mcp.tool()
def get_telemetry() -> dict:
    """Current pose estimate (dead reckoning, cm from start), ultrasonic distance, grayscale, camera angles."""
    t = robot.telemetry()
    t.pop("path", None)
    return t


@mcp.tool()
def snapshot() -> Image:
    """Take a photo with the robot's camera. Use this to see and count objects."""
    return Image(data=robot.frame, format="jpeg")


# ---- Stream app: a second port, deliberately without /mcp ----
# Published to Teleport as the plain HTTP app `picar-stream`. Teleport's `mcp.tools` only filters MCP apps, so
# anything on this port is open to whoever may open the app: it can show telemetry and video and stop the car.
async def telemetry_route(request: Request):
    return JSONResponse(robot.telemetry())


async def snapshot_route(request: Request):
    return Response(robot.frame, media_type="image/jpeg")


async def video_route(request: Request):
    async def gen():
        last, sent_at = None, 0.0
        while True:
            if await request.is_disconnected():
                break
            f = robot.frame
            # New frame, or resend the last one every second so the connection never goes idle
            # (an idle stream through the Teleport app proxy can stall; the page reconnects on silence).
            if f and (f is not last or time.monotonic() - sent_at > 1.0):
                last, sent_at = f, time.monotonic()
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
            await asyncio.sleep(0.07)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


async def stop_route(request: Request):
    try:
        return JSONResponse({"result": robot.emergency_stop(), "halted": True})
    except StopFailed as e:   # still latched, but say so: the car may be moving
        return JSONResponse({"ok": False, "halted": True, "error": str(e)}, status_code=500)


async def release_route(request: Request):
    return JSONResponse({"result": robot.release()})


async def resume_route(request: Request):
    # Only the dashboard's server calls this: a JSON POST with no Origin header. A web page in someone's browser
    # (a form, a text/plain fetch) is refused, so it can't lift a stop with the visitor's Teleport session.
    if (request.headers.get("origin") or request.headers.get("sec-fetch-site", "none") not in ("none", "same-origin")
            or "application/json" not in request.headers.get("content-type", "")):
        return JSONResponse({"error": "resume must be a JSON POST from the dashboard, not a browser page"},
                            status_code=403)
    try:
        return JSONResponse({"result": robot.resume(), "halted": False})
    except RuntimeError as e:
        return JSONResponse({"ok": False, "halted": True, "error": str(e)}, status_code=500)


stream_app = Starlette(routes=[
    Route("/telemetry", telemetry_route, methods=["GET"]),
    Route("/snapshot.jpg", snapshot_route, methods=["GET"]),
    Route("/video.mjpg", video_route, methods=["GET"]),
    Route("/stop", stop_route, methods=["POST"]),
    Route("/release", release_route, methods=["POST"]),
    Route("/resume", resume_route, methods=["POST"]),
])


def serve_stream():
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(stream_app, host=HOST, port=STREAM_PORT, log_level="warning"))
    threading.Thread(target=server.run, name="stream", daemon=True).start()


def _on_sigterm(signum, frame):
    robot.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    atexit.register(robot.shutdown)
    signal.signal(signal.SIGTERM, _on_sigterm)
    serve_stream()
    print(f"BeamBot robot server: MCP on http://{HOST}:{PORT}/mcp, stream on http://{HOST}:{STREAM_PORT} "
          f"(video, telemetry, stop; no /mcp), simulate={SIMULATE}")
    mcp.run(transport="streamable-http")
