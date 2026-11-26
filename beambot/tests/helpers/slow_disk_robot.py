"""The real robot server (robot_mcp.py) on a stalled SD card: every fsync sleeps FSYNC_STALL_S seconds first.

Run with the same env as robot_mcp.py plus HALT_FILE (a path, so the latch is saved) and FSYNC_STALL_S.
"""
import os
import runpy
import sys
import time

STALL = float(os.environ.get("FSYNC_STALL_S", "3"))
_fsync = os.fsync


def slow_fsync(fd):
    time.sleep(STALL)
    return _fsync(fd)


os.fsync = slow_fsync
robot_dir = os.path.join(os.path.dirname(__file__), "..", "..", "examples", "picar-beambot", "robot")
sys.path.insert(0, os.path.abspath(robot_dir))
runpy.run_path(os.path.join(os.path.abspath(robot_dir), "robot_mcp.py"), run_name="__main__")
