#!/usr/bin/env bash
# Can a least-privilege beam reach the robot's MCP app as plain HTTP and skip the role's tool list?
#
# Run INSIDE a beam created with a read-only role set, e.g.
#   tsh beams add --roles beam-user,picar-viewer
# PASS means the viewer could not drive (and could not even see `drive`) through `tsh proxy app`.
# Usage: bash check_mcp_bypass.sh [mcp-app] [stream-app]      (defaults: picar-mcp picar-stream)
# Env:   PORT (default 18443), FORBIDDEN (tool the role must not reach, default drive)
set -euo pipefail

APP="${1:-picar-mcp}"; STREAM="${2:-picar-stream}"; PORT="${PORT:-18443}"; FORBIDDEN="${FORBIDDEN:-drive}"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
command -v tsh >/dev/null || { echo "tsh is required" >&2; exit 2; }
WORK="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT

FAILS=0; INCONCLUSIVE=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
note() { echo "NOTE  $*"; }

# start_proxy <app> <port> -> 0 if a local proxy is listening, 1 if tsh refused or never came up
start_proxy() {
  local app="$1" port="$2" log="$WORK/proxy-$1.log"
  tsh proxy app "$app" --port "$port" >"$log" 2>&1 &
  PIDS+=("$!")
  for _ in $(seq 1 20); do
    if grep -qiE "proxying|listening|127\.0\.0\.1:$port" "$log" 2>/dev/null; then
      for _ in $(seq 1 20); do (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return 0; sleep 0.25; done
      return 0
    fi
    kill -0 "${PIDS[${#PIDS[@]}-1]}" 2>/dev/null || { cat "$log"; return 1; }
    sleep 0.5
  done
  cat "$log"; return 1
}

# rpc <port> <session-id|-> <json> -> prints "HTTP <code>" then the JSON payload (SSE "data:" lines unwrapped)
rpc() {
  local port="$1" sid="$2" body="$3" hdr=(-H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-03-26')
  [ "$sid" != "-" ] && hdr+=(-H "Mcp-Session-Id: $sid")
  curl -sS -m 20 -D "$WORK/h" -o "$WORK/b" -w 'HTTP %{http_code}\n' "${hdr[@]}" -d "$body" "http://127.0.0.1:$port/mcp" || echo "HTTP 000"
  if [ -f "$WORK/b" ]; then sed -n 's/^data: *//p;/^{/p' "$WORK/b" | head -1 || true; fi
}

echo "== 1. $APP through 'tsh proxy app' as plain HTTP =="
if ! start_proxy "$APP" "$PORT"; then
  pass "tsh would not open a plain-HTTP proxy to $APP (see message above)"
else
  INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bypass-check","version":"0"}}}'
  OUT="$(rpc "$PORT" - "$INIT")"; CODE="$(echo "$OUT" | sed -n 1p | awk '{print $2}')"
  SID="$(tr -d '\r' <"$WORK/h" | awk 'tolower($1)=="mcp-session-id:"{print $2}')"
  if [[ ! "$CODE" =~ ^2 ]]; then
    pass "initialize refused (HTTP $CODE)"
  else
    note "initialize worked over the plain proxy (HTTP $CODE); checking tools"
    rpc "$PORT" "${SID:--}" '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null || true
    LIST="$(rpc "$PORT" "${SID:--}" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | sed 1d)"
    NAMES="$(echo "$LIST" | jq -r '.result.tools[]?.name' 2>/dev/null | paste -sd, - || true)"
    echo "      tools/list -> ${NAMES:-<none>}"
    if echo ",$NAMES," | grep -q ",$FORBIDDEN,"; then
      fail "tools/list shows '$FORBIDDEN' to this role: the role's tool filter is NOT applied on the plain-HTTP path"
    else
      pass "tools/list hides '$FORBIDDEN'"
    fi
    CALL="$(rpc "$PORT" "${SID:--}" "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$FORBIDDEN\",\"arguments\":{\"speed\":0,\"seconds\":0.1,\"steer_deg\":0,\"wait\":false}}}")"
    echo "      tools/call $FORBIDDEN (speed 0) -> $(echo "$CALL" | sed -n 1p) $(echo "$CALL" | sed 1d | cut -c1-200)"
    PAYLOAD="$(echo "$CALL" | sed 1d)"
    if [[ ! "$(echo "$CALL" | sed -n 1p | awk '{print $2}')" =~ ^2 ]] \
       || echo "$PAYLOAD" | jq -e '.error != null or .result.isError == true' >/dev/null 2>&1; then
      pass "tools/call $FORBIDDEN was denied"
    else
      fail "tools/call $FORBIDDEN SUCCEEDED for a role that must not have it: this reopens the viewer-can-drive hole"
    fi
  fi
fi

echo "== 2. $STREAM must not serve /mcp =="
if [ -n "$STREAM" ] && start_proxy "$STREAM" $((PORT+1)); then
  CODE="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"drive","arguments":{"speed":0,"seconds":0.1}}}' \
    "http://127.0.0.1:$((PORT+1))/mcp" || true)"
  if [[ "$CODE" =~ ^(404|403|401)$ ]]; then pass "POST /mcp on $STREAM -> $CODE"; else fail "POST /mcp on $STREAM -> $CODE (expected 404)"; fi
else
  note "could not open a proxy to '$STREAM' (role may not include it): skipped"
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "RESULT: PASS"; else echo "RESULT: FAIL ($FAILS check(s))"; exit 1; fi
