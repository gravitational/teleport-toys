"""
Auto-map: drive the PiCar-X around the office on its own until the occupancy grid has no reachable frontier.

No LLM in the loop. Each step reads the app's occupancy grid (built by OfficeMap from ultrasonic + dead-reckoned
pose), plans to the nearest frontier, and makes one short move with the robot's own `drive` MCP tool, so every move
goes through Teleport (RBAC + audit) exactly like the chat agent's and the drive pad's.

  1. Scan: K-turn in place (forward + steer, reverse + opposite steer) until the heading has swept ~360°.
  2. Plan: Dijkstra from the robot's cell to the nearest frontier (a free cell bordering unknown space).
     Occupied cells are walls; cells within the robot's radius of a wall cost more; unknown costs a bit more.
  3. Move: forward with proportional steering toward a waypoint ~30 cm down the path, or K-turn if the waypoint is
     more than ALIGN_DEG off the nose. If the ultrasonic shows something under CLOSE_CM, the K-turn runs reverse
     half first so the robot backs away instead of asking for a move the robot will refuse (it refuses under 20 cm).
  4. Scan again every SCAN_EVERY_CM of travel. If GOAL_MOVES moves add no explored cells, blacklist that frontier.
  Reverse moves are skipped when the map shows a wall behind the robot (the ultrasonic only looks forward).
  Stops when no frontier is left, on the move / time limit, when STUCK_MOVES moves in a row didn't move the robot
  (state "stuck": blocked front and back, a human has to lift it), on the UI's Stop, or on any manual drive / STOP.

The planning half (frontiers, plan, next_move) is pure and unit-tested in test_automap.py, which also has a
closed-loop benchmark in a simulated room: `python3 test_automap.py`.
"""
import asyncio
import heapq
import math
import os
import time

SPEED = int(os.environ.get("AUTOMAP_SPEED", "30"))
STEP_S = float(os.environ.get("AUTOMAP_STEP_S", "1.0"))        # one forward move
TURN_S = float(os.environ.get("AUTOMAP_TURN_S", "0.8"))        # each half of a K-turn
MAX_MOVES = int(os.environ.get("AUTOMAP_MAX_MOVES", "300"))
MAX_MINUTES = float(os.environ.get("AUTOMAP_MAX_MINUTES", "15"))
ROBOT_RADIUS_CM = 15
MIN_GOAL_CM = 30          # ignore frontiers right under the robot
MIN_FRONTIER_CM = 25      # frontier clusters shorter than this are gaps in a wall, not openings
SCAN_EVERY_CM = 120       # re-scan after this much travel
LOOKAHEAD_CM = 30
ALIGN_DEG = 25            # beyond this, K-turn instead of steering
GOAL_MOVES = 8            # give up on a frontier after this many moves without new explored cells
BLACKLIST_CM = 25
SCAN_MAX_MOVES = 10
REAR_CLEAR_CM = 25        # reverse only if the map shows no wall this close behind (no rear sensor)
REAR_HALF_WIDTH_CM = 8    # PiCar-X is ~16 cm wide
CLOSE_CM = 30             # keep this much space ahead: forward moves are cut short to end here, and under it the
                          # robot backs away first (the robot itself refuses forward moves under 20 cm)
STUCK_MOVES = 5           # this many consecutive moves that didn't move the robot -> stuck, hand back to a human
SLIP_MOVES = 2            # forward moves in a row after which the distance ahead didn't change -> wheels slipping
DEFAULT_CM_PER_S = 18.0   # robot_mcp.py default: speed 30 x 0.6 cm/s per unit; the explorer re-learns it per run

FREE, UNKNOWN, OCC = -1, 0, 1
NEIGHBORS = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


class Stuck(RuntimeError):
    pass


def classify(v):
    """Same thresholds as OfficeMap.encoded(): >1 occupied, <0 free, else unknown."""
    return OCC if v > 1 else FREE if v < 0 else UNKNOWN


class Geometry:
    """Grid <-> world (cm) for OfficeMap's layout: row 0 is the top (+y), robot starts at the centre cell."""

    def __init__(self, size, cell_cm):
        self.size, self.cell_cm, self.origin = size, cell_cm, size // 2

    def cell(self, x_cm, y_cm):
        return self.origin + int(round(x_cm / self.cell_cm)), self.origin - int(round(y_cm / self.cell_cm))

    def world(self, cx, cy):
        return (cx - self.origin) * self.cell_cm, (self.origin - cy) * self.cell_cm

    def inside(self, cx, cy):
        return 0 <= cx < self.size and 0 <= cy < self.size


def frontiers(score, geo):
    """Frontier cells -> cluster index. A frontier cell is free with at least 2 unknown 8-neighbours (one unknown
    neighbour is usually just a ray's ragged edge). Clusters shorter than a robot width are dropped: they are gaps
    in a wall's outline, not openings."""
    cells = set()
    for cy in range(1, geo.size - 1):
        row = score[cy]
        for cx in range(1, geo.size - 1):
            if row[cx] >= 0:
                continue
            unknown = sum(1 for dx, dy in NEIGHBORS if score[cy + dy][cx + dx] == 0)
            if unknown >= 2:
                cells.add((cx, cy))
    min_cells = max(2, round(MIN_FRONTIER_CM / geo.cell_cm))
    out, seen, k = {}, set(), 0
    for c in cells:
        if c in seen:
            continue
        cluster, todo = [], [c]
        seen.add(c)
        while todo:
            p = todo.pop()
            cluster.append(p)
            for dx, dy in NEIGHBORS:
                n = (p[0] + dx, p[1] + dy)
                if n in cells and n not in seen:
                    seen.add(n)
                    todo.append(n)
        if len(cluster) >= min_cells:
            out.update((p, k) for p in cluster)
            k += 1
    return out


def inflate(score, geo, radius_cells):
    """Cells within radius of an occupied cell."""
    near = set()
    r2 = radius_cells * radius_cells
    offsets = [(dx, dy) for dx in range(-radius_cells, radius_cells + 1)
               for dy in range(-radius_cells, radius_cells + 1) if dx * dx + dy * dy <= r2]
    for cy in range(geo.size):
        for cx in range(geo.size):
            if score[cy][cx] > 1:
                for dx, dy in offsets:
                    near.add((cx + dx, cy + dy))
    return near


def plan(score, geo, start, blacklist=(), prefer=None, fr=None):
    """Nearest reachable frontier from `start` (cell). Returns (goal, path as list of cells) or (None, []).
    `prefer` (the current goal) wins while it's still a frontier, so two equally near frontiers don't flip-flop.
    `fr` is a precomputed frontiers() result."""
    radius = max(1, round(ROBOT_RADIUS_CM / geo.cell_cm))
    near_wall = inflate(score, geo, radius)
    if fr is None:
        fr = frontiers(score, geo)
    min_goal = MIN_GOAL_CM / geo.cell_cm
    bl = BLACKLIST_CM / geo.cell_cm
    goals = {g for g in fr
             if math.dist(g, start) >= min_goal and all(math.dist(g, b) > bl for b in blacklist)}
    if not goals:
        return None, []
    escape = 2 * radius
    if prefer is not None:
        near = {g for g in goals if math.dist(g, prefer) <= 2}
        if near:
            goal, path = _dijkstra(score, geo, start, near, near_wall, escape)
            if goal is not None:
                return goal, path
    return _dijkstra(score, geo, start, goals, near_wall, escape)


def _dijkstra(score, geo, start, goals, near_wall, escape):
    """Cells within the robot's radius of a wall are impassable, except within `escape` cells of the start (the
    robot may already be next to a wall and has to be allowed out). This also closes gaps in a wall's outline that
    are narrower than the robot, so frontiers leaking through them are unreachable rather than tempting."""
    dist = {start: 0.0}
    parent = {}
    heap = [(0.0, start)]
    while heap:
        d, c = heapq.heappop(heap)
        if d > dist.get(c, math.inf):
            continue
        if c in goals and c not in near_wall:
            path = [c]
            while path[-1] != start:
                path.append(parent[path[-1]])
            return c, path[::-1]
        for dx, dy in NEIGHBORS:
            n = (c[0] + dx, c[1] + dy)
            if not geo.inside(*n):
                continue
            v = score[n[1]][n[0]]
            if v > 1:
                continue                                    # wall
            step = math.hypot(dx, dy) * (1.0 if v < 0 else 2.0)   # prefer known-free space
            if n in near_wall:
                if math.dist(n, start) > escape:
                    continue
                step *= 8                                   # escaping a wall: allowed but expensive
            nd = d + step
            if nd < dist.get(n, math.inf):
                dist[n] = nd
                parent[n] = c
                heapq.heappush(heap, (nd, n))
    return None, []


def wrap_deg(a):
    return (a + 180) % 360 - 180


def bearing_err(pose, x_cm, y_cm):
    """Degrees from the robot's nose to a world point: positive = counter-clockwise."""
    return wrap_deg(math.degrees(math.atan2(y_cm - pose["y_cm"], x_cm - pose["x_cm"])) - pose["heading_deg"])


def forward_secs(want, ahead_cm, cm_per_s):
    """Cut a forward move short so it ends about CLOSE_CM from whatever the ultrasonic sees."""
    if ahead_cm is None or ahead_cm <= 0:
        return want
    return round(max(0.2, min(want, (ahead_cm - CLOSE_CM) / cm_per_s)), 2)


def next_move(pose, path, geo, kturn_deg=100.0, ahead_cm=None, speed=SPEED, cm_per_s=DEFAULT_CM_PER_S):
    """Drive command(s) toward the path. Positive steer turns the dead-reckoned heading counter-clockwise going
    forward (and clockwise in reverse), matching robot_mcp.py's pose model. `kturn_deg` is how far one full-length
    K-turn rotates (learned by AutoMapper); the K-turn is shortened to aim at the path instead of overshooting it.
    Forward moves are cut short to end CLOSE_CM from whatever is ahead (`ahead_cm`); with less than that already,
    the K-turn runs its reverse half first."""
    x, y = pose["x_cm"], pose["y_cm"]
    look = LOOKAHEAD_CM / geo.cell_cm
    target = path[-1]
    for c in path:
        if math.dist(geo.cell(x, y), c) >= look:
            target = c
            break
    err = bearing_err(pose, *geo.world(*target))
    close = ahead_cm is not None and 0 < ahead_cm < CLOSE_CM + 5
    if abs(err) <= ALIGN_DEG and not close:
        steer = max(-30, min(30, round(err * 1.5)))
        return [{"speed": speed, "seconds": forward_secs(STEP_S, ahead_cm, cm_per_s), "steer_deg": steer}]
    s = 30 if err >= 0 else -30
    secs = round(max(0.2, min(1.0, abs(err) / max(kturn_deg, 10))) * TURN_S, 2)
    fwd = {"speed": speed, "seconds": forward_secs(secs, ahead_cm, cm_per_s), "steer_deg": s}
    rev = {"speed": -speed, "seconds": secs, "steer_deg": -s}
    return [rev, fwd] if close else [fwd, rev]


class AutoMapper:
    """Runs the explore loop as one asyncio task. `call_tool(name, args) -> (ok, text)` goes through Teleport MCP."""

    def __init__(self, get_score, get_telemetry, geo):
        self.get_score, self.get_telemetry, self.geo = get_score, get_telemetry, geo
        self.task = None
        self.status = {"state": "idle"}
        self.speed, self.max_moves, self.max_minutes = SPEED, MAX_MOVES, MAX_MINUTES
        self.kturn_deg = 100.0   # learned during scans, kept across runs
        self.cm_per_s = DEFAULT_CM_PER_S
        self.no_motion = 0
        self.slips = 0

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def start(self, call_tool, speed=None, max_moves=None, max_minutes=None, on_view=None):
        """`on_view(pose) -> str | None` is an optional camera hook (app.py: snapshot + vision model) called at the
        start and half-way point of each scan. It may add landmarks or hazards to the map; it never moves the robot."""
        if self.running:
            return False
        self.on_view = on_view
        self.speed = max(15, min(40, int(speed or SPEED)))
        self.max_moves = max(10, int(max_moves or MAX_MOVES))
        self.max_minutes = max(1.0, float(max_minutes or MAX_MINUTES))
        self.no_motion = self.slips = 0
        self.status = {"state": "starting", "moves": 0, "scans": 1, "started": time.time(), "log": [],
                       "speed": self.speed, "max_moves": self.max_moves, "max_minutes": self.max_minutes}
        self._note(f"started: speed {self.speed}, up to {self.max_moves} moves / {self.max_minutes:g} min")
        self.task = asyncio.create_task(self._run(call_tool))
        return True

    def stop(self, why="stopped"):
        if self.running:
            self.status.update(state="stopping", detail=why)
            self.task.cancel()
            return True
        return False

    def _note(self, msg):
        """Short decision log for the UI's auto mode."""
        log = self.status.setdefault("log", [])
        log.append({"ts": time.time(), "msg": msg})
        del log[:-20]

    def _pose(self):
        t = self.get_telemetry()
        if t.get("error") or "pose" not in t:
            raise RuntimeError(t.get("error") or "no telemetry yet")
        return t["pose"]

    def _ahead(self):
        """Latest ultrasonic reading in cm, or None when the sensor has nothing."""
        d = self.get_telemetry().get("ultrasonic_cm")
        return float(d) if d is not None and d > 0 else None

    def _clear_behind(self):
        """The ultrasonic only looks forward, so check the map before reversing: no wall within REAR_CLEAR_CM.
        One occupied cell alone is usually a sonar-cone phantom off a corner; a wall is a run of them."""
        p = self._pose()
        th = math.radians(p["heading_deg"])
        score = self.get_score()
        hits = 0
        for back in range(self.geo.cell_cm, REAR_CLEAR_CM + 1, self.geo.cell_cm):
            for side in (-REAR_HALF_WIDTH_CM, 0, REAR_HALF_WIDTH_CM):
                x = p["x_cm"] - back * math.cos(th) - side * math.sin(th)
                y = p["y_cm"] - back * math.sin(th) + side * math.cos(th)
                cx, cy = self.geo.cell(x, y)
                if self.geo.inside(cx, cy) and score[cy][cx] > 1:
                    hits += 1
        return hits < 2

    async def _drive(self, call_tool, move, blind=False):
        if move["speed"] < 0 and not blind and not self._clear_behind():
            text = "skipped reverse: wall behind on the map"
        else:
            before, ahead0 = self._pose(), self._ahead()
            ok, text = await call_tool("drive", {**move, "wait": True})
            self.status["moves"] += 1
            if not ok:
                raise RuntimeError(f"drive failed: {text}")
            await asyncio.sleep(0.4)   # let the telemetry poller integrate the last readings
            self._learn(move, before, ahead0, text)
        self.status["last"] = text
        moved = not (text.startswith("refused") or text.startswith("skipped"))
        self.no_motion = 0 if moved and not blind else self.no_motion + 1
        if self.no_motion >= STUCK_MOVES:
            p = self._pose()
            raise Stuck(f"blocked front and back near ({p['x_cm']:.0f}, {p['y_cm']:.0f}) cm: "
                        "move BeamBot clear and press Start again")
        return text

    def _learn(self, move, before, ahead0, text):
        """After a forward move that ran its full time: learn cm/s from the dead-reckoned pose, and use the ultrasonic
        as a stand-in for wheel encoders. If the robot reports driving 15 cm or turning 40° but the thing ahead is
        still at the same distance, the wheels are turning and the car is not moving (caught on a cable, a rug, a
        chair leg). Dead reckoning would keep painting the map from a pose that is wrong, so stop early."""
        if move["speed"] <= 0 or not text.startswith("stopped: drive time"):
            return
        after, ahead1 = self._pose(), self._ahead()
        travelled = math.dist((before["x_cm"], before["y_cm"]), (after["x_cm"], after["y_cm"]))
        turned = abs(wrap_deg(after["heading_deg"] - before["heading_deg"]))
        if abs(move["steer_deg"]) <= 10 and travelled > 3:
            self.cm_per_s = 0.7 * self.cm_per_s + 0.3 * travelled / move["seconds"]
        sensed = ahead0 is not None and ahead1 is not None and ahead0 < 140 and ahead1 < 140
        if sensed and (travelled > 8 or turned > 30):
            self.slips = self.slips + 1 if abs(ahead0 - ahead1) < 3 else 0
            if self.slips >= SLIP_MOVES:
                raise Stuck(f"wheels turned for {travelled:.0f} cm but the distance ahead stayed {ahead1:.0f} cm: "
                            "BeamBot is caught on something. Free it and press Start again")

    async def _scan(self, call_tool):
        """Sweep ~360° with full-length K-turns, and learn how far one K-turn rotates (no encoders, so measure it).
        Each half is forward or reverse depending on what the sensor and the map say is clear."""
        self.status.update(state="scanning")
        self._note("scanning 360°")
        s = self.speed
        rev = {"speed": -s, "seconds": TURN_S, "steer_deg": -30}
        swept, last = 0.0, self._pose()["heading_deg"]
        echoes = 0
        await self._view("scan start")
        viewed_half = False
        for i in range(SCAN_MAX_MOVES):
            want_fwd = i % 2 == 0
            a = self._ahead()
            if want_fwd and a is not None and a < CLOSE_CM + 5:
                want_fwd = False
            elif not want_fwd and not self._clear_behind():
                want_fwd = True
            fwd = {"speed": s, "seconds": forward_secs(TURN_S, a, self.cm_per_s), "steer_deg": 30}
            await self._drive(call_tool, fwd if want_fwd else rev)
            echoes += self._ahead() is not None
            h = self._pose()["heading_deg"]
            turned = abs(wrap_deg(h - last))
            swept += turned
            last = h
            if turned > 5:
                self.kturn_deg = 0.7 * self.kturn_deg + 0.3 * 2 * turned   # a half K-turn turns ~half as far
            if swept >= 180 and not viewed_half:
                viewed_half = True
                await self._view("half-way round")
            if swept >= 360:
                break
        self._note(f"scan done: {swept:.0f}° swept, ~{self.kturn_deg:.0f}° per K-turn")
        if not echoes and not self.status.get("scans_ok"):
            # Nothing within ~3 m in any direction is not an office; it is a dead or unplugged sensor. Without it
            # the explorer would be blind, and so would the robot's own 20 cm cutoff.
            raise RuntimeError("the ultrasonic returned no echo in any direction during the scan: check the sensor")
        self.status["scans_ok"] = True

    async def _view(self, when):
        """Camera + vision model, if the app wired one in. Semantics only (what is here, is the floor ahead safe);
        a vision model is no good at geometry, so it never touches the pose or the occupancy grid's free space."""
        if not getattr(self, "on_view", None):
            return
        try:
            res = await self.on_view(self._pose())
        except Exception as e:  # noqa: BLE001
            self._note(f"camera ({when}): failed, {e.__class__.__name__}: {e}")
            return
        if res:
            self._note(f"camera ({when}): {res}")

    def _explored(self):
        return sum(1 for row in self.get_score() for v in row if v < 0)

    async def _run(self, call_tool):
        st = self.status
        blacklist = []
        deadline = time.time() + self.max_minutes * 60
        try:
            await self._scan(call_tool)
            p = self._pose()
            scanned_at = (p["x_cm"], p["y_cm"])
            goal, stalled, best = None, 0, self._explored()
            while True:
                if st["moves"] >= self.max_moves:
                    st.update(state="done", detail=f"move limit ({self.max_moves})")
                    break
                if time.time() > deadline:
                    st.update(state="done", detail=f"time limit ({self.max_minutes:g} min)")
                    break
                pose = self._pose()
                here = self.geo.cell(pose["x_cm"], pose["y_cm"])
                if math.dist(scanned_at, (pose["x_cm"], pose["y_cm"])) >= SCAN_EVERY_CM:
                    await self._scan(call_tool)
                    st["scans"] += 1
                    p = self._pose()
                    scanned_at = (p["x_cm"], p["y_cm"])
                    continue
                # Progress = newly explored cells. The nearest frontier shifts as the map grows, so judge the
                # explored area, not whether one particular goal was reached.
                explored = self._explored()
                if explored > best + 4:
                    best, stalled = explored, 0
                elif goal and stalled >= GOAL_MOVES:
                    blacklist.append(goal)
                    self._note("no progress toward ({}, {}) cm: skipping it".format(*self.geo.world(*goal)))
                    goal, stalled = None, 0
                score = self.get_score()
                prev = goal
                fr = await asyncio.to_thread(frontiers, score, self.geo)
                goal, path = await asyncio.to_thread(plan, score, self.geo, here, blacklist, prev, fr)
                st.update(frontiers_left=len(set(fr.values())), frontiers=[list(c) for c in list(fr)[:600]],
                          frontier_blacklist=len(blacklist))
                if goal is None:
                    st.update(state="done", detail="no reachable frontier left: map complete", path_cm=[])
                    break
                gx, gy = self.geo.world(*goal)
                away = len(path) * self.geo.cell_cm
                if prev is None or math.dist(prev, goal) * self.geo.cell_cm > 50:
                    self._note(f"heading for frontier at ({gx:.0f}, {gy:.0f}) cm, {away} cm away")
                path_cm = [[round(v) for v in self.geo.world(*c)] for c in path[::3]] + [[round(gx), round(gy)]]
                st.update(state="exploring", goal_cm=[round(gx), round(gy)], goal_dist_cm=away, path_cm=path_cm)
                ahead = self._ahead()
                if ahead is not None and ahead < 20 and self.no_motion >= 2:
                    # The robot refuses to go forward (live sensor) and the map vetoed reversing (old readings,
                    # phantom-prone). Trust the live sensor: back up a few cm blind rather than give up.
                    self._note("front blocked and the map says wall behind: backing up 5 cm blind")
                    await self._drive(call_tool, {"speed": -self.speed, "seconds": 0.3, "steer_deg": 0}, blind=True)
                    continue
                moves = next_move(pose, path, self.geo, self.kturn_deg, ahead, self.speed, self.cm_per_s)
                for move in moves:
                    text = await self._drive(call_tool, move)
                    if text.startswith("refused") or "obstacle" in text:
                        self._note(text)
                        break   # replan with the new reading; next_move backs away first when something is close
                stalled += 1
        except asyncio.CancelledError:
            st.update(state="stopped", path_cm=[])
            await call_tool("stop", {})
            raise
        except Stuck as e:
            st.update(state="stuck", detail=str(e), path_cm=[])
            await call_tool("stop", {})
        except Exception as e:  # noqa: BLE001
            st.update(state="error", detail=f"{e.__class__.__name__}: {e}", path_cm=[])
            await call_tool("stop", {})
        finally:
            st["ended"] = time.time()
            self._note(f"{st['state']}: {st.get('detail', '')}".rstrip(": "))
