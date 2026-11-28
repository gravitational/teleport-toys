#!/usr/bin/env bash
# Push the node kit to every Pi in cluster.conf and run setup_pi.sh on each.
# Uses your ~/.ssh/config host aliases (pi1..pi5). Copy cluster.conf.example to cluster.conf first.
#
#   export TELEPORT_PROXY=<your-tenant>.beams.run:443
#   export TELEPORT_TOKEN=$(tctl tokens add --type=node,app --ttl=1h --format=text)
#   PARALLEL=1 ./deploy.sh                   # all nodes at once (logs in ./logs/)
#   tctl tokens rm "$TELEPORT_TOKEN"         # once tsh ls shows every node: they hold their own certificates now
#   ./deploy.sh pi4                          # one node, interactive
#   EXTRA="--real" ./deploy.sh pi4           # real hardware on pi4
#
# Env: SSH_USER (unset = use host alias as-is; set = user@ip), ROBOT (default beambot-sim),
#      EXTRA (extra setup_pi.sh flags), PARALLEL=1
# The join token goes to each Pi over ssh's stdin into a 0600 file, never on a command line (ps, sudo's log).
set -euo pipefail
cd "$(dirname "$0")"

: "${TELEPORT_PROXY:?set TELEPORT_PROXY, e.g. <your-tenant>.beams.run:443}"
ROBOT="${ROBOT:-beambot-sim}"
EXTRA="${EXTRA:-}"
PARALLEL="${PARALLEL:-0}"
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
       $token_arg --peers $peers $EXTRA"
}

pids=""
while read -r host ip role <&3; do
  [[ -n "$ONLY" && "$ONLY" != "$host" ]] && continue
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

echo -e "\nCheck:  tsh ls robot=$ROBOT   and   tsh mcp ls robot=$ROBOT -v"
[[ -z "${TELEPORT_TOKEN:-}" ]] || echo 'Then revoke the join token:  tctl tokens rm "$TELEPORT_TOKEN"'
exit $rc
