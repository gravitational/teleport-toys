#!/usr/bin/env bash
# Can a least-privilege beam reach the robot's MCP app as plain HTTP and skip the role's tool filter?
#
# Run INSIDE a beam created with the role you want to test, e.g.
#   tsh beams add --roles beam-user,picar-driver
# The one call it makes can't run the tool: it calls FORBIDDEN with "arguments": "not-an-object". Teleport checks the
# tool name before the robot sees the arguments, so a role without the tool gets Teleport's RBAC denial. A call that
# gets past Teleport is refused by the robot's MCP server ("Invalid request parameters") before any tool runs. So the
# check never lifts a STOP or moves the car, even when the bypass is open. A list that shows the tool is a FAIL
# without calling it.
# Usage: bash check_mcp_bypass.sh [mcp-app] [stream-app]      (defaults: picar-mcp picar-stream)
# Env:   PORT (default 18443; PORT and PORT+1 must be free), FORBIDDEN (tool the role must not reach, default resume:
#        the stop latch's key), EXPECT_TOOLS (tools the role DOES have, default drive,get_telemetry,snapshot,stop),
#        REQUIRE_STREAM=1 (a skipped stream-app check counts as inconclusive)
#
# Exit 0  PASS          Teleport itself denied FORBIDDEN (its RBAC message, as a JSON-RPC error answering the call or
#                       in an HTTP 401/403) and the tool list did not show it.
# Exit 1  FAIL          FORBIDDEN was listed (in any letter case), or the call got through to the robot: a result came
#                       back, or the robot's MCP server rejected the arguments.
# Exit 2  INCONCLUSIVE  anything else: no proxy, a port already taken, no answer, an answer that is not Teleport's RBAC
#                       denial, a list that doesn't look like this robot's. Nothing was proven, so it is never a pass.
set -uo pipefail

APP="${1:-picar-mcp}"; STREAM="${2:-picar-stream}"; PORT="${PORT:-18443}"; FORBIDDEN="${FORBIDDEN:-resume}"
EXPECT_TOOLS="${EXPECT_TOOLS:-drive,get_telemetry,snapshot,stop}"; REQUIRE_STREAM="${REQUIRE_STREAM:-0}"
# The only denial that counts: Teleport's own RBAC message (seen on a real cluster as JSON-RPC error -32602).
TELEPORT_RBAC='RBAC is enforced by your Teleport roles'
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

# start_proxy <app> <port>: 0 = a local proxy accepts connections; 1 = tsh exited (message in $WORK/proxy.err); 2 = never
# listened; 3 = another program already listens on the port, so tsh wasn't started (that program would answer instead)
start_proxy() {
  local app="$1" port="$2" log="$WORK/proxy-$1.log" pid
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port on 127.0.0.1 is already taken by another program: set PORT to a free one" >"$WORK/proxy.err"; return 3
  fi
  tsh proxy app "$app" --port "$port" >"$log" 2>&1 &
  pid=$!; PIDS+=("$pid")
  for _ in $(seq 1 40); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then return 0; fi
    if ! kill -0 "$pid" 2>/dev/null; then head -c 400 "$log" >"$WORK/proxy.err"; return 1; fi
    sleep 0.25
  done
  head -c 400 "$log" >"$WORK/proxy.err"; return 2
}

# rpc <port> <session-id|-> <request-id|-> <json>: sets RPC_CODE (HTTP status, 000 = no answer), RPC_BODY (the raw answer)
# and RPC_MSG (the JSON-RPC message whose id is <request-id>, else empty). An SSE answer can carry log lines and other
# requests' answers first, so the message is picked by id, never by position.
rpc() {
  local port="$1" sid="$2" id="$3" body="$4" hdr=(-H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-03-26')
  local pick='if type == "array" then .[] else . end | select(type == "object" and .id == $id)'
  [ "$sid" != "-" ] && hdr+=(-H "Mcp-Session-Id: $sid")
  RPC_CODE="$(curl -sS -m 20 -D "$WORK/h" -o "$WORK/b" -w '%{http_code}' "${hdr[@]}" -d "$body" "http://127.0.0.1:$port/mcp" 2>/dev/null)" || RPC_CODE="000"
  RPC_BODY="" RPC_MSG=""
  [ -s "$WORK/b" ] || return 0
  RPC_BODY="$(head -c 4000 "$WORK/b")"
  [ "$id" = "-" ] && return 0
  if grep -q '^data:' "$WORK/b"; then
    RPC_MSG="$(sed -n 's/^data: *//p' "$WORK/b" | jq -Rc --argjson id "$id" "fromjson? | $pick" 2>/dev/null | head -1)"
  else
    RPC_MSG="$(jq -c --argjson id "$id" "$pick" "$WORK/b" 2>/dev/null | head -1)"
  fi
}

echo "== 1. $APP through 'tsh proxy app' as plain HTTP =="
start_proxy "$APP" "$PORT"; rc=$?
if [ "$rc" -eq 3 ]; then
  inconclusive "$(cat "$WORK/proxy.err"): nothing was tested"
elif [ "$rc" -ne 0 ]; then
  inconclusive "tsh would not give a plain-HTTP proxy to $APP: $(tr '\n' ' ' <"$WORK/proxy.err")"
  note "that is the client declining, not Teleport's tool filter answering: nothing was tested"
else
  INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"bypass-check","version":"0"}}}'
  rpc "$PORT" - 1 "$INIT"
  SID="$(tr -d '\r' <"$WORK/h" 2>/dev/null | awk 'tolower($1)=="mcp-session-id:"{print $2}')"
  if [ "$RPC_CODE" = "000" ]; then
    inconclusive "no HTTP answer from the proxy on port $PORT (initialize)"
  elif [[ "$RPC_CODE" =~ ^(401|403)$ ]] && grep -qF "$TELEPORT_RBAC" <<<"$RPC_BODY"; then
    pass "initialize was denied by Teleport RBAC (HTTP $RPC_CODE)"; PROVEN=1
  elif [[ ! "$RPC_CODE" =~ ^2 ]]; then
    inconclusive "initialize got HTTP $RPC_CODE with a body that is not Teleport's RBAC denial: $(echo "$RPC_BODY" | head -c 160)"
  else
    note "initialize worked over the plain proxy (HTTP $RPC_CODE); checking the tool list and the call"
    rpc "$PORT" "${SID:--}" - '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    rpc "$PORT" "${SID:--}" 2 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
    LIST_CODE="$RPC_CODE"
    NAMES="$(echo "$RPC_MSG" | jq -r '.result.tools[]?.name' 2>/dev/null | paste -sd, - || true)"
    echo "              tools/list (HTTP $LIST_CODE) -> ${NAMES:-<none>}"
    # Names are plain strings, not patterns (EXPECT_TOOLS='.*' must not match everything). FORBIDDEN counts in any
    # letter case: a "Resume" in the list is as much a leak as "resume".
    LISTED=0; echo ",$NAMES," | grep -qiF -- ",$FORBIDDEN," && LISTED=1
    HAS_EXPECTED=0
    IFS=', ' read -r -a WANT <<<"$EXPECT_TOOLS"
    for t in ${WANT[@]+"${WANT[@]}"}; do [ -n "$t" ] && echo ",$NAMES," | grep -qF -- ",$t," && HAS_EXPECTED=1; done
    if [ -z "$NAMES" ]; then
      inconclusive "tools/list returned no tools (HTTP $LIST_CODE): cannot tell what the role sees"
    elif [ "$LISTED" -eq 1 ]; then
      fail "tools/list shows '$FORBIDDEN' to this role: the tool filter is NOT applied on the plain-HTTP path"
    elif [ "$HAS_EXPECTED" -eq 0 ]; then
      inconclusive "tools/list shows none of the tools this role should have ($EXPECT_TOOLS): is this the right robot and role?"
    else
      pass "tools/list hides '$FORBIDDEN' and still shows the role's own tools"
    fi

    # If tools/list already showed the tool, the FAIL above is proven: don't call it at all.
    if [ "$LISTED" -eq 1 ]; then
      note "tools/list already showed '$FORBIDDEN'; not calling it"
    else
      # "arguments" must be an object, so the robot's MCP server refuses this call before any tool runs: if it gets
      # past Teleport it still can't lift a STOP or move anything. Teleport checks the tool name first.
      CALL="$(jq -cn --arg n "$FORBIDDEN" '{jsonrpc: "2.0", id: 3, method: "tools/call", params: {name: $n, arguments: "not-an-object"}}')"
      rpc "$PORT" "${SID:--}" 3 "$CALL"
      echo "              tools/call $FORBIDDEN (arguments \"not-an-object\") -> HTTP $RPC_CODE $(echo "${RPC_MSG:-$RPC_BODY}" | head -c 220)"
      if [ "$RPC_CODE" = "000" ]; then
        inconclusive "no HTTP answer to the $FORBIDDEN call"
      elif echo "$RPC_MSG" | jq -e '.result != null' >/dev/null 2>&1; then
        fail "the $FORBIDDEN call REACHED THE ROBOT (a result came back, even if it was a refusal): Teleport did not stop it"
      elif echo "$RPC_MSG" | jq -e '.error.code == -32602 and .error.message == "Invalid request parameters"' >/dev/null 2>&1; then
        fail "the $FORBIDDEN call REACHED THE ROBOT: its MCP server rejected the arguments (no tool ran), so Teleport did not stop it"
      elif echo "$RPC_MSG" | jq -e --arg m "$TELEPORT_RBAC" '.error.message | strings | contains($m)' >/dev/null 2>&1 \
           || { [[ "$RPC_CODE" =~ ^(401|403)$ ]] && grep -qF "$TELEPORT_RBAC" <<<"$RPC_BODY"; }; then
        pass "the $FORBIDDEN call was denied by Teleport RBAC"; PROVEN=1
      else
        inconclusive "the $FORBIDDEN call failed, but not with Teleport's RBAC denial (HTTP $RPC_CODE): $(echo "${RPC_MSG:-$RPC_BODY}" | head -c 160)"
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
