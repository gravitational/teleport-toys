"""Safety node: physical e-stop button + signalling.

Wire a normally-open button between ESTOP_GPIO (default BCM 27) and GND.
Uses libgpiod's `gpiomon` CLI (pacman -S libgpiod); works with v1 and v2 syntax.
"""
import logging
import os
import shutil
import subprocess
import threading
import time

from .. import safety
from ..registry import tool

log = logging.getLogger("safety-node")
SIM = os.environ.get("ROBOT_SIM", "1") == "1"
PIN = os.environ.get("ESTOP_GPIO", "27")
CHIP = os.environ.get("GPIO_CHIP", "gpiochip0")
_signal = {"pattern": "idle"}


def _watch_button():
    if not shutil.which("gpiomon"):
        log.warning("gpiomon not found; physical e-stop disabled (pacman -S libgpiod)")
        return
    attempts = [
        ["gpiomon", "-c", CHIP, "-e", "falling", "-b", "pull-up", PIN],  # libgpiod v2
        ["gpiomon", "-f", "-B", "pull-up", CHIP, PIN],                   # libgpiod v1
    ]
    for cmd in attempts:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        if p.poll() is not None:
            continue  # wrong syntax for this libgpiod version
        log.info("watching e-stop button: %s", " ".join(cmd))
        for _ in p.stdout:
            safety.trip("PHYSICAL E-STOP BUTTON", source="button")
        return
    log.error("could not start gpiomon for e-stop button")


if not SIM:
    threading.Thread(target=_watch_button, name="estop-button", daemon=True).start()


@tool("sense_estop_state", "Whether the robot-wide e-stop is latched, why, and since when.")
def estop_state():
    return safety.state()


@tool("act_signal", "Show a status pattern on the robot's light/buzzer so humans know what it is doing.",
      {"pattern": {"type": "string", "enum": ["idle", "thinking", "moving", "success", "warning"]}},
      ["pattern"])
def signal(pattern):
    _signal["pattern"] = pattern  # TODO: drive an LED/buzzer once hardware is chosen
    return {"pattern": pattern, "simulated": SIM}
