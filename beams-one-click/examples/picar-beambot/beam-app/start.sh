#!/usr/bin/env bash
# Run inside the Beam:  ./start.sh   then from your laptop:  tsh beams publish <beam-name>
#
# Storage: tries Postgres through Teleport Database Access first and falls back to SQLite inside the beam.
#
# Optional env:
#   USE_DBS=auto|1|0          auto (default): use Postgres if Teleport lists $PG_TELEPORT_NAME for this identity,
#                             otherwise SQLite. 1 = require the tunnel. 0 = SQLite only.
#   PG_TELEPORT_NAME=beambot-postgres   Teleport database resource name
#   PG_DB_NAME=beambot  DB_USER=app     database and user to connect as
#   USE_REDIS=1               also tunnel Redis (REDIS_TELEPORT_NAME, default beambot-redis) for a shared
#                             activity feed. Off by default. No password: Teleport grants access.
#   REDIS_PASSWORD=...        legacy: only for a Redis host still using password ACLs (implies USE_REDIS=1).
#   ROBOT_MCP_NAME=picar-mcp   Teleport MCP server for the robot
#   ROBOT_STREAM_APP=picar-stream    Teleport app for video/telemetry (empty = skip)
#   BEAM_EXPIRES="2026-10-06 18:04 UTC"   shown in the UI (copy from `tsh beams ls` on your laptop)
#   TUNNEL_WAIT=3             seconds to let the tunnels come up before the app starts
set -euo pipefail
cd "$(dirname "$0")"
PIDS=()
trap 'kill "${PIDS[@]}" 2>/dev/null' EXIT

# A beam's `tsh` is already logged in with the identity delegated from the person who created it, so the
# tunnels below need no extra setup. Robot and database calls are
# audited as that user plus the beam's workload identity.

# Robot: MCP tools + video/telemetry, through Teleport.
export ROBOT_MCP_NAME="${ROBOT_MCP_NAME:-picar-mcp}"
tsh proxy mcp "$ROBOT_MCP_NAME" -p 8888 >/tmp/proxy-mcp.log 2>&1 & PIDS+=($!)
ROBOT_STREAM_APP="${ROBOT_STREAM_APP-picar-stream}"          # set empty to skip (no stream app enrolled)
if [[ -n "$ROBOT_STREAM_APP" ]]; then
  tsh proxy app "$ROBOT_STREAM_APP" --port 8001 >/tmp/proxy-stream.log 2>&1 & PIDS+=($!)
fi

# Databases through Teleport Database Access (optional). Anything missing falls back to SQLite in the beam.
PG_TELEPORT_NAME="${PG_TELEPORT_NAME:-beambot-postgres}"
REDIS_TELEPORT_NAME="${REDIS_TELEPORT_NAME:-beambot-redis}"
PG_DB_NAME="${PG_DB_NAME:-beambot}"
DB_USER="${DB_USER:-app}"
export PG_TELEPORT_NAME REDIS_TELEPORT_NAME
USE_DBS="${USE_DBS:-auto}"
if [[ "$USE_DBS" == "auto" ]]; then
  # tsh marks a database that has an active session with a leading ">", so read the name from column 1 or 2
  if tsh db ls 2>/dev/null | awk '{print ($1 == ">" ? $2 : $1)}' | grep -qx "$PG_TELEPORT_NAME"; then
    USE_DBS=1
  else
    USE_DBS=0
    echo "==> no Teleport database '$PG_TELEPORT_NAME' visible to this identity: using SQLite in the beam"
  fi
fi
if [[ "$USE_DBS" == "1" ]]; then
  tsh proxy db --tunnel --db-user="$DB_USER" --db-name="$PG_DB_NAME" -p 5432 "$PG_TELEPORT_NAME" >/tmp/proxy-pg.log 2>&1 & PIDS+=($!)
  export DATABASE_URL="postgresql://$DB_USER@localhost:5432/$PG_DB_NAME"     # no password: Teleport cert auth
fi
if [[ "${USE_REDIS:-0}" == "1" || -n "${REDIS_PASSWORD:-}" ]]; then
  tsh proxy db --tunnel --db-user="$DB_USER" -p 6379 "$REDIS_TELEPORT_NAME" >/tmp/proxy-redis.log 2>&1 & PIDS+=($!)
  export REDIS_URL="redis://$DB_USER${REDIS_PASSWORD:+:${REDIS_PASSWORD}}@localhost:6379"   # no password: Teleport cert auth
fi
sleep "${TUNNEL_WAIT:-3}"
# A dead tunnel is the usual first-run problem (role missing, wrong resource name): say so instead of failing later.
for i in "${!PIDS[@]}"; do
  if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
    echo "WARN: a Teleport tunnel exited early; see /tmp/proxy-*.log" >&2
    tail -n 3 /tmp/proxy-*.log 2>/dev/null >&2 || true
    break
  fi
done

# Debian-based beams mark system Python "externally managed" (PEP 668): fall back to the explicit opt-in.
python3 -m pip install -q -r requirements.txt 2>/dev/null \
  || python3 -m pip install -q --user --break-system-packages -r requirements.txt
export BEAM_EXPIRES="${BEAM_EXPIRES:-}"
export ROBOT_MCP_URL="${ROBOT_MCP_URL:-http://127.0.0.1:8888}"
export ROBOT_STREAM_URL="${ROBOT_STREAM_URL:-http://127.0.0.1:8001}"
python3 -m uvicorn app:app --host 0.0.0.0 --port 8080
