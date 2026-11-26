#!/usr/bin/env bash
# Run on the LAPTOP after a normal `tsh login` (tsh 18.11.9+). Creates (or reuses) a beam, waits until it is
# reachable, copies beam_mcp_setup.sh in and runs it.
#
#   beam_mcp_up.sh --labels robot=beambot-sim --allow 'sense_*,safety_*' --deny 'admin_*' \
#                  --roles role-a,role-b [--beam <name>] [--dir /home/beams/work]
#
# --roles is required for a new beam: it limits the beam to those roles (tsh beams add --roles), which must be
# eligible for delegation. Without it a beam inherits ALL your roles, admin ones included, so the script refuses
# unless you pass --inherit-roles. Want hands later? Make a second beam with more roles.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

ROLES="" LABELS="" ALLOW="" DENY="" BEAM="" DIR="/home/beams/work" NO_HANDSHAKE="" INHERIT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --roles) ROLES="$2"; shift 2 ;;
    --labels) LABELS="$2"; shift 2 ;;
    --allow) ALLOW="$2"; shift 2 ;;
    --deny) DENY="$2"; shift 2 ;;
    --beam) BEAM="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --inherit-roles) INHERIT=1; shift ;;
    --no-handshake) NO_HANDSHAKE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
command -v tsh >/dev/null || { echo "tsh not found" >&2; exit 1; }
tsh status >/dev/null 2>&1 || { echo "not logged in: tsh login --proxy=<your-tenant>.beams.run:443" >&2; exit 1; }
VER=$(tsh version 2>/dev/null | sed -n 's/^Teleport v\([0-9][0-9.]*\).*/\1/p' | head -1)
if [[ -n "$VER" && "$(printf '%s\n18.11.9\n' "$VER" | sort -V | head -1)" != "18.11.9" ]]; then
  echo "WARN: tsh $VER is older than 18.11.9, the minimum the Beams docs list." >&2
fi

if [[ -z "$BEAM" ]]; then
  # A blank list counts as "not given" (never `--roles=" "`: tsh may then give the beam all your roles).
  ROLES="${ROLES//[[:space:]]/}"
  if [[ -n "$ROLES" && ( "$ROLES" == ,* || "$ROLES" == *, || "$ROLES" == *,,* ) ]]; then
    echo "Error: --roles has an empty entry ('$ROLES'): write it as role-a,role-b" >&2
    exit 2
  fi
  if [[ -z "$ROLES" && -z "$INHERIT" ]]; then
    echo "Error: pass --roles <role,role> (include beam-user so the beam can call models): without it the beam inherits ALL your roles (admin ones included). Use --inherit-roles to do that anyway." >&2
    exit 2
  fi
  echo "==> tsh beams add${ROLES:+ --roles=$ROLES}"
  BEAM=$(tsh beams add --no-console --format=json ${ROLES:+--roles="$ROLES"} | python3 -c '
import json, sys
s = sys.stdin.read()
d = json.loads(s[s.find("{"):])
def f(o):
    if isinstance(o, dict):
        for k in ("alias", "name", "id"):
            if isinstance(o.get(k), str) and o[k]: return o[k]
        for v in o.values():
            r = f(v)
            if r: return r
print(f(d) or "")')
  [[ -n "$BEAM" ]] || { echo "could not parse the beam name; pass --beam <name> (see tsh beams ls)" >&2; exit 1; }
fi
echo "==> beam: $BEAM"

# `beams add` can return before the beam is reachable
ready=""
for _ in $(seq 1 24); do
  if tsh beams exec "$BEAM" true >/dev/null 2>&1; then ready=1; break; fi
  sleep "${POLL_SECONDS:-5}"
done
[[ -n "$ready" ]] || { echo "beam $BEAM not reachable after 2 minutes" >&2; exit 1; }

tsh beams scp "$HERE/beam_mcp_setup.sh" "$BEAM:/home/beams/beam_mcp_setup.sh"
# ONE quoted string: tsh parses any later "-x"-style arg as its own flag otherwise
q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
CMD="bash /home/beams/beam_mcp_setup.sh --dir $(q "$DIR")"
[[ -n "$LABELS" ]] && CMD="$CMD --labels $(q "$LABELS")"
[[ -n "$ALLOW" ]] && CMD="$CMD --allow $(q "$ALLOW")"
[[ -n "$DENY" ]] && CMD="$CMD --deny $(q "$DENY")"
[[ -n "$NO_HANDSHAKE" ]] && CMD="$CMD --no-handshake"
tsh beams exec "$BEAM" "$CMD"

echo
echo "Attach:  tsh beams ssh $BEAM   then   cd $DIR && claude"
