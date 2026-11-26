#!/usr/bin/env bash
# Deploy BeamBot Control into a Teleport Beam and publish it.
# Run from your laptop after `tsh login` (a normal login, not from inside a beam).
#
#   ./deploy-beam.sh                    create a new beam
#   BEAM=warm-orbit ./deploy-beam.sh    redeploy into an existing beam
#
# Optional env:
#   USE_DBS=auto|1|0       Postgres via Teleport if available, else SQLite in the beam (default auto)
#   USE_REDIS=1            also tunnel Redis through Teleport (no password)
#   REDIS_PASSWORD=...     legacy password-ACL Redis only; written to a 0600 env file on the beam
#   LLM_PROVIDER=openai    openai (default) | anthropic
#   LLM_MODEL=...          override the model (default gpt-6.1-sol / claude-sonnet-5-5)
#   READY_TIMEOUT=180      seconds to wait for :8080 (start.sh runs pip install first)
#   BEAM_ROLES=beam-user,picar-driver,picar-stream,beambot-data   REQUIRED for a new beam: the only roles it will hold (tsh beams add
#                          --roles). They must be eligible for delegation. Without this a beam would inherit ALL
#                          your standing roles (access, editor, auditor...). INHERIT_ROLES=1 opts in to that anyway.
#                          Ignored with BEAM=.
#   ROBOT_MCP_NAME, ROBOT_STREAM_APP   Teleport apps for the robot (ROBOT_STREAM_APP= empty skips the video app)
#   PG_TELEPORT_NAME, PG_DB_NAME, DB_USER, REDIS_TELEPORT_NAME   see start.sh
#
# The app runs in a tmux session so it outlives the exec call; the script waits for :8080, then publishes.
set -euo pipefail
cd "$(dirname "$0")"

APP_DIR=/home/beams/beambot
NOISE_RE='^2.*WARN.*Client tools|^Copied successfully'

log() { echo "==> $*" >&2; }
die() { echo "Error: $*" >&2; exit 1; }
quiet() { "$@" 2> >(grep -Ev "$NOISE_RE" >&2 || true); }

command -v tsh >/dev/null || die "tsh not found"
command -v jq  >/dev/null || die "jq not found"

# ── beam ─────────────────────────────────────────────────────────────────────
if [[ -z "${BEAM:-}" ]]; then
  # A blank list must count as "not set", never reach `tsh beams add --roles=" "` (what that does is up to tsh: it
  # may give the beam every role you hold). Whitespace is stripped; empty entries are an error.
  BEAM_ROLES="${BEAM_ROLES:-}"; BEAM_ROLES="${BEAM_ROLES//[[:space:]]/}"
  if [[ -n "$BEAM_ROLES" && ( "$BEAM_ROLES" == ,* || "$BEAM_ROLES" == *, || "$BEAM_ROLES" == *,,* ) ]]; then
    die "BEAM_ROLES has an empty entry ('$BEAM_ROLES'): write it as role-a,role-b"
  fi
  if [[ -z "${BEAM_ROLES:-}" && "${INHERIT_ROLES:-0}" != "1" ]]; then
    die "set BEAM_ROLES (e.g. BEAM_ROLES=beam-user,picar-driver,picar-stream,beambot-data; beam-user is what allows model calls): otherwise the beam inherits ALL your roles, admin ones included. INHERIT_ROLES=1 to do that anyway."
  fi
  log "Creating beam with roles: ${BEAM_ROLES:-<all of yours>}"
  raw=$(tsh beams add -f json ${BEAM_ROLES:+--roles="$BEAM_ROLES"} 2>&1) || die "tsh beams add failed: ${raw}"
  beam_json=$(echo "$raw" | sed -n '/^{/,/^}/p')
  [[ -n "$beam_json" ]] || die "no JSON from tsh beams add: ${raw}"
  BEAM=$(echo "$beam_json" | jq -er '.alias // .name // .id // .metadata.name // empty') \
    || die "could not parse beam name: ${beam_json}"
fi
log "Beam: ${BEAM}"
elapsed=0
until tsh beams exec "$BEAM" true >/dev/null 2>&1; do
  (( elapsed >= 60 )) && die "beam ${BEAM} not ready after 60s"
  sleep 2; elapsed=$((elapsed + 2))
done

# ── upload ───────────────────────────────────────────────────────────────────
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

COPYFILE_DISABLE=1 tar --no-xattrs -czf "$tmp/beambot.tgz" app.py automap.py requirements.txt start.sh static

expires=$(tsh beams ls -f json 2>/dev/null \
  | jq -r --arg b "$BEAM" '.[]? | select(.id == $b or .alias == $b or .name == $b)
      | (.expires // .expires_at // .spec.expires // empty)' 2>/dev/null || true)

(
  umask 077
  {
    printf 'BEAM_NAME=%q\n' "$BEAM"
    printf 'BEAM_EXPIRES=%q\n' "${expires:-}"
    printf 'USE_DBS=%q\n' "${USE_DBS:-auto}"
    for v in LLM_PROVIDER LLM_MODEL CLAUDE_MODEL OPENAI_REASONING USE_REDIS PG_TELEPORT_NAME PG_DB_NAME REDIS_TELEPORT_NAME DB_USER; do
      [[ -n "${!v:-}" ]] && printf '%s=%q\n' "$v" "${!v}"
    done
    [[ -n "${ROBOT_MCP_NAME:-}" ]] && printf 'ROBOT_MCP_NAME=%q\n' "$ROBOT_MCP_NAME"
    # Set but empty is meaningful here: it means "no video app, skip it". (${v+x} also works on macOS's bash 3.2.)
    [[ -n "${ROBOT_STREAM_APP+x}" ]] && printf 'ROBOT_STREAM_APP=%q\n' "$ROBOT_STREAM_APP"
    [[ -n "${REDIS_PASSWORD:-}" ]] && printf 'REDIS_PASSWORD=%q\n' "$REDIS_PASSWORD"
    true
  } > "$tmp/beambot.env"
)

log "Uploading app to ${BEAM}:${APP_DIR}"
for f in beambot.tgz beambot.env; do   # tsh beams scp takes a single source
  quiet tsh beams scp -q "$tmp/$f" "${BEAM}:/home/beams/$f" >/dev/null
done
quiet tsh beams exec "$BEAM" "rm -rf ${APP_DIR} && mkdir -p ${APP_DIR} \
  && tar xzf /home/beams/beambot.tgz -C ${APP_DIR} && mv /home/beams/beambot.env ${APP_DIR}/.env \
  && chmod 600 ${APP_DIR}/.env && chmod +x ${APP_DIR}/start.sh && rm /home/beams/beambot.tgz"

# ── start ────────────────────────────────────────────────────────────────────
log "Starting app (tmux session 'beambot', log /tmp/beambot.log)"
quiet tsh beams exec "$BEAM" "tmux kill-session -t beambot 2>/dev/null; \
  tmux new-session -d -s beambot 'cd ${APP_DIR} && set -a && . ./.env && set +a && ./start.sh 2>&1 | tee /tmp/beambot.log'"

timeout="${READY_TIMEOUT:-180}"; elapsed=0
until [[ "$(tsh beams exec "$BEAM" "curl -s -o /dev/null -w '%{http_code}' localhost:8080/" 2>/dev/null)" == 200 ]]; do
  if (( elapsed >= timeout )); then
    tsh beams exec "$BEAM" "tail -n 40 /tmp/beambot.log" 2>/dev/null || true
    die "app not serving on :8080 after ${timeout}s"
  fi
  sleep 5; elapsed=$((elapsed + 5))
done
log "App is up"

# ── publish ──────────────────────────────────────────────────────────────────
out=$(tsh beams publish "$BEAM" 2>&1) || die "tsh beams publish failed: ${out}"
URL=$(echo "$out" | awk '/^URL:/ { print $2; exit }')
[[ -n "$URL" ]] || URL=$(echo "$out" | grep -Eo 'https://[^ ]+' | head -1 || true)
[[ -n "$URL" ]] || die "could not parse URL from publish output: ${out}"

echo "Beam: ${BEAM}"
echo "URL:  ${URL}"

echo "Logs:  tsh beams exec ${BEAM} tail -f /tmp/beambot.log"
