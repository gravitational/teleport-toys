"""STOP on a stalled SD card, and STOP with both robot routes hung (review round 5 follow-ups).

The motors already stopped before the disk write. These cover what was left:
- the STOP *answer* waited for the write (twice: both routes saved the file, one after the other),
- the blocking write ran on the robot servers' only thread, freezing video and telemetry,
- /api/stop had no timeout on its MCP route, so with both routes hung it never answered.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from conftest import ROOT, ROBOT_DIR, _clean_env, free_port, wait_robot_ready
from test_review_findings import SlowProxy, new_robot, rm, MCP_HEADERS  # noqa: F401  (rm is a fixture)

SLOW_ROBOT = ROOT / "tests/helpers/slow_disk_robot.py"


@pytest.fixture
def slow_robot(tmp_path):
    """The real robot server with every fsync stalled by `stall` seconds, saving its latch to a real file."""
    procs = []

    def spawn(stall):
        port, stream = free_port(), free_port()
        halt = tmp_path / f"halted-{port}"
        p = subprocess.Popen([sys.executable, str(SLOW_ROBOT)], cwd=ROBOT_DIR,
                             env=_clean_env(SIMULATE=1, ROBOT_PORT=port, STREAM_PORT=stream, SAY_CMD="true",
                                            HALT_FILE=halt, FSYNC_STALL_S=stall),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        wait_robot_ready(f"http://127.0.0.1:{stream}")
        return SimpleNamespace(port=port, stream_port=stream, mcp=f"http://127.0.0.1:{port}/mcp",
                               stream=f"http://127.0.0.1:{stream}", halt_file=halt)
    yield spawn
    for p in procs:
        p.kill()
        p.wait(5)


def tele(r, timeout=10):
    return httpx.get(f"{r.stream}/telemetry", timeout=timeout).json()


def mcp_call(r, tool, args=None, timeout=60):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}
    resp = httpx.post(r.mcp, json=body, headers=MCP_HEADERS, timeout=timeout)
    line = next(ln for ln in resp.text.splitlines() if ln.startswith("data:")) if "data:" in resp.text else resp.text
    return json.loads(line.removeprefix("data:"))


def timed(fn):
    t0 = time.time()
    out = fn()
    return out, time.time() - t0


# --------------------------------------------------------------------------- the robot answers STOP without the disk
@pytest.mark.parametrize("route", ["stream", "mcp"])
def test_a_stop_answers_before_a_stalled_latch_write_finishes(slow_robot, route):
    r = slow_robot(stall=4)
    mcp_call(r, "drive", {"speed": 20, "seconds": 3, "wait": False})
    if route == "stream":
        resp, took = timed(lambda: httpx.post(f"{r.stream}/stop", timeout=30))
        assert resp.status_code == 200 and resp.json()["halted"] is True, resp.text
        text = resp.json()["result"]
    else:
        res, took = timed(lambda: mcp_call(r, "stop"))
        text = res["result"]["content"][0]["text"]
    assert took < 1.5, f"the {route} STOP answered after {took:.1f}s: it waited for the disk"
    assert "still being saved" in text, text          # and it says the restart-proof copy isn't on disk yet
    t = tele(r)
    assert t["halted"] is True and t["moving"] is False and t["latch_saved"] is False
    time.sleep(4.5)
    assert r.halt_file.exists() and tele(r)["latch_saved"] is True


@pytest.mark.parametrize("route", ["stream", "mcp"])
def test_video_and_telemetry_keep_flowing_during_a_stalled_write(slow_robot, route):
    r = slow_robot(stall=3)
    threading.Thread(target=lambda: httpx.post(f"{r.stream}/stop", timeout=30) if route == "stream"
                     else mcp_call(r, "stop"), daemon=True).start()
    time.sleep(0.2)                                    # the write is stalled now
    for _ in range(3):
        _, took = timed(lambda: tele(r, timeout=5))
        assert took < 0.5, f"/telemetry took {took:.1f}s during a {route} STOP's disk write"
        _, took = timed(lambda: httpx.get(f"{r.stream}/snapshot.jpg", timeout=5))
        assert took < 0.5, f"/snapshot.jpg took {took:.1f}s during a {route} STOP's disk write"
        res, took = timed(lambda: mcp_call(r, "get_telemetry", timeout=5))
        assert took < 0.5 and "result" in res, f"MCP get_telemetry took {took:.1f}s during a {route} STOP's write"
        time.sleep(0.3)


def test_both_routes_at_once_write_the_latch_once(slow_robot):
    """The dashboard sends both STOPs together. The second used to queue behind the first's write and then write again
    (2 x the stall before it answered)."""
    r = slow_robot(stall=3)
    out = {}
    a = threading.Thread(target=lambda: out.update(stream=timed(lambda: httpx.post(f"{r.stream}/stop", timeout=30))))
    b = threading.Thread(target=lambda: out.update(mcp=timed(lambda: mcp_call(r, "stop"))))
    a.start(), b.start()
    a.join(30), b.join(30)
    assert out["stream"][1] < 1.5 and out["mcp"][1] < 1.5, {k: round(v[1], 2) for k, v in out.items()}
    t0 = time.time()
    while not tele(r)["latch_saved"] and time.time() - t0 < 10:
        time.sleep(0.2)
    took = time.time() - t0
    assert tele(r)["latch_saved"] is True and took < 4.5, f"the latch took {took:.1f}s more to save: written twice?"


def test_a_stop_on_an_already_saved_latch_does_not_write_again(rm, monkeypatch, tmp_path):
    monkeypatch.setattr(rm, "HALT_FILE", str(tmp_path / "halted"))
    saves = []
    orig = rm.Robot._save_halt
    monkeypatch.setattr(rm.Robot, "_save_halt", staticmethod(lambda on: (saves.append(on), orig(on))[1]))
    r = new_robot(rm)
    assert r.emergency_stop() == "stopped"
    assert r.emergency_stop() == "stopped" and saves == [True], saves
    r.resume()
    assert r.emergency_stop() == "stopped" and saves == [True, False, True], "after a Resume the latch is saved again"


def test_a_failed_latch_write_is_reported_and_retried_by_the_next_stop(rm, monkeypatch, tmp_path):
    monkeypatch.setattr(rm, "HALT_FILE", str(tmp_path / "halted"))
    r = new_robot(rm)
    with monkeypatch.context() as m:
        def denied(*a, **k):
            raise PermissionError("read-only file system")
        m.setattr(os, "open", denied)
        text = r.emergency_stop()
    assert "could not be saved" in text and "read-only" in text and r.halted, text
    assert r.emergency_stop() == "stopped" and (tmp_path / "halted").exists()


def test_a_resume_during_a_stalled_stop_write_stays_consistent(rm, monkeypatch, tmp_path):
    f = tmp_path / "halted"
    monkeypatch.setattr(rm, "HALT_FILE", str(f))
    orig = rm.Robot._save_halt
    monkeypatch.setattr(rm.Robot, "_save_halt", staticmethod(lambda on: (time.sleep(1.0 if on else 0), orig(on))[1]))
    r = new_robot(rm)
    text, took = timed(r.emergency_stop)
    assert took < 0.9 and "still being saved" in text
    r.resume()                                         # waits for the write, then clears it
    assert r.halted is False and not f.exists() and r.halt_saved is False
    r.emergency_stop()                                 # pressed again: it must be written again
    t0 = time.time()
    while not r.halt_saved and time.time() - t0 < 5:   # set after the write and fsync; the file appears before that
        time.sleep(0.05)
    assert f.exists() and r.halted and r.halt_saved


def test_a_save_request_handled_after_a_resume_writes_nothing(rm, monkeypatch, tmp_path):
    """A STOP's save can be handled late (the writer was busy). If a Resume got in first, it must not bring back a
    latch file for a robot that is running again: a restart would latch it for no reason."""
    f = tmp_path / "halted"
    monkeypatch.setattr(rm, "HALT_FILE", str(f))
    r = new_robot(rm)
    with r.halt_io:                                    # the writer is busy: the STOP's save has to wait
        r.emergency_stop()
        r.halted = False                               # what resume() leaves behind once it gets the file lock
    with r.save_cv:
        r.save_cv.wait_for(lambda: r.save_done >= r.save_req, timeout=5)
    assert not f.exists() and r.halt_saved is False


def test_stop_pressed_again_while_resume_clears_the_file_stays_latched(rm, monkeypatch, tmp_path):
    """The reviewer's untested race: STOP lands while resume() is removing the saved latch."""
    f = tmp_path / "halted"
    monkeypatch.setattr(rm, "HALT_FILE", str(f))
    r = new_robot(rm)
    r.emergency_stop()
    orig = rm.Robot._save_halt

    def slow_clear(on):
        if not on:
            threading.Thread(target=r.emergency_stop).start()   # STOP again, mid-clear
            time.sleep(0.5)
        return orig(on)
    monkeypatch.setattr(rm.Robot, "_save_halt", staticmethod(slow_clear))
    with pytest.raises(RuntimeError, match="STOP was pressed again"):
        r.resume()
    monkeypatch.setattr(rm.Robot, "_save_halt", staticmethod(orig))
    t0 = time.time()
    while not f.exists() and time.time() - t0 < 5:
        time.sleep(0.05)
    assert r.halted is True and f.exists(), "the second STOP's latch was lost"
    with pytest.raises(RuntimeError, match="latched"):
        r.drive(20, 0.2, 0, wait=False)


# --------------------------------------------------------------------------- the dashboard's STOP always answers
def test_a_stop_never_answers_with_an_earlier_stops_mcp_call(monkeypatch):
    """STOP, Resume, STOP: the second STOP must not be answered by the first one's MCP call, which may have reached the
    robot before the Resume."""
    import app as beam_app
    sent = []

    async def stream_down():
        raise ConnectionError("stream app down")

    async def mcp_stop():
        n = len(sent)
        sent.append(n)
        await asyncio.sleep(0.5)
        return "mcp", f"stopped #{n}"
    monkeypatch.setattr(beam_app, "_stop_via_stream", stream_down)
    monkeypatch.setattr(beam_app, "_stop_via_mcp", mcp_stop)

    async def go():
        first = asyncio.create_task(beam_app.robot_estop())
        await asyncio.sleep(0.3)                       # (a Resume could land here)
        second = await beam_app.robot_estop()
        return await first, second
    first, second = asyncio.run(go())
    assert first[2] == "stopped #0" and second[2] == "stopped #1" and sent == [0, 1], (first, second)


def test_dashboard_stop_answers_fast_on_a_stalled_disk(slow_robot, start_app):
    r = slow_robot(stall=4)
    app = start_app(robot_mcp=r.mcp, ROBOT_STREAM_URL=r.stream)
    assert app.post("/api/drive", json={"speed": 20, "seconds": 3}).json()["ok"]
    time.sleep(0.3)
    resp, took = timed(lambda: app.post("/api/stop"))
    assert resp.status_code == 200 and resp.json()["ok"], resp.text
    assert took < 2.0, f"/api/stop answered after {took:.1f}s on a 4 s disk stall"
    assert tele(r)["halted"] is True and tele(r)["moving"] is False


def test_dashboard_stop_reports_a_failure_when_both_routes_hang(start_app, monkeypatch):
    """Both routes accept and then say nothing. The page must get an answer (STOP FAILED), not wait forever."""
    hung_mcp, hung_stream = SlowProxy(1, delay=120), SlowProxy(1, delay=120)
    try:
        app = start_app(robot_mcp=f"http://127.0.0.1:{hung_mcp.port}/mcp",
                        ROBOT_STREAM_URL=f"http://127.0.0.1:{hung_stream.port}", MCP_STOP_TIMEOUT_S=3)
        resp, took = timed(lambda: app.post("/api/stop"))
        assert resp.status_code == 502 and resp.json()["ok"] is False, resp.text
        assert "stream app" in resp.json()["error"] and "MCP stop" in resp.json()["error"]
        assert took < 5, f"/api/stop took {took:.1f}s with both routes hung"
        assert app.get("/api/state").json()["halted"] is True
    finally:
        hung_mcp.close()
        hung_stream.close()


def test_repeated_stops_with_mcp_hung_do_not_pile_up(monkeypatch):
    """30 STOPs with MCP hung used to leave 31 background tasks (and connections) until MCP came back. Now at most
    MAX_MCP_STOPS_IN_FLIGHT wait at once, and each gives up after MCP_STOP_TIMEOUT_S."""
    import app as beam_app
    hung = SlowProxy(1, delay=120)
    monkeypatch.setattr(beam_app, "halt", {"on": False, "at": None})
    monkeypatch.setattr(beam_app, "ROBOT_MCP_URL", f"http://127.0.0.1:{hung.port}/mcp")
    monkeypatch.setattr(beam_app, "MCP_STOP_TIMEOUT_S", 1.0)

    async def fast_stream_stop():
        return "stream", "stopped"
    monkeypatch.setattr(beam_app, "_stop_via_stream", fast_stream_stop)

    async def go():
        for _ in range(30):
            ok, via, _ = await beam_app.robot_estop()
            assert ok and via == "stream"
        peak = len(beam_app._bg)
        await asyncio.sleep(1.5)
        return peak, len(beam_app._bg)
    try:
        peak, after = asyncio.run(go())
    finally:
        hung.close()
    assert peak <= beam_app.MAX_MCP_STOPS_IN_FLIGHT, f"{peak} MCP stops were left running at once"
    assert after == 0, f"{after} MCP stops still running after their timeout"


# --------------------------------------------------------------------------- STOP never queues behind other robot calls
def test_mcp_stop_does_not_wait_for_a_busy_thread_pool(rm, monkeypatch):
    """drive(wait=True), look and say each hold a thread of asyncio's default pool for seconds (8 threads on a
    4-core Pi). The MCP `stop` used to queue behind them while the car kept moving."""
    r = new_robot(rm)
    monkeypatch.setattr(rm, "robot", r)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        release = threading.Event()
        busy = [loop.run_in_executor(None, release.wait, 10) for _ in range(2)]   # every default thread is taken
        try:
            r.drive(-20, 3, 0, wait=False)
            assert r.car.speed == -20
            t0 = time.time()
            out = await asyncio.wait_for(rm.stop(), 2)
            return out, time.time() - t0
        finally:
            release.set()
            await asyncio.gather(*busy)

    out, took = asyncio.run(scenario())
    assert out.startswith("stopped") and took < 1.0, (out, took)
    assert r.car.speed == 0 and r.halted
