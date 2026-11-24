"""Sensors node: distance + IMU. Real drivers are TODO until hardware is chosen.

Recommendation: an I2C time-of-flight sensor (VL53L0X/VL53L1X) or an Arduino/ESP32
streaming readings over serial. HC-SR04 needs microsecond timing that Python on a
Pi 3 does badly.
"""
import os
import random
import time

from ..registry import ToolError, tool

SIM = os.environ.get("ROBOT_SIM", "1") == "1"
_sim = {"d": 120.0}


@tool("sense_distance", "Distance to the nearest obstacle straight ahead, in centimeters.")
def distance():
    if not SIM:
        raise ToolError("real distance sensor not wired yet (see roles/sensors.py)")
    _sim["d"] = max(5.0, min(250.0, _sim["d"] + random.uniform(-15, 15)))
    return {"distance_cm": round(_sim["d"], 1), "simulated": True, "t": time.time()}


@tool("sense_imu", "Orientation and acceleration from the IMU.")
def imu():
    if not SIM:
        raise ToolError("real IMU not wired yet (see roles/sensors.py)")
    return {"roll_deg": round(random.gauss(0, 1), 2), "pitch_deg": round(random.gauss(0, 1), 2),
            "yaw_deg": round(random.uniform(0, 360), 1),
            "accel_g": [round(random.gauss(0, 0.02), 3), round(random.gauss(0, 0.02), 3),
                        round(1 + random.gauss(0, 0.02), 3)],
            "simulated": True}
