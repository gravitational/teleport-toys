#!/usr/bin/env bash
# LIVE check, run inside a beam (or any shell with an active tsh session): proves that this identity can
# reach MCP servers, apps and databases through Teleport, with nothing to install or enroll.
#
#   bash verify_in_beam.sh [--labels key=value] [--app <tsh app>] [--db <tsh db>] [--db-user app] [--db-name beambot]
#
# Exits non-zero on the first failed check. Every check prints PASS/FAIL so the output can be pasted into an issue.
set -uo pipefail
LABELS="" APP="" DB="" DB_USER="app" DB_NAME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --labels) LABELS="$2"; shift 2 ;;
    --app) APP="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --db-user) DB_USER="$2"; shift 2 ;;
    --db-name) DB_NAME="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
FAILS=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
step() { printf '\n-- %s\n' "$*"; }

step "identity"
if tsh status >/tmp/verify-status.txt 2>&1; then pass "tsh has an active session"; sed -n 1,12p /tmp/verify-status.txt
  broad=$(sed -n "s/^ *Roles: *//p" /tmp/verify-status.txt | tr -d " " | tr "," "\n" | grep -xE "access|editor|auditor|admin" | paste -sd, -)
  [[ -n "$broad" ]] && printf "WARN  this beam holds broad roles (%s). Recreate it with --roles so it only has what the agent needs.\n" "$broad"
  else fail "tsh status"; cat /tmp/verify-status.txt; exit 1; fi

step "inference"
if tsh apps ls 2>/dev/null | grep -qE '[[:space:]]LLM[[:space:]]'; then pass "model apps (openai / anthropic) are visible"
else printf 'WARN  no LLM apps visible: Claude Code in this beam cannot call a model. Recreate it with --roles beam-user,<your roles>\n'; fi

step "MCP: list"
OUT=$(tsh mcp ls ${LABELS:+"$LABELS"} -v 2>&1) && pass "tsh mcp ls" || { fail "tsh mcp ls"; echo "$OUT"; }
echo "$OUT" | sed -n 1,20p
MCP_APP=$(echo "$OUT" | awk 'NR>2 && $1 !~ /^-/ {print $1; exit}')

step "MCP: handshake, tools/list"
TARGET="${APP:-$MCP_APP}"
if [[ -n "$TARGET" ]]; then
  REQ=$(printf '%s\n%s\n%s\n' \
   '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}' \
   '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
   '{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
  RES=$( { printf '%s\n' "$REQ"; sleep 8; } | TELEPORT_DEBUG=false timeout 30 tsh mcp connect "$TARGET" 2>&1 )
  if grep -q '"protocolVersion"' <<<"$RES" && grep -q '"tools"' <<<"$RES"; then pass "$TARGET answered initialize and tools/list";
  else fail "$TARGET handshake"; echo "$RES" | tail -8; fi
else
  fail "no MCP server found to test (pass --labels or --app)"
fi

step "Databases"
if [[ -n "$DB" ]]; then
  tsh db ls 2>&1 | awk '{print ($1 == ">" ? $2 : $1)}' | grep -qx "$DB" && pass "tsh db ls lists $DB" || fail "$DB not listed by tsh db ls"
  PORT=15432
  tsh proxy db --tunnel --db-user="$DB_USER" ${DB_NAME:+--db-name="$DB_NAME"} -p $PORT "$DB" >/tmp/verify-db.log 2>&1 &
  TP=$!; sleep 6
  if python3 - "$DB_USER" "${DB_NAME:-postgres}" "$PORT" <<'PY'
import sys
user, name, port = sys.argv[1:4]
try:
    import psycopg
except ImportError:
    sys.exit("psycopg not installed: python3 -m pip install --user --break-system-packages 'psycopg[binary]'")
with psycopg.connect(f"postgresql://{user}@localhost:{port}/{name}", connect_timeout=8, autocommit=True) as c:
    assert c.execute("SELECT 1").fetchone()[0] == 1
PY
  then pass "SELECT 1 through the Teleport tunnel (no password in the URL)"; else fail "database query"; tail -5 /tmp/verify-db.log; fi
  kill $TP 2>/dev/null
else
  echo "skipped (pass --db <name> to test a database)"
fi

echo
[[ $FAILS -eq 0 ]] && echo "ALL CHECKS PASSED" || echo "$FAILS check(s) failed"
exit $((FAILS > 0))
