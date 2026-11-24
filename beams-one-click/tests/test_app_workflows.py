"""The dashboard end to end: HTTP API -> agent loop (scripted LLM) -> robot MCP server (simulator) -> storage.

Everything runs on localhost with real processes and real sockets. What stands in for Teleport is the fake CLI
(identity) and the fact that the MCP server is reached directly instead of through `tsh proxy mcp`.
The workflows run once on SQLite and once on a real Postgres."""
import json
import math
import sqlite3
import time

import httpx
import pytest

ROBOT_TOOLS = {"drive", "look", "stop", "snapshot", "get_telemetry", "say"}


@pytest.fixture(params=["sqlite", "postgres"])
def app(request, start_app):
    if request.param == "postgres":
        url = request.getfixturevalue("pg_url")
        client = start_app(DATABASE_URL=url)
        client.pg_url = url
    else:
        client = start_app()
    client.mode = request.param
    return client


def sql(app, query):
    if app.mode == "sqlite":
        return sqlite3.connect(app.sqlite_path).execute(query).fetchall()
    import psycopg
    with psycopg.connect(app.pg_url, autocommit=True) as c:
        return c.execute(query).fetchall()


def telemetry(robot):
    return httpx.get(f"{robot.base}/telemetry").json()


def tool_names(request_body):
    return {t["name"] for t in request_body["tools"]}


# ----------------------------------------------------------------------------- page, tasks, beam card
def test_page_and_tasks(app):
    page = app.get("/")
    assert page.status_code == 200 and "BeamBot Control" in page.text
    assert "teleport-logo" not in page.text                      # no brand asset is required
    assert {"count", "map", "auto"} <= set(app.get("/api/tasks").json())


def test_beam_card_shows_identity_tools_and_storage(app, llm):
    card = app.get("/api/beam").json()
    assert card["identity"]["user"] == "alice" and card["identity"]["roles"] == ["picar-viewer"]
    assert set(card["mcp"]["allowed"]) == ROBOT_TOOLS and card["mcp"]["error"] is None
    assert "via_bot" not in card["mcp"], "tunnels use the beam's own identity: there is no bot"
    assert card["inference"]["endpoint_host"].endswith(llm.base.rsplit(":", 1)[1])
    assert card["inference"]["via_beam_proxy"] is False, "a local stand-in endpoint is not the beam's Teleport proxy"
    pg = card["databases"]["postgres"]
    assert pg["kind"] == ("Postgres" if app.mode == "postgres" else "SQLite") and pg["ok"]
    assert card["databases"]["redis"] == {"enabled": False}
    assert card["audit_url"].startswith("https://example.beams.run") and "/audit/events" in card["audit_url"]


# ----------------------------------------------------------------------------- agent workflows
def test_agent_counts_objects_and_persists_them(app, llm):
    llm.script({"tool": "snapshot"},
               {"tool": "record_count", "args": {"object": "chair", "count": 3, "confidence": 85, "note": "north wall"}},
               {"tool": "get_tally"},
               {"text": "I counted 3 chairs."})
    job = app.chat("count the chairs")
    assert job["status"] == "done" and job["reply"] == "I counted 3 chairs."
    assert [(t["tool"], t["via"]) for t in job["trace"]] == [
        ("snapshot", "teleport-mcp"), ("record_count", "local"), ("get_tally", "local")]

    assert app.get("/api/state").json()["tally"]["totals"] == {"chair": 3}
    assert sql(app, "SELECT object, count, confidence, note FROM counts") == [("chair", 3, 85, "north wall")]

    reqs = llm.requests()
    assert len(reqs) == 4
    first = reqs[0]["body"]
    assert first["model"] == "gpt-6.1-sol"                                       # the documented default
    assert ROBOT_TOOLS <= tool_names(first) and "record_count" in tool_names(first)
    assert "BeamBot" in first["instructions"]
    # the camera frame travelled robot -> MCP -> app -> model as an image
    second_input = reqs[1]["body"]["input"]
    outputs = [it for it in second_input if it.get("type") == "function_call_output"]
    assert outputs and any(o["type"] == "input_image" for o in outputs[0]["output"])

    activity = app.get("/api/beam").json()["activity"]
    assert any(a["tool"] == "snapshot" and a["ok"] for a in activity)


def test_agent_drives_the_robot(app, llm, robot):
    before = telemetry(robot)["pose"]
    llm.script({"tool": "drive", "args": {"speed": 20, "seconds": 0.6}}, {"tool": "stop"}, {"text": "moved"})
    job = app.chat("drive forward a little", task="map")
    assert job["status"] == "done"
    after = telemetry(robot)["pose"]
    assert math.hypot(after["x_cm"] - before["x_cm"], after["y_cm"] - before["y_cm"]) > 1
    assert telemetry(robot)["moving"] is False


def test_anthropic_provider(start_app, llm):
    app = start_app(provider="anthropic")
    llm.script({"tool": "get_telemetry"}, {"text": "pose read"})
    job = app.chat("where are you?", task="map")
    assert job["status"] == "done" and job["reply"] == "pose read"
    reqs = llm.requests()
    assert {r["api"] for r in reqs} == {"messages"} and reqs[0]["body"]["model"] == "claude-sonnet-5-5"
    assert app.get("/api/beam").json()["inference"]["provider"] == "anthropic"


def test_model_is_configurable(start_app, llm):
    app = start_app(LLM_MODEL="some-other-model")
    llm.script({"text": "hi"})
    app.chat("hello")
    assert llm.requests()[0]["body"]["model"] == "some-other-model"


def test_tools_teleport_hides_are_not_offered_and_calls_fail_cleanly(start_app, llm, tmp_path):
    """RBAC at work: a viewer-only identity sees three tools. Asking for `drive` anyway must not crash the app."""
    import subprocess
    import sys

    from conftest import HELPERS, _clean_env, free_port, wait_http
    port = free_port()
    p = subprocess.Popen([sys.executable, str(HELPERS / "restricted_robot.py"), str(port)], env=_clean_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_http(f"http://127.0.0.1:{port}/mcp")
    except Exception:
        pass
    try:
        app = start_app(robot_mcp=f"http://127.0.0.1:{port}/mcp")
        card = app.get("/api/beam").json()["mcp"]
        assert set(card["allowed"]) == {"stop", "get_telemetry", "snapshot"}
        assert {"drive", "look", "say"} <= set(card["all_tools"]) - set(card["allowed"])      # shown as locked

        llm.script({"tool": "drive", "args": {"speed": 20, "seconds": 1}}, {"text": "I could not drive."})
        job = app.chat("drive forward")
        assert job["status"] == "done" and job["reply"] == "I could not drive."
        reqs = llm.requests()
        assert "drive" not in tool_names(reqs[0]["body"])
        result = [it for it in reqs[1]["body"]["input"] if it.get("type") == "function_call_output"][0]
        assert any("drive" in o.get("text", "").lower() or "error" in o.get("text", "").lower() for o in result["output"])
    finally:
        p.terminate()


def test_robot_unreachable_is_reported_not_fatal(start_app, llm):
    app = start_app(robot_mcp="http://127.0.0.1:1/mcp")
    card = app.get("/api/beam").json()["mcp"]
    assert card["allowed"] == [] and card["error"]
    assert "ExceptionGroup" not in card["error"], "show the real cause (connection refused), not the wrapper"
    llm.script({"text": "never reached"})
    job = app.chat("hello")
    assert job["status"] == "error" and job["detail"]
    assert app.get("/api/tasks").status_code == 200                              # still serving


def test_postgres_down_app_still_counts(start_app, llm):
    app = start_app(DATABASE_URL="postgresql://app@127.0.0.1:1/beambot")
    pg = app.get("/api/beam").json()["databases"]["postgres"]
    assert pg["kind"] == "SQLite" and "Postgres unavailable" in pg["error"]
    llm.script({"tool": "record_count", "args": {"object": "plant", "count": 2, "confidence": 60}}, {"text": "ok"})
    assert app.chat("count plants")["status"] == "done"
    assert app.get("/api/state").json()["tally"]["totals"] == {"plant": 2}


def test_reset_counts(app, llm):
    llm.script({"tool": "record_count", "args": {"object": "chair", "count": 1, "confidence": 50}}, {"text": "ok"})
    app.chat("count")
    assert sql(app, "SELECT COUNT(*) FROM counts") == [(1,)]
    assert app.post("/api/reset", json={"what": "counts"}).json() == {"ok": True}
    assert sql(app, "SELECT COUNT(*) FROM counts") == [(0,)]
    assert app.get("/api/state").json()["tally"]["totals"] == {}


# ----------------------------------------------------------------------------- manual controls
def test_manual_controls_go_through_the_same_mcp_tools(app, robot):
    assert app.post("/api/look", json={"pan_deg": 20, "tilt_deg": 10}).json()["ok"]
    assert telemetry(robot)["camera"] == {"pan": 20, "tilt": 10}
    assert app.post("/api/say", json={"text": "hello beams"}).json()["ok"]
    assert app.post("/api/drive", json={"speed": 20, "seconds": 0.5, "steer_deg": 0}).json()["ok"]
    assert app.post("/api/drive/stop").json()["ok"]
    assert telemetry(robot)["moving"] is False
    assert app.post("/api/look", json={"pan_deg": 0, "tilt_deg": 0}).json()["ok"]
    seen = {a["tool"]: a["detail"] for a in app.get("/api/beam").json()["activity"]}
    assert {"look", "say", "drive", "stop"} <= set(seen) and all(v.startswith("manual:") for v in seen.values())


def test_emergency_stop_halts_the_robot(app, robot):
    httpx.post(f"{robot.base}/stop")
    app.post("/api/drive", json={"speed": 20, "seconds": 3, "steer_deg": 0})
    time.sleep(0.3)
    assert telemetry(robot)["moving"] is True
    assert app.post("/api/stop").status_code == 200
    time.sleep(0.2)
    assert telemetry(robot)["moving"] is False


# ----------------------------------------------------------------------------- maps
def test_saved_maps_round_trip(app):
    saved = app.post("/api/maps", json={"name": "Test floor"}).json()
    assert saved["ok"] and saved["store"] == ("Postgres" if app.mode == "postgres" else "SQLite")
    listed = app.get("/api/maps").json()["maps"]
    assert [m["name"] for m in listed] == ["Test floor"]
    assert sql(app, "SELECT name FROM maps") == [("Test floor",)]
    assert app.post(f"/api/maps/{saved['id']}/load").json() == {"ok": True, "name": "Test floor"}
    assert app.post("/api/maps/9999/load").status_code == 404
    app.delete(f"/api/maps/{saved['id']}")
    assert app.get("/api/maps").json()["maps"] == []


def test_auto_map_starts_and_stops(app):
    assert app.post("/api/automap/start", json={"vision": False, "max_moves": 10, "max_minutes": 1}).json() == {"ok": True}
    assert app.post("/api/automap/start", json={"vision": False}).status_code == 409        # one run at a time
    time.sleep(1)
    assert app.get("/api/state").json()["auto"]["state"] not in ("idle",)
    assert app.post("/api/automap/stop").json() == {"ok": True}
    deadline = time.time() + 10
    while time.time() < deadline and app.get("/api/state").json()["auto"]["state"] in ("starting", "scanning", "stopping", "moving"):
        time.sleep(0.3)
    httpx.post(f"{app.base_url}/api/stop")


# ----------------------------------------------------------------------------- Redis feed
def test_redis_activity_feed_through_the_app(start_app, redis_server, robot):
    app = start_app(REDIS_URL=redis_server.url)
    assert app.post("/api/say", json={"text": "hi"}).json()["ok"]
    feed = app.get("/api/beam").json()
    assert feed["databases"]["redis"]["ok"] and feed["databases"]["redis"]["events"] >= 1
    assert any(a["tool"] == "say" for a in feed["activity"])


def test_inference_card_recognises_the_beams_teleport_proxy(start_app):
    app = start_app(OPENAI_BASE_URL="https://openai.example.beams.run")           # cluster is example.beams.run
    assert app.get("/api/beam").json()["inference"]["via_beam_proxy"] is True


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_a_beam_without_model_access_gets_a_useful_error(start_app, llm, provider):
    """Seen on a real cluster: a beam created with --roles mcp-user could not see the LLM apps, and Teleport
    answered every model call with a 302 to a web login."""
    app = start_app(provider=provider)
    llm.script({"redirect": True})
    job = app.chat("hello")
    assert job["status"] == "error"
    assert "beam-user" in job["detail"] and "tsh apps ls" in job["detail"]
    assert "JSONDecodeError" not in job["detail"]
