"""Smoke test: start the PiCar-X server in simulation and talk to it with the official MCP client.

    pip install "mcp>=1.9,<2" uvicorn pillow pytest
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
EXPECTED = {"drive", "look", "stop", "say", "get_telemetry", "snapshot"}


@pytest.fixture(scope="module")
def server():
    env = {**os.environ, "SIMULATE": "1", "ROBOT_PORT": str(PORT), "SAY_CMD": "true"}
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "robot_mcp.py")], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/telemetry", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
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
    t = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/telemetry"))
    assert t["simulated"] is True
    frame = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/snapshot.jpg").read()
    assert frame[:2] == b"\xff\xd8"
