#!/usr/bin/env bash
# Does the car stop when the robot server dies hard? Proves ExecStopPost on the real Pi.
#
# Run ON THE PI as root, with the car on blocks (wheels in the air). The wheels WILL spin for a few seconds.
#   sudo bash check_stop_on_kill.sh kill      # SIGKILL the server mid-drive (what the kernel OOM killer sends)
#   sudo bash check_stop_on_kill.sh term      # plain SIGTERM mid-drive
#   sudo bash check_stop_on_kill.sh control   # SIGKILL with ExecStopPost disabled: the wheels SHOULD keep spinning
# `control` shows the check can fail: if the wheels stop by themselves there, the HAT does not hold its last PWM
# and ExecStopPost is not what is protecting you. In every mode Restart is switched off for the test so the server's
# own zero-at-startup cannot hide the result. The script drives IN REVERSE (the forward ultrasonic stop cannot block
# it), checks from telemetry that the car really started moving before it kills anything, and refuses to run if the
# robot is still latched from an earlier STOP (press Resume first). It removes its override and zeroes the motors
# when it ends.
#
# Env: UNIT (picar-robot)  URL (http://127.0.0.1:8765/mcp)  SPEED (30)
#      PY, STOP_CMD: default to the robot_hat_lite install under /opt/picar-beambot if it is there, otherwise to
#      /usr/bin/python3 and the SunFounder `picarx` library.
set -euo pipefail

MODE="${1:-kill}"; UNIT="${UNIT:-picar-robot}"; URL="${URL:-http://127.0.0.1:8765/mcp}"; SPEED="${SPEED:-30}"
if [ -f /opt/picar-beambot/robot_hat_lite.py ]; then
  PY="${PY:-/opt/picar-beambot/venv/bin/python}"; STOP_CMD="${STOP_CMD:-$PY /opt/picar-beambot/robot_hat_lite.py stop}"; FLAVOUR="robot_hat_lite (/opt/picar-beambot)"
else
  PY="${PY:-/usr/bin/python3}"; STOP_CMD="${STOP_CMD:-$PY -c 'from picarx import Picarx; Picarx().stop()'}"; FLAVOUR="SunFounder picarx library"
fi
case "$MODE" in kill|term|control) ;; *) echo "usage: $0 kill|term|control" >&2; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 2; }
DROPIN_DIR="${DROPIN_ROOT:-/run/systemd/system}/$UNIT.service.d"; DROPIN="$DROPIN_DIR/zz-kill-test.conf"; SETTLE_S="${SETTLE_S:-4}"
echo "unit: $UNIT   python: $PY   motor stop command: $STOP_CMD   ($FLAVOUR)"

cleanup() {
  rc=$?
  rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload || true
  if ! bash -c "$STOP_CMD" >/dev/null 2>&1; then          # never leave the wheels turning, and say so if we could not
    echo "WARNING: could not zero the motors with: $STOP_CMD   CHECK THE WHEELS AND CUT THE POWER IF THEY TURN." >&2
  fi
  systemctl restart "$UNIT" || echo "WARNING: could not restart $UNIT" >&2
  echo "(cleanup done: override removed, motor stop attempted, $UNIT restarted)"
  exit "$rc"
}
trap cleanup EXIT

echo "Car on blocks? Wheels will spin for a few seconds. Ctrl-C now to abort, Enter to go."; read -r _

CONF="$(mktemp)"
{ echo "[Service]"; echo "Restart=no"; [ "$MODE" = control ] && echo "ExecStopPost="; } >"$CONF"
mkdir -p "$DROPIN_DIR"; install -m644 "$CONF" "$DROPIN"; rm -f "$CONF"
systemctl daemon-reload; systemctl restart "$UNIT"; sleep "$SETTLE_S"
systemctl is-active --quiet "$UNIT" || { echo "FAIL: $UNIT is not running"; exit 1; }
echo "ExecStopPost configured for this run:"; systemctl show "$UNIT" -p ExecStopPost | cut -c1-200

echo "-- driving in reverse at speed $SPEED (the server's own 8 s deadline is the safety net) --"
set +e
"$PY" - "$URL" "$SPEED" <<'PYEOF'
import asyncio, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import json

def pose(t):
    p = t.get("pose") or {}
    return (p.get("x_cm"), p.get("y_cm"))

async def telemetry(s):
    res = await s.call_tool("get_telemetry", {})
    sc = getattr(res, "structuredContent", None)
    if sc:
        return sc
    for c in res.content:                      # older mcp 1.x: the dict comes back as JSON text
        try:
            return json.loads(getattr(c, "text", ""))
        except ValueError:
            pass
    return {}

async def main(url, speed):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            before = await telemetry(s)
            if before.get("halted"):
                print("robot is LATCHED from an earlier STOP: press Resume (dashboard) or POST /resume first")
                return 3
            res = await s.call_tool("drive", {"speed": -int(speed), "seconds": 8, "steer_deg": 0, "wait": False})
            text = "".join(getattr(c, "text", "") for c in res.content)[:200]
            print("drive ->", text)
            if res.isError or text.startswith("refused"):
                return 5
            await asyncio.sleep(1.2)
            after = await telemetry(s)
            moved = bool(after.get("moving")) or pose(after) != pose(before)
            print(f"moving={after.get('moving')} pose {pose(before)} -> {pose(after)}")
            return 0 if moved else 4

sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
PYEOF
drive_rc=$?
set -e
case "$drive_rc" in
  0) ;;
  3) echo "INVALID: the car is latched, so nothing was tested. Resume it and run again."; exit 1;;
  4) echo "INVALID: the drive was accepted but the car did NOT move (telemetry shows no motion), so a 'wheels stopped' answer would prove nothing. Check the battery, the wheels and the units, then run again."; exit 1;;
  *) echo "INVALID: could not start a drive (exit $drive_rc), so nothing was tested."; exit 1;;
esac

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
