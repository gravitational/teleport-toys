"""Shared fixtures: a simulated robot, a scripted LLM, a fake Teleport CLI, and the dashboard app as a subprocess."""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).parent.resolve()
BEAM_APP = ROOT / "examples/picar-beambot/beam-app"
ROBOT_DIR = ROOT / "examples/picar-beambot/robot"
HELPERS = ROOT / "tests/helpers"
SCRIPTS = ROOT / "beam-mcp-setup/scripts"

# Importing app.py in-process (test_automap does) must not create files in the source tree.
os.environ.setdefault("SQLITE_PATH", str(Path(tempfile.mkdtemp(prefix="beambot-test-")) / "inproc.db"))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            httpx.get(url, timeout=2)
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"{url} did not come up")


def wait_robot_ready(stream_base, timeout=30):
    """Wait until a freshly started robot can drive, not just answer.

    Known issue (workaround): robot_mcp.py starts serving MCP and /telemetry a few ms before its control loop takes the
    first ultrasonic reading. Until then `distance` is -1 and a forward `drive` is refused with "no valid distance
    reading" (fail-closed, by design). A test that drove straight after `wait_http(.../telemetry)` failed now and then.
    So wait for a real reading: telemetry's `ultrasonic_cm > 0`. Use this, not wait_http, before a test drives."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(f"{stream_base}/telemetry", timeout=2).json().get("ultrasonic_cm", -1) > 0:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"{stream_base} never reported a distance reading")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENAI_", "ANTHROPIC_", "TELEPORT_", "LLM_", "DATABASE_", "REDIS_", "USE_"))}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", **{k: str(v) for k, v in extra.items()}})
    return env


# --------------------------------------------------------------------------- simulated robot
@pytest.fixture(scope="session")
def robot():
    port, stream_port = free_port(), free_port()
    p = subprocess.Popen([sys.executable, str(ROBOT_DIR / "robot_mcp.py")], cwd=ROBOT_DIR,
                         env=_clean_env(SIMULATE=1, ROBOT_PORT=port, STREAM_PORT=stream_port, SAY_CMD="true"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_robot_ready(f"http://127.0.0.1:{stream_port}")
    # `base` is the stream app (video, telemetry, stop: what picar-stream publishes), `mcp` the MCP endpoint.
    yield SimpleNamespace(port=port, stream_port=stream_port, base=f"http://127.0.0.1:{stream_port}",
                          mcp=f"http://127.0.0.1:{port}/mcp", mcp_base=f"http://127.0.0.1:{port}")
    p.terminate()
    p.wait(5)


# --------------------------------------------------------------------------- scripted model provider
@pytest.fixture(scope="session")
def _llm_server():
    port = free_port()
    p = subprocess.Popen([sys.executable, str(HELPERS / "fake_llm.py"), str(port)], env=_clean_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_http(f"http://127.0.0.1:{port}/__requests")
    yield port
    p.terminate()
    p.wait(5)


@pytest.fixture
def llm(_llm_server):
    base = f"http://127.0.0.1:{_llm_server}"

    def script(*steps):
        httpx.post(f"{base}/__script", json={"steps": list(steps)}).raise_for_status()

    script()
    return SimpleNamespace(base=base, script=script, requests=lambda: httpx.get(f"{base}/__requests").json())


# --------------------------------------------------------------------------- fake tsh / tctl
@pytest.fixture
def teleport(tmp_path):
    """A fake Teleport CLI on PATH. Tune with .set(**state); inspect calls with .calls(prog, *prefix)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    beam_home = tmp_path / "beam-home"
    state_file, log_file = tmp_path / "state.json", tmp_path / "calls.jsonl"
    state = {"beam_home": str(beam_home), "counter_file": str(tmp_path / "counter")}
    state_file.write_text(json.dumps(state))
    log_file.touch()
    for prog in ("tsh", "tctl"):
        w = bin_dir / prog
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPERS / "fake_teleport.py"}" {prog} "$@"\n')
        w.chmod(w.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()

    def set_(**kw):
        state.update(kw)
        state_file.write_text(json.dumps(state))

    def calls(prog=None, *prefix):
        rows = [json.loads(line) for line in log_file.read_text().splitlines() if line]
        return [r for r in rows if (prog is None or r["prog"] == prog) and r["args"][:len(prefix)] == list(prefix)]

    def env(**extra):
        e = _clean_env(HOME=home, FAKE_TELEPORT=state_file, FAKE_TELEPORT_LOG=log_file, **extra)
        e["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        return e

    return SimpleNamespace(bin=bin_dir, home=home, beam_home=beam_home, set=set_, calls=calls, env=env)


# --------------------------------------------------------------------------- Postgres (real) and Redis (TCP fake)
@pytest.fixture(scope="session")
def pg_server(tmp_path_factory):
    pgserver = pytest.importorskip("pixeltable_pgserver", reason="pip install pixeltable-pgserver for the Postgres tests")
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield srv
    srv.cleanup()


@pytest.fixture
def pg_url(pg_server):
    """A fresh, empty database in the embedded Postgres, as a libpq URL."""
    import psycopg
    name = "t_" + os.urandom(4).hex()
    with psycopg.connect(pg_server.get_uri(), autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    return pg_server.get_uri().replace("/postgres?", f"/{name}?")


@pytest.fixture
def redis_server():
    """A fake Redis speaking the real wire protocol on a TCP port (RESP2 only, like Teleport's Redis proxy)."""
    fakeredis = pytest.importorskip("fakeredis")
    if not hasattr(fakeredis, "TcpFakeServer"):
        pytest.skip("fakeredis>=2.26 needed for TcpFakeServer")
    import threading
    port = free_port()
    server = fakeredis.TcpFakeServer(("127.0.0.1", port), server_type="redis")
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Teleport's proxy authenticates to Redis itself and accepts whatever the client sends; mimic a password-less user
    import redis
    admin = redis.Redis(host="127.0.0.1", port=port, protocol=2)
    admin.execute_command("ACL", "SETUSER", "app", "on", "nopass", "~*", "+@all")
    admin.close()
    yield SimpleNamespace(url=f"redis://app@127.0.0.1:{port}", port=port)
    server.shutdown()
    server.server_close()


# --------------------------------------------------------------------------- the dashboard app
class AppClient(httpx.Client):
    def chat(self, message, task="count", timeout=60):
        job = self.post("/api/chat", json={"message": message, "task": task}).json()["job"]
        end = time.time() + timeout
        while time.time() < end:
            j = self.get(f"/api/chat/{job}").json()
            if j["status"] != "running":
                return j
            time.sleep(0.2)
        raise TimeoutError("chat job still running")


@pytest.fixture
def start_app(tmp_path, robot, llm, teleport):
    """start_app(**env) -> client for a fresh dashboard process wired to the simulated robot and fake LLM."""
    procs = []

    def start(provider="openai", robot_mcp=None, **extra):
        port = free_port()
        env = teleport.env(**{
            "ROBOT_MCP_URL": robot_mcp or robot.mcp, "ROBOT_STREAM_URL": robot.base, "SQLITE_PATH": tmp_path / "app.db",
            "LLM_PROVIDER": provider, "OPENAI_BASE_URL": f"{llm.base}/v1", "OPENAI_API_KEY": "placeholder",
            "ANTHROPIC_BASE_URL": llm.base, "ANTHROPIC_API_KEY": "placeholder", **extra})   # extra overrides defaults
        p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port), "--log-level", "warning"],
                             cwd=BEAM_APP, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        procs.append(p)
        wait_http(f"http://127.0.0.1:{port}/api/tasks")
        c = AppClient(base_url=f"http://127.0.0.1:{port}", timeout=30)
        c.sqlite_path = tmp_path / "app.db"
        return c

    yield start
    for p in procs:
        p.terminate()
        p.wait(5)
