# Live tests: prove it on a real beam

The hermetic suite (`make test`) uses a fake `tsh`, so it proves our scripts and app behave. These tests prove
**Teleport** behaves: production beams really can run MCP, tunnels and databases with their delegated identity.

Run them **inside a beam** (or anywhere `tsh status` shows an active session):

```bash
tsh beams add --roles picar-driver        # roles must be eligible for delegation
tsh beams ssh <beam>
git clone <this repo> && cd beams-one-click
python3 -m pip install --user --break-system-packages -r tests/requirements.txt   # beams have no python3-venv
PY=python3 make test-live
```

| Env | Default | Meaning |
|---|---|---|
| `LIVE_MCP_APP` | `picar-mcp` | Teleport MCP app to call |
| `LIVE_STREAM_APP` | `picar-stream` | Teleport app for video/telemetry (empty skips) |
| `LIVE_DB` | `beambot-postgres` | Teleport Postgres (skips if not visible) |
| `LIVE_DB_USER` / `LIVE_DB_NAME` | `app` / `beambot` | |
| `LIVE_REDIS` | `beambot-redis` | Teleport Redis (skips if not visible) |
| `LIVE_DRIVE` | unset | `1` also asserts the robot can move (needs a driver role; the robot will move!) |

Tests skip, rather than fail, for resources your roles can't see: that is RBAC working.
