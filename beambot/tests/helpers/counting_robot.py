"""A stand-in robot with NO emergency-stop latch that records every `drive` it receives (one line each) in a file.
It lets a test prove the dashboard's own halt checks work, without leaning on the real robot's latch.

    python counting_robot.py <port> <log-file>
"""
import sys

from mcp.server.fastmcp import FastMCP

LOG = sys.argv[2]
mcp = FastMCP("counting-robot", host="127.0.0.1", port=int(sys.argv[1]), stateless_http=True)


def note(line):
    with open(LOG, "a") as f:
        f.write(line + "\n")


@mcp.tool()
def drive(speed: int, seconds: float, steer_deg: int = 0, wait: bool = True) -> str:
    """Drive."""
    note("drive")
    return "driving"


@mcp.tool()
def look(pan_deg: int = 0, tilt_deg: int = 0) -> str:
    """Look."""
    note("look")
    return "looking"


@mcp.tool()
def stop() -> str:
    """Stop."""
    note("stop")
    return "stopped"


@mcp.tool()
def get_telemetry() -> dict:
    """Telemetry."""
    return {"pose": {"x_cm": 0, "y_cm": 0, "heading_deg": 0}, "ultrasonic_cm": 100}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
