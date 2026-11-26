#!/usr/bin/env bash
# Can a least-privilege beam reach the robot's MCP app as plain HTTP and skip the role's tool filter?
#
# Run INSIDE a beam created with the role you want to test, e.g.
#   tsh beams add --roles beam-user,picar-driver
# With the default FORBIDDEN=resume it reads get_telemetry first and only sends the real resume call while the robot
# says halted=false, so a run never clears a STOP. (A STOP pressed in the few ms between the two calls could still
# be cleared if the bypass is open: don't run it while someone may need the STOP.) A list that shows the tool is a
# FAIL without calling it.
# Usage: bash check_mcp_bypass.sh [mcp-app] [stream-app]      (defaults: picar-mcp picar-stream)
# Env:   PORT (default 18443), FORBIDDEN (tool the role must not reach, default resume: the stop latch's key),
#        EXPECT_TOOLS (tools the role DOES have, default drive,get_telemetry,snapshot,stop),
#        REQUIRE_STREAM=1 (a skipped stream-app check counts as inconclusive)
#
# Exit 0  PASS          Teleport itself denied `drive` (the JSON-RPC RBAC error or an HTTP 401/403 from Teleport) and
#                       the tool list did not show it.
# Exit 1  FAIL          `drive` was listed, or the call got through to the robot (even if the robot then refused it).
# Exit 2  INCONCLUSIVE  anything else: no proxy, no answer, an answer that is not Teleport's, a list that doesn't look
#                       like this robot's. Nothing was proven, so it is never reported as a pass.
set -uo pipefail

APP="${1:-picar-mcp}"; STREAM="${2:-picar-stream}"; PORT="${PORT:-18443}"; FORBIDDEN="${FORBIDDEN:-resume}"
EXPECT_TOOLS="${EXPECT_TOOLS:-drive,get_telemetry,snapshot,stop}"; REQUIRE_STREAM="${REQUIRE_STREAM:-0}"
DENY_RE='RBAC is enforced by your Teleport roles|access to resource denied|access denied'
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
command -v tsh >/dev/null || { echo "tsh is required" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
WORK="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done; rm -rf "$WORK"; }
trap cleanup EXIT

PASSES=0; FAILS=0; INCONCLUSIVE=0; SKIPPED=0; PROVEN=0
pass() { echo "PASS          $*"; PASSES=$((PASSES+1)); }
fail() { echo "FAIL          $*"; FAILS=$((FAILS+1)); }
inconclusive() { echo "INCONCLUSIVE  $*"; INCONCLUSIVE=$((INCONCLUSIVE+1)); }
skip() { echo "SKIPPED       $*"; SKIPPED=$((SKIPPED+1)); }
note() { echo "              $*"; }

# start_proxy <app> <port>: 0 = a local proxy accepts connections; 1 = tsh exited (message in $WORK/proxy.err); 2 = never listened
start_proxy() {
  local app="$1" port="$2" log="$WORK/proxy-$1.log" pid
  tsh proxy app "$app" --port "$port" >"$log" 2>&1 &
  pid=$!; PIDS+=("$pid")
  for _ in $(seq 1 40); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then return 0; fi
    if ! kill -0 "$pid" 2>/dev/null; then head -c 400 "$log" >"$WORK/proxy.err"; return 1; fi
    sleep 0.25
  done
  head -c 400 "$log" >"$WORK/proxy.err"; return 2
}

# rpc <port> <session-id|-> <json>: sets RPC_CODE (HTTP status, 000 = no answer) and RPC_BODY (SSE "data:" unwrapped)
rpc() {
  local port="$1" sid="$2" body="$3" hdr=(-H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-03-26')
  [ "$sid" != "-" ] && hdr+=(-H "Mcp-Session-Id: $sid")
  RPC_CODE="$(curl -sS -m 20 -D "$WORK/h" -o "$WORK/b" -w '%{http_code}' "${hdr[@]}" -d "$body" "http://127.0.0.1:$port/mcp" 2>/dev/null)" || RPC_CODE="000"
  RPC_BODY=""
  if [ -s "$WORK/b" ]; then
    if grep -q '^data:' "$WORK/b"; then RPC_BODY="$(sed -n 's/^data: *//p' "$WORK/b" | head -1)"; else RPC_BODY="$(head -c 4000 "$WORK/b")"; fi
  fi
}

echo "== 1. $APP through 'tsh proxy app' as plain HTTP =="
start_proxy "$APP" "$PORT"; rc=$?
if [ "$rc" -ne 0 ]; then
  inconclusive "tsh would not give a plain-HTTP proxy to $APP: $(tr '\n' ' ' <"$WORK/proxy.err")"
  note "that is the client declining, not Teleport's tool filter answering: nothing was tested"
else
  INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bypass-check","version":"0"}}}'
  rpc "$PORT" - "$INIT"
  SID="$(tr -d '\r' <"$WORK/h" 2>/dev/null | awk 'tolower($1)=="mcp-session-id:"{print $2}')"
  if [ "$RPC_CODE" = "000" ]; then
    inconclusive "no HTTP answer from the proxy on port $PORT (initialize)"
  elif [[ "$RPC_CODE" =~ ^(401|403)$ ]] && echo "$RPC_BODY" | grep -qiE "$DENY_RE|teleport"; then
    pass "initialize was denied by Teleport (HTTP $RPC_CODE)"; PROVEN=1
  elif [[ ! "$RPC_CODE" =~ ^2 ]]; then
    inconclusive "initialize got HTTP $RPC_CODE with a body that is not a Teleport denial: $(echo "$RPC_BODY" | head -c 160)"
  else
    note "initialize worked over the plain proxy (HTTP $RPC_CODE); checking the tool list and the call"
    rpc "$PORT" "${SID:--}" '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    rpc "$PORT" "${SID:--}" '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    LIST_CODE="$RPC_CODE"
    NAMES="$(echo "$RPC_BODY" | jq -r '.result.tools[]?.name' 2>/dev/null | paste -sd, - || true)"
    echo "              tools/list (HTTP $LIST_CODE) -> ${NAMES:-<none>}"
    HAS_EXPECTED=0
    for t in ${EXPECT_TOOLS//,/ }; do echo ",$NAMES," | grep -q ",$t," && HAS_EXPECTED=1; done
    if [ -z "$NAMES" ]; then
      inconclusive "tools/list returned no tools (HTTP $LIST_CODE): cannot tell what the role sees"
    elif echo ",$NAMES," | grep -q ",$FORBIDDEN,"; then
      fail "tools/list shows '$FORBIDDEN' to this role: the tool filter is NOT applied on the plain-HTTP path"
    elif [ "$HAS_EXPECTED" -eq 0 ]; then
      inconclusive "tools/list shows none of the tools this role should have ($EXPECT_TOOLS): is this the right robot and role?"
    else
      pass "tools/list hides '$FORBIDDEN' and still shows the role's own tools"
    fi

    # Never send a real `resume` while the robot is latched: if the bypass is open, the call would clear a STOP
    # someone pressed. So read the latch first, and skip the call unless the robot plainly says halted=false.
    # (If tools/list already showed the tool, the FAIL above is proven; don't call it at all.)
    SKIP_CALL=""
    if echo ",$NAMES," | grep -q ",$FORBIDDEN,"; then
      SKIP_CALL="tools/list already showed '$FORBIDDEN'; not calling it"
    elif [ "$FORBIDDEN" = "resume" ]; then
      rpc "$PORT" "${SID:--}" '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_telemetry","arguments":{}}}'
      HALTED="$(echo "$RPC_BODY" | jq -r '[.result.structuredContent.halted?, (.result.content[]? | select(.type=="text") | .text | fromjson? | .halted?)]
                 | map(select(. != null)) | if length > 0 then (.[0] | tostring) else "unknown" end' 2>/dev/null || echo unknown)"
      echo "              get_telemetry (HTTP $RPC_CODE) -> halted=${HALTED:-unknown}"
      if [ "$HALTED" = "true" ]; then
        SKIP_CALL="the robot is LATCHED (halted=true): a real resume call could clear that STOP, so it was not sent. Resume it from the dashboard and re-run, or pass FORBIDDEN=<another tool>"
      elif [ "$HALTED" != "false" ]; then
        SKIP_CALL="could not read the latch from get_telemetry (HTTP $RPC_CODE), so the resume call was not sent: it could clear a STOP"
      fi
    fi
    if [ -n "$SKIP_CALL" ]; then
      if echo ",$NAMES," | grep -q ",$FORBIDDEN,"; then note "$SKIP_CALL"; else inconclusive "$SKIP_CALL"; fi
    else
      CALL_ARGS="{}"; [ "$FORBIDDEN" = "drive" ] && CALL_ARGS="{\"speed\":0,\"seconds\":0.1,\"steer_deg\":0,\"wait\":false}"
      rpc "$PORT" "${SID:--}" "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$FORBIDDEN\",\"arguments\":$CALL_ARGS}}"
      echo "              tools/call $FORBIDDEN -> HTTP $RPC_CODE $(echo "$RPC_BODY" | head -c 220)"
      ERRMSG="$(echo "$RPC_BODY" | jq -r '.error.message // empty' 2>/dev/null || true)"
      if [ "$RPC_CODE" = "000" ]; then
        inconclusive "no HTTP answer to the $FORBIDDEN call"
      elif echo "$RPC_BODY" | jq -e '.result != null' >/dev/null 2>&1; then
        fail "the $FORBIDDEN call REACHED THE ROBOT (a result came back, even if it was a refusal): Teleport did not stop it"
      elif { [ -n "$ERRMSG" ] && echo "$ERRMSG $RPC_BODY" | grep -qE "$DENY_RE"; } \
           || { [[ "$RPC_CODE" =~ ^(401|403)$ ]] && echo "$RPC_BODY" | grep -qiE "$DENY_RE|teleport"; }; then
        pass "the $FORBIDDEN call was denied by Teleport RBAC"; PROVEN=1
      else
        inconclusive "the $FORBIDDEN call failed, but not with a Teleport denial (HTTP $RPC_CODE): $(echo "$RPC_BODY" | head -c 160)"
      fi
    fi
  fi
fi

echo "== 2. $STREAM must not serve /mcp =="
if [ -z "$STREAM" ]; then
  skip "no stream app named"
else
  start_proxy "$STREAM" $((PORT+1)); rc=$?
  if [ "$rc" -ne 0 ]; then
    if grep -qiE "not found|not visible|no apps|access denied" "$WORK/proxy.err" 2>/dev/null; then
      skip "$STREAM is not visible to this role (expected for a role without picar-stream); run this from a beam that holds picar-stream to test it"
    else
      inconclusive "could not open a proxy to $STREAM: $(tr '\n' ' ' <"$WORK/proxy.err")"
    fi
  else
    CODE="$(curl -sS -m 15 -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"drive","arguments":{"speed":0,"seconds":0.1}}}' \
      "http://127.0.0.1:$((PORT+1))/mcp" 2>/dev/null)" || CODE="000"
    if [[ "$CODE" =~ ^(404|403|401)$ ]]; then pass "POST /mcp on $STREAM -> $CODE"
    elif [[ "$CODE" =~ ^2 ]]; then fail "POST /mcp on $STREAM -> $CODE: the plain HTTP app serves MCP"
    else inconclusive "POST /mcp on $STREAM -> $CODE (expected 404)"; fi
  fi
fi
if [ "$REQUIRE_STREAM" = "1" ] && [ "$SKIPPED" -gt 0 ]; then inconclusive "REQUIRE_STREAM=1 and the stream check was skipped"; fi

echo
echo "checks: $PASSES passed, $FAILS failed, $INCONCLUSIVE inconclusive, $SKIPPED skipped"
if [ "$FAILS" -gt 0 ]; then echo "RESULT: FAIL"; exit 1; fi
if [ "$INCONCLUSIVE" -gt 0 ] || [ "$PROVEN" -ne 1 ]; then
  echo "RESULT: INCONCLUSIVE (nothing was proven: Teleport never visibly denied $FORBIDDEN)"; exit 2
fi
echo "RESULT: PASS"
