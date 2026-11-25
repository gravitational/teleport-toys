#!/usr/bin/env bash
# Does the HAT stop when the robot server dies hard? Proves ExecStopPost on the real Pi.
#
# Run ON THE PI as root, with the car on blocks (wheels in the air). The wheels WILL spin for a few seconds.
#   sudo bash check_stop_on_kill.sh kill      # SIGKILL the server mid-drive (what the kernel OOM killer sends)
#   sudo bash check_stop_on_kill.sh term      # plain SIGTERM mid-drive
#   sudo bash check_stop_on_kill.sh control   # SIGKILL with ExecStopPost disabled: the wheels SHOULD keep spinning
# `control` shows the check can fail: if the wheels stop by themselves there, the HAT does not hold its last PWM
# and ExecStopPost is not what is protecting you. In every mode Restart is switched off for the test so the server's
# own zero-at-startup cannot hide the result. The script removes its override and stops the motors when it ends.
#
# Env: UNIT (picar-robot)  PY (/opt/picar-beambot/venv/bin/python)  STOP_CMD (<PY> /opt/picar-beambot/robot_hat_lite.py stop)
#      URL (http://127.0.0.1:8765/mcp)  SPEED (30)
set -euo pipefail

MODE="${1:-kill}"; UNIT="${UNIT:-picar-robot}"; PY="${PY:-/opt/picar-beambot/venv/bin/python}"
STOP_CMD="${STOP_CMD:-$PY /opt/picar-beambot/robot_hat_lite.py stop}"; URL="${URL:-http://127.0.0.1:8765/mcp}"; SPEED="${SPEED:-30}"
case "$MODE" in kill|term|control) ;; *) echo "usage: $0 kill|term|control" >&2; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
DROPIN_DIR="/run/systemd/system/$UNIT.service.d"; DROPIN="$DROPIN_DIR/zz-kill-test.conf"

cleanup() {
  rc=$?
  rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload || true
  # shellcheck disable=SC2086
  $STOP_CMD >/dev/null 2>&1 || true            # never leave the wheels turning
  systemctl restart "$UNIT" || true
  echo "(cleanup done: override removed, motors zeroed, $UNIT restarted)"
  exit "$rc"
}
trap cleanup EXIT

echo "Car on blocks? Wheels will spin for a few seconds. Ctrl-C now to abort, Enter to go."; read -r _

{ echo "[Service]"; echo "Restart=no"; [ "$MODE" = control ] && echo "ExecStopPost="; } >/tmp/zz-kill-test.conf
mkdir -p "$DROPIN_DIR"; install -m644 /tmp/zz-kill-test.conf "$DROPIN"; rm -f /tmp/zz-kill-test.conf
systemctl daemon-reload; systemctl restart "$UNIT"; sleep 4
systemctl is-active --quiet "$UNIT" || { echo "FAIL: $UNIT is not running"; exit 1; }
echo "ExecStopPost configured for this run:"; systemctl show "$UNIT" -p ExecStopPost | cut -c1-200

echo "-- driving at speed $SPEED for 8 s (the server's own deadline is the safety net) --"
"$PY" - "$URL" "$SPEED" <<'PYEOF'
import asyncio, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main(url, speed):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("drive", {"speed": int(speed), "seconds": 8, "steer_deg": 0, "wait": False})
            print("drive ->", "".join(getattr(c, "text", "") for c in res.content)[:200])
asyncio.run(main(sys.argv[1], sys.argv[2]))
PYEOF
sleep 1.5

case "$MODE" in
  term) echo "-- SIGTERM --"; systemctl kill -s TERM --kill-whom=main "$UNIT";;
  *)    echo "-- SIGKILL --"; systemctl kill -s KILL --kill-whom=main "$UNIT";;
esac
sleep 1
echo "unit result: $(systemctl show "$UNIT" -p Result,ActiveState,ExecMainStatus | paste -sd' ' -)"
echo "ExecStopPost record:"; systemctl show "$UNIT" -p ExecStopPost | grep -o 'status=[^;}]*\|code=[a-z]*' | paste -sd' ' - || true
echo "journal:"; journalctl -u "$UNIT" --since "-30s" --no-pager -o cat | tail -8

echo
read -r -p "Look at the wheels NOW. Did they STOP within about a second of the kill? [y/n] " ans
case "$MODE:$ans" in
  control:n|control:N) echo "RESULT: control behaved as expected: the HAT keeps its last PWM duty, so ExecStopPost is what stops the car.";;
  control:*)           echo "RESULT: INTERESTING: wheels stopped with ExecStopPost disabled. The HAT does not hold PWM; ExecStopPost is not needed on this hardware."; exit 0;;
  *:y|*:Y)             echo "RESULT: PASS: the wheels stopped after the server died ($MODE).";;
  *)                   echo "RESULT: FAIL: the wheels kept turning after the server died ($MODE). Fix before trusting the car unattended."; exit 1;;
esac
