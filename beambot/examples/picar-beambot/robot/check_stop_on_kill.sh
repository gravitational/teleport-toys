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
# it), checks from telemetry that the server accepted the drive, then asks YOU whether the wheels are turning before it
# kills anything (telemetry only shows the commanded speed, so it can't see the wheels). It keeps re-sending the drive
# while you look, so the kill lands mid-drive. It checks the unit really died before it asks whether the wheels stopped,
# and refuses to run if the robot is still latched from an earlier STOP (press Resume first). It removes its override
# and zeroes the motors when it ends.
#
# Env: UNIT (picar-robot)  URL (http://127.0.0.1:8765/mcp)  SPEED (30)
#      PY, STOP_CMD: default to the robot_hat_lite install under /opt/picar-beambot if the unit sets HW=hat
#      (picar-robot-hat.service), otherwise to /usr/bin/python3 and the SunFounder `picarx` library.
set -euo pipefail

MODE="${1:-kill}"; UNIT="${UNIT:-picar-robot}"; URL="${URL:-http://127.0.0.1:8765/mcp}"; SPEED="${SPEED:-30}"
UNIT_ENV="$(systemctl show -p Environment "$UNIT" 2>/dev/null || true)"    # what the unit runs: HW=hat is robot_hat_lite
case " $UNIT_ENV " in
  *"=HW=hat "*|*" HW=hat "*)
    PY="${PY:-/opt/picar-beambot/venv/bin/python}"; STOP_CMD="${STOP_CMD:-$PY /opt/picar-beambot/robot_hat_lite.py stop}"; FLAVOUR="robot_hat_lite: the unit sets HW=hat";;
  *)
    PY="${PY:-/usr/bin/python3}"; STOP_CMD="${STOP_CMD:-$PY -c 'from picarx import Picarx; Picarx().stop()'}"; FLAVOUR="SunFounder picarx library: the unit does not set HW=hat";;
esac
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

echo "-- driving in reverse at speed $SPEED (the server stops each drive after 3 s, MAX_DRIVE_SECONDS: it is re-sent every second until you answer) --"
set +e
"$PY" - "$URL" "$SPEED" 3<&0 <<'PYEOF'
import asyncio, os, sys, threading
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import json

def ask(prompt):
    """The operator's answer from the script's stdin (fd 3), read in the background one byte at a time, so the answer
    to the next question stays there for bash."""
    done, box = threading.Event(), []
    def read():
        line = b""
        while not line.endswith(b"\n"):
            c = os.read(3, 1)
            if not c:
                break
            line += c
        box.append(line.decode(errors="replace").strip())
        done.set()
    print(prompt, end="", flush=True)
    threading.Thread(target=read, daemon=True).start()
    return done, box

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
            async def drive():
                res = await s.call_tool("drive", {"speed": -int(speed), "seconds": 3, "steer_deg": 0, "wait": False})
                text = "".join(getattr(c, "text", "") for c in res.content)[:200]
                return text, res.isError or text.startswith("refused")

            text, refused = await drive()
            print("drive ->", text)
            if refused:
                return 5
            await asyncio.sleep(1.2)
            after = await telemetry(s)
            moved = bool(after.get("moving")) or pose(after) != pose(before)
            print(f"moving={after.get('moving')} pose {pose(before)} -> {pose(after)}")
            if not moved:
                return 4
            # Telemetry is the commanded speed and a dead-reckoned pose: only the operator can see the wheels turn.
            done, box = ask("Look at the wheels. Are they TURNING now? [y/n] ")
            for _ in range(60):                  # keep driving while they look, 60 s at most
                if await asyncio.to_thread(done.wait, 1.0):
                    break
                text, refused = await drive()
                if refused:
                    print("\ndrive ->", text)
                    return 5
            else:
                print("\nno answer in 60 s")
                return 6
            if box[0].lower() != "y":
                return 6
            text, refused = await drive()        # a fresh 3 s, so the kill lands mid-drive
            return 5 if refused else 0

sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
PYEOF
drive_rc=$?
set -e
case "$drive_rc" in
  0) ;;
  3) echo "INVALID: the car is latched, so nothing was tested. Resume it and run again."; exit 1;;
  4) echo "INVALID: the drive was accepted but the car did NOT move (telemetry shows no motion), so a 'wheels stopped' answer would prove nothing. Check the battery, the wheels and the units, then run again."; exit 1;;
  6) echo "INVALID: you did not see the wheels turning, so a 'wheels stopped' answer would prove nothing. Check the battery, the wheels and the units, then run again."; exit 1;;
  *) echo "INVALID: could not start a drive (exit $drive_rc), so nothing was tested."; exit 1;;
esac

case "$MODE" in
  term) echo "-- SIGTERM --"; systemctl kill -s TERM --kill-whom=main "$UNIT";;
  *)    echo "-- SIGKILL --"; systemctl kill -s KILL --kill-whom=main "$UNIT";;
esac
sleep 1
state="$(systemctl is-active "$UNIT" || true)"; i=0
while [ "$i" -lt 50 ]; do                # up to 10 s more for systemd to see the server gone
  case "$state" in active|activating|reloading) ;; *) break;; esac
  sleep 0.2; i=$((i + 1)); state="$(systemctl is-active "$UNIT" || true)"
done
case "$state" in      # deactivating: the server is gone and ExecStopPost is still running
  active|activating|reloading) echo "INVALID: $UNIT is still $state after the kill: the server did not die, so nothing was tested."; exit 1;;
esac
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
