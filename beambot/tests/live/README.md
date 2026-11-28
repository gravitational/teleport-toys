# Live tests: prove it on a real beam

The hermetic suite (`make test`) uses a fake `tsh`, so it proves our scripts and app behave. These tests prove
**Teleport** behaves: production beams really can run MCP, tunnels and databases with their delegated identity.

Run them **inside a beam** (or anywhere `tsh status` shows an active session):

```bash
tsh beams add --roles beam-user,picar-driver   # roles must be eligible for delegation; beam-user lets it call models
tsh beams ssh <beam>
git clone https://github.com/gravitational/teleport-toys && cd teleport-toys/beambot
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

## Is the tool filter really on the only way in?

`check_mcp_bypass.sh` runs inside a **least-privilege beam** (`tsh beams add --roles beam-user,picar-driver`) and
tries to reach the robot's MCP app as plain HTTP through `tsh proxy app`, listing and calling `resume` (the tool that lifts an emergency stop, which `picar-driver` withholds) without
the MCP tool filter. It also confirms the video/telemetry app does not serve `/mcp`. `RESULT: PASS` means the
beam could not see or call `resume` that way. Set `FORBIDDEN=drive` to check a trimmed copy of the role. Run it again after changing roles.

It never passes on silence. **PASS** (exit 0) needs Teleport's own RBAC denial of the `resume` call ("RBAC is enforced
by your Teleport roles"), with `resume` missing from the tool list and the role's own tools still there. **FAIL** (exit 1)
means `resume` was listed (in any letter case) or the call reached the robot, even if the robot then refused it. Everything else, such as no proxy, no
answer, a list that doesn't look like this robot's, or an error that isn't Teleport's, is **INCONCLUSIVE** (exit 2). The stream
app check is **SKIPPED** from a beam without it, because the driver role can't see `picar-stream`; `REQUIRE_STREAM=1` turns a skip
into INCONCLUSIVE, and the line `checks: N passed, N failed, ...` says what ran.

The check can't lift a STOP. Its one call sends `resume` with `"arguments": "not-an-object"`. Teleport checks the tool
name before the robot sees the arguments, so a role without `resume` gets Teleport's denial. A call that gets past
Teleport is refused by the robot's MCP server ("Invalid request parameters") before any tool runs, and the check
reports that as FAIL. If Teleport itself ever refused the malformed arguments, its answer would not be the RBAC message,
so the check would say INCONCLUSIVE, never PASS. If the tool list already shows `resume`, that is a FAIL and it isn't
called. It needs `PORT` and `PORT+1` (default 18443 and 18444) free, and says INCONCLUSIVE if another program already
listens there.

```bash
bash tests/live/check_mcp_bypass.sh            # [mcp-app] [stream-app], defaults picar-mcp picar-stream
```
