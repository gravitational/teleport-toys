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
TURN = {"direction": "left", "duration_ms": 100}


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HEARTBEAT_MAGIC = _module(PI / "robot_mcp/safety.py", "fivepi_safety_constants").HEARTBEAT_MAGIC
ROBOT_NAME = "beambot-sim"


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


class Beat:
    """Plays a safety node's heartbeat at one node's e-stop port; pause() makes the mesh go quiet."""

    def __init__(self, port, robot=ROBOT_NAME):
        self.packet = HEARTBEAT_MAGIC + b"|" + robot.encode()
        self.port, self.on, self.done = port, threading.Event(), threading.Event()
        self.on.set()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            while not self.done.is_set():
                if self.on.is_set():
                    s.sendto(self.packet, ("127.0.0.1", self.port))
                time.sleep(0.1)

    def pause(self):
        self.on.clear()

    def resume(self):
        self.on.set()


@pytest.fixture
def node(tmp_path):
    """node(role, impl, estop_port=None, heartbeat=True, **env) -> a running node; waits until /healthz answers
    (any status). heartbeat=True plays the safety node's heartbeat at it (n.beat); a real safety node needs none."""
    procs, beats = [], []

    def start(role="drive", impl="stdlib", estop_port=None, heartbeat=True, **env):
        port, estop_port = free_port(), estop_port or free_udp_port()
        log = open(tmp_path / f"{role}-{impl}-{port}.log", "w")
        # ESTOP_BROADCAST points at loopback: a test must never broadcast STOP to the real LAN.
        env = {"ESTOP_BROADCAST": "127.0.0.1", "ESTOP_PEERS": "", "ESTOP_HEARTBEAT_TIMEOUT_MS": 1000,
               "ROBOT_NAME": ROBOT_NAME, **env}
        p = subprocess.Popen([sys.executable, "-m", "robot_mcp"], cwd=PI, stdout=log, stderr=subprocess.STDOUT,
                             env=_clean_env(ROBOT_ROLE=role, ROBOT_SIM=1, MCP_IMPL=impl, MCP_PORT=port,
                                            ESTOP_PORT=estop_port, LOG_LEVEL="INFO", **env))
        procs.append(p)
        beat = Beat(estop_port) if heartbeat else None
        beats.append(beat)
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
        return SimpleNamespace(port=port, estop_port=estop_port, proc=p, beat=beat)

    yield start
    for b in beats:
        if b:
            b.done.set()
    for p in procs:
        if p.poll() is None:
            p.terminate()
            p.wait(10)


def ready(n, timeout=10, code=200):
    """Wait until /healthz answers `code` (200: healthy, 503: not)."""
    end = time.time() + timeout
    while healthz(n.port)[0] != code:
        assert time.time() < end, f"node never answered {code}: {healthz(n.port)}"
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
        assert code == 503 and body["ok"] is False and body["estop_listener"] is False, (code, body)
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
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(safety.HEARTBEAT, ("127.0.0.1", safety.ESTOP_PORT))
    while not safety.heartbeat_ok():
        assert time.time() < end
        time.sleep(0.01)
    safety.require_armed()                                # listening and hearing the safety node: motion allowed

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


# =========================================================================== 3b. heartbeat: no safety node, no motion
@pytest.mark.parametrize("impl", ["stdlib", "sdk"])
def test_a_quiet_mesh_blocks_motion_and_a_recovered_one_allows_it(node, impl):
    n = ready(node(impl=impl))
    assert not call(n.port, "act_turn", TURN)["isError"]
    n.beat.pause()                                        # the safety node goes quiet
    ready(n, code=503)
    body = healthz(n.port)[1]
    assert body["ok"] is False and body["safety_heartbeat"] is False and body["estop_listener"] is True, body
    res = call(n.port, "act_turn", TURN)
    assert res["isError"] and "No heartbeat from the safety node" in text(res), res
    # Stopping, looking and resetting never wait for the mesh.
    assert not call(n.port, "safety_drive_stop")["isError"]
    assert not call(n.port, "sense_drive_state")["isError"]
    assert not call(n.port, "safety_stop", {"reason": "quiet mesh"})["isError"]
    assert not call(n.port, "admin_reset_estop", {"confirm": True})["isError"]
    n.beat.resume()                                       # it is back
    ready(n)
    assert not call(n.port, "act_turn", TURN)["isError"]


def test_another_robots_heartbeat_does_not_count(node):
    assert "\nROBOT_NAME=$ROBOT\n" in (PI / "setup_pi.sh").read_text()    # each Pi knows its robot's name
    n = node(heartbeat=False, ROBOT_NAME="robot-a")
    other = Beat(n.estop_port, robot=ROBOT_NAME)           # another robot's safety node, same LAN and port
    try:
        time.sleep(1.5)
        assert healthz(n.port)[0] == 503
        res = call(n.port, "act_turn", TURN)
        assert res["isError"] and "No heartbeat from the safety node" in text(res), res
    finally:
        other.done.set()
    own = Beat(n.estop_port, robot="robot-a")
    try:
        ready(n)                                           # its own safety node counts
        assert not call(n.port, "act_turn", TURN)["isError"]
    finally:
        own.done.set()


def test_the_safety_node_is_the_heartbeat_and_the_others_stop_when_it_dies(node):
    mesh = free_udp_port()
    # Loopback broadcast reaches every node on the port, as LAN broadcast does on the Pis.
    shared = dict(estop_port=mesh, heartbeat=False, ESTOP_BROADCAST="127.255.255.255", ESTOP_HEARTBEAT_MS=200)
    guard = ready(node(role="safety", **shared))
    assert not call(guard.port, "act_signal", {"pattern": "idle"})["isError"]   # it doesn't wait for itself
    drive = ready(node(**shared))
    assert not call(drive.port, "act_turn", TURN)["isError"]
    guard.proc.kill()                                     # the safety node dies
    guard.proc.wait(10)
    ready(drive, code=503)
    res = call(drive.port, "act_turn", TURN)
    assert res["isError"] and "No heartbeat from the safety node" in text(res), res
    assert not call(drive.port, "safety_drive_stop")["isError"]
    ready(node(role="safety", **shared))                  # a new one comes up
    ready(drive)
    assert not call(drive.port, "act_turn", TURN)["isError"]


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
FAKE_TCTL = r'''
import json, os, sys
args = sys.argv[1:]
path = os.environ["FAKE_TCTL_STATE"]
with open(path) as f:
    st = json.load(f)
with open(os.environ["FAKE_CLI_LOG"], "a") as f:
    f.write(json.dumps({"prog": "tctl", "args": args, "stdin": ""}) + "\n")
if args[:2] == ["tokens", "add"]:
    print(MADE)
elif args[:2] == ["tokens", "rm"]:
    sys.exit(st.get("rm_exit", 0))
elif args[:1] == ["get"]:
    kind = args[1]
    polls = st.setdefault("polls", {})
    polls[kind] = polls.get(kind, 0) + 1
    with open(path, "w") as f:
        json.dump(st, f)
    recs = st["stale"] + (st["joined"] if polls[kind] > st["after"].get(kind, 0) else [])
    if kind == "nodes":
        print(json.dumps([{"kind": "node", "metadata": {"name": i}, "spec": {"hostname": n}} for i, n in recs]))
    else:
        print(json.dumps([{"kind": "app_server", "metadata": {"name": n},
                           "spec": {"host_id": i, "app": {"kind": "app", "metadata": {"name": n}}}} for i, n in recs]))
else:
    sys.exit("fake tctl: unexpected " + " ".join(args))
'''
MADE = "fake-made-token-7e2a"
BOTH = [["uuid-new-pi1", "beambot-sim-safety"], ["uuid-new-pi3", "beambot-sim-drive"]]   # [host ID, name]


@pytest.fixture
def pis(tmp_path):
    """deploy.sh copied next to its kit and a cluster.conf, with fake ssh/scp/tctl that log argv and stdin.
    run(joined=[[id, name]...], after={"nodes": n, "app_server": m}, stale=[...], **env): tctl lists `stale` always
    and `joined` from its n+1-th / m+1-th poll on. Each Pi reports host ID uuid-new-<host> (none with FAKE_NO_HOST_ID).
    FAKE_SSH_FAIL=1 makes every setup_pi.sh run fail."""
    ex = tmp_path / "ex"
    ex.mkdir()
    (ex / "deploy.sh").write_text((EXAMPLE / "deploy.sh").read_text())
    subprocess.run(["cp", "-r", str(PI), str(ex / "pi")], check=True)
    (ex / "cluster.conf").write_text("pi1 192.0.2.11 safety\npi3 192.0.2.13 drive\n")
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.jsonl"
    bin_dir.mkdir()
    log.touch()
    (bin_dir / "scp").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPERS / "fake_cli.py"}" scp "$@"\n')
    (bin_dir / "ssh").write_text(f'#!/bin/sh\n"{sys.executable}" "{HELPERS / "fake_cli.py"}" ssh "$@" || exit\n'
                                 'case "$*" in\n'
                                 '  *setup_pi.sh*) [ -n "$FAKE_SSH_FAIL" ] && exit 1 ;;\n'
                                 '  *"cat /var/tmp/beambot-sim.host_id"*) [ -n "$FAKE_NO_HOST_ID" ] || printf "uuid-new-%s" "$1" ;;\n'
                                 'esac\nexit 0\n')
    (tmp_path / "fake_tctl.py").write_text(FAKE_TCTL.replace("MADE", repr(MADE)))
    (bin_dir / "tctl").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "fake_tctl.py"}" "$@"\n')
    for prog in ("ssh", "scp", "tctl"):
        (bin_dir / prog).chmod(0o755)
    state = tmp_path / "tctl.json"

    def run(joined=(), after=None, stale=(), rm_exit=0, **env):
        state.write_text(json.dumps({"joined": list(joined), "after": after or {}, "stale": list(stale),
                                     "rm_exit": rm_exit}))
        e = _clean_env(**{"FAKE_CLI_LOG": log, "FAKE_TCTL_STATE": state, "TELEPORT_PROXY": "example.beams.run:443",
                          "JOIN_TIMEOUT": 10, "JOIN_POLL": 0.05, **env})
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
    assert not [c for c in calls if c["prog"] == "tctl"]                    # a token you pass is yours: not revoked
    assert "Then revoke the join token you passed in" in r.stdout


def tctl_calls(pis, *prefix):
    return [i for i, c in enumerate(pis.calls()) if c["prog"] == "tctl" and c["args"][:len(prefix)] == list(prefix)]


@pytest.mark.parametrize("parallel", ["0", "1"])
def test_deploy_makes_its_own_token_and_revokes_it_only_once_every_node_joined(pis, parallel):
    # The SSH nodes show up on the 3rd poll, the MCP apps only on the 5th: the revoke must wait for both.
    r = pis.run(joined=BOTH, after={"nodes": 2, "app_server": 4}, PARALLEL=parallel)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = pis.calls()
    assert [calls[i]["args"] for i in tctl_calls(pis, "tokens", "add")] == [
        ["tokens", "add", "--type=node,app", "--ttl=1h", "--format=text"]]
    assert MADE not in json.dumps([c["args"] for c in calls if c["prog"] in ("ssh", "scp")])
    assert len([c for c in calls if c["prog"] == "ssh" and c["stdin"] == MADE]) == 2
    rm = tctl_calls(pis, "tokens", "rm")
    assert [calls[i]["args"] for i in rm] == [["tokens", "rm", MADE]]
    assert rm[0] > tctl_calls(pis, "get", "app_server")[4]          # only after the poll that listed every app
    assert "join token revoked" in r.stdout and "still valid" not in r.stderr


@pytest.mark.parametrize("late", ["nodes", "app_server"])
def test_deploy_waits_for_the_pis_own_host_id_not_an_old_record_with_its_name(pis, late):
    # A replaced or re-flashed Pi: Teleport still lists the old one under the same name for a while, and the new
    # Pi's SSH node or its MCP app shows up only on the 5th poll.
    old = [["uuid-old-1", "beambot-sim-safety"], ["uuid-old-3", "beambot-sim-drive"]]
    r = pis.run(joined=BOTH, stale=old, after={late: 4})
    assert r.returncode == 0, r.stdout + r.stderr
    rm = tctl_calls(pis, "tokens", "rm")
    assert len(rm) == 1 and rm[0] > tctl_calls(pis, "get", late)[4]


def test_deploy_waits_for_the_ssh_node_as_well_as_the_app(pis):
    r = pis.run(joined=BOTH, after={"nodes": 10 ** 6}, JOIN_TIMEOUT=1)
    assert r.returncode == 1 and not tctl_calls(pis, "tokens", "rm")


def test_deploy_keeps_its_token_when_a_node_never_joins(pis):
    r = pis.run(joined=BOTH[:1], JOIN_TIMEOUT=1)
    assert r.returncode == 1
    assert not tctl_calls(pis, "tokens", "rm")
    assert "doesn't list these as joined: beambot-sim-drive" in r.stderr
    assert f"tctl tokens rm {MADE}" in r.stderr                          # says so, and how to clean up


def test_deploy_keeps_its_token_when_it_cannot_tell_which_host_joins(pis):
    r = pis.run(joined=BOTH, FAKE_NO_HOST_ID=1)
    assert r.returncode == 1
    assert not tctl_calls(pis, "tokens", "rm") and not tctl_calls(pis, "get")
    assert "couldn't read the Teleport host ID of: pi1 pi3" in r.stderr and f"tctl tokens rm {MADE}" in r.stderr


@pytest.mark.parametrize("parallel", ["0", "1"])
def test_deploy_keeps_its_token_when_a_deploy_fails(pis, parallel):
    r = pis.run(joined=BOTH, PARALLEL=parallel, FAKE_SSH_FAIL=1)
    assert r.returncode != 0
    assert not tctl_calls(pis, "tokens", "rm") and not tctl_calls(pis, "get")
    assert f"tctl tokens rm {MADE}" in r.stderr


def test_deploy_says_so_when_the_revoke_fails(pis):
    r = pis.run(joined=BOTH, rm_exit=1)
    assert r.returncode == 1
    assert len(tctl_calls(pis, "tokens", "rm")) == 1
    assert "Could not revoke" in r.stderr and f"tctl tokens rm {MADE}" in r.stderr


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
