"""A robot MCP server that only exposes what a trimmed-down copy of picar-driver would see (what Teleport RBAC does by hiding tools).

    python restricted_robot.py <port>
"""
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("restricted-robot", host="127.0.0.1", port=int(sys.argv[1]), stateless_http=True)


@mcp.tool()
def stop() -> str:
    """Stop all motion."""
    return "stopped"


@mcp.tool()
def get_telemetry() -> dict:
    """Telemetry."""
    return {"pose": {"x_cm": 0, "y_cm": 0, "heading_deg": 0}, "ultrasonic_cm": 100}


@mcp.tool()
def snapshot() -> str:
    """Pretend photo."""
    return "no camera on this stand-in"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
