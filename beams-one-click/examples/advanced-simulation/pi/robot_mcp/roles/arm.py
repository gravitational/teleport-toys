"""Arm node: servos on a PCA9685. Limits are enforced HERE, not in the prompt."""
import json
import os
import threading
import time

from .. import safety
from ..registry import ToolError, tool

SIM = os.environ.get("ROBOT_SIM", "1") == "1"

# name -> channel, min, max, home (degrees). Override with ARM_JOINTS='{"base":[0,10,170,90],...}'
JOINTS = {
    "base":     [0, 10, 170, 90],
    "shoulder": [1, 20, 160, 90],
    "elbow":    [2, 10, 170, 90],
    "gripper":  [3, 30, 120, 60],   # 30 = open, 120 = closed (tune on real hardware)
}
JOINTS.update({k: v for k, v in json.loads(os.environ.get("ARM_JOINTS", "{}")).items()})
GRIPPER_OPEN, GRIPPER_CLOSED = JOINTS["gripper"][1], JOINTS["gripper"][2]
MAX_DEG_PER_STEP = 3
STEP_S = 0.02

_pose = {j: float(v[3]) for j, v in JOINTS.items()}
_lock = threading.Lock()
_pwm = None


def _hw():
    global _pwm
    if _pwm is None and not SIM:
        from ..hw.pca9685 import PCA9685
        _pwm = PCA9685(bus=int(os.environ.get("I2C_BUS", "1")),
                       address=int(os.environ.get("PCA9685_ADDR", "0x40"), 16))
    return _pwm


def _write(joint, deg):
    _pose[joint] = deg
    if SIM:
        return
    ch = JOINTS[joint][0]
    _hw().set_pulse_us(ch, 500 + (deg / 180.0) * 2000)


def _go(joint, target):
    ch, lo, hi, _ = JOINTS[joint]
    if not lo <= target <= hi:
        raise ToolError(f"{joint} angle {target} outside safe range [{lo}, {hi}]")
    with _lock:
        cur = _pose[joint]
        while abs(target - cur) > 0.01:
            if safety.state()["estopped"]:
                raise ToolError(f"e-stop during move; {joint} stopped at {cur:.1f}")
            step = max(-MAX_DEG_PER_STEP, min(MAX_DEG_PER_STEP, target - cur))
            cur += step
            _write(joint, cur)
            time.sleep(STEP_S)
    return round(cur, 1)


def _release():
    if not SIM and _pwm is not None:
        _pwm.release_all()


safety.register_stop_hook(_release)


@tool("sense_arm_pose", "Current joint angles (degrees) and the safe limits for each joint.")
def pose():
    return {"pose": {k: round(v, 1) for k, v in _pose.items()},
            "limits": {k: {"min": v[1], "max": v[2], "home": v[3]} for k, v in JOINTS.items()}}


@tool("act_arm_move_joint",
      "Move one joint to an absolute angle in degrees. Motion is slew-limited; out-of-range is refused.",
      {"joint": {"type": "string", "enum": [j for j in JOINTS if j != "gripper"]},
       "angle": {"type": "number", "minimum": 0, "maximum": 180}},
      ["joint", "angle"])
def move_joint(joint, angle):
    return {"joint": joint, "angle": _go(joint, float(angle))}


@tool("act_arm_gripper", "Open or close the gripper.",
      {"action": {"type": "string", "enum": ["open", "close"]}}, ["action"])
def gripper(action):
    return {"gripper": action, "angle": _go("gripper", GRIPPER_OPEN if action == "open" else GRIPPER_CLOSED)}


@tool("act_arm_home", "Move every joint to its home position.")
def home():
    return {j: _go(j, float(v[3])) for j, v in JOINTS.items()}
