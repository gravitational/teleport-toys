#!/usr/bin/env bash
# Push the node kit to every Pi in cluster.conf and run setup_pi.sh on each.
# Uses your ~/.ssh/config host aliases (pi1..pi5). Copy cluster.conf.example to cluster.conf first.
#
#   export TELEPORT_PROXY=<your-tenant>.beams.run:443
#   PARALLEL=1 ./deploy.sh                   # all nodes at once (logs in ./logs/)
#   ./deploy.sh pi4                          # one node, interactive
#   EXTRA="--real" ./deploy.sh pi4           # real hardware on pi4
#
# Join token: deploy.sh creates a 1-hour one with tctl, and revokes it once every Pi it deployed shows up in
# Teleport (SSH node and MCP app, under that Pi's own host ID), waiting up to JOIN_TIMEOUT seconds. If a node
# fails or doesn't show up in time, the token is left in place and the revoke command printed.
# To use a token you have, set TELEPORT_TOKEN: deploy.sh then neither creates nor revokes one.
# Either way the token reaches each Pi over ssh's stdin into a 0600 file, never on a command line (ps, sudo's log).
#
# Env: SSH_USER (unset = use host alias as-is; set = user@ip), ROBOT (default beambot-sim),
#      EXTRA (extra setup_pi.sh flags), PARALLEL=1, TELEPORT_TOKEN, JOIN_TIMEOUT (default 300), JOIN_POLL (default 5)
set -euo pipefail
cd "$(dirname "$0")"

: "${TELEPORT_PROXY:?set TELEPORT_PROXY, e.g. <your-tenant>.beams.run:443}"
ROBOT="${ROBOT:-beambot-sim}"
EXTRA="${EXTRA:-}"
PARALLEL="${PARALLEL:-0}"
JOIN_TIMEOUT="${JOIN_TIMEOUT:-300}"   # seconds to wait for the nodes to join before giving up (and keeping the token)
JOIN_POLL="${JOIN_POLL:-5}"
ONLY="${1:-}"

[[ -f cluster.conf ]] || { echo "cluster.conf not found: cp cluster.conf.example cluster.conf and edit it" >&2; exit 1; }

NODES=$(grep -Ev '^[[:space:]]*(#|$)' cluster.conf)   # bash 3.2 (macOS) friendly
ALL_IPS=$(echo "$NODES" | awk '{print $2}')

# Stage on the Pi's SD card, NOT /tmp: on Arch /tmp is a RAM tmpfs.
STAGE=/var/tmp/beambot-sim
KIT=/tmp/beambot-pi.tgz   # laptop side

echo "==> packing kit"
TAR_FLAGS=""
tar --version 2>/dev/null | grep -qi bsdtar && TAR_FLAGS="--no-xattrs --no-mac-metadata"  # macOS: no Apple xattr headers
# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar $TAR_FLAGS -czf "$KIT" --exclude='__pycache__' --exclude='.fuse_hidden*' -C pi .
mkdir -p logs

TOKEN_CREATED=0 TOKEN_REVOKED=0
token_left() {   # on any exit: a token we made and didn't revoke must not go unmentioned
  if [[ $TOKEN_CREATED == 1 && $TOKEN_REVOKED == 0 ]]; then
    echo "The join token deploy.sh created is still valid (it expires within 1 hour). Revoke it when you're done:" >&2
    echo "  tctl tokens rm $TELEPORT_TOKEN" >&2
  fi
}
if [[ -z "${TELEPORT_TOKEN:-}" ]]; then
  command -v tctl >/dev/null || { echo "tctl not found: it creates the join token (or set TELEPORT_TOKEN)" >&2; exit 1; }
  command -v python3 >/dev/null || { echo "python3 not found: it reads tctl's node list" >&2; exit 1; }
  echo "==> creating a 1-hour join token (revoked once every node has joined)"
  TELEPORT_TOKEN="$(tctl tokens add --type=node,app --ttl=1h --format=text)"
  [[ -n "$TELEPORT_TOKEN" ]] || { echo "tctl returned no token" >&2; exit 1; }
  TOKEN_CREATED=1
  trap token_left EXIT
fi

registered() {   # registered nodes|app_server: "<host ID> <name>" for every SSH node / app server Teleport lists
  tctl get "$1" --format=json 2>/dev/null | python3 -c '
import json, sys
for r in json.load(sys.stdin) or []:
    s, m = r.get("spec") or {}, r.get("metadata") or {}
    if sys.argv[1] == "nodes":
        print(m.get("name"), s.get("hostname"))
    else:
        print(s.get("host_id"), (s.get("app") or {}).get("metadata", {}).get("name") or m.get("name"))' "$1" \
    2>/dev/null
}

wait_until_joined() {   # wait_until_joined host...: each Pi is listed as an SSH node and an app server under its own
  local deadline=$((SECONDS + JOIN_TIMEOUT)) nodes apps missing h id name   # host ID, or give up after JOIN_TIMEOUT
  missing=""
  for h in "$@"; do [[ -s "logs/$h.host_id" ]] || missing="$missing $h"; done
  if [[ -n "$missing" ]]; then
    echo "Not revoking the join token: couldn't read the Teleport host ID of:$missing" >&2
    return 1
  fi
  echo "==> waiting up to ${JOIN_TIMEOUT}s for $# node(s) to join Teleport before revoking the join token"
  while :; do
    nodes=$(registered nodes || true)
    apps=$(registered app_server || true)
    missing=""
    for h in "$@"; do
      id=$(head -n1 "logs/$h.host_id")
      name="$ROBOT-$(awk -v h="$h" '$1 == h {print $3}' <<<"$NODES")"   # setup_pi.sh: <robot>-<role>
      { grep -qxF "$id $name" <<<"$nodes" && grep -qxF "$id $name" <<<"$apps"; } || missing="$missing $name"
    done
    [[ -z "$missing" ]] && return 0
    if (( SECONDS >= deadline )); then
      echo "Not revoking the join token: after ${JOIN_TIMEOUT}s Teleport doesn't list these as joined:$missing" >&2
      echo "(or tctl can't read them: check 'tctl get nodes' and 'tctl get app_server')" >&2
      return 1
    fi
    sleep "$JOIN_POLL"
  done
}

deploy_one() {
  local host=$1 ip=$2 role=$3 target peers token_arg=""
  target=$([[ -n "${SSH_USER:-}" ]] && echo "$SSH_USER@$ip" || echo "$host")
  peers=$(echo "$ALL_IPS" | grep -vx "$ip" | paste -sd, -)
  ssh "$target" "mkdir -p $STAGE"
  scp -q "$KIT" "$target:$STAGE.tgz"
  if [[ -n "${TELEPORT_TOKEN:-}" ]]; then
    printf '%s' "$TELEPORT_TOKEN" | ssh "$target" "rm -f $STAGE.token && umask 077 && cat > $STAGE.token"
    token_arg="--token-file $STAGE.token"
  fi
  # shellcheck disable=SC2029
  ssh $([[ $PARALLEL == 1 ]] || echo -t) "$target" \
    "trap 'rm -f $STAGE.token' EXIT; set -e; rm -rf $STAGE && mkdir -p $STAGE && tar -xzf $STAGE.tgz -C $STAGE && rm -f $STAGE.tgz && \
     SUDO=\$([ \"\$(id -u)\" -eq 0 ] || echo sudo); \
     \$SUDO bash $STAGE/setup_pi.sh --role $role --robot $ROBOT --proxy $TELEPORT_PROXY \
       $token_arg --host-id-file $STAGE.host_id --peers $peers $EXTRA"
  # The Teleport host ID this Pi joins under, for the join check before deploy.sh revokes the token it made.
  [[ $TOKEN_CREATED == 1 ]] || return 0
  ssh "$target" "cat $STAGE.host_id" < /dev/null > "logs/$host.host_id" 2>/dev/null || : > "logs/$host.host_id"
}

pids="" want=""
while read -r host ip role <&3; do
  [[ -n "$ONLY" && "$ONLY" != "$host" ]] && continue
  want="$want $host"
  if [[ $PARALLEL == 1 ]]; then
    echo "==> $host ($ip) -> $role   [logs/$host.log]"
    deploy_one "$host" "$ip" "$role" > "logs/$host.log" 2>&1 < /dev/null &
    pids="$pids $!:$host"
  else
    echo -e "\n\033[1;35m######## $host ($ip) -> $role ########\033[0m"
    deploy_one "$host" "$ip" "$role"
  fi
done 3< <(echo "$NODES")   # fd 3 so ssh/sudo keep the terminal

rc=0
for p in $pids; do
  if wait "${p%%:*}"; then echo "OK    ${p#*:}"; else echo "FAIL  ${p#*:}  (see logs/${p#*:}.log)"; rc=1; fi
done

if [[ $TOKEN_CREATED == 1 ]]; then
  if [[ $rc -ne 0 ]]; then
    echo "Not revoking the join token: a deploy failed, and that node may still need it." >&2
  # shellcheck disable=SC2086
  elif wait_until_joined $want; then
    if tctl tokens rm "$TELEPORT_TOKEN" >/dev/null 2>&1; then
      TOKEN_REVOKED=1
      echo "==> every node joined: join token revoked"
    else
      echo "Could not revoke the join token." >&2
      rc=1
    fi
  else
    rc=1
  fi
fi

echo -e "\nCheck:  tsh ls robot=$ROBOT   and   tsh mcp ls robot=$ROBOT -v"
[[ $TOKEN_CREATED == 1 ]] || echo 'Then revoke the join token you passed in:  tctl tokens rm "$TELEPORT_TOKEN"'
exit $rc
