"""Exercise every node with the OFFICIAL MCP Python client (Streamable HTTP).

Run on a laptop (not needed on the Pis):
  pip install "mcp>=1.10,<2"
  python3 tests/test_mcp_client.py          # official-SDK server (default)
  python3 tests/test_mcp_client.py stdlib   # zero-dependency fallback server
Starts all 5 roles locally in sim mode on ports 8101-8105.
"""
import asyncio
import base64
import os
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pi")
ROLES = ["safety", "vision", "drive", "arm", "sensors"]
PORTS = {r: 8101 + i for i, r in enumerate(ROLES)}
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


async def session(role):
    return streamablehttp_client(f"http://127.0.0.1:{PORTS[role]}/mcp")


async def call(role, name, args=None):
    async with streamablehttp_client(f"http://127.0.0.1:{PORTS[role]}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await s.call_tool(name, args or {})


async def main():
    for role in ROLES:
        async with streamablehttp_client(f"http://127.0.0.1:{PORTS[role]}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                print(f"[{role}] {init.serverInfo.name} proto={init.protocolVersion} tools={tools}")
                check("safety_stop" in tools and "sense_node_status" in tools, f"{role}: common tools present")
                check(all(t.split("_")[0] in ("sense", "act", "safety", "admin") for t in tools),
                      f"{role}: all tool names use RBAC prefixes")

    res = await call("vision", "sense_capture_image", {"width": 160, "height": 120})
    img = [c for c in res.content if c.type == "image"]
    check(bool(img) and base64.b64decode(img[0].data)[:4] == b"\x89PNG", "vision: returns a real PNG image block")

    res = await call("arm", "act_arm_move_joint", {"joint": "base", "angle": 175})
    check(res.isError, "arm: out-of-range angle refused (base max 170)")
    res = await call("arm", "act_arm_move_joint", {"joint": "base", "angle": 120})
    check(not res.isError, "arm: in-range move works")
    res = await call("drive", "act_drive", {"direction": "forward", "duration_ms": 5000})
    check(res.isError, "drive: duration > 2000 ms refused by schema")
    res = await call("drive", "act_drive", {"direction": "sideways", "duration_ms": 100})
    check(res.isError, "drive: bad enum refused")
    t = time.monotonic()
    res = await call("drive", "act_drive", {"direction": "forward", "duration_ms": 300})
    check(not res.isError and time.monotonic() - t >= 0.3, "drive: timed burst runs then auto-stops")

    # e-stop from the SAFETY node must propagate over the UDP mesh to drive + arm
    res = await call("safety", "safety_stop", {"reason": "test"})
    check(not res.isError, "safety: safety_stop accepted")
    await asyncio.sleep(0.5)
    res = await call("drive", "act_turn", {"direction": "left", "duration_ms": 100})
    check(res.isError and "E-STOP" in res.content[0].text, "drive: actuation refused after REMOTE e-stop")
    res = await call("arm", "act_arm_home")
    check(res.isError, "arm: actuation refused after REMOTE e-stop")
    res = await call("drive", "sense_drive_state")
    check(not res.isError, "drive: sensing still allowed while e-stopped")
    res = await call("drive", "admin_reset_estop", {"confirm": True})
    res = await call("drive", "act_turn", {"direction": "left", "duration_ms": 100})
    check(not res.isError, "drive: works again after admin_reset_estop")

    # e-stop must cut a motion that is IN PROGRESS (server must not block on the running tool)
    async def stop_later():
        await asyncio.sleep(0.4)
        return await call("sensors", "safety_stop", {"reason": "mid-motion test"})
    t = time.monotonic()
    drive_res, _ = await asyncio.gather(
        call("drive", "act_drive", {"direction": "forward", "duration_ms": 2000}), stop_later())
    elapsed = time.monotonic() - t
    check(elapsed < 1.5, f"drive: 2000 ms burst cut short by e-stop from another node ({elapsed:.2f}s)")
    await call("drive", "admin_reset_estop", {"confirm": True})


if __name__ == "__main__":
    impl = sys.argv[1] if len(sys.argv) > 1 else "sdk"
    print(f"== testing MCP_IMPL={impl}")
    env = dict(os.environ, MCP_IMPL=impl, ROBOT_SIM="1", ESTOP_PORT="47999",
               ESTOP_BROADCAST="127.255.255.255", ESTOP_PEERS="127.0.0.1", LOG_LEVEL="WARNING")
    procs = [subprocess.Popen([sys.executable, "-m", "robot_mcp"], cwd=ROOT,
                              env=dict(env, ROBOT_ROLE=r, MCP_PORT=str(p)))
             for r, p in PORTS.items()]
    time.sleep(4)
    try:
        asyncio.run(main())
    finally:
        for p in procs:
            p.terminate()
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)
