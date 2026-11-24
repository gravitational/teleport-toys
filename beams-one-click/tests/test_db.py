"""Storage workflows: SQLite by default, real Postgres through a Teleport-style URL, fallback when it is down,
and the optional Redis activity feed (RESP2, as Teleport's Redis proxy requires)."""
import importlib
import sys

import pytest

POSE = {"x_cm": 12.5, "y_cm": -3.0, "heading_deg": 90.0}
SUMMARY = {"cell_cm": 10, "explored_m2": 4.2, "obstacle_cells": 7}


@pytest.fixture
def load_app(monkeypatch, tmp_path):
    def load(**env):
        for k in ("DATABASE_URL", "REDIS_URL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "app.db"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("app", None)
        return importlib.import_module("app")

    yield load
    sys.modules.pop("app", None)


def exercise(store):
    """Every storage operation the dashboard performs."""
    store.add_count("Chair ", 3, 80, POSE, "by the window")
    store.add_count("monitor", 2, 90, POSE, "")
    rows = store.counts()
    assert [(r["object"], r["count"], r["confidence"], r["note"]) for r in rows] == [
        ("chair", 3, 80, "by the window"), ("monitor", 2, 90, "")]      # names are normalised
    assert rows[0]["x_cm"] == 12.5 and rows[0]["heading_deg"] == 90.0

    map_id = store.save_map("floor 1", "GRIDDATA", SUMMARY, 5, [{"label": "desk"}])
    listed = store.maps()
    assert listed[0]["id"] == map_id and listed[0]["name"] == "floor 1" and listed[0]["landmarks"] == 1
    assert store.map_get(map_id) == {"name": "floor 1", "grid": "GRIDDATA", "landmarks": [{"label": "desk"}]}
    assert store.map_get(map_id + 99) is None
    store.map_delete(map_id)
    assert store.map_get(map_id) is None and store.maps() == []

    stats = store.stats()
    assert stats["ok"] and stats["rows"] == 2
    store.clear()
    assert store.counts() == []


def test_sqlite_is_the_default(load_app, tmp_path):
    app = load_app()
    assert app.store.kind == "SQLite" and app.store.error is None
    exercise(app.store)
    assert (tmp_path / "app.db").exists()


def test_sqlite_survives_a_restart(load_app):
    app = load_app()
    app.store.add_count("plant", 4, 70, POSE, "")
    app = load_app()                                  # new process, same file
    assert [r["object"] for r in app.store.counts()] == ["plant"]


def test_postgres_when_a_url_is_given(load_app, pg_url):
    app = load_app(DATABASE_URL=pg_url)
    assert app.store.kind == "Postgres" and app.store.error is None
    exercise(app.store)
    stats = app.store.stats()
    assert stats["password_in_url"] is False         # Teleport's certificate is the login, never a password
    assert stats["teleport_name"] == "beambot-postgres"


def test_postgres_data_survives_a_restart(load_app, pg_url):
    app = load_app(DATABASE_URL=pg_url)
    app.store.add_count("plant", 4, 70, POSE, "")
    app = load_app(DATABASE_URL=pg_url)
    assert [r["object"] for r in app.store.counts()] == ["plant"]


def test_postgres_down_falls_back_to_sqlite(load_app):
    app = load_app(DATABASE_URL="postgresql://app@127.0.0.1:1/beambot")      # nothing listens on port 1
    assert app.store.kind == "SQLite"
    assert "Postgres unavailable" in app.store.error
    exercise(app.store)                                                       # and the app keeps working


def test_teleport_database_name_is_configurable(load_app, pg_url):
    app = load_app(DATABASE_URL=pg_url, PG_TELEPORT_NAME="my-postgres")
    assert app.store.stats()["teleport_name"] == "my-postgres"


def test_redis_is_off_by_default(load_app):
    app = load_app()
    assert app.cache.r is None and app.cache.stats() == {"enabled": False}
    app.cache.push_activity({"tool": "x"})           # a no-op, never an error
    assert app.cache.activity() is None


def test_redis_activity_feed(load_app, redis_server):
    app = load_app(REDIS_URL=redis_server.url)
    assert app.cache.r is not None and app.cache.error is None
    app.cache.push_activity({"tool": "snapshot", "ok": True})
    app.cache.push_activity({"tool": "drive", "ok": True})
    assert [a["tool"] for a in app.cache.activity()] == ["drive", "snapshot"]      # newest first
    app.cache.set_telemetry({"pose": {"x_cm": 1}})
    stats = app.cache.stats()
    assert stats["ok"] and stats["events"] == 2 and stats["password_in_url"] is False
    assert stats["teleport_name"] == "beambot-redis"


def test_redis_uses_resp2(load_app, redis_server, monkeypatch):
    """Teleport's Redis proxy rejects RESP3's `HELLO 3` handshake, which newer redis-py sends by default."""
    import redis
    seen = {}
    original = redis.Redis.from_url.__func__

    def spy(cls, url, **kw):
        seen.update(kw)
        return original(cls, url, **kw)

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(spy))
    load_app(REDIS_URL=redis_server.url)
    assert seen.get("protocol") == 2


def test_redis_down_degrades_quietly(load_app):
    app = load_app(REDIS_URL="redis://app@127.0.0.1:1")
    assert app.cache.r is None and "Redis unavailable" in app.cache.error
    assert app.cache.stats()["ok"] is False
    app.cache.push_activity({"tool": "x"})
    assert app.cache.activity() is None
