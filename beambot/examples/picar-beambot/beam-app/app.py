"""
BeamBot Control — the web app that runs inside a Teleport Beam (port 8080, `tsh beams publish`).

Layout (from the whiteboard):  CHAT | VIDEO + DATA / OFFICE MAP, plus a Beams feature strip.

How it reaches the robot (no secrets anywhere):
  ROBOT_MCP_URL     -> `tsh proxy mcp $ROBOT_MCP_NAME -p 8888`  (robot tools, RBAC + audit by Teleport)
  ROBOT_STREAM_URL  -> `tsh proxy app picar-stream --port 8001`  (video + telemetry)
  LLM               -> LLM_PROVIDER=openai (default): $OPENAI_BASE_URL/responses (llm://openai.<cluster>, the codex endpoint)
                       LLM_PROVIDER=anthropic: $ANTHROPIC_BASE_URL
                       Either way the Beam's inference proxy injects credentials after traffic leaves the beam.

State (counts) lives in Postgres if DATABASE_URL is set, otherwise SQLite.
"""
import asyncio
import base64
import json
import math
import os
import re
import shutil
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import anthropic
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from automap import AutoMapper, Geometry

ROBOT_MCP_URL = os.environ.get("ROBOT_MCP_URL", "http://127.0.0.1:8888")


def why(e):
    """One readable line for an exception. MCP's task groups wrap the real error in an ExceptionGroup."""
    while getattr(e, "exceptions", None):
        e = e.exceptions[0]
    msg = str(e).strip().splitlines()[0][:500] if str(e).strip() else ""
    return f"{e.__class__.__name__}: {msg}" if msg else e.__class__.__name__

ROBOT_STREAM_URL = os.environ.get("ROBOT_STREAM_URL", "http://127.0.0.1:8001")
ROBOT_MCP_NAME = os.environ.get("ROBOT_MCP_NAME", "picar-mcp")   # Teleport MCP server name
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")   # openai (Responses API) | anthropic
MODEL = os.environ.get("LLM_MODEL") or (os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5") if LLM_PROVIDER == "anthropic"
                                        else "gpt-6.1-sol")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_REASONING = os.environ.get("OPENAI_REASONING", "low")   # robot loop wants quick turns
DATABASE_URL = os.environ.get("DATABASE_URL", "")   # e.g. postgresql://app@localhost:5432/beambot via tsh proxy db
REDIS_URL = os.environ.get("REDIS_URL", "")         # e.g. redis://app@localhost:6379 via tsh proxy db (no password)
PG_TELEPORT_NAME = os.environ.get("PG_TELEPORT_NAME", "beambot-postgres")
REDIS_TELEPORT_NAME = os.environ.get("REDIS_TELEPORT_NAME", "beambot-redis")
HERE = Path(__file__).parent

# Every tool the robot exposes. Teleport RBAC decides which of these this identity actually sees.
ROBOT_TOOLS_ALL = [t for t in os.environ.get("ROBOT_TOOLS", "drive,look,stop,snapshot,get_telemetry,say").split(",") if t]

# Beams inject inference credentials in the proxy, so the SDK just needs a non-empty placeholder.
# follow_redirects=False: Teleport answers a request from an identity that can't see the model app with a 302 to a
# web login. Following it would hand the SDK an HTML page; we want to say what is actually wrong.
claude = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "injected-by-teleport-beam"),
                                  http_client=anthropic.DefaultAsyncHttpxClient(follow_redirects=False, timeout=120))

NO_MODEL_ACCESS = ("The inference endpoint sent us to a Teleport login, which means this beam's identity can't use the "
                   "model apps. Create the beam with the beam-user role (e.g. BEAM_ROLES=beam-user,mcp-user) and "
                   "check that `tsh apps ls` inside it lists the openai / anthropic apps.")
openai_http = httpx.AsyncClient(base_url=OPENAI_BASE_URL, timeout=120, headers={
    "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'injected-by-teleport-beam')}"})

# =============================================================================
# Tasks: each one sets the agent's instructions, the suggested prompts, and the DATA panel layout.
# Add a task by adding an entry here. Widget types: stat, totals, records, list.
# =============================================================================
COMMON_RULES = """You are BeamBot, a small PiCar-X robot in an office. You see through a camera (`snapshot`, aim with `look`)
and sense distance with a forward ultrasonic sensor. Drive in short moves (<= 3s, speed <= 40). The robot refuses
to drive into obstacles closer than 20 cm. Positions are dead-reckoned estimates in cm from the start point.
If a tool you need is missing, Teleport RBAC has not granted it to this identity: say so plainly.
`drive` ends by itself, so you never need `stop` to finish a move: `stop` is an emergency stop that blocks driving until a
human resumes it. Keep replies short and concrete."""

TASKS = {
    "count": {
        "label": "Count objects",
        "icon": "ph-hash",
        "prompt": """TASK: object counter.
  1. Take a snapshot; pan the camera left/center/right if the area is wide.
  2. Count only clearly visible instances. If unsure, give a range and lower your confidence.
  3. Call `record_count` once per area so it is pinned on the office map.
  4. When you move to a new area, call out overlap with previous views to avoid double counting.""",
        "chips": ["Count the chairs you can see", "Look left, center, and right and count monitors",
                  "Drive forward a bit and count again", "What's the tally so far?"],
        "data": [
            {"type": "totals", "title": "Object tally"},
            {"type": "stat", "title": "Areas counted", "key": "count.areas"},
            {"type": "stat", "title": "Avg confidence", "key": "count.avg_confidence", "suffix": "%"},
            {"type": "records", "title": "Latest counts", "limit": 4},
        ],
    },
    "map": {
        "label": "Map the office",
        "icon": "ph-map-trifold",
        "prompt": """TASK: map the office.
  Explore in short moves. After each move, check `get_telemetry`; if the distance ahead is under 40 cm, turn.
  Use `get_map_summary` to report explored area. Prefer unexplored directions. Stop after ~10 moves and summarize.""",
        "chips": ["Explore for 10 moves and map what you find", "How much have we mapped?",
                  "Turn around and scan behind you"],
        "data": [
            {"type": "stat", "title": "Explored", "key": "map.explored_m2", "suffix": " m²"},
            {"type": "stat", "title": "Obstacle cells", "key": "map.obstacle_cells"},
            {"type": "stat", "title": "Distance ahead", "key": "t.ultrasonic_cm", "suffix": " cm"},
            {"type": "stat", "title": "Heading", "key": "t.pose.heading_deg", "suffix": "°"},
            {"type": "stat", "title": "Path length", "key": "map.path_m", "suffix": " m"},
        ],
    },
    "auto": {
        "label": "Auto map",
        "icon": "ph-path",
        "mode": "auto",   # UI shows Start/Stop controls; the explorer in automap.py drives, not the agent
        "prompt": """TASK: narrate auto-mapping.
  The robot is exploring on its own (frontier exploration, no LLM). Do NOT call `drive` or `look`: that hands control
  back from the explorer. Use `get_map_summary` and `get_telemetry` to answer questions about progress.""",
        "chips": ["How much have we mapped?", "Where is BeamBot now?", "Take a snapshot of what's ahead"],
        "data": [
            {"type": "stat", "title": "Mode", "key": "auto.state"},
            {"type": "stat", "title": "Explored", "key": "map.explored_m2", "suffix": " m²"},
            {"type": "stat", "title": "Moves", "key": "auto.moves"},
            {"type": "stat", "title": "Frontiers left", "key": "auto.frontiers_left"},
            {"type": "stat", "title": "Next frontier", "key": "auto.goal_dist_cm", "suffix": " cm"},
            {"type": "stat", "title": "Landmarks seen", "key": "vision.landmarks"},
            {"type": "log", "title": "Explorer log", "key": "auto.log", "limit": 6},
            {"type": "maps", "title": "Saved maps"},
        ],
    },
}

# =============================================================================
# Storage: Postgres (whiteboard) or SQLite fallback
# =============================================================================
class Store:
    def __init__(self):
        self.error = None
        if DATABASE_URL.startswith("postgres"):
            try:
                import psycopg
                self.conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=5)
                self.ph, serial, self.kind = "%s", "SERIAL PRIMARY KEY", "Postgres"
                self._create(serial)
                return
            except Exception as e:  # noqa: BLE001  (tunnel down -> keep the demo running on SQLite)
                self.error = f"Postgres unavailable ({e.__class__.__name__}); using SQLite"
        import sqlite3
        self.conn = sqlite3.connect(os.environ.get("SQLITE_PATH") or HERE / "beambot.db", check_same_thread=False)
        self.conn.isolation_level = None
        self.ph, serial, self.kind = "?", "INTEGER PRIMARY KEY AUTOINCREMENT", "SQLite"
        self._create(serial)

    def _create(self, serial):
        self.conn.execute(f"""CREATE TABLE IF NOT EXISTS counts (
            id {serial}, ts DOUBLE PRECISION, object TEXT, count INTEGER, confidence INTEGER,
            x_cm DOUBLE PRECISION, y_cm DOUBLE PRECISION, heading_deg DOUBLE PRECISION, note TEXT)""")
        self.conn.execute(f"""CREATE TABLE IF NOT EXISTS maps (
            id {serial}, ts DOUBLE PRECISION, name TEXT, cell_cm INTEGER, grid_size INTEGER,
            explored_m2 DOUBLE PRECISION, obstacle_cells INTEGER, moves INTEGER, landmarks TEXT, grid TEXT)""")

    # ---- saved office maps (the occupancy grid + landmarks, ~35 KB each) ----
    def save_map(self, name, grid, summary, moves, landmarks):
        p = self.ph
        self.conn.execute(
            f"INSERT INTO maps (ts, name, cell_cm, grid_size, explored_m2, obstacle_cells, moves, landmarks, grid) "
            f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})",
            (time.time(), name, summary["cell_cm"], GRID, summary["explored_m2"], summary["obstacle_cells"],
             int(moves), json.dumps(landmarks), grid))
        return self.conn.execute("SELECT MAX(id) FROM maps").fetchone()[0]

    def maps(self):
        cols = ["id", "ts", "name", "explored_m2", "obstacle_cells", "moves", "landmarks"]
        cur = self.conn.execute(f"SELECT {', '.join(cols)} FROM maps ORDER BY id DESC LIMIT 20")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for row in rows:
            row["landmarks"] = len(json.loads(row["landmarks"] or "[]"))
        return rows

    def map_get(self, map_id):
        cur = self.conn.execute(f"SELECT name, grid, landmarks FROM maps WHERE id = {self.ph}", (int(map_id),))
        row = cur.fetchone()
        return {"name": row[0], "grid": row[1], "landmarks": json.loads(row[2] or "[]")} if row else None

    def map_delete(self, map_id):
        self.conn.execute(f"DELETE FROM maps WHERE id = {self.ph}", (int(map_id),))

    def add_count(self, obj, n, conf, pose, note):
        p = self.ph
        self.conn.execute(
            f"INSERT INTO counts (ts, object, count, confidence, x_cm, y_cm, heading_deg, note) "
            f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p})",
            (time.time(), obj.lower().strip(), int(n), int(conf), pose["x_cm"], pose["y_cm"], pose["heading_deg"], note))

    def counts(self):
        cols = ["id", "ts", "object", "count", "confidence", "x_cm", "y_cm", "heading_deg", "note"]
        cur = self.conn.execute(f"SELECT {', '.join(cols)} FROM counts ORDER BY id")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def clear(self):
        self.conn.execute("DELETE FROM counts")

    def stats(self):
        t0 = time.time()
        try:
            n = self.conn.execute("SELECT COUNT(*) FROM counts").fetchone()[0]
            ok = True
        except Exception:  # noqa: BLE001
            n, ok = None, False
        info = {"kind": self.kind, "ok": ok, "rows": n, "ms": round((time.time() - t0) * 1000, 1), "error": self.error}
        if self.kind == "Postgres":
            u = urlparse(DATABASE_URL)
            info.update(teleport_name=PG_TELEPORT_NAME, db_user=u.username, database=u.path.lstrip("/"),
                        local=f"{u.hostname}:{u.port}", password_in_url=bool(u.password))
        return info


store = Store()


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def clean_activity(x):
    """A feed item with every field forced to its expected type, or None if it is not an item at all."""
    if not isinstance(x, dict) or not isinstance(x.get("tool"), str):
        return None
    inp = x.get("input")
    return {"ts": _num(x.get("ts")), "tool": x["tool"][:64], "input": inp if isinstance(inp, dict) else {},
            "ok": x.get("ok") is True, "ms": int(_num(x.get("ms"))),
            "detail": x["detail"][:160] if isinstance(x.get("detail"), str) else ""}


class Cache:
    """Optional Redis (via Teleport tunnel): shared activity feed + latest telemetry that survive app restarts."""
    def __init__(self):
        self.r, self.error = None, None
        if REDIS_URL:
            try:
                import redis
                # protocol=2: Teleport's Redis proxy rejects RESP3's `HELLO 3` handshake (newer redis-py default)
                self.r = redis.Redis.from_url(REDIS_URL, socket_timeout=2, decode_responses=True, protocol=2)
                self.r.ping()
            except Exception as e:  # noqa: BLE001
                self.r, self.error = None, f"Redis unavailable ({e.__class__.__name__})"

    def push_activity(self, item):
        if self.r:
            try:
                self.r.lpush("beambot:activity", json.dumps(item)); self.r.ltrim("beambot:activity", 0, 99)
            except Exception:  # noqa: BLE001
                pass

    def activity(self, n=25):
        if self.r:
            try:
                # The feed lives in a shared Redis: any identity with `beambot-data` can write to it. Rebuild every
                # item from typed fields and drop what doesn't fit, so nothing but numbers, booleans and short
                # strings ever reaches the page.
                items = []
                for raw in self.r.lrange("beambot:activity", 0, n - 1):
                    try:
                        item = clean_activity(json.loads(raw))
                    except ValueError:      # one bad entry must not hide the rest
                        continue
                    if item:
                        items.append(item)
                return items
            except Exception:  # noqa: BLE001
                pass
        return None

    def set_telemetry(self, t):
        if self.r:
            try:
                self.r.set("beambot:telemetry", json.dumps(t), ex=30)
            except Exception:  # noqa: BLE001
                pass

    def stats(self):
        if not REDIS_URL:
            return {"enabled": False}
        if not self.r:
            return {"enabled": True, "ok": False, "error": self.error}
        t0 = time.time()
        try:
            self.r.ping(); n = self.r.llen("beambot:activity"); ok = True
        except Exception:  # noqa: BLE001
            n, ok = None, False
        u = urlparse(REDIS_URL)
        return {"enabled": True, "ok": ok, "ms": round((time.time() - t0) * 1000, 1), "events": n,
                "teleport_name": REDIS_TELEPORT_NAME, "db_user": u.username, "local": f"{u.hostname}:{u.port}",
                "password_in_url": bool(u.password)}


cache = Cache()

# =============================================================================
# Office map: occupancy grid built from ultrasonic readings + dead-reckoned pose
# =============================================================================
CELL_CM = 5
GRID = 160                      # 160 x 160 cells = 8 m x 8 m, robot starts in the middle
ORIGIN = GRID // 2
MAX_RANGE_CM = 150              # ignore ultrasonic readings beyond this (noisy)
BEAM_RAYS_DEG = (-10, -5, 0, 5, 10)


class OfficeMap:
    def __init__(self):
        self.reset()

    def reset(self):
        self.score = [[0] * GRID for _ in range(GRID)]  # <0 free, >0 occupied
        self.telemetry = {}
        self.landmarks = []      # from the vision model during auto-map scans: {label, x_cm, y_cm, ts}
        self.hazards = []        # {text, x_cm, y_cm, ts}
        self.virtual = set()     # cells the vision model declared unsafe; the ultrasonic can't clear them
        self.prev_d = None
        self.loaded = None       # name of a saved map that replaced the live grid

    def _cell(self, x_cm, y_cm):
        return ORIGIN + int(round(x_cm / CELL_CM)), ORIGIN - int(round(y_cm / CELL_CM))

    def integrate(self, t):
        self.telemetry = t
        d = t.get("ultrasonic_cm", -1)
        if d is None or d <= 2:
            return
        # HC-SR04 spikes: a reading that disagrees with the previous one by >30% is not trusted on its own. Free
        # space is cleared only up to the shorter of the two and no obstacle is painted. A real change costs one
        # sample at 5 Hz, which is nothing; a phantom wall or a hole in a real one would cost the planner.
        prev, self.prev_d = self.prev_d, d
        trusted = prev is not None and abs(d - prev) <= 0.3 * max(d, prev)
        clear_to = d if trusted or prev is None else min(d, prev)
        pose = t["pose"]
        heading = pose["heading_deg"]  # ultrasonic is fixed to the chassis, facing forward
        rng = min(clear_to, MAX_RANGE_CM)
        for off in BEAM_RAYS_DEG:   # HC-SR04 beam is a ~20° cone, not a line
            th = math.radians(heading + off)
            for i in range(int(rng / (CELL_CM / 2))):
                r = i * CELL_CM / 2
                cx, cy = self._cell(pose["x_cm"] + r * math.cos(th), pose["y_cm"] + r * math.sin(th))
                if 0 <= cx < GRID and 0 <= cy < GRID and (cx, cy) not in self.virtual:
                    self.score[cy][cx] = max(-5, self.score[cy][cx] - 1)
            if trusted and d < MAX_RANGE_CM:
                cx, cy = self._cell(pose["x_cm"] + d * math.cos(th), pose["y_cm"] + d * math.sin(th))
                if 0 <= cx < GRID and 0 <= cy < GRID:
                    self.score[cy][cx] = min(8, self.score[cy][cx] + (3 if off == 0 else 2))

    def add_landmark(self, label, x_cm, y_cm):
        """Same label within 60 cm is the same thing seen again."""
        for lm in self.landmarks:
            if lm["label"] == label and math.dist((lm["x_cm"], lm["y_cm"]), (x_cm, y_cm)) < 60:
                return False
        self.landmarks.append({"label": label, "x_cm": round(x_cm), "y_cm": round(y_cm), "ts": time.time()})
        del self.landmarks[:-60]
        return True

    def add_hazard(self, text, pose):
        self.hazards.append({"text": text[:80], "x_cm": round(pose["x_cm"]), "y_cm": round(pose["y_cm"]),
                             "ts": time.time()})
        del self.hazards[:-20]

    def block_ahead(self, pose, dist_cm=40):
        """Paint a short wall across the robot's nose: the vision model saw something the ultrasonic can't (a step
        down, a cable, glass). The planner routes elsewhere; the ultrasonic is not allowed to clear these cells."""
        th = math.radians(pose["heading_deg"])
        for side in range(-20, 21, CELL_CM):
            x = pose["x_cm"] + dist_cm * math.cos(th) - side * math.sin(th)
            y = pose["y_cm"] + dist_cm * math.sin(th) + side * math.cos(th)
            cx, cy = self._cell(x, y)
            if 0 <= cx < GRID and 0 <= cy < GRID:
                self.score[cy][cx] = 8
                self.virtual.add((cx, cy))

    def encoded(self):
        b = bytes(2 if v > 1 else 1 if v < 0 else 0 for row in self.score for v in row)
        return base64.b64encode(b).decode()

    def dump(self):
        return base64.b64encode(bytes(v + 5 for row in self.score for v in row)).decode()

    def load(self, grid_b64, landmarks=None, name=None):
        raw = base64.b64decode(grid_b64)
        if len(raw) != GRID * GRID:
            raise ValueError("saved grid does not match this grid size")
        self.score = [[raw[cy * GRID + cx] - 5 for cx in range(GRID)] for cy in range(GRID)]
        self.landmarks = list(landmarks or [])
        self.virtual = set()
        self.loaded = name

    def summary(self):
        occ = sum(1 for row in self.score for v in row if v > 1)
        free = sum(1 for row in self.score for v in row if v < 0)
        path = self.telemetry.get("path") or []
        length = sum(math.dist(path[i - 1], path[i]) for i in range(1, len(path))) / 100
        return {"cell_cm": CELL_CM, "explored_m2": round(free * (CELL_CM / 100) ** 2, 2),
                "obstacle_cells": occ, "path_m": round(length, 1), "robot": self.telemetry.get("pose")}


office_map = OfficeMap()
auto_mapper = AutoMapper(lambda: office_map.score, lambda: office_map.telemetry, Geometry(GRID, CELL_CM))

# =============================================================================
# Beams evidence: what the demo shows about the runtime, identity, inference, MCP access, publishing
# =============================================================================
activity = []        # robot tool calls made through Teleport MCP access
inference = {"calls": 0, "total_ms": 0, "last_ms": None, "errors": 0}
beam_cache = {"ts": 0, "data": None}
SECRET_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


def log_activity(tool, args, ok, ms, detail=""):
    item = {"ts": time.time(), "tool": tool, "input": args, "ok": ok, "ms": ms, "detail": detail[:160]}
    activity.append(item)
    del activity[:-100]
    cache.push_activity(item)


async def run_cmd(*args, timeout=6):
    if not shutil.which(args[0]):
        return None
    try:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode() if p.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


# Inference key vars and the base URL that decides whether they are real keys or Beam placeholders.
INFERENCE_KEYS = {"OPENAI_API_KEY": "OPENAI_BASE_URL", "ANTHROPIC_API_KEY": "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_AUTH_TOKEN": "ANTHROPIC_BASE_URL"}


def teleport_endpoint(base_var, cluster):
    """Host of a provider base URL if it is this cluster's Teleport inference proxy, else None."""
    host = urlparse(os.environ.get(base_var, "")).hostname or ""
    return host if cluster and host.endswith("." + cluster) else None


def secret_scan(cluster=None):
    """Names (never values) of environment variables that look like credentials.

    Inference key vars whose base URL is the cluster's Teleport proxy are placeholders (the proxy adds the
    real credentials after traffic leaves the beam), and TELEPORT_* vars are tsh's own state; both are
    listed as Teleport-managed instead of flagged."""
    hits, managed = [], []
    for k, v in os.environ.items():
        if not (SECRET_PATTERN.search(k) or (urlparse(v).password if "://" in v else None)):
            continue
        if k.startswith("TELEPORT_") or (k in INFERENCE_KEYS and teleport_endpoint(INFERENCE_KEYS[k], cluster)):
            managed.append(k)
        else:
            hits.append(k)
    files = [str(p) for p in (Path.home() / ".aws/credentials", Path.home() / ".config/gcloud/credentials.db",
                              Path.home() / ".netrc", Path.home() / ".anthropic") if p.exists()]
    return {"env_names": sorted(hits), "files": files, "teleport_managed": sorted(managed),
            "endpoints": {"openai": teleport_endpoint("OPENAI_BASE_URL", cluster),
                          "anthropic": teleport_endpoint("ANTHROPIC_BASE_URL", cluster)}}


async def allowed_robot_tools():
    try:
        async with streamablehttp_client(ROBOT_MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools), None
    except Exception as e:  # noqa: BLE001
        return [], why(e)


async def beam_info(host_header: str):
    if beam_cache["data"] and time.time() - beam_cache["ts"] < 20:
        data = dict(beam_cache["data"])
    else:
        status = await run_cmd("tsh", "status", "--format=json")
        ident = {}
        if status:
            try:
                prof = json.loads(status).get("active") or {}
                ident = {"user": prof.get("username"), "roles": prof.get("roles") or [],
                         "cluster": prof.get("cluster"), "valid_until": prof.get("valid_until"),
                         "proxy": urlparse(prof.get("profile_url") or "").netloc}
            except Exception:  # noqa: BLE001
                pass
        tools, tools_err = await allowed_robot_tools()
        base = os.environ.get("ANTHROPIC_BASE_URL" if LLM_PROVIDER == "anthropic" else "OPENAI_BASE_URL", "")
        data = {
            "runtime": {"beam": os.environ.get("BEAM_NAME") or socket.gethostname(),
                        "expires": os.environ.get("BEAM_EXPIRES"),  # set by start.sh if available
                        "inside_beam": bool(shutil.which("tsh")) and os.environ.get("USER", "") == "beams"},
            "identity": ident,
            "secrets": secret_scan(ident.get("cluster")),
            "inference": {"endpoint_host": urlparse(base).netloc or "(not set: running outside a Beam)",
                          "via_beam_proxy": teleport_endpoint("ANTHROPIC_BASE_URL" if LLM_PROVIDER == "anthropic" else "OPENAI_BASE_URL", ident.get("cluster")) is not None, "provider": LLM_PROVIDER, "model": MODEL,
                          "label": "Claude" if LLM_PROVIDER == "anthropic" else "Codex"},
            "mcp": {"server": ROBOT_MCP_NAME, "all_tools": sorted(set(ROBOT_TOOLS_ALL) | set(tools)), "allowed": tools, "error": tools_err},
        }
        if ident.get("proxy") and ident.get("cluster"):
            data["audit_url"] = f"https://{ident['proxy']}/web/cluster/{ident['cluster']}/audit/events"
        beam_cache.update(ts=time.time(), data=data)
        data = dict(data)
    data["published_url"] = f"https://{host_header}" if host_header else None
    data["inference_stats"] = {**inference, "avg_ms": round(inference["total_ms"] / inference["calls"]) if inference["calls"] else None}
    data["activity"] = cache.activity() or activity[-25:][::-1]
    data["databases"] = {"postgres": store.stats(), "redis": cache.stats()}
    return data


# =============================================================================
# App + background telemetry poller
# =============================================================================
async def telemetry_poller():
    async with httpx.AsyncClient(timeout=3) as http:
        while True:
            try:
                r = await http.get(f"{ROBOT_STREAM_URL}/telemetry")
                t = r.json()
                office_map.integrate(t)
                if int(time.time() * 5) % 5 == 0:   # ~1/s
                    cache.set_telemetry({k: v for k, v in t.items() if k != "path"})
            except Exception as e:  # noqa: BLE001
                office_map.telemetry = {**office_map.telemetry, "error": f"robot unreachable: {why(e)}"}
            await asyncio.sleep(0.2)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(telemetry_poller())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

# =============================================================================
# Chat agent: LLM (codex endpoint or Claude) + robot MCP tools (via Teleport) + local counter/map tools
# =============================================================================
LOCAL_TOOLS = [
    {"name": "record_count",
     "description": "Save an object count for the robot's current location. It appears on the office map and in the tally.",
     "input_schema": {"type": "object", "properties": {
         "object": {"type": "string", "description": "singular noun, e.g. 'chair', 'monitor', 'plant'"},
         "count": {"type": "integer"},
         "confidence": {"type": "integer", "description": "0-100, how sure you are of this count"},
         "note": {"type": "string", "description": "where, and anything partially hidden"}},
         "required": ["object", "count", "confidence"]}},
    {"name": "get_tally", "description": "Get all recorded counts and totals per object.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_map_summary", "description": "Get explored area, obstacles, path length and robot position.",
     "input_schema": {"type": "object", "properties": {}}},
]


def tally():
    rows = store.counts()
    totals = {}
    for r in rows:
        totals[r["object"]] = totals.get(r["object"], 0) + r["count"]
    confs = [r["confidence"] for r in rows if r["confidence"] is not None]
    return {"totals": totals, "records": rows, "areas": len(rows),
            "avg_confidence": round(sum(confs) / len(confs)) if confs else None}


def mcp_content_to_claude(result):
    blocks = []
    for c in result.content:
        if c.type == "text":
            blocks.append({"type": "text", "text": c.text})
        elif c.type == "image":
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": c.mimeType, "data": c.data}})
    return blocks or [{"type": "text", "text": "(no output)"}]


history = []
chat_lock = asyncio.Lock()


def prune_old_images(keep_last=2):
    """Replace all but the most recent snapshots with a placeholder to keep requests small."""
    seen = 0
    for msg in reversed(history):
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") != "tool_result":
                continue
            for i, c in enumerate(block["content"]):
                if c.get("type") == "image":
                    seen += 1
                    if seen > keep_last:
                        block["content"][i] = {"type": "text", "text": "[older snapshot removed]"}


# Agent runs can take minutes (mapping is up to 20 steps), longer than the published app's proxy keeps a request
# open, so /api/chat starts a job and the UI polls /api/chat/{id}, rendering the trace as it grows.
jobs = {}
_job_tasks = set()   # keep references so running jobs aren't garbage collected

# Emergency stop latches: once STOP is pressed no robot call (except `stop`) is made until the user resumes.
halt = {"on": False, "at": None}
HALT_MSG = "Emergency stop is active: the robot will not move until you press Resume."


class Halted(Exception):
    pass


async def run_job(job, message, task):
    async with chat_lock:  # one robot, one conversation at a time
        start = len(history)
        try:
            job.update(status="done", **await run_agent(message, task, job["trace"]))
        except asyncio.CancelledError:  # the STOP button
            del history[start:]
            job.update(status="stopped", detail="Stopped: the emergency stop cancelled this run.")
            raise
        except Exception as e:  # noqa: BLE001
            del history[start:]  # never leave a dangling tool_use in history
            job.update(status="error", detail=why(e))


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    task = TASKS.get(body.get("task"), TASKS["count"])
    if halt["on"]:
        return JSONResponse({"detail": HALT_MSG}, status_code=423)
    if chat_lock.locked():
        return JSONResponse({"detail": "BeamBot is still working on the previous message."}, status_code=409)
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = job = {"status": "running", "trace": [], "started": time.time()}
    for old in list(jobs)[:-20]:
        del jobs[old]
    t = asyncio.create_task(run_job(job, body["message"], task))
    _job_tasks.add(t)
    t.add_done_callback(_job_tasks.discard)
    return {"job": job_id}


@app.get("/api/chat/{job_id}")
async def chat_job(job_id: str):
    job = jobs.get(job_id)
    return job if job else JSONResponse({"detail": "unknown job (the app may have restarted)"}, status_code=404)


def to_responses_input(messages):
    """Translate the Anthropic-shaped history into Responses API input items."""
    items = []
    for m in messages:
        if isinstance(m["content"], str):
            items.append({"role": m["role"], "content": m["content"]})
            continue
        if m["role"] == "user" and all(b["type"] in ("text", "image") for b in m["content"]):
            items.append({"role": "user", "content": [
                {"type": "input_text", "text": b["text"]} if b["type"] == "text" else
                {"type": "input_image",
                 "image_url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"}
                for b in m["content"]]})
            continue
        for b in m["content"]:
            if b["type"] == "text":
                items.append({"role": m["role"], "content": b["text"]})
            elif b["type"] == "tool_use":
                items.append({"type": "function_call", "call_id": b["id"], "name": b["name"],
                              "arguments": json.dumps(b["input"])})
            elif b["type"] == "tool_result":
                out = [{"type": "input_text", "text": c["text"]} if c["type"] == "text" else
                       {"type": "input_image",
                        "image_url": f"data:{c['source']['media_type']};base64,{c['source']['data']}"}
                       for c in b["content"]]
                items.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": out})
    return items


async def call_openai(model, max_tokens, system, tools, messages):
    """One Responses API turn, returned in the same shape run_agent reads from Anthropic responses."""
    r = await openai_http.post("/responses", json={
        "model": model, "instructions": system, "input": to_responses_input(messages), "store": False,
        "max_output_tokens": max_tokens * 4,  # reasoning tokens count against this
        "reasoning": {"effort": OPENAI_REASONING},
        "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                   "parameters": t["input_schema"]} for t in tools]})
    if 300 <= r.status_code < 400:
        raise RuntimeError(NO_MODEL_ACCESS)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code} from {OPENAI_BASE_URL}: {r.text[:300]}")
    content = []
    for o in r.json().get("output", []):
        if o["type"] == "message":
            text = "".join(c.get("text", "") for c in o["content"] if c["type"] == "output_text")
            if text:
                content.append(SimpleNamespace(type="text", text=text))
        elif o["type"] == "function_call":
            content.append(SimpleNamespace(type="tool_use", id=o["call_id"], name=o["name"],
                                           input=json.loads(o["arguments"] or "{}")))
    stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
    return SimpleNamespace(content=content, stop_reason=stop)


async def call_llm(**kw):
    t0 = time.time()
    try:
        if LLM_PROVIDER == "anthropic":
            if not kw.get("tools"):
                kw.pop("tools", None)
            return await claude.messages.create(**kw)
        return await call_openai(**kw)
    except anthropic.APIStatusError as e:
        inference["errors"] += 1
        if 300 <= e.status_code < 400:
            raise RuntimeError(NO_MODEL_ACCESS) from e
        raise
    except Exception:
        inference["errors"] += 1
        raise
    finally:
        ms = round((time.time() - t0) * 1000)
        inference["calls"] += 1
        inference["total_ms"] += ms
        inference["last_ms"] = ms


# =============================================================================
# Camera + vision model during auto-map scans. Semantics only: what is here, is the floor ahead safe. A vision
# model cannot measure how far the robot moved or turned, so it never touches the pose or clears map cells.
# =============================================================================
VISION_PROMPT = """You are the eyes of a small wheeled robot (10 cm tall, 16 cm wide) mapping an office floor.
Look at the photo from its front camera and reply with JSON only, no prose:
{"landmarks": [{"label": "desk", "bearing": "left|center|right", "distance_m": 1.5}],
 "hazards": ["short phrases"], "blocked_ahead": false, "scene": "one short sentence"}
Rules: at most 5 landmarks, only things you can clearly see, labels are 1-2 lowercase words (desk, chair, door,
window, whiteboard, plant, printer, kitchen, sofa, person). distance_m is your estimate from the camera, 0.3 to 4.
hazards: things a forward ultrasonic cannot sense and a 10 cm robot must avoid: cables, steps or drops, glass,
people's feet, water. blocked_ahead is true only if driving straight ahead for the next metre is unsafe."""
VISION_BEARING_DEG = {"left": 30, "center": 0, "right": -30}   # camera FOV is ~60°
vision = {"calls": 0, "errors": 0, "landmarks": 0, "hazards": 0, "last": ""}


async def describe_view(pose):
    async with httpx.AsyncClient(timeout=5) as http:
        r = await http.get(f"{ROBOT_STREAM_URL}/snapshot.jpg")
    if r.status_code != 200 or len(r.content) < 1000:
        raise RuntimeError("no camera frame")
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                       "data": base64.b64encode(r.content).decode()}}
    vision["calls"] += 1
    try:
        resp = await call_llm(model=MODEL, max_tokens=400, system=VISION_PROMPT, tools=[], messages=[
            {"role": "user", "content": [img, {"type": "text", "text": "Reply with the JSON object only."}]}])
    except Exception:
        vision["errors"] += 1
        raise
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    added = []
    for lm in (data.get("landmarks") or [])[:5]:
        try:
            label = re.sub(r"[^a-z0-9 ]", "", str(lm.get("label", "")).lower()).strip()[:24]
            dist = max(30.0, min(400.0, float(lm.get("distance_m", 1.5)) * 100))
            off = VISION_BEARING_DEG.get(str(lm.get("bearing", "center")).lower(), 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not label:
            continue
        th = math.radians(pose["heading_deg"] + off)
        if office_map.add_landmark(label, pose["x_cm"] + dist * math.cos(th), pose["y_cm"] + dist * math.sin(th)):
            added.append(label)
    vision["landmarks"] = len(office_map.landmarks)
    hazards = [str(h).strip()[:80] for h in (data.get("hazards") or [])[:3] if str(h).strip()]
    for h in hazards:
        office_map.add_hazard(h, pose)
    vision["hazards"] += len(hazards)
    blocked = bool(data.get("blocked_ahead"))
    if blocked:
        office_map.block_ahead(pose)
    vision["last"] = str(data.get("scene", ""))[:120]
    parts = ([f"new: {', '.join(added)}"] if added else []) + ([f"hazards: {'; '.join(hazards)}"] if hazards else [])
    if blocked:
        parts.append("floor ahead unsafe, marked as blocked")
    return "; ".join(parts) or (vision["last"] or "nothing new")


async def run_agent(message, task, trace=None):
    history.append({"role": "user", "content": message})
    trace = [] if trace is None else trace   # caller's list, so a polling client sees steps as they happen
    system = COMMON_RULES + "\n\n" + task["prompt"]
    async with streamablehttp_client(ROBOT_MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            robot_tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                           for t in (await session.list_tools()).tools]
            robot_names = {t["name"] for t in robot_tools}
            tools = robot_tools + LOCAL_TOOLS
            for _ in range(20):  # max agent steps per message
                prune_old_images()
                resp = await call_llm(model=MODEL, max_tokens=1500, system=system, tools=tools, messages=history)
                history.append({"role": "assistant", "content": [
                    {"type": "text", "text": b.text} if b.type == "text" else
                    {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                    for b in resp.content if b.type in ("text", "tool_use")]})
                if resp.stop_reason != "tool_use":
                    break
                results = []
                for b in resp.content:
                    if b.type != "tool_use":
                        continue
                    via = "teleport-mcp" if b.name in robot_names else "local"
                    trace.append({"tool": b.name, "input": b.input, "via": via})
                    t0 = time.time()
                    try:
                        if b.name == "record_count":
                            pose = office_map.telemetry.get("pose", {"x_cm": 0, "y_cm": 0, "heading_deg": 0})
                            store.add_count(b.input["object"], b.input["count"], b.input.get("confidence", 70),
                                            pose, b.input.get("note", ""))
                            content = [{"type": "text", "text": json.dumps(tally()["totals"])}]
                        elif b.name == "get_tally":
                            content = [{"type": "text", "text": json.dumps(tally(), default=str)}]
                        elif b.name == "get_map_summary":
                            content = [{"type": "text", "text": json.dumps(office_map.summary())}]
                        else:
                            if halt["on"] and b.name != "stop":
                                raise Halted(HALT_MSG)
                            if b.name == "drive":
                                auto_mapper.stop("chat agent took over")
                            res = await session.call_tool(b.name, b.input)
                            content = mcp_content_to_claude(res)
                            first = next((c["text"] for c in content if c["type"] == "text"), "image")
                            log_activity(b.name, b.input, not res.isError, round((time.time() - t0) * 1000), first)
                    except Halted:
                        raise   # end the run: do not hand an error back to the model and let it try again
                    except Exception as e:  # noqa: BLE001
                        if via == "teleport-mcp":
                            log_activity(b.name, b.input, False, round((time.time() - t0) * 1000), str(e))
                        content = [{"type": "text", "text": f"error: {e}"}]
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
                history.append({"role": "user", "content": results})
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {"reply": text, "trace": trace}


# =============================================================================
# Endpoints
# =============================================================================
@app.get("/api/tasks")
async def tasks():
    return {k: {kk: v.get(kk) for kk in ("label", "icon", "chips", "data", "mode")} for k, v in TASKS.items()}


@app.get("/api/beam")
async def beam(req: Request):
    host = req.headers.get("x-forwarded-host") or req.headers.get("host", "")
    return await beam_info(host)


@app.post("/api/reset")
async def reset(req: Request):
    what = (await req.json()).get("what", "chat")
    if what in ("chat", "all"):
        history.clear()
    if what in ("map", "all"):
        auto_mapper.stop("map reset")
        office_map.reset()
    if what in ("counts", "all"):
        store.clear()
    return {"ok": True}


async def robot_estop():
    """Latch the emergency stop ON THE ROBOT: the stream app's /stop, or the MCP `stop` tool if there is no stream
    app or it fails. Both stop the motors and make the robot refuse `drive` until it is resumed, so a drive that is
    already on the wire (a pad press whose MCP session is still opening, an agent call mid-flight) or one from
    another client is refused when it arrives. Returns (ok, via, text-or-errors)."""
    errors = []
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            r = await http.post(f"{ROBOT_STREAM_URL}/stop")
        r.raise_for_status()
        return True, "stream", r.json().get("result", "stopped")
    except Exception as e:  # noqa: BLE001
        errors.append(f"stream app: {why(e)}")
    resp = await robot_call("stop", {})
    body = json.loads(resp.body)
    if body.get("ok"):
        return True, "mcp", body.get("result", "stopped")
    errors.append(f"MCP stop: {body.get('error') or body.get('result')}")
    return False, None, "; ".join(errors)


@app.post("/api/stop")
async def stop():
    """Emergency stop: latch it on the robot first, then end everything in this app that could move the robot.
    Only a 200 means the robot was told to stop: anything else is reported as an error. Cancelling the chat job
    stops NEW calls; the latch on the robot is what refuses the ones that were already sent."""
    halt.update(on=True, at=time.time())
    manual_stop["gen"] += 1
    auto_mapper.stop("emergency stop")
    ok, via, text = await robot_estop()
    running = [t for t in _job_tasks if not t.done()]
    for t in running:
        t.cancel()
    if running:
        await asyncio.wait(running, timeout=3)
    if ok:
        return {"ok": True, "halted": True, "via": via, "result": text}
    return JSONResponse({"ok": False, "halted": True, "error": text}, status_code=502)


@app.post("/api/resume")
async def resume():
    """Clear the latch on the robot (stream app, or the MCP `resume` tool), then in this app. If the robot can't be
    reached the latch stays: the page keeps showing Resume."""
    errors = []
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            r = await http.post(f"{ROBOT_STREAM_URL}/resume")
        r.raise_for_status()
        via = "stream"
    except Exception as e:  # noqa: BLE001
        errors.append(f"stream app: {why(e)}")
        body = json.loads((await robot_call("resume", {})).body)
        if not body.get("ok"):
            errors.append(f"MCP resume: {body.get('error') or body.get('result')}"
                          " (resuming needs the picar-stream role, or a role that grants the `resume` tool)")
            return JSONResponse({"ok": False, "halted": True, "error": "; ".join(errors)}, status_code=502)
        via = "mcp"
    halt.update(on=False, at=None, cleared=time.time())
    return {"ok": True, "halted": False, "via": via}


EXEMPT_WHEN_HALTED = ("stop", "resume")   # the only robot calls an emergency stop does not block


async def robot_call(tool, args):
    """One robot MCP tool call through Teleport, logged like the agent's calls."""
    refused = JSONResponse({"ok": False, "halted": True, "error": HALT_MSG}, status_code=423)
    if halt["on"] and tool not in EXEMPT_WHEN_HALTED:
        return refused
    t0 = time.time()
    try:
        blocked = False
        async with streamablehttp_client(ROBOT_MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()          # can take seconds: STOP may have been pressed meanwhile
                if halt["on"] and tool not in EXEMPT_WHEN_HALTED:
                    blocked = True
                else:
                    res = await s.call_tool(tool, args)
        if blocked:
            return refused
        text = next((c.text for c in res.content if c.type == "text"), "")
        log_activity(tool, args, not res.isError, round((time.time() - t0) * 1000), "manual: " + text)
        return JSONResponse({"ok": not res.isError, "result": text}, status_code=502 if res.isError else 200)
    except Exception as e:  # noqa: BLE001
        log_activity(tool, args, False, round((time.time() - t0) * 1000), f"manual: {e}")
        return JSONResponse({"ok": False, "error": why(e)}, status_code=502)


# Manual calls go one at a time so a release (stop) is never overtaken by a drive that was still in flight.
manual_lock = asyncio.Lock()
manual_stop = {"gen": 0}


@app.post("/api/drive")
async def manual_drive(req: Request):
    """Hold-to-drive from the UI pad: the same `drive` tool the agent uses, so Teleport RBAC + audit apply.
    The UI re-sends every ~0.8 s while held; each call is a short non-blocking move the robot times out itself."""
    b = await req.json()
    auto_mapper.stop("manual drive took over")
    gen = manual_stop["gen"]
    async with manual_lock:
        if gen != manual_stop["gen"]:   # released while this was queued
            return {"ok": True, "result": "skipped: stopped"}
        return await robot_call("drive", {"speed": int(b.get("speed", 0)), "seconds": float(b.get("seconds", 1)),
                                          "steer_deg": int(b.get("steer_deg", 0)), "wait": False})


@app.post("/api/drive/stop")
async def manual_drive_stop():
    """Letting go of the pad: a plain stop that does NOT latch (that is what the STOP button is for). Fast over the
    stream app, then an ordered speed-0 `drive` after any in-flight drive."""
    manual_stop["gen"] += 1
    if halt["on"]:
        return {"ok": True, "result": "already stopped: the emergency stop is active"}
    try:
        async with httpx.AsyncClient(timeout=3) as http:
            await http.post(f"{ROBOT_STREAM_URL}/release")
    except Exception:  # noqa: BLE001
        pass
    async with manual_lock:
        return await robot_call("drive", {"speed": 0, "seconds": 0.1, "steer_deg": 0, "wait": False})


async def auto_call(tool, args):
    """Robot MCP call for the auto-mapper: same Teleport path and activity log as the agent, prefixed `auto:`."""
    if halt["on"] and tool not in EXEMPT_WHEN_HALTED:
        return False, HALT_MSG
    t0 = time.time()
    try:
        blocked = False
        async with streamablehttp_client(ROBOT_MCP_URL) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if halt["on"] and tool not in EXEMPT_WHEN_HALTED:
                    blocked = True
                else:
                    res = await s.call_tool(tool, args)
        if blocked:
            return False, HALT_MSG
        text = next((c.text for c in res.content if c.type == "text"), "")
        log_activity(tool, args, not res.isError, round((time.time() - t0) * 1000), "auto: " + text)
        return not res.isError, text
    except Exception as e:  # noqa: BLE001
        log_activity(tool, args, False, round((time.time() - t0) * 1000), f"auto: {e}")
        return False, why(e)


@app.post("/api/automap/start")
async def automap_start(req: Request):
    """Body (all optional): speed 15-40, max_moves, max_minutes, vision (camera + model during scans, default on)."""
    b = await req.json() if (await req.body()) else {}
    if halt["on"]:
        return JSONResponse({"ok": False, "error": HALT_MSG}, status_code=423)
    on_view = describe_view if b.get("vision", True) else None
    if not auto_mapper.start(auto_call, speed=b.get("speed"), max_moves=b.get("max_moves"),
                             max_minutes=b.get("max_minutes"), on_view=on_view):
        return JSONResponse({"ok": False, "error": "already running"}, status_code=409)
    return {"ok": True}


@app.get("/api/maps")
async def maps_list():
    return {"maps": store.maps(), "store": store.kind}


@app.post("/api/maps")
async def maps_save(req: Request):
    b = await req.json() if (await req.body()) else {}
    name = str(b.get("name") or f"Office {time.strftime('%b %d %H:%M')}")[:60]
    s = office_map.summary()
    mid = store.save_map(name, office_map.dump(), s, auto_mapper.status.get("moves", 0), office_map.landmarks)
    return {"ok": True, "id": mid, "name": name, "store": store.kind, "explored_m2": s["explored_m2"]}


@app.post("/api/maps/{map_id}/load")
async def maps_load(map_id: int):
    m = store.map_get(map_id)
    if not m:
        return JSONResponse({"ok": False, "error": "no such map"}, status_code=404)
    auto_mapper.stop("a saved map was loaded")
    office_map.load(m["grid"], m["landmarks"], m["name"])
    return {"ok": True, "name": m["name"]}


@app.delete("/api/maps/{map_id}")
async def maps_delete(map_id: int):
    store.map_delete(map_id)
    return {"ok": True}


@app.post("/api/say")
async def say(req: Request):
    """Speaker demo: the robot's `say` MCP tool through Teleport, like every other robot call."""
    b = await req.json() if (await req.body()) else {}
    return await robot_call("say", {"text": str(b.get("text") or "Hello Beams, I love you")[:120]})


@app.post("/api/automap/stop")
async def automap_stop():
    return {"ok": auto_mapper.stop("stopped from the UI")}


@app.post("/api/look")
async def manual_look(req: Request):
    b = await req.json()
    return await robot_call("look", {"pan_deg": int(b.get("pan_deg", 0)), "tilt_deg": int(b.get("tilt_deg", 0))})


def robot_latched():
    """The robot's own latch, as last polled: it shows an emergency stop pressed by another client too. Just after a
    Resume the last poll can still be stale, so it is ignored for a moment."""
    return bool(office_map.telemetry.get("halted")) and time.time() - (halt.get("cleared") or 0) > 1.5


@app.get("/api/state")
async def state():
    return {"telemetry": dict(office_map.telemetry), "grid": office_map.encoded(), "grid_size": GRID,
            "cell_cm": CELL_CM, "tally": tally(), "map": office_map.summary(), "auto": auto_mapper.status,
            "landmarks": office_map.landmarks, "hazards": office_map.hazards[-5:], "loaded_map": office_map.loaded,
            "vision": vision, "halted": halt["on"] or robot_latched()}


@app.get("/api/video")
async def video():
    async def relay():
        # The robot sends at least one frame per second, so 5 s of silence means the upstream stalled.
        # End the response then; the page notices and reconnects (a hung relay would freeze the video forever).
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=5.0)) as http:
                async with http.stream("GET", f"{ROBOT_STREAM_URL}/video.mjpg") as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
        except httpx.HTTPError:
            return
    return StreamingResponse(relay(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")
