"""Dashboard (beam-app/app.py) follow-ups from the PR #14 review. Each test reproduces the reviewer's scenario.

1. robot-moving POSTs come only from the dashboard page (the stop routes stay open to any POST)
2. chat messages and the shared chat history are capped
3. every robot call counts toward the agent's step limit, so one turn can't fan out into many drives
4. request bodies and shared feed items are validated: no NaN, Infinity or missing field reaches a 500
5. a stalled or dropped database tunnel never holds up STOP, and the dashboard recovers when it comes back
6. the third-party script on the page is pinned and hash-checked
7. a burst of STOPs with the MCP route hung opens at most MAX_MCP_STOPS_IN_FLIGHT MCP stops
"""
import asyncio
import json
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import ROOT, BEAM_APP, _clean_env, free_port, wait_http
from test_review_findings import own_robot  # noqa: F401  (a fixture: a private simulated robot)


def tele(r):
    return httpx.get(f"{r.stream}/telemetry").json()


def timed(fn):
    t0 = time.time()
    try:
        out = fn()
    except httpx.TimeoutException:
        out = None
    return out, time.time() - t0


# =========================================================================== 1. only the page can move the robot
PAGE_ONLY = {
    "/api/drive": {"speed": 20, "seconds": 1},
    "/api/look": {"pan_deg": 40},
    "/api/say": {"text": "hi"},
    "/api/chat": {"message": "drive forward"},
    "/api/automap/start": {},
    "/api/reset": {"what": "all"},
    "/api/maps": {"name": "planted"},
    "/api/maps/1/load": {},
    "/api/resume": {},
}
# What a browser sends from another site without asking the app first (form, text/plain, a Blob with no type), and a
# JSON POST from a sibling site (another beam's published app).
FORGED = [
    {"content-type": "text/plain"},
    {"content-type": 'text/plain; x="application/json"'},
    {"content-type": "application/x-www-form-urlencoded"},
    {},
    {"content-type": "application/json", "sec-fetch-site": "cross-site"},
    {"content-type": "application/json", "sec-fetch-site": "same-site"},
]


def test_only_the_dashboard_page_can_move_the_robot(own_robot, start_app, llm):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    x0 = tele(r)["pose"]["x_cm"]
    for path, body in PAGE_ONLY.items():
        for headers in FORGED:
            resp = httpx.post(f"{app.base_url}{path}", content=json.dumps(body), headers=headers)
            assert resp.status_code == 403, (path, headers, resp.status_code, resp.text)
    time.sleep(1.5)
    t = tele(r)
    assert t["pose"]["x_cm"] == x0 and t["moving"] is False, "a forged POST moved the car"
    assert llm.requests() == [], "a forged chat reached the model"
    assert app.get("/api/state").json()["auto"].get("state") in (None, "idle"), "a forged POST started an auto-map"

    # the page's own requests still work
    page = {"sec-fetch-site": "same-origin"}
    assert app.post("/api/drive", json={"speed": 20, "seconds": 0.3}, headers=page).status_code == 200
    assert app.post("/api/look", json={"pan_deg": 0}, headers=page).status_code == 200


def test_the_stop_routes_take_any_post(own_robot, start_app):
    """A forged stop can only stop the car, so STOP stays as easy to press as it was: no check at all."""
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    forged = {"content-type": "text/plain", "sec-fetch-site": "cross-site"}
    for path in ("/api/drive/stop", "/api/automap/stop", "/api/stop"):
        resp = httpx.post(f"{app.base_url}{path}", content="{}", headers=forged)
        assert resp.status_code == 200, (path, resp.text)   # (automap/stop answers ok: false when nothing runs)
    assert tele(r)["halted"] is True
    assert app.post("/api/resume", json={}).status_code == 200


# =========================================================================== 2. chat size and history caps
def test_an_oversized_chat_message_is_refused(start_app, llm):
    app = start_app()
    llm.script({"text": "ok"})
    resp = app.post("/api/chat", json={"message": "x" * 100_000})
    assert resp.status_code == 422, resp.text
    assert llm.requests() == [], "the oversized message reached the model"


def test_the_shared_chat_history_is_capped(start_app, llm):
    import app as beam_app
    app = start_app()
    n = beam_app.HISTORY_TURNS + 4
    steps = []
    for i in range(n):
        steps += [{"tool": "get_telemetry"}, {"text": f"reply {i}"}]
    llm.script(*steps)
    for i in range(n):
        assert app.chat(f"message {i}", task="map")["status"] == "done"
    sent = llm.requests()[-1]["body"]["input"]
    typed = [it["content"] for it in sent if it.get("role") == "user" and isinstance(it.get("content"), str)]
    assert len(typed) == beam_app.HISTORY_TURNS and typed[-1] == f"message {n - 1}", typed
    assert "message 0" not in typed
    # cut at a typed message: the oldest exchange left still starts with its question, never with a tool result
    assert sent[0] == {"role": "user", "content": f"message {n - beam_app.HISTORY_TURNS}"}


# =========================================================================== 3. robot calls count toward the step limit
def test_parallel_robot_calls_count_toward_the_step_limit(start_app, llm, tmp_path):
    """The reviewer's case: one model turn asked for 8 drives at once and got all 8. Here two turns ask for 15 each."""
    import app as beam_app
    port, log = free_port(), tmp_path / "calls.txt"
    p = subprocess.Popen([sys.executable, str(ROOT / "tests/helpers/counting_robot.py"), str(port), str(log)],
                         env=_clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_http(f"http://127.0.0.1:{port}/")
        app = start_app(robot_mcp=f"http://127.0.0.1:{port}/mcp", ROBOT_STREAM_URL="http://127.0.0.1:1")
        drive = {"tool": "drive", "args": {"speed": 20, "seconds": 3, "wait": False}}
        llm.script({"tools": [drive] * 15}, {"tools": [drive] * 15}, {"text": "done"})
        job = app.chat("drive around", task="map")
        assert job["status"] == "done", job
        drives = log.read_text().split().count("drive") if log.exists() else 0
        assert drives == beam_app.MAX_AGENT_STEPS, f"{drives} drives sent for one message"
    finally:
        p.terminate()
        p.wait(5)


# =========================================================================== 4. validated bodies and feed items
BAD_BODIES = [
    ("/api/drive", '{"speed": 20, "seconds": NaN}'),
    ("/api/drive", '{"speed": 20, "seconds": Infinity}'),
    ("/api/drive", '{"speed": "fast"}'),
    ("/api/look", '{"pan_deg": "left"}'),
    ("/api/chat", '{}'),
    ("/api/chat", '{"task": "count"}'),
    ("/api/reset", '{"what": 5}'),
    ("/api/automap/start", '{"max_minutes": Infinity}'),
    ("/api/automap/start", '{"max_moves": NaN}'),
    ("/api/automap/start", '{"speed": "fast"}'),
]


def test_bad_request_bodies_are_refused_not_500(own_robot, start_app):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    x0 = tele(r)["pose"]["x_cm"]
    for path, text in BAD_BODIES:
        resp = app.post(path, content=text, headers={"content-type": "application/json"})
        assert resp.status_code == 422, (path, text, resp.status_code, resp.text)
    assert tele(r)["pose"]["x_cm"] == x0
    assert app.get("/api/state").json()["auto"].get("state") in (None, "idle"), "an auto-map started"
    assert app.get("/api/beam").status_code == 200


def test_auto_map_limits_are_clamped(own_robot, start_app):
    r = own_robot()
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    resp = app.post("/api/automap/start", json={"max_minutes": 1e9, "max_moves": 1e9, "speed": 99, "vision": False})
    try:
        assert resp.status_code == 200, resp.text
        auto = app.get("/api/state").json()["auto"]
        assert auto["max_minutes"] == 60 and auto["max_moves"] == 2000 and auto["speed"] == 40, auto
    finally:
        app.post("/api/automap/stop")
        app.post("/api/stop")


def test_a_nan_in_the_shared_feed_does_not_break_the_beam_card(start_app, redis_server):
    """Any identity with beambot-data can write to the feed. NaN used to make /api/beam a 500 on every dashboard."""
    import redis
    app = start_app(REDIS_URL=redis_server.url)
    rc = redis.Redis(host="127.0.0.1", port=redis_server.port, protocol=2, username="app")
    rc.lpush("beambot:activity", '{"ts": 1, "tool": "drive", "ok": true, "ms": 5, "detail": "", '
                                 '"input": {"speed": 20, "seconds": NaN, "steer_deg": Infinity, "deep": {"x": 1}}}')
    card = app.get("/api/beam")
    assert card.status_code == 200, card.text
    item = next(a for a in card.json()["activity"] if a["tool"] == "drive")
    assert item["input"] == {"speed": 20}, item


# =========================================================================== 5. a stalled or dropped database tunnel
class Tunnel:
    """Stands in for `tsh proxy db --tunnel`: a local TCP port in front of a database, which a test can stall (bytes stop
    flowing, sockets stay open) or drop (every open connection is cut; new ones still get through)."""

    def __init__(self, connect):
        self.connect, self.stalled, self.socks = connect, threading.Event(), []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.sock.accept()
                u = self.connect()
            except OSError:
                return
            self.socks += [c, u]
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, a, b):
        try:
            while (d := a.recv(65536)):
                while self.stalled.is_set():
                    time.sleep(0.02)
                b.sendall(d)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def drop(self):
        for s in self.socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.socks.clear()

    def close(self):
        self.sock.close()
        self.drop()


def pg_tunnel(pg_url):
    """A Tunnel to the embedded Postgres (which listens on a Unix socket) and the app's DATABASE_URL through it."""
    u = urlparse(pg_url)
    sock_path = str(next(p for p in Path(parse_qs(u.query)["host"][0]).glob(".s.PGSQL.*") if p.suffix != ".lock"))

    def connect():
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock_path)
        return s
    t = Tunnel(connect)
    return t, f"postgresql://postgres@127.0.0.1:{t.port}{u.path}"


def test_a_stalled_postgres_tunnel_never_holds_up_stop(own_robot, start_app, pg_url):
    r = own_robot()
    tunnel, url = pg_tunnel(pg_url)
    try:
        app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream, DATABASE_URL=url)
        assert app.get("/api/beam").json()["databases"]["postgres"]["kind"] == "Postgres"
        tunnel.stalled.set()
        got = {}
        poll = threading.Thread(target=lambda: got.update(state=timed(lambda: app.get("/api/state", timeout=8))))
        poll.start()
        time.sleep(0.5)                                     # the page's poll is now waiting on the database
        resp, took = timed(lambda: app.post("/api/stop", timeout=8))
        assert resp is not None and resp.status_code == 200, "STOP did not answer"
        assert took < 1.5, f"STOP took {took:.1f}s while the database was stalled"
        assert tele(r)["halted"] is True
        poll.join(10)
        state, took = got["state"]
        assert state is not None and state.status_code == 200 and took < 5, (took, state and state.text)
        assert state.json()["tally"]["error"], "the page should say the counts are unavailable"
    finally:
        tunnel.stalled.clear()
        tunnel.close()


def test_the_dashboard_recovers_when_a_dropped_postgres_tunnel_comes_back(start_app, pg_url, llm):
    tunnel, url = pg_tunnel(pg_url)
    try:
        app = start_app(DATABASE_URL=url)
        llm.script({"tool": "record_count", "args": {"object": "chair", "count": 2, "confidence": 90}}, {"text": "ok"})
        assert app.chat("count")["status"] == "done"
        tunnel.drop()                                       # the tunnel restarts: the app's connection is gone
        codes, tally = [], {}
        for _ in range(10):
            resp = app.get("/api/state")
            codes.append(resp.status_code)
            tally = resp.json().get("tally", {}) if resp.status_code == 200 else {}
            if tally.get("totals") == {"chair": 2}:
                break
            time.sleep(0.3)
        assert set(codes) == {200}, f"/api/state answered {codes}"
        assert tally.get("totals") == {"chair": 2}, f"the counts never came back: {tally}"
        assert app.get("/api/beam").json()["databases"]["postgres"]["ok"] is True
    finally:
        tunnel.close()


def test_a_stalled_redis_tunnel_never_holds_up_stop(own_robot, start_app, redis_server):
    r = own_robot()
    tunnel = Tunnel(lambda: socket.create_connection(("127.0.0.1", redis_server.port)))
    try:
        app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream,
                        REDIS_URL=f"redis://app@127.0.0.1:{tunnel.port}")
        assert app.get("/api/beam").json()["databases"]["redis"]["ok"] is True
        tunnel.stalled.set()
        took = []
        for _ in range(5):
            resp, t = timed(lambda: app.post("/api/stop", timeout=8))
            assert resp is not None and resp.status_code == 200, "STOP did not answer"
            took.append(t)
            time.sleep(0.3)
        assert max(took) < 1.0, f"STOP took {max(took):.1f}s while Redis was stalled ({took})"
        card, t = timed(lambda: app.get("/api/beam", timeout=15))
        assert card is not None and card.status_code == 200 and t < 8, t
    finally:
        tunnel.stalled.clear()
        tunnel.close()


# =========================================================================== 6. third-party script is hash-checked
def test_third_party_scripts_are_pinned_and_hash_checked():
    page = (BEAM_APP / "static/index.html").read_text()
    tags = [t for t in re.findall(r"<script\b[^>]*>", page) if re.search(r'src="https?://', t)]
    assert tags, "expected the icon script"
    for tag in tags:
        src = re.search(r'src="([^"]+)"', tag).group(1)
        assert re.search(r"@\d+\.\d+\.\d+/\S+\.js$", src), f"{src}: pin the exact version and file"
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag), f"{src}: no SRI hash"
        assert 'crossorigin="anonymous"' in tag, f"{src}: SRI needs crossorigin"


# =========================================================================== 7. the MCP stop cap is reached
class HungServer:
    """Accepts connections, counts them, never answers: an MCP route that hangs."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port, self.conns = self.sock.getsockname()[1], []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                self.conns.append(self.sock.accept()[0])
            except OSError:
                return

    def close(self):
        self.sock.close()
        for c in self.conns:
            c.close()


def test_a_burst_of_stops_with_mcp_hung_opens_at_most_the_cap(monkeypatch):
    """30 STOPs while the MCP route hangs: every one still answers (through the stream app), and only the first
    MAX_MCP_STOPS_IN_FLIGHT of them reach the MCP route. Raising the cap, or dropping it, fails here."""
    import app as beam_app
    hung = HungServer()
    monkeypatch.setattr(beam_app, "halt", {"on": False, "at": None})
    monkeypatch.setattr(beam_app, "ROBOT_MCP_URL", f"http://127.0.0.1:{hung.port}/mcp")
    monkeypatch.setattr(beam_app, "MCP_STOP_TIMEOUT_S", 1.0)

    async def fast_stream_stop():
        return "stream", "stopped"
    monkeypatch.setattr(beam_app, "_stop_via_stream", fast_stream_stop)

    async def go():
        results = [await beam_app.robot_estop() for _ in range(30)]
        await asyncio.sleep(0.5)                            # every MCP stop that was started has connected by now
        opened = len(hung.conns)
        await asyncio.sleep(1.5)                            # and has given up
        return results, opened
    try:
        results, opened = asyncio.run(go())
    finally:
        hung.close()
    assert all(ok and via == "stream" for ok, via, _ in results)
    assert opened == beam_app.MAX_MCP_STOPS_IN_FLIGHT, f"{opened} MCP stops reached the hung route"
    assert beam_app.MAX_MCP_STOPS_IN_FLIGHT <= 3, "the cap was raised"
