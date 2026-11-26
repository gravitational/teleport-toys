"""Opt-in checks against real Teleport. `BEAMS_LIVE=1 pytest tests/live`. See README.md in this folder."""
import json
import os
import shutil
import subprocess
import sys
import time

import httpx
import pytest

from conftest import BEAM_APP, free_port, wait_http

pytestmark = pytest.mark.live
if os.environ.get("BEAMS_LIVE") != "1":
    pytest.skip("set BEAMS_LIVE=1 inside a real beam", allow_module_level=True)
if not shutil.which("tsh"):
    pytest.skip("tsh not found", allow_module_level=True)

MCP_APP = os.environ.get("LIVE_MCP_APP", "picar-mcp")
STREAM_APP = os.environ.get("LIVE_STREAM_APP", "picar-stream")
DB = os.environ.get("LIVE_DB", "beambot-postgres")
DB_USER = os.environ.get("LIVE_DB_USER", "app")
DB_NAME = os.environ.get("LIVE_DB_NAME", "beambot")
REDIS = os.environ.get("LIVE_REDIS", "beambot-redis")


def tsh(*args, timeout=60):
    return subprocess.run(["tsh", *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture
def tunnel():
    procs = []

    def open_(*args, ready_url=None, wait=4):
        p = subprocess.Popen(["tsh", "proxy", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
        if ready_url:
            wait_http(ready_url, timeout=20)
        else:
            time.sleep(wait)
        assert p.poll() is None, f"tunnel exited early: {p.stdout.read()}"
        return p

    yield open_
    for p in procs:
        p.terminate()


def test_session_is_active():
    r = tsh("status")
    assert r.returncode == 0, r.stderr


def test_mcp_server_is_visible():
    r = tsh("mcp", "ls", "-v")
    assert r.returncode == 0 and MCP_APP in r.stdout, r.stdout + r.stderr


def test_mcp_over_stdio_connect_needs_no_extra_identity():
    """`tsh mcp connect` is what Claude Code runs. 'not allowed to reissue' here means a non-production beam."""
    msgs = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]) + "\n"
    p = subprocess.Popen(["tsh", "mcp", "connect", MCP_APP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        p.stdin.write(msgs); p.stdin.flush()
        seen = {}
        end = time.time() + 30
        while time.time() < end and len(seen) < 2:
            line = p.stdout.readline()
            if not line:
                break
            m = json.loads(line)
            if "id" in m:
                seen[m["id"]] = m
        assert 1 in seen, p.stderr.read() if p.poll() is not None else "no initialize reply"
        assert "reissue" not in json.dumps(seen)
        assert 2 in seen and seen[2]["result"]["tools"], "tools/list returned nothing (RBAC hides every tool?)"
    finally:
        p.terminate()


def test_mcp_tools_over_a_local_tunnel(tunnel):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    import anyio
    port = free_port()
    tunnel("mcp", MCP_APP, "-p", str(port))

    async def go():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert names, "no tools visible"
                if "get_telemetry" in names:
                    res = await s.call_tool("get_telemetry", {})
                    assert not res.isError
                if os.environ.get("LIVE_DRIVE") == "1" and "drive" in names:
                    res = await s.call_tool("drive", {"speed": 15, "seconds": 0.5})
                    assert not res.isError
                    # A speed-0 drive ends the move without latching. Do NOT call `stop` here: it latches the
                    # real robot's emergency stop, and a driver beam cannot resume it.
                    await s.call_tool("drive", {"speed": 0, "seconds": 0.1})
    anyio.run(go)


def test_stream_app_through_teleport(tunnel):
    if not STREAM_APP:
        pytest.skip("LIVE_STREAM_APP is empty")
    port = free_port()
    tunnel("app", STREAM_APP, "--port", str(port))
    assert httpx.get(f"http://127.0.0.1:{port}/telemetry", timeout=10).status_code == 200


def test_postgres_roundtrip_through_teleport(tunnel):
    if DB not in tsh("db", "ls").stdout:
        pytest.skip(f"database {DB} is not visible to this identity")
    psycopg = pytest.importorskip("psycopg")
    port = free_port()
    tunnel("db", "--tunnel", f"--db-user={DB_USER}", f"--db-name={DB_NAME}", "-p", str(port), DB)
    url = f"postgresql://{DB_USER}@localhost:{port}/{DB_NAME}"
    with psycopg.connect(url, connect_timeout=10) as c:
        assert c.execute("SELECT 1").fetchone() == (1,)
    os.environ["DATABASE_URL"] = url
    sys.path.insert(0, str(BEAM_APP))
    import importlib
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    try:
        assert app.store.kind == "Postgres", app.store.error
        app.store.add_count("live-test", 1, 99, {"x_cm": 0, "y_cm": 0, "heading_deg": 0}, "pytest")
        assert any(r["object"] == "live-test" for r in app.store.counts())
    finally:
        app.store.clear()
        sys.modules.pop("app", None)
        os.environ.pop("DATABASE_URL", None)


def test_redis_resp2_through_teleport(tunnel):
    if REDIS not in tsh("db", "ls").stdout:
        pytest.skip(f"database {REDIS} is not visible to this identity")
    redis = pytest.importorskip("redis")
    port = free_port()
    tunnel("db", "--tunnel", f"--db-user={DB_USER}", "-p", str(port), REDIS)
    r = redis.Redis.from_url(f"redis://{DB_USER}@localhost:{port}", protocol=2, socket_timeout=5)
    assert r.ping()


def test_start_sh_serves_the_dashboard():
    """The real launch script, real tunnels, real robot: the dashboard comes up and sees the robot's tools."""
    port = 8080
    p = subprocess.Popen(["bash", str(BEAM_APP / "start.sh")], cwd=BEAM_APP, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    try:
        wait_http(f"http://127.0.0.1:{port}/api/tasks", timeout=120)
        card = httpx.get(f"http://127.0.0.1:{port}/api/beam", timeout=20).json()
        assert card["mcp"]["error"] is None, card["mcp"]["error"]
        assert card["mcp"]["allowed"], "robot tools not visible through the tunnel"
    finally:
        p.terminate()
        subprocess.run(["pkill", "-f", "tsh proxy"], check=False)
