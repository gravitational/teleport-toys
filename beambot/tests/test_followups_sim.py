"""Follow-ups from the PR 14 review for the five-Pi simulation (examples/advanced-simulation).

Each test starts the real node server (`python -m robot_mcp`) on free ports, or loads the node's package in-process
under its own name (the PiCar's robot_mcp.py is on the test path, so `import robot_mcp` would find that one)."""
import concurrent.futures
import importlib
import importlib.util
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from conftest import ROOT, HELPERS, _clean_env, free_port

EXAMPLE = ROOT / "examples/advanced-simulation"
PI = EXAMPLE / "pi"
DRIVE = {"direction": "forward", "duration_ms": 1500}


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def rpc(port, body, timeout=30):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body.encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def call(port, name, args=None, timeout=30):
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
    return rpc(port, json.dumps(msg), timeout)["result"]


def text(res):
    return res["content"][0]["text"]


def healthz(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def node(tmp_path):
    """node(role, impl, estop_port=None) -> a running node; waits until /healthz answers (any status)."""
    procs = []

    def start(role="drive", impl="stdlib", estop_port=None):
        port, estop_port = free_port(), estop_port or free_udp_port()
        log = open(tmp_path / f"{role}-{impl}-{port}.log", "w")
        # ESTOP_BROADCAST points at loopback: a test must never broadcast STOP to the real LAN.
        p = subprocess.Popen([sys.executable, "-m", "robot_mcp"], cwd=PI, stdout=log, stderr=subprocess.STDOUT,
                             env=_clean_env(ROBOT_ROLE=role, ROBOT_SIM=1, MCP_IMPL=impl, MCP_PORT=port,
                                            ESTOP_PORT=estop_port, ESTOP_BROADCAST="127.0.0.1", ESTOP_PEERS="",
                                            LOG_LEVEL="INFO"))
        procs.append(p)
        end = time.time() + 30
        while True:
            try:
                # __main__ falls back to the stdlib server if the SDK won't import: make sure we test the one asked for
                assert (healthz(port)[1].get("impl") == "sdk") == (impl == "sdk")
                break
            except OSError:
                if time.time() > end or p.poll() is not None:
                    raise TimeoutError(f"node did not come up: {(tmp_path / log.name).read_text()[-2000:]}")
                time.sleep(0.1)
        return SimpleNamespace(port=port, estop_port=estop_port, proc=p)

    yield start
    for p in procs:
        p.terminate()
        p.wait(10)


def ready(n, timeout=10):
    end = time.time() + timeout
    while healthz(n.port)[0] != 200:
        assert time.time() < end, f"node not healthy: {healthz(n.port)}"
        time.sleep(0.05)
    return n


@pytest.fixture
def sim():
    """The node's package loaded in-process under a private name, fresh for each test."""
    name = "fivepi_under_test"
    spec = importlib.util.spec_from_file_location(name, PI / "robot_mcp/__init__.py",
                                                  submodule_search_locations=[str(PI / "robot_mcp")])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[name] = pkg
    spec.loader.exec_module(pkg)
    yield SimpleNamespace(safety=importlib.import_module(f"{name}.safety"),
                          registry=importlib.import_module(f"{name}.registry"))
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[k]


# =========================================================================== 1. no motion after the latch
def test_motion_calls_waiting_for_the_node_do_not_run_after_an_estop(node):
    n = ready(node())
    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        first = ex.submit(call, n.port, "act_drive", DRIVE)
        time.sleep(0.2)                                   # first holds the node; the next two wait for it
        waiting = [ex.submit(call, n.port, "act_drive", DRIVE) for _ in range(2)]
        time.sleep(0.3)
        assert not call(n.port, "safety_stop", {"reason": "test"})["isError"]
        first, waiting = first.result(10), [w.result(10) for w in waiting]
    # The burst in progress is cut short and says so, with the time it really ran.
    assert first["isError"], first
    ran = int(re.search(r"after (\d+) of 1500 ms", text(first)).group(1))
    assert ran < 1500
    # The ones that were waiting never start.
    for w in waiting:
        assert w["isError"] and "E-STOP is latched" in text(w), w
    pose = json.loads(text(call(n.port, "sense_drive_state")))["sim_pose"]
    assert pose["x_cm"] < 12 * 1.5 and not pose["moving"]   # 12 cm/s at the default speed: less than one burst


# =========================================================================== 2. STOP does not queue on the SDK server
def test_safety_stop_does_not_wait_behind_queued_motion_on_the_sdk_server(node):
    n = ready(node(impl="sdk"))
    bursts = 50                                           # more than anyio's 40 worker threads
    with concurrent.futures.ThreadPoolExecutor(bursts) as ex:
        queued = [ex.submit(call, n.port, "act_drive", {"direction": "forward", "duration_ms": 2000}, 120)
                  for _ in range(bursts)]
        time.sleep(1.5)
        t = time.monotonic()
        stop = call(n.port, "safety_stop", {"reason": "flood"}, timeout=60)
        took = time.monotonic() - t
        for q in queued:
            q.result(120)
    assert not stop["isError"]
    assert took < 2, f"safety_stop took {took:.1f} s behind {bursts} queued bursts"


# =========================================================================== 3. the e-stop mesh fails closed
@pytest.mark.parametrize("impl", ["stdlib", "sdk"])
def test_a_node_that_cannot_hear_the_estop_mesh_refuses_motion_and_reports_unhealthy(node, impl):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as squatter:  # takes the port, no SO_REUSE*: bind fails
        squatter.bind(("", 0))
        n = node(impl=impl, estop_port=squatter.getsockname()[1])
        time.sleep(0.5)                                   # the listener thread has tried and failed by now
        code, body = healthz(n.port)
        assert code == 503 and body["ok"] is False, (code, body)
        res = call(n.port, "act_drive", {"direction": "forward", "duration_ms": 100})
        assert res["isError"] and "listener" in text(res), res
        assert not call(n.port, "safety_drive_stop")["isError"]      # stopping still works


def test_a_listener_that_dies_later_also_blocks_motion(sim, monkeypatch):
    safety = sim.safety
    safety.ESTOP_PORT = free_udp_port()
    with pytest.raises(sim.registry.ToolError, match="listener"):
        safety.require_armed()                            # not started yet
    safety.start_listener()
    end = time.time() + 5
    while not safety.listener_up():
        assert time.time() < end
        time.sleep(0.01)
    safety.require_armed()                                # listening: motion allowed

    def crash(*_a, **_k):
        raise RuntimeError("listener crashed")
    monkeypatch.setattr(safety, "trip", crash)
    crashes = []
    monkeypatch.setattr(threading, "excepthook", crashes.append)       # the crash is the point: keep it out of the report
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(safety.ESTOP_MAGIC + b"|x", ("127.0.0.1", safety.ESTOP_PORT))
    end = time.time() + 5
    while safety.listener_up():
        assert time.time() < end, "listener thread died but the node still says it is listening"
        time.sleep(0.01)
    with pytest.raises(sim.registry.ToolError, match="listener"):
        safety.require_armed()
    for t in threading.enumerate():
        if t.name == "estop-listener":
            t.join(5)
    assert [type(c.exc_value) for c in crashes] == [RuntimeError]


# =========================================================================== 4. NaN is refused
@pytest.mark.parametrize("impl", ["stdlib", "sdk"])
def test_nan_speed_is_refused_and_odometry_stays_finite(node, impl):
    n = ready(node(impl=impl))
    msg = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "act_drive", '
           '"arguments": {"direction": "forward", "duration_ms": 100, "speed": NaN}}}')
    reply = rpc(n.port, msg)
    assert "error" in reply or reply["result"]["isError"], reply
    pose = json.loads(text(call(n.port, "sense_drive_state")))["sim_pose"]
    assert all(math.isfinite(pose[k]) for k in ("x_cm", "y_cm", "heading_deg")), pose


def test_the_validator_refuses_nan_and_infinity(sim):
    schema = {"properties": {"v": {"type": "number"}}}
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(sim.registry.ToolError):
            sim.registry.validate(schema, {"v": bad})
    assert sim.registry.validate(schema, {"v": 0.5}) == {"v": 0.5}
    with pytest.raises(sim.registry.ToolError, match="<= 1.0"):                # a huge int is refused, not a crash
        sim.registry.validate({"properties": {"v": {"type": "number", "maximum": 1.0}}}, {"v": 10 ** 400})


# =========================================================================== 6. the join token stays off command lines
@pytest.fixture
def pis(tmp_path):
    """deploy.sh copied next to its kit and a cluster.conf, with fake ssh/scp that log argv and stdin."""
    ex = tmp_path / "ex"
    ex.mkdir()
    (ex / "deploy.sh").write_text((EXAMPLE / "deploy.sh").read_text())
    subprocess.run(["cp", "-r", str(PI), str(ex / "pi")], check=True)
    (ex / "cluster.conf").write_text("pi1 192.0.2.11 safety\npi3 192.0.2.13 drive\n")
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.jsonl"
    bin_dir.mkdir()
    log.touch()
    for prog in ("ssh", "scp"):
        w = bin_dir / prog
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPERS / "fake_cli.py"}" {prog} "$@"\n')
        w.chmod(0o755)

    def run(**env):
        e = _clean_env(FAKE_CLI_LOG=log, TELEPORT_PROXY="example.beams.run:443", **env)
        e["PATH"] = f"{bin_dir}:{e['PATH']}"
        return subprocess.run(["bash", str(ex / "deploy.sh")], env=e, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines() if line]

    return SimpleNamespace(run=run, calls=calls)


@pytest.mark.parametrize("parallel", ["0", "1"])
def test_deploy_sends_the_join_token_on_stdin_never_on_a_command_line(pis, parallel):
    token = "fake-join-token-5c1d"
    r = pis.run(TELEPORT_TOKEN=token, PARALLEL=parallel)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = pis.calls()
    assert token not in json.dumps([c["args"] for c in calls])            # not in ps, not in sudo's log
    sent = [c for c in calls if c["prog"] == "ssh" and c["stdin"] == token]
    assert len(sent) == 2 and all("rm -f /var/tmp/beambot-sim.token && umask 077" in c["args"][-1] for c in sent)
    setups = [c["args"][-1] for c in calls if c["prog"] == "ssh" and "setup_pi.sh" in c["args"][-1]]
    assert len(setups) == 2 and all("--token-file /var/tmp/beambot-sim.token" in s for s in setups)
    # The Pi drops the token file however setup ends (a bad EXTRA flag, sudo without a terminal, a failed untar).
    assert all(s.startswith("trap 'rm -f /var/tmp/beambot-sim.token' EXIT;") for s in setups)


def test_deploy_without_a_token_sends_none(pis):
    r = pis.run(PARALLEL="0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in pis.calls() if c["stdin"]]
    assert all("--token" not in c["args"][-1] for c in pis.calls() if c["prog"] == "ssh")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="setup_pi.sh would really run as root")
def test_setup_pi_reads_the_token_file_and_deletes_it(tmp_path):
    f = tmp_path / "join.token"
    f.write_text("fake-join-token-5c1d")
    f.chmod(0o600)
    r = subprocess.run(["bash", str(PI / "setup_pi.sh"), "--role", "drive", "--proxy", "example.beams.run:443",
                        "--token-file", str(f)], capture_output=True, text=True, timeout=30, env=_clean_env())
    assert r.returncode != 0 and "run as root" in r.stderr                 # stops before touching the system
    assert not f.exists()


# =========================================================================== 7. no fixed ports
def test_two_runs_of_the_simulation_test_at_once_do_not_collide():
    runs = [subprocess.Popen([sys.executable, "tests/test_mcp_client.py", "stdlib"], cwd=EXAMPLE, env=_clean_env(),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(2)]
    outs = [p.communicate(timeout=150)[0] for p in runs]
    for p, out in zip(runs, outs):
        assert p.returncode == 0 and "ALL PASSED" in out, out[-3000:]


# =========================================================================== 8. MAX_MOTION_MS is pinned
def test_max_motion_ms_stays_at_two_seconds(sim, monkeypatch):
    monkeypatch.delenv("MAX_MOTION_MS", raising=False)
    safety = importlib.reload(sim.safety)
    assert safety.MAX_MOTION_MS <= 2000                                     # the code's default
    shipped = re.search(r"^MAX_MOTION_MS=(\d+)$", (PI / "setup_pi.sh").read_text(), re.M)
    assert shipped and int(shipped.group(1)) <= 2000                        # what setup_pi.sh writes on the Pi
    safety.MAX_MOTION_MS = 40                                               # and timed() holds any caller to it
    t = time.monotonic()
    assert safety.timed(600000, lambda: None, lambda: None) == 40
    assert time.monotonic() - t < 1
