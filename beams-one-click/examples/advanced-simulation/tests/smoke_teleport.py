"""End-to-end smoke test THROUGH TELEPORT, the same path Claude uses in the Beam:
MCP client -> `tsh mcp connect <app>` (stdio) -> Teleport proxy -> Pi app_service -> robot_mcp.

  pip install "mcp>=1.10,<2"
  python3 tests/smoke_teleport.py              # read-only + e-stop round trip
  python3 tests/smoke_teleport.py --move       # also does one tiny act_turn (needs act_* allowed)

Prints per-call latency: those are the numbers for your demo.
"""
import asyncio
import base64
import json
import os
import shutil
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROBOT = os.environ.get("ROBOT", "beambot-sim")
TSH = os.environ.get("TSH", shutil.which("tsh") or "tsh")
ROLES = ["safety", "vision", "drive", "arm", "sensors"]
MOVE = "--move" in sys.argv
results = []


def text_of(res):
    return " ".join(c.text for c in res.content if getattr(c, "type", "") == "text")


async def with_node(role, fn):
    app = f"{ROBOT}-{role}"
    params = StdioServerParameters(command=TSH, args=["mcp", "connect", app],
                                   env={**os.environ, "TELEPORT_DEBUG": "false"})
    t0 = time.monotonic()
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            connect_ms = (time.monotonic() - t0) * 1000
            tools = [t.name for t in (await s.list_tools()).tools]
            print(f"\n[{app}] connected in {connect_ms:.0f} ms, {len(tools)} tools visible: {tools}")
            await fn(s, tools)


async def timed_call(s, name, args=None):
    t0 = time.monotonic()
    res = await s.call_tool(name, args or {})
    ms = (time.monotonic() - t0) * 1000
    flag = "ERR" if res.isError else "ok "
    print(f"  {flag} {name:<22} {ms:6.0f} ms  {text_of(res)[:110].replace(chr(10), ' ')}")
    results.append((name, ms, res.isError))
    return res


async def main():
    async def status(s, tools):
        await timed_call(s, "sense_node_status")

    for role in ROLES:
        await with_node(role, status)

    async def look(s, tools):
        res = await timed_call(s, "sense_capture_image", {"width": 160, "height": 120})
        img = [c for c in res.content if c.type == "image"]
        if img:
            print(f"      image: {img[0].mimeType}, {len(base64.b64decode(img[0].data))} bytes")
    await with_node("vision", look)

    async def sense(s, tools):
        await timed_call(s, "sense_distance")
    await with_node("sensors", sense)

    async def drive(s, tools):
        await timed_call(s, "sense_drive_state")
        if MOVE:
            if "act_turn" in tools:
                await timed_call(s, "act_turn", {"direction": "left", "duration_ms": 200})
            else:
                print("  -- act_turn not visible to this identity (RBAC working as intended?)")
    await with_node("drive", drive)

    # e-stop round trip: trip on the safety node, observe it on the arm node via the LAN mesh
    async def trip(s, tools):
        await timed_call(s, "safety_stop", {"reason": "smoke test"})
    await with_node("safety", trip)

    async def observe(s, tools):
        res = await timed_call(s, "sense_node_status")
        try:
            est = json.loads(text_of(res))["estop"]
            print(f"      arm node sees estop={est['estopped']} source={est['source']}")
        except Exception:
            pass
        if "admin_reset_estop" in tools:
            await timed_call(s, "admin_reset_estop", {"confirm": True})
        else:
            print("  -- admin_reset_estop not visible: reset each node as admin")
    await with_node("arm", observe)

    # The e-stop latches on every node; reset the rest so the robot is usable again.
    for role in ("safety", "vision", "drive", "sensors"):
        async def reset(s, tools):
            if "admin_reset_estop" in tools:
                await timed_call(s, "admin_reset_estop", {"confirm": True})
        await with_node(role, reset)

    calls = [ms for _, ms, _ in results]
    errs = [n for n, _, e in results if e]
    print(f"\n{len(results)} calls, median {sorted(calls)[len(calls)//2]:.0f} ms, max {max(calls):.0f} ms,"
          f" errors: {errs or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
