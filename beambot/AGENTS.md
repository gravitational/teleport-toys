# AGENTS.md

Guidance for AI coding agents (and humans) working in **BeamBot**: a pack of scripts and
examples that let an agent running in a Teleport Beam use Teleport-protected MCP servers, including ones
that move real hardware. `CLAUDE.md` just points here.

## Layout

```
beam-mcp-setup/        skill + scripts: beam, mcp.json, Claude Code permission rules
examples/picar-beambot/        PiCar-X robot server (robot/), dashboard app (beam-app/), roles (teleport/)
examples/advanced-simulation/  five-Pi simulated robot (pi/), roles (teleport/), agent rules (beam/), tests/
```

## Commands

```bash
make setup     # .venv with mcp<2, uvicorn, pillow, fastapi, httpx, anthropic, pytest
make test      # automap + PiCar simulator + five-node simulation (SDK and stdlib servers)
make lint      # bash -n over every shell script (run shellcheck too if you have it)
```

Run the PiCar server with no hardware: `SIMULATE=1 python3 examples/picar-beambot/robot/robot_mcp.py`.
All tests run without a Teleport cluster, a Pi or an LLM key. Keep it that way.

## Rules that matter

**Safety first (this code can move things in a room with people).**
- Safety limits live **on the robot**, not in prompts or the app: `MAX_SPEED`, `MAX_DRIVE_SECONDS`,
  `OBSTACLE_STOP_CM` (PiCar), `MAX_MOTION_MS`, joint limits and the e-stop mesh (simulation).
  Never raise a limit, remove a check, or let a client override one without the maintainer saying so.
- A stop tool must always be callable. `stop` / `safety_stop` stay in every role, including read-only ones.
- Don't add a tool that moves hardware without a time box and a bounded magnitude.

**Teleport RBAC is the boundary.**
- Tool names carry the permission tier. Simulation: `sense_*` observe, `act_*` move, `admin_*` reset,
  `safety_*` everyone. PiCar tools are listed per role in `examples/picar-beambot/teleport/roles.yaml`.
  When you add a tool, update the role files and the README tool table in the same change.
- No `deny` rules in roles meant to be combined: Teleport deny is greedy across the whole identity and
  would block a later grant.
- Claude Code allow rules take a glob only after a literal `mcp__<server>__` prefix. Deny rules take
  wildcards anywhere. `--allowedTools` is variadic: pipe prompts on stdin.
- Production beams carry the creator's delegated identity: `tsh mcp connect` / `tsh proxy` just work inside one.
  Never reintroduce Machine ID bots, `tbot` or identity files (`tests/test_no_tbot.py` fails if you do). To change
  what an agent can do, create a beam with different `--roles`. Always give new beams an explicit `--roles` list:
  the scripts refuse to create one that would inherit every role of the person running them.

**No secrets, no site-specific values.**
- Placeholders only: `<your-tenant>.beams.run`, never a real tenant, hostname, IP or token. LAN addresses in
  examples use the documentation range (`192.0.2.0/24`).
- Model credentials are injected by the beam's inference proxy. Never add API keys to code, env files or docs.
- Join tokens are single-use and short-lived. Never commit one, never put one on a command line you log.

**Code style**
- Shell: `set -euo pipefail`, quote everything, keep bash 3.2 (macOS) compatible on laptop-side scripts,
  `bash -n` clean, shellcheck clean where practical. Exit non-zero with a one-line reason.
- Python: the MCP SDK is pinned `mcp>=1.9,<2` (2.x renamed the APIs used here). The five-Pi simulation pins
  `mcp==1.19.0` on-device because that is the newest release with prebuilt armv7 wheels, and it ships a
  zero-dependency stdlib fallback server (`MCP_IMPL=stdlib`). Keep both working.
- Simulators must keep working: `SIMULATE=1` (PiCar) and `ROBOT_SIM=1` (five-Pi) are how everyone tests.
- Storage in the PiCar app: Postgres through Teleport if reachable, otherwise SQLite, optional Redis.
  A missing database must degrade, never crash.
- Default model for the dashboard is `gpt-6.1-sol` (`LLM_PROVIDER=openai`) or `claude-sonnet-5-5`
  (`LLM_PROVIDER=anthropic`); `LLM_MODEL` overrides either.

## Adding things

- **A tool**: implement it on the robot server, add it to the right role(s) and the README table, add a case
  to the simulator test. Return bounded, time-boxed actions.
- **An example**: new directory under `examples/`, its own README with a *simulate it first* path, its own
  roles file named `<example>-<tier>`, labels `project: <example>` (or `robot: <example>`), and tests that
  run without hardware. Add a row to the root README.
- **A gotcha**: if you lost an hour to a Teleport error, add the exact message and the fix to the root
  README table and `beam-mcp-setup/SKILL.md`.

## If you are the agent running *inside* a beam

Read `examples/advanced-simulation/beam/CLAUDE.beam.md` (copy it to the beam as `CLAUDE.md`). Short version:
look before you move, small steps, stop on doubt, and if you lack a tool, say what you need and why instead
of working around Teleport.

## Verify before you finish

Run `make test` and `make lint`. For anything touching scripts that talk to Teleport (`beam_mcp_*.sh`,
`deploy*.sh`, `start.sh`, `setup_pi.sh`), say plainly in your summary that they cannot be exercised without
a cluster, and what you checked instead.
