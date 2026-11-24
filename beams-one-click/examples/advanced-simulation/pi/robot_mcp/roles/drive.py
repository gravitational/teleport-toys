"""Drive node: two DC motors. Every motion is time-boxed and auto-stops (watchdog).

Backends (DRIVE_BACKEND):
  sim      - default, tracks a pretend pose
  pca9685  - H-bridge (TB6612/L298N) inputs wired to PCA9685 channels.
             DRIVE_CHANNELS='{"left":[4,5],"right":[6,7]}'  (IN1, IN2 per motor)
"""
import json
import math
import os

from .. import safety
from ..registry import tool

SIM = os.environ.get("ROBOT_SIM", "1") == "1"
BACKEND = "sim" if SIM else os.environ.get("DRIVE_BACKEND", "pca9685")
CHANNELS = json.loads(os.environ.get("DRIVE_CHANNELS", '{"left":[4,5],"right":[6,7]}'))
MAX_SPEED = float(os.environ.get("DRIVE_MAX_SPEED", "0.6"))  # hard cap, 0..1
SIM_CM_PER_S = 30.0
SIM_DEG_PER_S = 120.0

_sim = {"x_cm": 0.0, "y_cm": 0.0, "heading_deg": 0.0, "moving": False}
_pwm = None


def _hw():
    global _pwm
    if _pwm is None:
        from ..hw.pca9685 import PCA9685
        _pwm = PCA9685(bus=int(os.environ.get("I2C_BUS", "1")),
                       address=int(os.environ.get("PCA9685_ADDR", "0x40"), 16), freq_hz=1000)
    return _pwm


def _motor(side, speed):
    """speed -1..1"""
    if BACKEND == "sim":
        return
    a, b = CHANNELS[side]
    duty = int(min(abs(speed), 1.0) * 4095)
    pwm = _hw()
    if speed > 0:
        pwm.set_pwm(a, 0, duty); pwm.set_pwm(b, 0, 0)
    elif speed < 0:
        pwm.set_pwm(a, 0, 0); pwm.set_pwm(b, 0, duty)
    else:
        pwm.set_pwm(a, 0, 0); pwm.set_pwm(b, 0, 0)


def _stop():
    _sim["moving"] = False
    if BACKEND != "sim":
        _motor("left", 0)
        _motor("right", 0)


safety.register_stop_hook(_stop)


@tool("sense_drive_state", "Drive base state (simulated odometry in sim mode) and limits.")
def state():
    return {"backend": BACKEND, "max_speed": MAX_SPEED, "max_motion_ms": safety.MAX_MOTION_MS,
            "sim_pose": {k: round(v, 1) if isinstance(v, float) else v for k, v in _sim.items()}}


@tool("act_drive",
      "Drive straight for a short, time-limited burst, then automatically stop.",
      {"direction": {"type": "string", "enum": ["forward", "backward"]},
       "speed": {"type": "number", "minimum": 0.05, "maximum": 1.0, "default": 0.4,
                 "description": "fraction of the node's hard max speed"},
       "duration_ms": {"type": "integer", "minimum": 50, "maximum": 2000}},
      ["direction", "duration_ms"])
def drive(direction, duration_ms, speed=0.4):
    sp = speed * MAX_SPEED * (1 if direction == "forward" else -1)

    def go():
        _sim["moving"] = True
        _motor("left", sp)
        _motor("right", sp)

    ran = safety.timed(duration_ms, go, _stop)
    d = SIM_CM_PER_S * (sp / MAX_SPEED) * ran / 1000.0
    h = math.radians(_sim["heading_deg"])
    _sim["x_cm"] += d * math.cos(h)
    _sim["y_cm"] += d * math.sin(h)
    return {"ran_ms": ran, "stopped": True, **state()}


@tool("act_turn", "Spin in place left or right for a short, time-limited burst.",
      {"direction": {"type": "string", "enum": ["left", "right"]},
       "duration_ms": {"type": "integer", "minimum": 50, "maximum": 2000},
       "speed": {"type": "number", "minimum": 0.05, "maximum": 1.0, "default": 0.4}},
      ["direction", "duration_ms"])
def turn(direction, duration_ms, speed=0.4):
    s = speed * MAX_SPEED
    l, r = (-s, s) if direction == "left" else (s, -s)

    def go():
        _sim["moving"] = True
        _motor("left", l)
        _motor("right", r)

    ran = safety.timed(duration_ms, go, _stop)
    sign = 1 if direction == "left" else -1
    _sim["heading_deg"] = (_sim["heading_deg"] + sign * SIM_DEG_PER_S * speed * ran / 1000.0) % 360
    return {"ran_ms": ran, "stopped": True, **state()}


@tool("safety_drive_stop", "Immediately stop the drive motors (does not latch e-stop).")
def drive_stop(_caller="unknown"):
    _stop()
    return {"stopped": True}
