"""Regression tests for the four must-fix review findings. Each test reproduces the reviewer's scenario.

1. a viewer must not be able to drive through the plain HTTP app
2. STOP must stop the agent, not just the car
3. a hardware fault must never leave the motors running
4. the dashboard must not run script smuggled in through the shared Redis feed
"""
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import yaml

from conftest import ROOT, ROBOT_DIR, BEAM_APP, _clean_env, free_port, wait_http  # noqa

EXAMPLE = ROOT / "examples/picar-beambot"
MCP_LABEL = "teleport.internal/app-sub-kind"


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def own_robot():
    """A private simulated robot (the shared one has been driven around and may be next to a wall)."""
    procs = []

    def spawn(**env):
        port, stream = free_port(), free_port()
        p = subprocess.Popen([sys.executable, str(ROBOT_DIR / "robot_mcp.py")], cwd=ROBOT_DIR,
                             env=_clean_env(SIMULATE=1, ROBOT_PORT=port, STREAM_PORT=stream, SAY_CMD="true", **env),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        wait_http(f"http://127.0.0.1:{stream}/telemetry")
        return SimpleNamespace(proc=p, port=port, stream_port=stream, mcp=f"http://127.0.0.1:{port}/mcp",
                               mcp_base=f"http://127.0.0.1:{port}", stream=f"http://127.0.0.1:{stream}")

    yield spawn
    for p in procs:
        p.terminate()
        p.wait(5)


def tele(r):
    return httpx.get(f"{r.stream}/telemetry").json()


MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
DRIVE_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "drive", "arguments": {"speed": 20, "seconds": 1}}}


# =========================================================================== 1. picar-stream cannot drive
def test_stream_port_has_no_mcp_and_cannot_drive(own_robot):
    r = own_robot()
    x0 = tele(r)["pose"]["x_cm"]
    for path in ("/mcp", "/mcp/"):
        resp = httpx.post(f"{r.stream}{path}", headers=MCP_HEADERS, json=DRIVE_CALL)
        assert resp.status_code == 404, f"the stream port must not serve {path}"
    time.sleep(1.5)
    t = tele(r)
    assert t["pose"]["x_cm"] == x0 and t["moving"] is False, "a POST to the stream app moved the car"
    # the stream app still does its job: watch and stop
    assert httpx.get(f"{r.stream}/snapshot.jpg").content[:2] == b"\xff\xd8"
    assert httpx.post(f"{r.stream}/stop").json() == {"result": "stopped", "halted": True}


def test_mcp_port_serves_only_mcp(own_robot):
    r = own_robot()
    for path in ("/telemetry", "/snapshot.jpg", "/video.mjpg"):
        assert httpx.get(f"{r.mcp_base}{path}").status_code == 404
    assert httpx.post(f"{r.mcp_base}/stop").status_code == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    assert httpx.post(r.mcp, headers=MCP_HEADERS, json=init).status_code == 200


def _apps_and_roles():
    robot = yaml.safe_load((EXAMPLE / "robot/teleport.yaml").read_text())["app_service"]["apps"]
    k8s = yaml.safe_load((EXAMPLE / "k8s/teleport-agent-values.yaml").read_text())["apps"]
    roles = {d["metadata"]["name"]: d["spec"]["allow"]
             for d in yaml.safe_load_all((EXAMPLE / "teleport/roles.yaml").read_text()) if d and d["kind"] == "role"}
    return {"robot/teleport.yaml": robot, "k8s values": k8s}, roles


def _port(uri):
    return uri.rsplit(":", 1)[1].split("/")[0]


def test_the_http_app_is_never_on_the_port_that_serves_mcp():
    sets, _ = _apps_and_roles()
    for where, apps in sets.items():
        by = {a["name"]: a for a in apps}
        assert by["picar-mcp"]["uri"].startswith("mcp+http://") and by["picar-mcp"]["uri"].endswith(":8765/mcp"), where
        assert by["picar-stream"]["uri"].startswith("http://") and not by["picar-stream"]["uri"].startswith("mcp+"), where
        assert _port(by["picar-stream"]["uri"]) == "8766" != _port(by["picar-mcp"]["uri"]), where


def test_roles_cannot_reach_the_stream_app_with_a_tool_grant():
    """Replays Teleport's label matching: a role matches an app when every label it names is present."""
    sets, roles = _apps_and_roles()
    for where, apps in sets.items():
        eff = {a["name"]: {**a["labels"], **({MCP_LABEL: "mcp"} if a["uri"].startswith("mcp+") else {})} for a in apps}
        matches = lambda role, app: all(eff[app].get(k) == v for k, v in roles[role]["app_labels"].items())  # noqa: E731
        for role in ("picar-viewer", "picar-driver"):
            assert roles[role]["mcp"]["tools"]
            assert roles[role]["app_labels"].get(MCP_LABEL) == "mcp", f"{role} must only ever match MCP apps"
            assert matches(role, "picar-mcp") and not matches(role, "picar-stream"), (where, role)
        assert "mcp" not in roles["picar-stream"]
        assert matches("picar-stream", "picar-stream") and not matches("picar-stream", "picar-mcp"), where


# =========================================================================== 2. STOP stops the agent
def _script_six_drives(llm):
    llm.script(*[{"tool": "drive", "args": {"speed": 20, "seconds": 1}}] * 6, {"text": "all done"})


def test_stop_cancels_the_running_chat_job_and_latches(own_robot, start_app, llm):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    _script_six_drives(llm)
    job = app.post("/api/chat", json={"message": "go", "task": "count"}).json()["job"]
    time.sleep(1.5)
    assert tele(r)["moving"] is True
    resp = app.post("/api/stop")
    assert resp.status_code == 200 and resp.json()["ok"] and resp.json()["halted"]
    x = tele(r)["pose"]["x_cm"]
    time.sleep(3.5)          # before the fix the agent's next drive started the car again here
    t = tele(r)
    assert t["moving"] is False and t["pose"]["x_cm"] == pytest.approx(x, abs=0.5), "the agent drove after STOP"
    j = app.get(f"/api/chat/{job}").json()
    assert j["status"] == "stopped", j
    assert len([q for q in llm.requests()]) <= 3, "the model kept being asked after STOP"

    # latched: nothing moves until the user resumes
    assert app.get("/api/state").json()["halted"] is True
    assert app.post("/api/chat", json={"message": "again", "task": "count"}).status_code == 423
    assert app.post("/api/drive", json={"speed": 20, "seconds": 1}).status_code == 423
    assert app.post("/api/look", json={"pan_deg": 10, "tilt_deg": 0}).status_code == 423
    assert app.post("/api/automap/start", json={"vision": False}).status_code == 423
    assert tele(r)["moving"] is False
    assert app.post("/api/drive/stop").json()["ok"], "stop itself is always allowed"

    assert app.post("/api/resume").json() == {"ok": True, "halted": False, "via": "stream"}
    assert app.get("/api/state").json()["halted"] is False
    assert app.post("/api/drive", json={"speed": 20, "seconds": 0.3}).json()["ok"]


def test_stop_without_a_stream_app_falls_back_to_the_mcp_stop_tool(own_robot, start_app):
    """ROBOT_STREAM_APP empty (the Kubernetes path): /api/stop used to return 500 and leave the car running."""
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL="http://127.0.0.1:1")
    assert app.post("/api/drive", json={"speed": 20, "seconds": 3}).json()["ok"]
    time.sleep(0.4)
    assert tele(r)["moving"] is True
    resp = app.post("/api/stop")
    assert resp.status_code == 200 and resp.json()["via"] == "mcp"
    time.sleep(0.3)
    assert tele(r)["moving"] is False


def test_stop_that_cannot_reach_the_robot_says_so(start_app):
    app = start_app(robot_mcp="http://127.0.0.1:1/mcp", ROBOT_STREAM_URL="http://127.0.0.1:1")
    resp = app.post("/api/stop")
    assert resp.status_code == 502 and resp.json()["ok"] is False and resp.json()["error"]
    assert resp.json()["halted"] is True, "even a failed stop must keep the app from sending more commands"


def test_the_page_checks_the_stop_result_and_offers_resume():
    html = (BEAM_APP / "static/index.html").read_text()
    estop = html[html.index("async function estop"):html.index("function setHalted")]
    assert "r.ok" in estop and "STOP FAILED" in estop and "Emergency stop sent." not in estop
    assert 'id="resumeBtn"' in html and "/api/resume" in html and "S.halted" in html


# =========================================================================== 3. faults never leave the motors on
@pytest.fixture
def rm(monkeypatch):
    monkeypatch.setenv("SIMULATE", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    spec = importlib.util.spec_from_file_location("robot_mcp_under_test", ROBOT_DIR / "robot_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROBOT_DIR))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(ROBOT_DIR))
    return mod


def new_robot(rm):
    r = rm.Robot()
    time.sleep(0.25)            # a few control-loop passes: valid distance reading
    return r


def test_a_failing_stop_does_not_kill_the_loop_and_is_retried(rm):
    r = new_robot(rm)
    real_stop, calls = r.car.stop, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise OSError("I2C write failed")
        real_stop()
    r.car.stop = flaky
    r.drive(20, 0.2, 0, wait=False)
    time.sleep(1.2)
    assert r.car.speed == 0 and r.speed == 0, "the motors kept their speed after a failed stop"
    r.car.read_distance = lambda: 55.5
    time.sleep(0.3)
    assert r.distance == 55.5, "the control loop died"


def test_forward_motion_needs_a_valid_fresh_reading(rm, monkeypatch):
    r = new_robot(rm)
    assert not r.drive(20, 0.1, 0).startswith("refused")
    for bad in (lambda: -1.0, lambda: (_ for _ in ()).throw(RuntimeError("echo timeout"))):
        r.car.read_distance = bad
        time.sleep(0.25)
        assert r.drive(20, 1, 0).startswith("refused"), "drove blind"
        assert r.car.speed == 0
        assert not r.drive(-20, 0.1, 0).startswith("refused"), "reverse has no forward sensor to wait for"
    r.car.read_distance = lambda: 80.0
    time.sleep(0.25)
    assert not r.drive(20, 0.1, 0).startswith("refused")
    monkeypatch.setattr(rm, "ALLOW_BLIND_FORWARD", True)      # the documented opt-out
    r.car.read_distance = lambda: -1.0
    time.sleep(0.25)
    assert not r.drive(20, 0.1, 0).startswith("refused")


def test_losing_the_sensor_mid_drive_stops_the_car(rm):
    r = new_robot(rm)
    r.drive(20, 3, 0, wait=False)
    assert r.car.speed == 20
    r.car.read_distance = lambda: -1.0
    time.sleep(0.4)
    assert r.car.speed == 0


def test_a_stale_reading_is_not_a_clear_path(rm):
    r = new_robot(rm)
    r.distance_ok_at = time.time() - 10
    r._tick = lambda *a: None          # a stalled loop leaves the old distance in place
    assert r.drive(20, 1, 0).startswith("refused")


def test_every_drive_stops_at_its_own_deadline_even_if_the_loop_is_dead(rm):
    r = new_robot(rm)
    r._tick = lambda *a: None          # the loop no longer enforces anything
    r.drive(20, 0.3, 0, wait=False)
    assert r.car.speed == 20
    time.sleep(0.7)
    assert r.car.speed == 0, "drive() did not stop at its own deadline"
    assert r.drive(20, 0.2, 0, wait=True) and r.car.speed == 0


def test_a_half_applied_drive_command_is_undone(rm):
    r = new_robot(rm)

    def one_motor_then_fail(s):
        r.car.speed = s
        raise OSError("second motor write failed")
    r.car.forward = one_motor_then_fail
    with pytest.raises(OSError):
        r.drive(20, 1, 0)
    assert r.car.speed == 0


def test_motors_are_zeroed_at_startup(rm):
    class Spinning(rm.SimCar):
        def __init__(self):
            super().__init__()
            self.speed = 30           # left running by a crashed previous process
    rm.SimCar = Spinning
    r = rm.Robot()
    assert r.car.speed == 0


def test_a_failed_startup_stop_is_retried(rm):
    class Stubborn(rm.SimCar):
        fails = 3

        def __init__(self):
            super().__init__()
            self.speed = 30

        def stop(self):
            if Stubborn.fails > 0:
                Stubborn.fails -= 1
                raise OSError("bus busy")
            super().stop()
    rm.SimCar = Stubborn
    r = rm.Robot()
    time.sleep(0.8)
    assert r.car.speed == 0


def test_shutdown_and_sigterm_zero_the_motors(rm):
    r = new_robot(rm)
    rm.robot = r
    r.drive(20, 3, 0, wait=False)
    with pytest.raises(SystemExit):
        rm._on_sigterm(signal.SIGTERM, None)
    assert r.car.speed == 0


def test_the_server_stops_cleanly_on_sigterm(own_robot):
    r = own_robot()
    r.proc.send_signal(signal.SIGTERM)
    assert r.proc.wait(10) == 0


def _load_safety():
    spec = importlib.util.spec_from_file_location("safety_under_test", EXAMPLE.parent / "advanced-simulation/pi/robot_mcp/safety.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_five_pi_timed_stops_whatever_started_even_if_start_fails():
    safety = _load_safety()
    log = []

    def start():
        log.append("left on")
        raise OSError("second motor write failed")
    with pytest.raises(OSError):
        safety.timed(100, start, lambda: log.append("stopped"))
    assert log == ["left on", "stopped"]


def test_five_pi_timed_retries_a_failed_stop_once():
    safety = _load_safety()
    log = []

    def stop():
        log.append("stop")
        if log.count("stop") == 1:
            raise OSError("bus busy")
    assert safety.timed(50, lambda: log.append("start"), stop) == 50
    assert log == ["start", "stop", "stop"]


# =========================================================================== 4. no script through the Redis feed
EVIL = "<img src=x onerror=fetch('/api/drive',{method:'POST',body:'{}'})>"


def test_feed_items_from_redis_are_retyped_before_they_reach_the_page(start_app, redis_server):
    import redis
    r = redis.Redis.from_url(redis_server.url, protocol=2)
    r.delete("beambot:activity")
    good = {"ts": 1700000000.5, "tool": "drive", "input": {"speed": 20}, "ok": True, "ms": 12, "detail": "ok"}
    for item in (
        {**good, "ms": EVIL},                                    # the reported payload
        {**good, "ts": EVIL, "ok": EVIL, "detail": {"a": EVIL}},
        {**good, "tool": {"x": 1}},
        {**good, "ms": True},
        [EVIL], EVIL, 7, None, {"ms": 1},
        good,
    ):
        r.lpush("beambot:activity", json.dumps(item))
    r.lpush("beambot:activity", "{not json")
    app = start_app(REDIS_URL=redis_server.url)
    acts = app.get("/api/beam").json()["activity"]
    assert acts, "the feed should still show the valid items"
    for a in acts:
        assert set(a) == {"ts", "tool", "input", "ok", "ms", "detail"}
        assert type(a["ms"]) is int and isinstance(a["ts"], (int, float)) and type(a["ok"]) is bool
        assert isinstance(a["tool"], str) and isinstance(a["detail"], str) and isinstance(a["input"], dict)
    assert not any("<" in json.dumps(a["ms"]) for a in acts)
    assert any(a["ms"] == 12 and a["tool"] == "drive" for a in acts)


def test_the_page_never_puts_database_values_in_html_unescaped():
    """Every value that can come from Redis or the database goes through esc() or num() before innerHTML."""
    html = (BEAM_APP / "static/index.html").read_text()
    risky = re.compile(r"\$\{([^${}]*\b(?:a\.ms|a\.ts|m\.explored_m2|m\.landmarks|m\.id|m\.ts|x\.confidence|x\.count)\b[^${}]*)\}")
    for m in risky.finditer(html):
        expr = m.group(1)
        assert re.search(r"\b(esc|num)\(", expr), f"unescaped: ${{{expr}}}"
    assert "${a.ms}" not in html and "${x.confidence" not in html


# =========================================================================== reviewer questions
def test_systemd_zeroes_the_motors_after_the_server_exits_however_it_exits():
    """The HAT's own timers keep the last PWM duty when the host process dies (crash, SIGKILL, OOM)."""
    hat = (ROBOT_DIR / "picar-robot-hat.service").read_text()
    assert re.search(r"^ExecStopPost=-?\S*python\S* \S*robot_hat_lite\.py stop$", hat, re.M)
    assert re.search(r"^ExecStopPost=.*Picarx\(\)\.stop\(\)", (ROBOT_DIR / "picar-robot.service").read_text(), re.M)


def test_robot_hat_lite_stop_command_zeroes_the_motors(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("hat_lite_under_test", ROBOT_DIR / "robot_hat_lite.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod.Hat, "__init__", lambda self: None)
    monkeypatch.setattr(mod.Hat, "stop", lambda self: calls.append("stop"))
    assert mod.main(["stop"]) == 0 and calls == ["stop"]
    assert mod.main([]) == 2 and calls == ["stop"], "no argument must not touch the hardware"


def test_readme_says_the_dashboard_acts_with_the_beams_roles_not_the_visitors():
    text = (EXAMPLE / "README.md").read_text()
    assert "Who can open the published URL" in text and "beam's roles" in text


# =========================================================================== STOP is a latch on the robot
def mcp_call(url, tool, args=None):
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        async with streamablehttp_client(url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await s.call_tool(tool, args or {})
    res = asyncio.run(go())
    return res, "".join(c.text for c in res.content if c.type == "text")


DRIVE = {"speed": 20, "seconds": 0.5}


def x_of(r):
    return tele(r)["pose"]["x_cm"]


def test_stop_latches_on_the_robot_until_resume(own_robot):
    r = own_robot()
    assert not mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused") and x_of(r) > 0
    assert httpx.post(f"{r.stream}/stop").json()["halted"] is True
    assert tele(r)["halted"] is True and tele(r)["moving"] is False
    x = x_of(r)
    _, text = mcp_call(r.mcp, "drive", DRIVE)           # a different client, or a request that was already on the wire
    assert text.startswith("refused") and "latched" in text
    _, text = mcp_call(r.mcp, "drive", {"speed": -20, "seconds": 0.5})
    assert text.startswith("refused"), "reverse is motion too"
    time.sleep(0.8)
    assert x_of(r) == x and tele(r)["moving"] is False
    assert httpx.post(f"{r.stream}/resume").json() == {"result": "resumed", "halted": False}
    assert tele(r)["halted"] is False
    assert not mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused") and x_of(r) > x


def test_the_mcp_stop_tool_latches_and_the_resume_tool_clears_it(own_robot):
    r = own_robot()
    assert "latched" in mcp_call(r.mcp, "stop")[1] and tele(r)["halted"] is True
    assert mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused")
    assert mcp_call(r.mcp, "resume")[1] == "resumed" and tele(r)["halted"] is False
    assert not mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused")


def test_release_stops_without_latching(own_robot):
    r = own_robot()
    mcp_call(r.mcp, "drive", {"speed": 20, "seconds": 3, "wait": False})
    time.sleep(0.3)
    assert tele(r)["moving"] is True
    assert httpx.post(f"{r.stream}/release").json() == {"result": "stopped"}
    assert tele(r)["moving"] is False and tele(r)["halted"] is False
    assert not mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused")


def test_a_drive_in_flight_is_cut_short_and_the_next_one_is_refused(own_robot):
    r = own_robot()
    done = {}
    t = threading.Thread(target=lambda: done.update(text=mcp_call(r.mcp, "drive", {"speed": 20, "seconds": 3})[1]))
    t.start()
    time.sleep(0.6)
    assert tele(r)["moving"] is True
    httpx.post(f"{r.stream}/stop")
    time.sleep(0.15)
    assert tele(r)["moving"] is False
    x = x_of(r)
    t.join(8)
    assert mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused")
    time.sleep(0.5)
    assert x_of(r) == pytest.approx(x, abs=0.5)


def test_claude_code_in_another_beam_cannot_drive_after_a_dashboard_stop(own_robot, start_app):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    assert app.post("/api/drive", json={"speed": 20, "seconds": 3}).json()["ok"]
    time.sleep(0.3)
    assert app.post("/api/stop").status_code == 200
    x = x_of(r)
    res, text = mcp_call(r.mcp, "drive", DRIVE)         # what an agent in a different beam would send
    assert text.startswith("refused")
    time.sleep(0.6)
    assert x_of(r) == pytest.approx(x, abs=0.5) and tele(r)["moving"] is False
    assert app.post("/api/resume").json()["via"] == "stream"
    assert not mcp_call(r.mcp, "drive", DRIVE)[1].startswith("refused")


class SlowProxy:
    """TCP proxy that waits `delay` seconds before it connects each new connection: a slow Teleport hop."""

    def __init__(self, target_port, delay):
        import socket
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port, self.target, self.delay = self.sock.getsockname()[1], target_port, delay
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        import socket
        time.sleep(self.delay)
        try:
            u = socket.create_connection(("127.0.0.1", self.target))
        except OSError:
            c.close()
            return

        def pump(a, b):
            try:
                while (d := a.recv(65536)):
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        threading.Thread(target=pump, args=(c, u), daemon=True).start()
        threading.Thread(target=pump, args=(u, c), daemon=True).start()

    def close(self):
        self.sock.close()


def test_a_pad_press_still_opening_its_session_when_stop_is_pressed_never_moves_the_car(own_robot, start_app):
    """The reviewer's race: a drive-pad press whose MCP session is still opening when STOP lands used to reach the
    robot afterwards and move it."""
    r = own_robot()
    slow = SlowProxy(r.port, delay=1.0)
    try:
        app = start_app(robot_mcp=f"http://127.0.0.1:{slow.port}/mcp", ROBOT_STREAM_URL=r.stream)
        got = {}
        t = threading.Thread(target=lambda: got.update(resp=app.post("/api/drive", json={"speed": 20, "seconds": 3}, timeout=40)))
        t.start()
        time.sleep(0.4)                                  # the press is in flight, its session not open yet
        assert app.post("/api/stop").status_code == 200
        t.join(30)
        assert got["resp"].status_code == 423
        deadline = time.time() + 3
        while time.time() < deadline:
            assert tele(r)["moving"] is False and x_of(r) == 0.0, "the car moved after STOP"
            time.sleep(0.1)
    finally:
        slow.close()


def test_the_dashboard_follows_a_latch_set_by_another_client_and_can_clear_it(own_robot, start_app):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    assert app.get("/api/state").json()["halted"] is False
    mcp_call(r.mcp, "stop")                              # e.g. Claude Code pressed the emergency stop
    deadline = time.time() + 5
    while time.time() < deadline and not app.get("/api/state").json()["halted"]:
        time.sleep(0.2)
    assert app.get("/api/state").json()["halted"] is True, "the page would not show Resume"
    assert app.post("/api/resume").status_code == 200
    assert tele(r)["halted"] is False


def test_stop_and_resume_work_without_a_stream_app(own_robot, start_app):
    """The Kubernetes path with ROBOT_STREAM_APP empty: everything goes through the MCP tools."""
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL="http://127.0.0.1:1")
    assert app.post("/api/drive", json={"speed": 20, "seconds": 3}).json()["ok"]
    time.sleep(0.3)
    stop = app.post("/api/stop")
    assert stop.status_code == 200 and stop.json()["via"] == "mcp"
    assert tele(r)["halted"] is True and tele(r)["moving"] is False
    assert app.post("/api/resume").json()["via"] == "mcp"
    assert tele(r)["halted"] is False
    assert app.post("/api/drive", json={"speed": 20, "seconds": 0.3}).json()["ok"]


def test_resume_that_cannot_reach_the_robot_keeps_everything_stopped(start_app):
    app = start_app(robot_mcp="http://127.0.0.1:1/mcp", ROBOT_STREAM_URL="http://127.0.0.1:1")
    assert app.post("/api/stop").status_code == 502
    resume = app.post("/api/resume")
    assert resume.status_code == 502 and resume.json()["ok"] is False and resume.json()["halted"] is True
    assert app.get("/api/state").json()["halted"] is True
    assert app.post("/api/chat", json={"message": "go", "task": "count"}).status_code == 423


def test_everyday_stops_do_not_latch(own_robot, start_app):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    assert app.post("/api/drive", json={"speed": 20, "seconds": 3}).json()["ok"]
    time.sleep(0.3)
    assert app.post("/api/drive/stop").json()["ok"]                       # letting go of the pad
    assert tele(r)["moving"] is False and tele(r)["halted"] is False
    assert app.post("/api/automap/start", json={"vision": False, "max_moves": 10, "max_minutes": 1}).json() == {"ok": True}
    time.sleep(1.2)
    assert app.post("/api/automap/stop").json() == {"ok": True}           # cancelling an auto-map run
    deadline = time.time() + 15
    while time.time() < deadline and app.get("/api/state").json()["auto"]["state"] in ("starting", "scanning", "stopping", "moving", "exploring"):
        time.sleep(0.3)
    assert tele(r)["moving"] is False and tele(r)["halted"] is False, "ending an auto-map run latched the robot"
    assert app.post("/api/drive", json={"speed": 20, "seconds": 0.3}).json()["ok"]


def test_default_roles_cannot_resume_a_stop():
    _, roles = _apps_and_roles()
    for role in ("picar-viewer", "picar-driver"):
        tools = roles[role]["mcp"]["tools"]
        assert "stop" in tools and "resume" not in tools, role


def test_readme_explains_the_latch():
    text = (EXAMPLE / "README.md").read_text()
    assert "STOP** is a latch on the robot" in text and "`resume`" in text and "Claude Code" in text
