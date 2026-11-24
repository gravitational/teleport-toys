"""Auto-map tests: planner units + a closed loop in a simulated 4 m x 3 m room.

  python3 -m pytest test_automap.py
"""
import asyncio
import math

import pytest

import automap
from app import CELL_CM, GRID, OfficeMap
from automap import SCAN_MAX_MOVES, AutoMapper, Geometry, forward_secs, frontiers, next_move, plan

GEO = Geometry(GRID, CELL_CM)


def empty():
    return [[0] * GRID for _ in range(GRID)]


def test_geometry_round_trip():
    for x, y in [(0, 0), (100, -50), (-35, 240)]:
        assert GEO.world(*GEO.cell(x, y)) == (x, y)
    assert GEO.cell(0, 10)[1] < GEO.cell(0, 0)[1]   # +y is up (smaller row)


def test_plan_finds_frontier_and_avoids_wall():
    s = empty()
    o = GEO.origin
    for cy in range(o - 20, o + 21):            # known free square around the robot
        for cx in range(o - 20, o + 21):
            s[cy][cx] = -3
    for cy in range(o - 20, o + 21):            # wall on the east side
        s[cy][o + 10] = 5
    start = (o, o)
    goal, path = plan(s, GEO, start)
    assert goal is not None and path[0] == start and path[-1] == goal
    assert all(s[cy][cx] <= 1 for cx, cy in path)
    assert goal[0] < o + 10                      # frontier on the open (west/north/south) side


def test_plan_none_when_enclosed():
    s = empty()
    o = GEO.origin
    for cy in range(o - 15, o + 16):
        for cx in range(o - 15, o + 16):
            edge = max(abs(cx - o), abs(cy - o)) == 15
            s[cy][cx] = 5 if edge else -3
    assert not frontiers(s, GEO)
    assert plan(s, GEO, (o, o)) == (None, [])


def test_next_move_steers_or_kturns():
    o = GEO.origin
    ahead = [(o + i, o) for i in range(20)]                  # path along +x
    left = [(o, o - i) for i in range(20)]                   # path along +y (row decreases)
    m = next_move({"x_cm": 0, "y_cm": 0, "heading_deg": 0}, ahead, GEO)
    assert len(m) == 1 and m[0]["speed"] > 0 and m[0]["steer_deg"] == 0
    k = next_move({"x_cm": 0, "y_cm": 0, "heading_deg": 0}, left, GEO)
    assert len(k) == 2 and k[0]["steer_deg"] > 0 and k[1]["speed"] < 0 and k[1]["steer_deg"] < 0


# ---- closed loop: kinematics + ultrasonic copied from robot_mcp.SimCar ----
WALLS = [((-50, -150), (350, -150)), ((350, -150), (350, 150)), ((350, 150), (-50, 150)), ((-50, 150), (-50, -150)),
         ((120, -60), (180, -60)), ((180, -60), (180, 0)), ((180, 0), (120, 0)), ((120, 0), (120, -60)),
         ((230, 60), (290, 60)), ((290, 60), (290, 110)), ((290, 110), (230, 110)), ((230, 110), (230, 60))]
CM_PER_SEC_PER_SPEED, WHEELBASE_CM, OBSTACLE_STOP_CM = 0.6, 9.5, 20


def crosses(a, b, wall):
    (x1, y1), (x2, y2) = wall
    def side(px, py):
        return (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    def side2(px, py):
        return (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
    return side(*a) * side(*b) < 0 and side2(x1, y1) * side2(x2, y2) < 0


class SimRobot:
    def __init__(self, office_map):
        self.x = self.y = self.th = 0.0
        self.map = office_map
        self.calls = 0
        self.trail = []
        self.bumps = 0
        self.sense()

    def distance(self):
        """Nearest echo in a ±10° cone, like a real HC-SR04 (robot_mcp.SimCar casts one ray)."""
        return min(self.ray(self.th + math.radians(a)) for a in (-10, -5, 0, 5, 10))

    def ray(self, th):
        best, dx, dy = 300.0, math.cos(th), math.sin(th)
        for (x1, y1), (x2, y2) in WALLS:
            ex, ey = x2 - x1, y2 - y1
            den = dx * ey - dy * ex
            if abs(den) < 1e-9:
                continue
            t = ((x1 - self.x) * ey - (y1 - self.y) * ex) / den
            u = ((x1 - self.x) * dy - (y1 - self.y) * dx) / den
            if t > 0 and 0 <= u <= 1:
                best = min(best, t)
        return round(best, 1)

    def sense(self):
        self.map.integrate({"pose": {"x_cm": self.x, "y_cm": self.y, "heading_deg": math.degrees(self.th) % 360},
                            "ultrasonic_cm": self.distance(), "path": []})

    async def call_tool(self, tool, args):
        self.calls += 1
        if tool != "drive":
            return True, "stopped"
        speed, steer, dt = args["speed"], args["steer_deg"], 0.05
        if speed > 0 and self.distance() < OBSTACLE_STOP_CM:
            return True, f"refused: obstacle at {self.distance()} cm"
        for _ in range(int(args["seconds"] / dt)):
            if speed > 0 and self.distance() < OBSTACLE_STOP_CM:
                return True, f"stopped: obstacle at {self.distance()} cm"
            v = speed * CM_PER_SEC_PER_SPEED
            th = self.th + v / WHEELBASE_CM * math.tan(math.radians(steer)) * dt
            x, y = self.x + v * math.cos(th) * dt, self.y + v * math.sin(th) * dt
            if any(crosses((self.x, self.y), (x, y), w) for w in WALLS):
                self.bumps += 1                          # hit something (reversing blind): stay put
                return True, "stopped: drive time elapsed"
            self.th, self.x, self.y = th, x, y
            self.trail.append((self.x, self.y))
            self.sense()
        return True, "stopped: drive time elapsed"


@pytest.fixture
def fast(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(automap.asyncio, "sleep", lambda s: real_sleep(0))


def test_closed_loop_maps_sim_room(fast):
    m = OfficeMap()
    bot = SimRobot(m)
    mapper = AutoMapper(lambda: m.score, lambda: m.telemetry, GEO)

    async def go():
        mapper.start(bot.call_tool)
        await mapper.task

    asyncio.run(go())
    st, summ = mapper.status, m.summary()
    print(st, summ)
    assert st["state"] == "done", st
    assert summ["explored_m2"] > 6, summ                  # room is 12 m², minus desks / shadows
    assert all(-50 < x < 350 and -150 < y < 150 for x, y in bot.trail)   # never left the room
    assert bot.bumps <= 2, bot.bumps


def test_plan_keeps_current_goal():
    s = empty()
    o = GEO.origin
    for cy in range(o - 14, o + 15):            # free corridor along y, unknown at both ends: two equal frontiers
        for cx in range(o - 2, o + 3):
            s[cy][cx] = -3
    first, _ = plan(s, GEO, (o, o))
    other = (first[0], 2 * o - first[1])        # mirror image across the robot
    again, _ = plan(s, GEO, (o, o), prefer=other)
    assert math.dist(again, other) <= 2


def test_office_map_rejects_sonar_spikes():
    m = OfficeMap()
    pose = {"x_cm": 0, "y_cm": 0, "heading_deg": 0}
    for d in (100, 100, 100, 300, 100, 30, 100, 100):    # steady wall at 1 m, one far spike, one near spike
        m.integrate({"pose": pose, "ultrasonic_cm": d, "path": []})
    cell_at = lambda cm: m.score[GEO.origin][GEO.origin + round(cm / CELL_CM)]   # noqa: E731
    assert cell_at(100) > 1                    # the wall is there
    assert cell_at(30) <= 0                    # the near spike painted no obstacle
    assert all(m.score[GEO.origin][GEO.origin + c] == 0 for c in range(22, 29))   # nothing cleared past the wall


def test_office_map_dump_load_round_trip():
    m = OfficeMap()
    m.integrate({"pose": {"x_cm": 0, "y_cm": 0, "heading_deg": 90}, "ultrasonic_cm": 80, "path": []})
    m.integrate({"pose": {"x_cm": 0, "y_cm": 0, "heading_deg": 90}, "ultrasonic_cm": 80, "path": []})
    m.add_landmark("desk", 10, 90)
    m2 = OfficeMap()
    m2.load(m.dump(), m.landmarks, "saved")
    assert m2.score == m.score and m2.landmarks[0]["label"] == "desk" and m2.loaded == "saved"


def test_forward_secs_stops_short_of_obstacle():
    assert forward_secs(1.0, None, 18) == 1.0
    assert forward_secs(1.0, 200, 18) == 1.0
    assert forward_secs(1.0, 48, 18) == 1.0
    assert forward_secs(1.0, 39, 18) == 0.5
    assert forward_secs(1.0, 31, 18) == 0.2


class FrozenRobot(SimRobot):
    """Reports dead-reckoned motion like the real robot would, but never actually moves: wheels spinning on a cable."""

    def __init__(self, office_map):
        self.fx, self.fy, self.fth = 0.0, 0.0, 0.0
        super().__init__(office_map)

    def distance(self):
        x, y, th = self.x, self.y, self.th
        self.x, self.y, self.th = self.fx, self.fy, self.fth
        try:
            return super().distance()
        finally:
            self.x, self.y, self.th = x, y, th


def test_frozen_robot_is_detected_as_stuck(fast):
    m = OfficeMap()
    bot = FrozenRobot(m)
    bot.x = bot.fx = 250.0            # 100 cm from the east wall, facing it
    bot.sense()
    mapper = AutoMapper(lambda: m.score, lambda: m.telemetry, GEO)

    async def go():
        mapper.start(bot.call_tool)
        await mapper.task

    asyncio.run(go())
    assert mapper.status["state"] == "stuck", mapper.status
    assert "caught" in mapper.status["detail"]
    assert mapper.status["moves"] <= 6


class DeafRobot(SimRobot):
    """Ultrasonic never gets an echo (unplugged sensor): the robot reports -1 like robot_mcp.py does."""

    def distance(self):
        return -1.0


def test_dead_ultrasonic_aborts_the_run(fast):
    m = OfficeMap()
    bot = DeafRobot(m)
    mapper = AutoMapper(lambda: m.score, lambda: m.telemetry, GEO)

    async def go():
        mapper.start(bot.call_tool)
        await mapper.task

    asyncio.run(go())
    assert mapper.status["state"] == "error" and "no echo" in mapper.status["detail"], mapper.status
    assert mapper.status["moves"] <= SCAN_MAX_MOVES


def test_vision_hook_is_called_and_failures_are_logged(fast):
    m = OfficeMap()
    bot = SimRobot(m)
    mapper = AutoMapper(lambda: m.score, lambda: m.telemetry, GEO)
    seen, notes = [], []
    orig_note = mapper._note
    mapper._note = lambda msg: (notes.append(msg), orig_note(msg))   # the status log keeps only the last 20

    async def on_view(pose):
        seen.append(pose)
        if len(seen) == 1:
            raise RuntimeError("model down")
        return "desk left 1.5 m"

    async def go():
        mapper.start(bot.call_tool, on_view=on_view)
        await mapper.task

    asyncio.run(go())
    assert len(seen) >= 2 and all("heading_deg" in p for p in seen)
    assert any("model down" in x for x in notes) and any("desk left" in x for x in notes)


# ---- benchmark: python3 test_automap.py  (8 start poses; coverage, moves, refused moves, collisions) ----
POSES = [(0, 0, 0), (0, 0, 90), (0, 0, 200), (250, -100, 45),
         (60, 100, 270), (300, 0, 180), (30, -100, 0), (200, 120, 180)]


def bench(poses=POSES, quiet=False):
    real_sleep = asyncio.sleep
    automap.asyncio.sleep = lambda s: real_sleep(0)
    rows = []
    for x, y, th in poses:
        m = OfficeMap()
        bot = SimRobot(m)
        bot.x, bot.y, bot.th = x, y, math.radians(th)
        bot.sense()
        mapper = AutoMapper(lambda: m.score, lambda: m.telemetry, GEO)
        texts = []
        orig = bot.call_tool

        async def spy(tool, args):
            r = await orig(tool, args)
            texts.append(r[1])
            return r

        async def go():
            mapper.start(spy)
            await mapper.task

        asyncio.run(go())
        st = mapper.status
        row = {"pose": (x, y, th), "state": st["state"], "detail": st.get("detail", "")[:26], "moves": st["moves"],
               "refused": sum(t.startswith("refused") for t in texts), "m2": m.summary()["explored_m2"],
               "bumps": bot.bumps}
        rows.append(row)
        if not quiet:
            print("{pose!s:16} {state:8} {detail:26} moves={moves:<4} refused={refused:<4} m2={m2:<6} "
                  "bumps={bumps}".format(**row))
    automap.asyncio.sleep = real_sleep
    if not quiet:
        n = len(rows)
        print(f"mean m2={sum(r['m2'] for r in rows) / n:.2f}  mean moves={sum(r['moves'] for r in rows) / n:.0f}"
              f"  refused={sum(r['refused'] for r in rows)}  bumps={sum(r['bumps'] for r in rows)}")
    return rows


if __name__ == "__main__":
    bench()
