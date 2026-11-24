"""The BeamBot launch scripts, against a fake `tsh`. start.sh runs with a stub python3 so the app itself isn't
started; what's asserted is which Teleport tunnels are opened and what the app would be told (env)."""
import json
import os
import stat
import subprocess

import pytest

from conftest import BEAM_APP

START, DEPLOY = BEAM_APP / "start.sh", BEAM_APP / "deploy-beam.sh"


@pytest.fixture
def stubbed(teleport, tmp_path):
    """python3 that records its argv + the env start.sh exported, instead of installing/serving."""
    out = tmp_path / "python-calls.jsonl"
    stub = teleport.bin / "python3"
    stub.write_text('#!/bin/sh\nenv | grep -E "^(DATABASE_URL|REDIS_URL|ROBOT_|PG_|REDIS_)" | sort > "%s.env"\n'
                    'echo "$@" >> "%s"\n' % (out, out))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return out


def start(teleport, stubbed, tmp_path, **env):
    r = subprocess.run(["bash", str(START)], env=teleport.env(**{"TUNNEL_WAIT": 1, **env}), capture_output=True, text=True,
                       timeout=60, cwd=tmp_path)
    proxies = [c["args"] for c in teleport.calls("tsh", "proxy")]
    exported = {}
    envfile = stubbed.with_name(stubbed.name + ".env")
    if envfile.exists():
        exported = dict(l.split("=", 1) for l in envfile.read_text().splitlines())
    return r, proxies, exported


def kinds(proxies):
    return sorted((a[1], a[-1] if a[1] == "db" else a[2]) for a in proxies)   # tunnels start in the background


def test_defaults_use_the_beams_own_identity_and_sqlite(teleport, stubbed, tmp_path):
    teleport.set(dbs=[])
    r, proxies, env = start(teleport, stubbed, tmp_path)
    assert r.returncode == 0, r.stderr
    assert kinds(proxies) == [("app", "picar-stream"), ("mcp", "picar-mcp")]
    assert "DATABASE_URL" not in env and "using SQLite" in r.stdout
    assert env["ROBOT_MCP_URL"] == "http://127.0.0.1:8888" and teleport.calls("tctl") == []
    assert "-m uvicorn app:app" in stubbed.read_text()


def test_postgres_is_used_when_teleport_shows_it(teleport, stubbed, tmp_path):
    r, proxies, env = start(teleport, stubbed, tmp_path)
    assert ("db", "beambot-postgres") in kinds(proxies)
    db = next(a for a in proxies if a[1] == "db")
    assert "--tunnel" in db and "--db-user=app" in db and "--db-name=beambot" in db and "5432" in db
    assert env["DATABASE_URL"] == "postgresql://app@localhost:5432/beambot"      # certificate auth: no password


def test_postgres_is_found_when_tsh_marks_it_active(teleport, stubbed, tmp_path):
    """`tsh db ls` prefixes a database that has a live session with '>' (seen on a real cluster)."""
    teleport.set(db_active=["beambot-postgres"])
    r, proxies, env = start(teleport, stubbed, tmp_path)
    assert ("db", "beambot-postgres") in kinds(proxies) and "DATABASE_URL" in env


def test_databases_can_be_switched_off(teleport, stubbed, tmp_path):
    r, proxies, env = start(teleport, stubbed, tmp_path, USE_DBS=0)
    assert not any(k == "db" for k, _ in kinds(proxies)) and "DATABASE_URL" not in env


def test_databases_can_be_required(teleport, stubbed, tmp_path):
    teleport.set(dbs=[])
    r, proxies, env = start(teleport, stubbed, tmp_path, USE_DBS=1)
    assert ("db", "beambot-postgres") in kinds(proxies)


def test_redis_is_opt_in_and_independent_of_postgres(teleport, stubbed, tmp_path):
    teleport.set(dbs=[])
    r, proxies, env = start(teleport, stubbed, tmp_path, USE_REDIS=1)
    assert kinds(proxies) == [("app", "picar-stream"), ("db", "beambot-redis"), ("mcp", "picar-mcp")]
    assert env["REDIS_URL"] == "redis://app@localhost:6379" and "DATABASE_URL" not in env
    r, proxies, env = start(teleport, stubbed, tmp_path)
    assert "REDIS_URL" not in env


def test_custom_resource_names(teleport, stubbed, tmp_path):
    teleport.set(dbs=["pg2"])
    r, proxies, env = start(teleport, stubbed, tmp_path, ROBOT_MCP_NAME="my-robot", ROBOT_STREAM_APP="",
                            PG_TELEPORT_NAME="pg2", PG_DB_NAME="d2", DB_USER="bob")
    assert kinds(proxies) == [("db", "pg2"), ("mcp", "my-robot")]
    assert env["DATABASE_URL"] == "postgresql://bob@localhost:5432/d2"


def test_a_dead_tunnel_is_called_out(teleport, stubbed, tmp_path):
    teleport.set(tunnel_fail=["mcp"])
    r, *_ = start(teleport, stubbed, tmp_path, )
    assert "WARN" in r.stderr and "tunnel" in r.stderr


# --------------------------------------------------------------------------- deploy-beam.sh
def deploy(teleport, **env):
    return subprocess.run(["bash", str(DEPLOY)], env=teleport.env(READY_TIMEOUT=20, **env), capture_output=True,
                          text=True, timeout=120)


def test_deploy_creates_a_beam_ships_the_app_and_publishes(teleport):
    r = deploy(teleport, LLM_PROVIDER="openai", BEAM_ROLES="picar-driver")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--roles=picar-driver" in teleport.calls("tsh", "beams", "add")[0]["args"]
    assert teleport.calls("tctl") == []
    app = teleport.beam_home / "beambot"
    for f in ("app.py", "automap.py", "requirements.txt", "start.sh", "static/index.html", ".env"):
        assert (app / f).exists(), f
    env = (app / ".env").read_text()
    assert "LLM_PROVIDER=openai" in env and "USE_DBS=auto" in env
    assert "BOT" not in env and "tbot" not in env.lower()
    assert oct((app / ".env").stat().st_mode & 0o777) == "0o600"
    assert teleport.calls("tsh", "beams", "publish")[0]["args"][2] == "fake-beam"
    assert "https://fake-beam-4618.example.beams.run" in r.stdout + r.stderr


def test_deploy_refuses_to_create_a_beam_that_inherits_everything(teleport):
    r = deploy(teleport)
    assert r.returncode != 0 and "BEAM_ROLES" in r.stderr
    assert teleport.calls("tsh", "beams", "add") == []


def test_deploy_can_inherit_when_explicitly_asked(teleport):
    r = deploy(teleport, INHERIT_ROLES=1)
    assert r.returncode == 0 and not any(a.startswith("--roles") for a in teleport.calls("tsh", "beams", "add")[0]["args"])


def test_deploy_redeploys_into_an_existing_beam(teleport):
    r = deploy(teleport, BEAM="warm-orbit")
    assert r.returncode == 0 and teleport.calls("tsh", "beams", "add") == []
    assert teleport.calls("tsh", "beams", "publish")[0]["args"][2] == "warm-orbit"


def test_deploy_forwards_robot_app_names_including_an_empty_stream_app(teleport):
    """`ROBOT_STREAM_APP=` (empty) means "no video app" and must reach the beam as an empty value."""
    r = deploy(teleport, BEAM_ROLES="beam-user,mcp-user", ROBOT_STREAM_APP="", ROBOT_MCP_NAME="my-robot")
    assert r.returncode == 0, r.stdout + r.stderr
    env = (teleport.beam_home / "beambot/.env").read_text().splitlines()
    assert "ROBOT_STREAM_APP=''" in env and "ROBOT_MCP_NAME=my-robot" in env


def test_deploy_leaves_robot_app_names_alone_when_unset(teleport):
    deploy(teleport, BEAM_ROLES="beam-user,mcp-user")
    assert "ROBOT_" not in (teleport.beam_home / "beambot/.env").read_text()
