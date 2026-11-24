# PiCar-X BeamBot 🚗

Chat with a little robot car, watch what it sees, and let it map the room, while **Teleport** decides which
tools the agent may use and logs every move. The robot is one **MCP server** on a SunFounder **PiCar-X**.
The brain is a web app running in a **Teleport Beam**.

```
 browser ──► Beam app :8080   chat · live video · data panels · map · "Beams tour"
              │ model calls ─► $OPENAI_BASE_URL / $ANTHROPIC_BASE_URL  (beam inference proxy injects credentials)
              ├─ tsh proxy mcp picar-mcp    ─┐
              └─ tsh proxy app picar-stream ─┤   Teleport: RBAC on tool names + audit log
                                             ▼
        PiCar-X (Raspberry Pi) ── Teleport app_service ──► robot_mcp.py, two ports on 127.0.0.1
                                                           :8765  /mcp                                  (picar-mcp)
                                                           :8766  /video.mjpg /snapshot.jpg /telemetry /stop   (picar-stream)
```

Nothing on the robot listens on a public interface, and the beam holds no model keys.

## The robot's tools

| Tool | What it does | In `picar-viewer` | In `picar-driver` |
|---|---|:-:|:-:|
| `snapshot` | JPEG from the camera (the agent can see it) | ✅ | ✅ |
| `get_telemetry` | dead-reckoned pose, ultrasonic distance, grayscale, camera angles | ✅ | ✅ |
| `stop` | emergency stop: stop now and refuse `drive` until resumed | ✅ | ✅ |
| `resume` | clear the emergency stop (not in either role: see [Safety limits](#safety-limits-enforced-on-the-robot-whoever-calls)) | | |
| `look` | pan/tilt the camera | | ✅ |
| `drive` | speed −40..40, 0.1–3 s, steer ±30°. Stops itself near obstacles. | | ✅ |
| `say` | speak a short sentence | | ✅ |

Roles are in [`teleport/roles.yaml`](teleport/roles.yaml). Teleport hides tools a role doesn't allow, so a
viewer-only agent asked to drive explains that it hasn't been granted the tool. That is the demo.

That filter only applies to MCP apps. So the robot serves **two ports**: `:8765` has `/mcp` and is published as the
MCP app `picar-mcp`; `:8766` has the video, telemetry and a `/stop` button and is published as the plain HTTP app
`picar-stream`, with no `/mcp` on it. A viewer who can open `picar-stream` can watch and stop the car, never drive
it. The `picar-stream` role grants that app; add it next to `picar-viewer` or `picar-driver` for the video panel.

## Try it with no robot (2 minutes)

The simulator has the same seven tools, a camera and telemetry as the real car, so everything below works without
hardware. Pick where the fake robot lives:

| | Robot runs | Dashboard runs | Needs | Good for |
|---|---|---|---|---|
| [**A. Your laptop**](#a-your-laptop) | on your laptop | on your laptop | Python | hacking on the code |
| [**B. Inside a beam**](#b-inside-a-beam) | in the beam | in the beam | a beam | seeing it on Beams with nothing else to set up |
| [**C. In Kubernetes**](#c-in-kubernetes) | a pod, published to Teleport | in a beam | a cluster + `helm` | the real thing: robot behind Teleport, always on |

### A. Your laptop

```bash
pip install "mcp>=1.9,<2" uvicorn pillow fastapi httpx anthropic "psycopg[binary]" redis
SIMULATE=1 python3 robot/robot_mcp.py &          # a fake car in a 4 m x 3 m room with two desks: MCP on 8765, video/telemetry on 8766

cd beam-app
ROBOT_MCP_URL=http://127.0.0.1:8765/mcp ROBOT_STREAM_URL=http://127.0.0.1:8766 \
OPENAI_API_KEY=... python3 -m uvicorn app:app --port 8080     # or LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...
```

Open <http://localhost:8080>. Pick **Count objects**, **Map the office** or **Auto map**. Auto map needs no
model at all. Locally you bring your own model key.

### B. Inside a beam

Robot and dashboard both run in one beam. Nothing to enroll: the beam's model credentials are injected for you, so
no key either. The beam needs `beam-user` (that is what lets it call models) and nothing else.

```bash
# laptop
tsh beams add --roles beam-user                 # note the beam name
tar czf /tmp/beambot.tgz -C .. picar-beambot    # run from examples/picar-beambot
tsh beams scp /tmp/beambot.tgz <beam>:/home/beams/beambot.tgz
tsh beams ssh <beam>
```
```bash
# in the beam
tar xzf beambot.tgz 2>/dev/null && cd picar-beambot
python3 -m pip install --user --break-system-packages -q "mcp>=1.9,<2" uvicorn pillow fastapi httpx anthropic
(cd robot && SIMULATE=1 SAY_CMD=true nohup python3 robot_mcp.py >/tmp/robot.log 2>&1 &)
cd beam-app && ROBOT_MCP_URL=http://127.0.0.1:8765/mcp ROBOT_STREAM_URL=http://127.0.0.1:8766 \
  nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 >/tmp/app.log 2>&1 &
```
```bash
# laptop again
tsh beams publish <beam>                        # prints the URL: sign in with Teleport and open it
```

The app reaches the robot over localhost and the model through the beam's `$OPENAI_BASE_URL`. There is no Teleport
MCP in the robot's path here, so the dashboard's "Robot access over MCP" card shows a local server: use C to see
that part for real.

### C. In Kubernetes

The robot becomes a pod in a cluster you own, and a Teleport agent next to it publishes it to your Teleport
cluster. The dashboard then reaches it exactly as it would a real Pi: through Teleport, with your role deciding the
tools and every call in the audit log. The robot stays up when your laptop sleeps.

```bash
cd k8s && ./deploy.sh                      # builds the image, deploys the robot + a Teleport agent (see k8s/README.md
                                           # for remote clusters and CPU architecture)
tsh mcp ls                                 # picar-mcp appears within a minute or two
```

Then put the dashboard in a beam. The beam holds only the roles you list: `beam-user` for model access plus
something that grants the robot.

```bash
cd ../beam-app
BEAM_ROLES=beam-user,mcp-user ROBOT_STREAM_APP= ./deploy-beam.sh        # prints the published URL
```

- `mcp-user` (built in) matches every MCP app, so the beam sees `picar-mcp` with all tools. It doesn't match the
  video app, hence `ROBOT_STREAM_APP=` (no live video panel).
- For video and a tool tier, create the roles once (`tctl create -f ../teleport/roles.yaml`) and use
  `BEAM_ROLES=beam-user,picar-driver,picar-stream` (all tools plus video) or `beam-user,picar-viewer,picar-stream`
  (look only: drive, aim and speak show up locked).
- Check what the beam can reach with `tsh beams exec <beam> tsh mcp ls`.

### Tests

`pytest robot/test_robot_sim.py` (MCP tools against the simulator) and
`cd beam-app && pytest test_automap.py` (planner, spike filter, stuck detection, closed-loop mapping). The whole
repo has a suite that needs no cluster: `make test` from the repository root.

## Real hardware

**Hardware:** PiCar-X + Raspberry Pi, camera, Robot HAT. Two driver paths, same server:

| Path | When | Run |
|---|---|---|
| **SunFounder libs** (default) | Raspberry Pi OS with `robot-hat`, `vilib`, `picar-x` installed per the [PiCar-X docs](https://docs.sunfounder.com/projects/picar-x-v20/en/latest/) | `python3 robot_mcp.py` |
| **`HW=hat`** (no vendor libs) | A minimal OS (we used Arch Linux ARM): `robot_hat_lite.py` talks to the Robot HAT over I²C + gpiod and uses libcamera via GStreamer | `HW=hat python3 robot_mcp.py` |

1. Confirm one of SunFounder's examples drives the car (SunFounder path), or run
   `python3 robot/motor_test.py all` to bench-check motors and servos (HAT path; stop the service first, it owns the HAT).
2. Copy `robot/` to the Pi and `pip3 install -r robot/requirements.txt` (`--break-system-packages` on Bookworm).
   Smoke test: `sudo python3 robot_mcp.py`, then `curl localhost:8766/telemetry`.
3. Install as a service: `robot/picar-robot.service` (SunFounder path) or `robot/picar-robot-hat.service`
   (HAT path; its header lists the packages, groups and camera udev rule it needs). Then
   `sudo systemctl enable --now picar-robot`.
4. Speaker (optional): `espeak-ng` + `alsa-utils`, service user in group `audio`. The Robot HAT V4 speaker is an
   I²S DAC with its amp switch on GPIO 20; V5 needs `SPK_EN=12` in `robot_hat_lite.py`. Override the voice with `SAY_CMD`.
5. Camera pan/tilt servos swapped on your build? Set `CAM_PAN_CH` / `CAM_TILT_CH`.

**Calibrate dead reckoning.** The PiCar-X has no wheel encoders: position is speed × time. Drive 2 s at
speed 30, measure the distance in cm, set `CM_PER_SEC_PER_SPEED = cm / 60`, and set `WHEELBASE_CM`.
Expect drift of tens of centimetres after a few minutes.

### Safety limits (enforced on the robot, whoever calls)

Speed ≤ `MAX_SPEED` (40/100), each move ≤ `MAX_DRIVE_SECONDS` (3 s), forward motion stopped under
`OBSTACLE_STOP_CM` (20 cm). Tune with environment variables in the service file. These are deliberately not tool arguments.

The robot also fails safe when hardware misbehaves:

- Every `drive` stops at its own deadline, and the 20 Hz control loop survives a failed I2C write and keeps
  retrying the stop until it works.
- Forward motion needs a **valid, fresh** ultrasonic reading (`SENSOR_MAX_AGE_S`, 0.5 s). A `-1`/`None` reading (no echo,
  or a dead sensor) counts as "unknown", so forward drives are refused and a forward move in progress stops. Reverse is
  unaffected. The sensor only hears something within about 3 m, so in a large empty space set `ALLOW_BLIND_FORWARD=1`
  if you accept driving without it.
- The motors are zeroed at startup, on SIGTERM and at exit.

**STOP** is a latch on the robot, not just a message from the dashboard. The dashboard's STOP button, the MCP `stop` tool
and the stream app's `/stop` all stop the motors and make the robot refuse `drive` until it is resumed. That covers a
drive request that was already on its way when you pressed STOP, and any other client: Claude Code running in another
beam is refused too. STOP also cancels the dashboard's running chat job and auto-map and halts the dashboard (423 on
every robot call except stop) until you press **Resume**. If the stop could not reach the robot the page says so, and if
Resume can't reach it the robot stays stopped.

Clearing the latch is deliberate. The `resume` tool is in neither `picar-viewer` nor `picar-driver`, so an agent can't undo
a stop. Resume works through the stream app (`picar-stream`) or a role that grants `resume` (the built-in `mcp-user`
does). Give `picar-stream` to the dashboard's beam and not to beams that run Claude Code. Everyday stops don't latch: letting go
of the drive pad, or ending or cancelling an auto-map run, uses a plain speed-0 stop.

## Put it on Teleport

You need a Teleport cluster with Beams, `tsh`/`tctl` on your laptop, and `jq`.

```bash
tsh login --proxy=<your-tenant>.beams.run:443

# 1. Roles
tctl create -f teleport/roles.yaml

# 2. Enroll the robot (on the Pi)
tctl tokens add --type=app --format=text            # put the token in /tmp/token on the Pi
curl "https://<your-tenant>.beams.run:443/scripts/install.sh" | sudo bash     # or your cluster's install script
sudo cp robot/teleport.yaml /etc/teleport.yaml      # set proxy_server, then:
sudo systemctl enable --now teleport

# 3. Check
tsh mcp ls            # picar-mcp
tsh apps ls           # picar-stream
```

**Deploy the app into a beam.** A beam already holds your delegated Teleport identity, so `start.sh` simply
opens its tunnels with the beam's own `tsh`. Nothing to enroll:

```bash
cd beam-app && BEAM_ROLES=beam-user,picar-driver,picar-stream,beambot-data ./deploy-beam.sh           # new beam → upload → start in tmux → wait for :8080 → tsh beams publish
BEAM=<name> ./deploy-beam.sh               # redeploy into an existing beam
```

Robot calls appear in the audit log as you plus the beam's workload identity. Beams expire after 12 hours by default, so the URL changes.
`BEAM_ROLES` is required: a beam holds only the roles you list (they must be delegable), never your admin ones. `beam-user` is what lets the dashboard (and Claude Code) call models inside the beam: leave it out and every chat fails with a Teleport login redirect. `beambot-data` (in `teleport/roles.yaml`) is what lets it reach Postgres and Redis; drop it for SQLite only. Want the viewer experience? `BEAM_ROLES=beam-user,picar-viewer,picar-stream,beambot-data ./deploy-beam.sh`: the drive tools show as locked. Check what a beam holds with `tsh beams exec <beam> tsh status`.

**Who can open the published URL?** It is a Teleport-protected app, so a visitor signs in to Teleport first. The Teleport
docs don't say which users are let in, so check that before you share the link. The dashboard has no login of its own and
acts with the *beam's roles*, not the visitor's: whoever gets in can do whatever the beam can. A `picar-driver` beam lets
every visitor drive. For a shared link deploy with `picar-viewer` (and `picar-stream`); keep driver beams unpublished and short-lived.

No Pi handy, or want it available while your laptop sleeps? [`k8s/`](k8s/README.md) runs the simulator in a Kubernetes cluster with its own Teleport agent.

You can also use the robot straight from Claude Desktop: `tsh mcp config picar-mcp --client-config=claude`.

## Storage: databases optional

`start.sh` looks for a Teleport database called `beambot-postgres` that your identity can see. If it's there,
counts and saved maps go to Postgres through Teleport Database Access (no password: Teleport's certificate
logs you in). If not, or if the tunnel is down, the app falls back to **SQLite** inside the beam and the
Databases card says so.

| Variable | Default | Meaning |
|---|---|---|
| `USE_DBS` | `auto` | `auto` use Postgres if visible, else SQLite · `1` require it · `0` SQLite only |
| `PG_TELEPORT_NAME` / `PG_DB_NAME` / `DB_USER` | `beambot-postgres` / `beambot` / `app` | which Teleport database and login |
| `USE_REDIS` | off | also tunnel Redis (`REDIS_TELEPORT_NAME`, default `beambot-redis`) for a shared activity feed |
| `REDIS_PASSWORD` | unset | only for a Redis still using password ACLs |

The bot (or user) needs a Teleport role granting access to those databases. The app creates its tables on first run.

## Other settings

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` (Responses API via `$OPENAI_BASE_URL`) or `anthropic` (via `$ANTHROPIC_BASE_URL`) |
| `LLM_MODEL` | `gpt-6.1-sol` / `claude-sonnet-5-5` | model for the chat agent, per provider. `CLAUDE_MODEL` also works for Anthropic |
| `OPENAI_REASONING` | `low` | reasoning effort; a robot loop wants quick turns |
| `ROBOT_MCP_NAME` | `picar-mcp` | Teleport MCP app to connect to |
| `ROBOT_STREAM_APP` | `picar-stream` | Teleport app for video/telemetry (empty = skip) |
| `ROBOT_TOOLS` | the six driving tools (`resume` is shown only if your roles grant it) | which tools the UI shows as granted vs. locked |
| `BEAM_EXPIRES` | unset | shown in the UI; copy it from `tsh beams ls` |
| `MAX_SPEED`, `MAX_DRIVE_SECONDS`, `OBSTACLE_STOP_CM` | 40, 3, 20 | robot-side safety limits |
| `SENSOR_MAX_AGE_S`, `ALLOW_BLIND_FORWARD` | 0.5, off | robot-side: how old an ultrasonic reading may be; `1` allows forward motion without one |
| `ROBOT_PORT`, `STREAM_PORT` | 8765, 8766 | robot-side: the MCP port and the video/telemetry/stop port (no `/mcp` there) |

## Tasks, Auto map and the Beams tour

The Data panel follows the selected task. Add your own by adding one entry to `TASKS` in `beam-app/app.py`
(a `prompt`, suggested `chips`, and a `data` list of `stat` / `totals` / `records` widgets).

- **Count objects**: tally bars, areas counted, confidence.
- **Map the office**: explored area, obstacles, heading, path length.
- **Auto map**: no LLM driving. `automap.py` does a 360° scan, plans to the nearest frontier with Dijkstra, moves
  in short `drive` calls through Teleport MCP, and rescans every 1.2 m. It stops when no frontier is reachable,
  on its limits, when stuck, or when you take over with the drive pad or STOP. It ends forward moves ~30 cm from
  walls and skips reversing when the map shows a wall behind. With *Camera landmarks + hazards* on, each scan asks
  the model for landmarks and hazards the ultrasonic can't see. The model never touches the pose.
- **Save map** / **View** / **PNG** store and reload maps (positions are relative to that run's start).
- **Manual driving**: drive pad (WASD/arrows, space = stop) and camera pad (IJKL, C = centre). These call the same
  MCP tools as the agent, so the same RBAC and audit log apply.
- **Tour Beams features** walks through isolated runtime, identity-not-secrets, inference proxy, MCP access,
  databases and the published app, with live evidence for each.

## Known limits

- The deploy and `start.sh` paths need a live Teleport cluster and have been exercised against one, but the
  auto-detect of `beambot-postgres` (`tsh db ls`) has not been run against a live Postgres. Check the Databases
  card on first run.
- Counting is from camera frames; overlapping views can double count. Counts are stored per location so you can review them.
- The dashboard loads Lato from Google Fonts and Phosphor icons from unpkg. Offline, text falls back to system
  fonts and the icons are blank.
- `mcp>=1.9,<2` only. The 2.x SDK renamed the APIs used here.
