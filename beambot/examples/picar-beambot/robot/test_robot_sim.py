"""Smoke test: start the PiCar-X server in simulation and talk to it with the official MCP client.

    pip install "mcp>=1.11,<2" uvicorn pillow pytest
    pytest robot/test_robot_sim.py
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18765
STREAM = 18766
EXPECTED = {"drive", "look", "stop", "resume", "say", "get_telemetry", "snapshot"}


@pytest.fixture(scope="module")
def server():
    env = {**os.environ, "SIMULATE": "1", "ROBOT_PORT": str(PORT), "STREAM_PORT": str(STREAM), "SAY_CMD": "true"}
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "robot_mcp.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for a real distance reading, not just an answer: the server is up a few ms before its control loop's first
    # ultrasonic read, and until then a forward drive is refused (see wait_robot_ready in beambot/conftest.py).
    for _ in range(200):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{STREAM}/telemetry", timeout=1) as r:
                if json.load(r).get("ultrasonic_cm", -1) > 0:
                    break
        except Exception:
            pass
        time.sleep(0.05)
    else:
        p.kill()
        pytest.fail("simulated robot did not start")
    yield p
    p.terminate()


async def _session():
    async with streamablehttp_client(f"http://127.0.0.1:{PORT}/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            drive = await s.call_tool("drive", {"speed": 20, "seconds": 0.5})
            tele = await s.call_tool("get_telemetry", {})
            snap = await s.call_tool("snapshot", {})
            stop = await s.call_tool("stop", {})
            return tools, drive, tele, snap, stop


def test_tools_and_calls(server):
    tools, drive, tele, snap, stop = asyncio.run(_session())
    assert tools == EXPECTED
    assert not drive.isError and not stop.isError
    pose = json.loads(tele.content[0].text)["pose"]
    assert pose["x_cm"] > 0, "driving forward should advance the dead-reckoned pose"
    assert snap.content[0].type == "image"


def test_http_faces(server):
    t = json.load(urllib.request.urlopen(f"http://127.0.0.1:{STREAM}/telemetry"))
    assert t["simulated"] is True
    frame = urllib.request.urlopen(f"http://127.0.0.1:{STREAM}/snapshot.jpg").read()
    assert frame[:2] == b"\xff\xd8"
