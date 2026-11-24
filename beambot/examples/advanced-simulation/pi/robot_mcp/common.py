"""Tools every node exposes, regardless of role."""
import os
import socket
import time

from . import safety
from .registry import tool

BOOT = time.time()


def _cpu_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except OSError:
        return None


def _throttled():
    # Undervoltage on a Pi 3 = flaky servos and random reboots. Worth surfacing.
    try:
        import subprocess
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() or None
    except Exception:
        return None


@tool("sense_node_status",
      "Health of this robot node: role, simulation mode, e-stop state, CPU temp, uptime, last caller.")
def node_status():
    from .server import LAST_CALLER
    return {
        "node": socket.gethostname(),
        "role": os.environ.get("ROBOT_ROLE"),
        "simulated": os.environ.get("ROBOT_SIM", "1") == "1",
        "estop": safety.state(),
        "cpu_temp_c": _cpu_temp_c(),
        "throttled": _throttled(),
        "uptime_s": int(time.time() - BOOT),
        "last_call": LAST_CALLER["value"],
    }


@tool("safety_stop",
      "EMERGENCY STOP the whole robot. Latches e-stop on this node and broadcasts it to every node "
      "on the LAN. Always allowed. Use it whenever anything looks wrong.",
      {"reason": {"type": "string", "description": "why you are stopping"}})
def safety_stop(reason="agent requested stop", _caller="unknown"):
    safety.trip(f"{reason} (by {_caller})", source=f"mcp:{_caller}")
    return {"estopped": True, "broadcast": True, "state": safety.state()}


@tool("admin_reset_estop",
      "Clear a latched e-stop on THIS node. Human/admin only. Confirm the robot is physically safe first.",
      {"confirm": {"type": "boolean", "description": "must be true"}}, ["confirm"])
def admin_reset(confirm, _caller="unknown"):
    from .registry import ToolError
    if not confirm:
        raise ToolError("confirm must be true")
    safety.reset(by=_caller)
    return {"estopped": False}
