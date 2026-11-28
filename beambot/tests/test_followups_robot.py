"""Review follow-ups after PR #14 for the PiCar robot server and its tooling (robot_mcp.py, check_stop_on_kill.sh)."""
import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_live_check_scripts import KILL_SCRIPT, kill_check  # noqa: F401  (kill_check is a fixture)
from test_review_findings import new_robot, own_robot, rm  # noqa: F401  (own_robot and rm are fixtures)


def wait_for_writer(r, timeout=5):
    """Until the latch writer has handled every STOP so far."""
    with r.save_cv:
        assert r.save_cv.wait_for(lambda: r.save_done >= r.save_req, timeout=timeout), "the latch writer never answered"


# --------------------------------------------------------------------------- say(): text is never an option
# Parses its arguments the way espeak-ng does (GNU getopt, the same option string) and writes what it would have said.
FAKE_ESPEAK = r'''
import getopt, sys
opts, args = getopt.gnu_getopt(sys.argv[1:], "a:b:d:f:g:hk:l:mp:qs:v:w:xXz")
said = "".join(open(v).read() for k, v in opts if k == "-f") or " ".join(args)   # -f FILE: speak the file
open(sys.argv[0] + ".said", "w").write(said)
'''


def test_say_text_starting_with_a_dash_is_spoken_not_read_as_an_option(rm, monkeypatch, tmp_path):
    """say("-fteleport.yaml") made espeak-ng read the config file aloud."""
    fake = tmp_path / "espeak.py"
    fake.write_text(FAKE_ESPEAK)
    (tmp_path / "secret.yaml").write_text("join_token: not-for-the-speaker\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rm, "SAY_CMD", f"{shlex.quote(sys.executable)} {shlex.quote(str(fake))} -a 200 -s 150 -v en")
    r = new_robot(rm)
    r.car.speaker = lambda on: None
    with monkeypatch.context() as m:               # the simulator never runs the speech command: act like the HAT
        m.setattr(rm, "SIMULATE", False)
        m.setattr(rm, "HW", "hat")
        r.say("-fsecret.yaml")
    said = (tmp_path / "espeak.py.said").read_text()
    assert said == "-fsecret.yaml", f"the text was read as an option; the speaker said {said!r}"


# --------------------------------------------------------------------------- the latch writer
def test_the_latch_writer_survives_an_unexpected_error_and_reports_it(rm, monkeypatch, tmp_path):
    """An exception _save_halt doesn't catch used to kill the writer: every later STOP waited 0.3 s, said "still being
    saved" forever and wrote nothing."""
    f = tmp_path / "halted"
    monkeypatch.setattr(rm, "HALT_FILE", str(f))
    r = new_robot(rm)
    with monkeypatch.context() as m:
        def broken(on):
            raise ValueError("not an OSError")
        m.setattr(rm.Robot, "_save_halt", staticmethod(broken))
        text = r.emergency_stop()
    assert "could not be saved" in text and "not an OSError" in text, text
    t = r.telemetry()
    assert t["halted"] is True and t["latch_saved"] is False and "not an OSError" in t["latch_error"], t
    second = r.emergency_stop()
    wait_for_writer(r)                                 # a real fsync: don't count on it beating the 0.3 s answer
    assert "could not" not in second and f.exists() and r.halt_saved, f"the writer died, the next STOP saved nothing: {second}"
    assert r.telemetry()["latch_error"] == ""


def test_a_latch_save_that_fails_slowly_is_reported(rm, monkeypatch, tmp_path):
    """EIO after 0.5 s: the STOP has already answered "still being saved". The error must still reach the operator."""
    monkeypatch.setattr(rm, "HALT_FILE", str(tmp_path / "halted"))

    def slow_eio(on):
        time.sleep(0.5)
        return "[Errno 5] Input/output error"
    monkeypatch.setattr(rm.Robot, "_save_halt", staticmethod(slow_eio))
    r = new_robot(rm)
    assert "still being saved" in r.emergency_stop()
    wait_for_writer(r)
    t = r.telemetry()
    assert t["latch_saved"] is False and "Input/output error" in t["latch_error"], t
    second = r.emergency_stop()                       # its own save fails slowly too
    assert "still being saved" in second and "Input/output error" in second, second


def test_no_latch_file_means_latch_saved_is_false(own_robot):
    """HALT_FILE empty: nothing survives a restart, so telemetry must not say the latch is saved."""
    import httpx
    r = own_robot(HALT_FILE="")
    resp = httpx.post(f"{r.stream}/stop", timeout=10)
    assert resp.status_code == 200 and resp.json()["result"] == "stopped", resp.text
    t = httpx.get(f"{r.stream}/telemetry", timeout=10).json()
    assert t["halted"] is True and t["latch_saved"] is False, t


# --------------------------------------------------------------------------- the stream /stop has its own threads
def test_stream_stop_does_not_wait_for_a_busy_thread_pool(rm, monkeypatch):
    """The stream app's /stop runs on the STOP pool too: with the default thread pool full it still stops the car."""
    r = new_robot(rm)
    monkeypatch.setattr(rm, "robot", r)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        release = threading.Event()
        busy = [loop.run_in_executor(None, release.wait, 10) for _ in range(2)]   # every default thread is taken
        try:
            r.drive(-20, 3, 0, wait=False)
            assert r.car.speed == -20
            t0 = time.time()
            resp = await asyncio.wait_for(rm.stop_route(None), 2)
            return resp, time.time() - t0
        finally:
            release.set()
            await asyncio.gather(*busy)

    resp, took = asyncio.run(scenario())
    body = json.loads(resp.body)
    assert resp.status_code == 200 and body["halted"] is True and body["result"].startswith("stopped"), body
    assert took < 1.0 and r.car.speed == 0 and r.halted, took


# --------------------------------------------------------------------------- check_stop_on_kill.sh
def test_kill_check_asks_whether_the_wheels_turn_before_the_kill(kill_check):
    """Telemetry "moving" is the commanded speed: the simulator has no wheels and still says moving=True."""
    rc, out, calls = kill_check(answers="\nn\ny\n")
    assert "moving=True" in out and "TURNING" in out, out
    assert rc == 1 and "INVALID" in out and "RESULT: PASS" not in out, out
    assert "kill -s" not in calls, "it must not kill anything when nobody saw the wheels turn"


def test_kill_check_is_invalid_when_the_unit_did_not_die(kill_check):
    rc, out, calls = kill_check(FAKE_KILL="noop")
    assert "kill -s KILL" in calls
    assert rc == 1 and "INVALID" in out and "did not die" in out and "RESULT: PASS" not in out, out


def test_kill_check_kills_mid_drive_however_long_the_operator_takes(kill_check):
    """Drives are capped at 3 s on the robot. An operator who answers after 4 s must still see a kill mid-drive."""
    def slow_operator(proc):
        out, asked = [], threading.Event()

        def pump():
            for chunk in iter(lambda: proc.stdout.read1(256), b""):
                out.append(chunk)
                if b"TURNING" in b"".join(out):
                    asked.set()
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        if asked.wait(30):                              # never asked: close stdin so the script ends instead of hanging
            time.sleep(4)
            proc.stdin.write(b"y\ny\n")
        proc.stdin.close()
        t.join(60)
        return b"".join(out).decode()
    rc, out, calls = kill_check(answers=slow_operator)
    assert rc == 0 and "RESULT: PASS" in out, out
    assert "moving_at_kill=True" in calls, calls
    assert "8 s" not in KILL_SCRIPT.read_text(), "drives are capped at 3 s (MAX_DRIVE_SECONDS), not 8"


@pytest.mark.parametrize("unit_env, expect", [("HW=hat MAX_SPEED=40", "robot_hat_lite.py stop"),
                                              ("MAX_SPEED=40", "from picarx import Picarx")])
def test_kill_check_picks_the_motor_stop_by_what_the_unit_runs(tmp_path, unit_env, expect):
    """Not by whether /opt/picar-beambot/robot_hat_lite.py happens to exist."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("id", "echo 0\n"), ("py", "exit 0\n"),       # py: the cleanup's motor stop must not run for real
                       ("systemctl", f'case "$1" in show) echo "Environment={unit_env}";; esac\n')):
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), "PY": str(bin_dir / "py"),
           "DROPIN_ROOT": str(tmp_path / "dropins")}
    p = subprocess.run(["bash", str(KILL_SCRIPT), "kill"], input="", capture_output=True, text=True, timeout=60, env=env)
    first = (p.stdout + p.stderr).splitlines()[0]   # stdin is empty: it stops at "Enter to go"
    assert first.startswith("unit: picar-robot") and expect in first, first
