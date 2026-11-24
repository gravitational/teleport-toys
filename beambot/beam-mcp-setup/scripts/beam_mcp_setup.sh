#!/usr/bin/env bash
# Runs INSIDE a Teleport Beam. Wires Claude Code to Teleport-protected MCP servers.
#
# A beam's `tsh` is preinstalled and already logged in with the identity delegated from the user who
# created the beam, so there is nothing to enroll: this script lists the MCP servers that identity can
# reach, writes the Claude Code config, adds allow/deny rules, and proves it with a real MCP handshake.
#
# Usage:
#   bash beam_mcp_setup.sh --labels robot=beambot-sim [--allow 'sense_*,safety_*'] [--deny 'admin_*'] [--dir ~/work]
#
# Idempotent: safe to re-run (e.g. after the person who owns the beam changes what the beam may do).
# Env: HANDSHAKE_WAIT (seconds to let the MCP server answer, default 6)
set -uo pipefail

LABELS="" ALLOW="" DENY="" DIR="$HOME/work" HANDSHAKE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --labels) LABELS="$2"; shift 2 ;;
    --allow) ALLOW="$2"; shift 2 ;;
    --deny) DENY="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --no-handshake) HANDSHAKE=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
mkdir -p "$DIR"

# ------------------------------------------------------------------ 1. identity
say "identity"
command -v tsh >/dev/null || die "tsh not found: are you inside a beam?"
tsh status || die "tsh has no active session. Run this inside a beam (tsh beams ssh <name>), or after tsh login."
VER=$(tsh version 2>/dev/null | sed -n 's/^Teleport v\([0-9][0-9.]*\).*/\1/p' | head -1)
if [[ -n "$VER" && "$(printf '%s\n18.11.9\n' "$VER" | sort -V | head -1)" != "18.11.9" ]]; then
  echo "WARN: tsh $VER is older than 18.11.9, the minimum the Beams docs list." >&2
fi

# ------------------------------------------------------------------ 2. what can this identity reach?
say "MCP servers visible to this beam${LABELS:+ ($LABELS)}"
tsh mcp ls ${LABELS:+"$LABELS"} -v || die "tsh mcp ls failed. Does the beam's owner have roles that grant MCP access to these apps?"

# ------------------------------------------------------------------ 3. Claude Code MCP config
say "writing $DIR/mcp.json"
[[ -f "$DIR/mcp.json" ]] || echo '{"mcpServers":{}}' > "$DIR/mcp.json"
if [[ -n "$LABELS" ]]; then
  tsh mcp config --labels "$LABELS" --client-config="$DIR/mcp.json" >/dev/null || die "tsh mcp config failed"
else
  tsh mcp config --all --client-config="$DIR/mcp.json" >/dev/null || die "tsh mcp config failed"
fi

python3 - "$DIR" "$ALLOW" "$DENY" <<'PY'
import json, os, sys
d, allow, deny = sys.argv[1:4]
p = os.path.join(d, "mcp.json")
cfg = json.load(open(p))
servers = cfg.get("mcpServers", {})
for s in servers.values():
    s.setdefault("type", "stdio")
json.dump(cfg, open(p, "w"), indent=2)

# Claude Code permissions. Allow globs are only valid AFTER a literal mcp__<server>__ prefix,
# so expand each pattern per server. Deny rules may use wildcards anywhere.
pats = lambda s: [x.strip() for x in s.split(",") if x.strip()]
allow_rules = ([f"mcp__{n}__{t}" for n in servers for t in pats(allow)] if allow
               else [f"mcp__{n}" for n in servers])
deny_rules = [f"mcp__{n}__{t}" for n in servers for t in pats(deny)]
os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
sp = os.path.join(d, ".claude", "settings.json")
settings = json.load(open(sp)) if os.path.exists(sp) else {}
perm = settings.setdefault("permissions", {})
perm["allow"] = sorted(set(perm.get("allow", [])) | set(allow_rules))
if deny_rules:
    perm["deny"] = sorted(set(perm.get("deny", [])) | set(deny_rules))
settings["enableAllProjectMcpServers"] = True
json.dump(settings, open(sp, "w"), indent=2)
with open(os.path.join(d, "allowed_tools.txt"), "w") as f:
    f.write("\n".join(allow_rules) + "\n")
print(f"{len(servers)} servers wired; {len(allow_rules)} allow / {len(deny_rules)} deny rules -> {sp}")
PY
[[ $? -eq 0 ]] || die "could not write the Claude Code config"
cp "$DIR/mcp.json" "$DIR/.mcp.json"   # project-scoped config Claude Code picks up automatically

# ------------------------------------------------------------------ 4. live MCP handshake
if [[ $HANDSHAKE -eq 1 ]]; then
  say "MCP handshake test"
  FIRST=$(python3 -c 'import json,sys;s=json.load(open(sys.argv[1]))["mcpServers"];print(next(iter(s)) if s else "")' "$DIR/mcp.json")
  APP=${FIRST#teleport-mcp-}
  if [[ -z "$APP" ]]; then
    die "tsh mcp config wrote no servers: nothing matched${LABELS:+ '$LABELS'} for this identity"
  fi
  INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"setup-check","version":"0"}}}'
  OUT=$( { printf '%s\n' "$INIT"; sleep "${HANDSHAKE_WAIT:-6}"; } | TELEPORT_DEBUG=false timeout 25 tsh mcp connect "$APP" 2>&1 )
  if grep -q '"protocolVersion"' <<<"$OUT"; then
    echo "OK: $APP answered initialize with the beam's identity"
  else
    echo "$OUT" | tail -8
    die "handshake with $APP failed (see above and SKILL.md 'Gotchas')"
  fi
fi

say "done"
cat <<EOT
Claude Code (interactive):   cd $DIR && claude
Claude Code (headless):      cd $DIR && echo "<prompt>" | claude -p --mcp-config ./mcp.json \\
                               --allowedTools \$(paste -sd' ' allowed_tools.txt)
EOT
