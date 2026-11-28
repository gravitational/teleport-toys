# BeamBot 🤖🔌

**Give an AI agent real hands, and keep the leash in your own pocket.**

A [Teleport Beam](https://goteleport.com/docs/beams/) is a short-lived, isolated microVM where an agent like Claude Code can run with no API keys and no network path to anything. This pack shows how to hand that agent *just enough* reach into the physical world: a robot car, a robot arm, anything that speaks [MCP](https://modelcontextprotocol.io). Teleport decides which tools the agent can see, and every call lands in the audit log.

```
 ┌───────────── Beam (microVM) ─────────────┐
 │  claude / your app  ── tsh mcp connect ──┼──┐   no secrets in the beam
 └───────────────────────────────────────────┘  │   model calls go through the beam's inference proxy
                                                ▼
                                    Teleport (RBAC on tool names + audit log)
                                                │  outbound reverse tunnel from the robot
                                                ▼
                             robot  ──  MCP server bound to 127.0.0.1
```

## About BeamBot

BeamBot lets an AI agent operate a real robot car. The car is a battery-powered SunFounder PiCar-X built around a Raspberry Pi. An agent can plan a mission, drive the motors, read the distance sensor, and capture camera frames, but only through the tools an operator has approved.

The agent runs in a [Teleport Beam](https://goteleport.com/docs/beams/), a short-lived, isolated runtime for AI agents with no secrets inside. The Beam calls the car's MCP server through Teleport, which checks each tool call against the operator's access rules and records it in an audit log. The car connects outward through a reverse tunnel, so it needs no inbound ports, no static IP, and no VPN.

The pattern isn't specific to the PiCar-X. Any robot, drone, or edge device that can run an MCP server and an outbound Teleport agent can be added the same way. You expose its capabilities as MCP tools, and the agent and access rules stay the same. The PiCar-X's tools are `drive`, `look`, `say`, `snapshot`, `get_telemetry`, `stop` and `resume`, and its roles list them by name (`examples/picar-beambot/teleport/roles.yaml`). The five-Pi simulation instead puts the permission tier in the name (`sense_*` observe, `act_*` move, `admin_*` reset, `safety_*` everyone), which keeps role files short when a robot has many tools. Either works: pick one and list every tool in a role. To adapt BeamBot to your hardware, replace the car's MCP server with one that wraps your device's functions, then update the tool list and access roles.

See the [architecture diagram](docs/beambot-architecture.pdf) (PDF) for how a command flows from the operator to the car.

## What's in the box

| Thing | What it does | Start here |
|---|---|---|
| **`beam-mcp-setup/`** | **The one-click part.** One command spins up a beam and wires Claude Code inside it to your Teleport MCP servers, with allow/deny rules on top of RBAC. A beam already carries your delegated Teleport identity, so there is nothing to install or enroll. | [`beam-mcp-setup/SKILL.md`](beam-mcp-setup/SKILL.md) |
| **`examples/picar-beambot/`** | A real **SunFounder PiCar-X** driven by one MCP server, plus a web dashboard (chat, live video, auto-mapping) that runs in a beam. Also runs as a **simulator**, no robot needed. | [README](examples/picar-beambot/README.md) |
| **`examples/advanced-simulation/`** | A **five-Raspberry-Pi** simulated robot (safety, vision, drive, arm, sensors), each Pi its own MCP server with its own RBAC tier. The e-stop works over the LAN with no cloud. | [README](examples/advanced-simulation/README.md) |

## One click (really)

From your laptop, logged in with `tsh login --proxy=<your-tenant>.beams.run:443`. This example uses the roles from the PiCar
example (an admin creates them once with `tctl create -f`):

```bash
tctl create -f examples/picar-beambot/teleport/roles.yaml
bash beam-mcp-setup/scripts/beam_mcp_up.sh \
  --roles beam-user,picar-driver --labels project=picar-beambot \
  --allow 'snapshot,get_telemetry,stop' --deny 'drive,look,say'
```

It will:

1. create a beam limited to the roles you name (`--roles`; they must be eligible for delegation) and wait until it is reachable,
2. write `mcp.json`, `.mcp.json` and `.claude/settings.json` (allow/deny rules) inside it,
3. run a real MCP `initialize` handshake to prove it works.

Then `tsh beams ssh <beam>` and run `claude`. To give the agent hands later, create a new beam with more roles
(`--roles beam-user,picar-driver`). `beam-user` is what lets the beam call models: without it Claude Code inside the beam can't think. A running agent can't grant itself anything. Verify any beam with
`beam-mcp-setup/scripts/verify_in_beam.sh`.

## Gotchas we paid for so you don't have to

| You see | It means | Do this |
|---|---|---|
| `tsh: error: unknown short flag '-x'` from `tsh beams exec <id> tar -xzf …` | `tsh` parses flags after the command. | Pass the remote command as one quoted string. |
| `Wildcard tool name "act_*" is not supported in allow rules` | Claude Code only allows globs after a literal `mcp__<server>__` prefix. | Expand per server. Deny rules may use wildcards anywhere. |
| `claude -p --allowedTools … "prompt"` says no prompt was given | `--allowedTools` is variadic and swallowed the prompt. | Pipe the prompt on stdin. |
| Model calls from a beam return HTML or a `302` to `/web/launch/...` | The beam's roles don't include `beam-user`, so it can't see the model apps. | Create the beam with `--roles beam-user,<others>`; `tsh apps ls` in the beam should list `openai` and `anthropic`. |
| `ERROR: You do not have permission to delegate the following roles: [...]` from `tsh beams add --roles` | You can only hand a beam roles your own user holds (also shown for blank entries like `" "` or `a,,b`). | Add the role to your user (`tctl users update <you> --set-roles <current roles>,<new role>`), log in again, retry. |
| A beam holds far more roles than you asked for | `--roles=""` means "no `--roles`": the beam inherits ALL your roles. An unset shell variable does it silently. | Never pass an empty list. The scripts here refuse a blank one. |
| `tsh mcp ls` is empty inside the beam | The beam only holds the roles you gave it. | Include one that grants the MCP app: the built-in `mcp-user` matches every MCP app, but it grants every tool, including the robot's `resume` (it can lift an emergency stop). Prefer a role from the example's `roles.yaml`. |
| `No module named 'mcp.server.fastmcp'` | `pip install mcp` pulled the 2.x SDK, which renamed `FastMCP`. | Install `'mcp>=1.9,<2'`. |
| `python3 -m venv` fails, or `pip` says "externally managed" | Beam images have no python3-venv and mark system Python as managed. | `python3 -m pip install --user --break-system-packages <pkg>`. |
| `tsh proxy ...` exits right away inside a beam | The beam's identity lacks access to that app or database. | Create the beam with roles that grant it (`--roles`), and read `/tmp/proxy-*.log`. |
| Redis through Teleport fails right after connecting | Newer `redis-py` opens with RESP3 `HELLO 3`, which Teleport's Redis proxy rejects. | Connect with `protocol=2`. |

More in [`beam-mcp-setup/SKILL.md`](beam-mcp-setup/SKILL.md).

## Numbers

Measured laptop → Teleport → Pi with the MCP Python client against the five-Pi simulation: **~2.5 s** to open a session, **~210 ms median** per tool call (max ~490 ms for a camera frame). Your network will differ.

## Requirements

- A Teleport cluster with Beams (Teleport and `tsh` **18.11.9+** for production Beams, 18.3+ for MCP access). Keep Pi agents at or below the cluster's version.
- `tsh` on your laptop (`tctl` only for an admin creating the roles); `jq` for the PiCar deploy script.
- Python 3.10+ and `pip install "mcp>=1.9,<2"`. The code uses the 1.x SDK API; 2.x renamed it.
- Hardware examples: a PiCar-X, or five Raspberry Pi 3 (see each example). Neither is needed for the simulators.

## Tested on a real cluster

Run on a production Beams cluster (Teleport 18.x): beam identity shows the `user → beam` delegation chain with no
bot or tbot setup; Postgres (`SELECT 1`) and Redis (`PING`, `protocol=2`) work through `tsh proxy db --tunnel`;
the dashboard connects to the robot over MCP, runs the agent loop and writes its activity feed to Redis through
Teleport. `tsh mcp connect` and `tsh proxy mcp` work from a least-privilege beam against an MCP app served by a
Teleport agent in Kubernetes, and the dashboard drives that robot through the tunnel. Claude Code inside the beam
calls models once the beam has the `beam-user` role. The beam's creator could open its published app and other users
could not; what would let another user in is untested (see `examples/picar-beambot/README.md`, "Who can open the
published URL?").
`tests/live/check_mcp_bypass.sh` checks that opening the robot's MCP app as plain HTTP with `tsh proxy app` doesn't
skip the tool filter: a `picar-driver` beam must not be able to list or call `resume`. It reports `INCONCLUSIVE`,
not a pass, when it can't see Teleport's denial, and it has not been run against a cluster with that role yet.

## Tests

```bash
make setup && make test        # about 8 min on our test machine, no cluster needed
make test-live                 # inside a real beam: proves MCP, tunnels and databases against real Teleport
```

`make test` runs the scripts and the dashboard for real, with a scripted LLM, a fake `tsh`, the simulated robot,
an embedded Postgres and a fake Redis. It shows our code is right; `make test-live` ([tests/live](tests/live/README.md))
shows your cluster is.

## Layout

```
beam-mcp-setup/            the skill: SKILL.md, scripts/, references/
examples/
  picar-beambot/           robot/ (MCP server, Teleport + systemd config), beam-app/ (dashboard), teleport/ (roles),
                           k8s/ (the simulated robot + Teleport agent for a Kubernetes cluster)
  advanced-simulation/     pi/ (five-role MCP server + installer), teleport/ (roles), beam/ (agent rules), tests/
docs/                      beambot-architecture.pdf: how a command flows from operator to robot
AGENTS.md                  how to work in this repo (CLAUDE.md points at it)
Makefile                   `make test`
```

## Hack on it

```bash
make setup   # creates .venv with the test dependencies
make test    # automap, PiCar simulator, and five-node simulation tests
```

Contributions welcome. Read [`AGENTS.md`](AGENTS.md) first: it has the safety rules, whether you are a human or an agent.

## License

MIT, see [LICENSE](LICENSE). "Teleport" is a trademark of Gravitational, Inc.; this is a community toy that works with Teleport.
