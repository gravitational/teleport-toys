"""check_mcp_bypass.sh must only say PASS when Teleport visibly denied the call. Every other outcome is a FAIL
(the call got through) or INCONCLUSIVE (nothing was proven): the false-PASS cases the review found."""
import os
import stat
import subprocess
import sys

import pytest

from conftest import ROOT, HELPERS, free_port

SCRIPT = ROOT / "tests/live/check_mcp_bypass.sh"


@pytest.fixture
def run_check(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    w = bin_dir / "tsh"
    w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPERS / "fake_proxy_tsh.py"}" "$@"\n')
    w.chmod(w.stat().st_mode | stat.S_IEXEC)

    def run(mode, *args, **env):
        e = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_MODE": mode,
             "PORT": str(free_port()), **{k: str(v) for k, v in env.items()}}
        p = subprocess.run(["bash", str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr
    return run


def test_a_real_teleport_denial_passes(run_check):
    rc, out = run_check("good")
    assert rc == 0 and "RESULT: PASS" in out, out
    assert "denied by Teleport RBAC" in out and "POST /mcp on picar-stream -> 404" in out


def test_a_403_from_teleport_on_initialize_passes(run_check):
    rc, out = run_check("denied_init", FAKE_STREAM="missing")
    assert rc == 0 and "RESULT: PASS" in out, out


@pytest.mark.parametrize("mode", ["leaky_list", "leaky_call", "latched"])
def test_a_call_that_reaches_the_robot_fails(run_check, mode):
    rc, out = run_check(mode)
    assert rc == 1 and "RESULT: FAIL" in out, out
    if mode == "latched":
        assert "REACHED THE ROBOT" in out, "a refusal by the robot's own latch is not a Teleport denial"


@pytest.mark.parametrize("mode", ["odd_error", "empty_list", "wrong_list", "dead", "refuses"])
def test_anything_unproven_is_inconclusive_not_a_pass(run_check, mode):
    rc, out = run_check(mode)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and "RESULT: PASS" not in out, out


def test_a_proxy_that_never_listens_is_inconclusive(run_check):
    rc, out = run_check("silent")
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and "RESULT: PASS" not in out, out


def test_a_skipped_stream_check_is_said_out_loud_and_can_be_required(run_check):
    rc, out = run_check("good", FAKE_STREAM="missing")
    assert rc == 0 and "SKIPPED" in out and "1 skipped" in out, out        # a beam without picar-stream
    rc, out = run_check("good", FAKE_STREAM="missing", REQUIRE_STREAM=1)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out, out


# --------------------------------------------------------------------------- the check must never clear a STOP
def _calls(path):
    return path.read_text().split() if path.exists() else []


def test_a_latched_robot_is_never_sent_resume(run_check, tmp_path):
    calls = tmp_path / "calls.txt"
    rc, out = run_check("leaky_call", FAKE_HALTED=1, FAKE_CALLS=calls)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and "LATCHED" in out, out
    assert "resume" not in _calls(calls), "the check sent resume to a latched robot"


def test_an_unreadable_latch_is_never_sent_resume(run_check, tmp_path):
    calls = tmp_path / "calls.txt"
    rc, out = run_check("leaky_call", FAKE_HALTED="missing", FAKE_CALLS=calls)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and "could not read the latch" in out, out
    assert "resume" not in _calls(calls)


def test_a_list_that_shows_resume_fails_without_calling_it(run_check, tmp_path):
    calls = tmp_path / "calls.txt"
    rc, out = run_check("leaky_list", FAKE_CALLS=calls)
    assert rc == 1 and "RESULT: FAIL" in out, out
    assert "resume" not in _calls(calls)


@pytest.fixture
def sim():
    import httpx
    from types import SimpleNamespace
    from conftest import ROBOT_DIR, _clean_env, wait_robot_ready
    port, sport = free_port(), free_port()
    p = subprocess.Popen([sys.executable, str(ROBOT_DIR / "robot_mcp.py")], cwd=ROBOT_DIR,
                         env=_clean_env(SIMULATE=1, ROBOT_PORT=port, STREAM_PORT=sport, SAY_CMD="true", HALT_FILE=""),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_robot_ready(f"http://127.0.0.1:{sport}")
    halted = lambda: httpx.get(f"http://127.0.0.1:{sport}/telemetry").json()["halted"]  # noqa: E731
    yield SimpleNamespace(mcp=f"http://127.0.0.1:{port}/mcp", stop=lambda: httpx.post(f"http://127.0.0.1:{sport}/stop"), halted=halted)
    p.terminate()
    p.wait(5)


@pytest.mark.parametrize("mode", ["forward", "forward_hide"])
def test_the_reviewers_bypass_against_a_latched_simulator_keeps_the_stop(run_check, sim, mode):
    """A plain proxy to the simulator's unfiltered /mcp, robot latched through /stop: the check must not lift it."""
    sim.stop()
    assert sim.halted() is True
    rc, out = run_check(mode, FAKE_UPSTREAM=sim.mcp, FAKE_STREAM="missing")
    assert "RESULT: PASS" not in out, out
    assert rc == (1 if mode == "forward" else 2), out
    assert sim.halted() is True, "running the check cleared the STOP:\n" + out


def test_an_unlatched_simulator_behind_a_list_only_filter_fails(run_check, sim):
    """The gate still lets the check prove the bypass when nobody has pressed STOP."""
    assert sim.halted() is False
    rc, out = run_check("forward_hide", FAKE_UPSTREAM=sim.mcp, FAKE_STREAM="missing")
    assert rc == 1 and "halted=false" in out and "REACHED THE ROBOT" in out, out


# --------------------------------------------------------------------------- robot/check_stop_on_kill.sh
KILL_SCRIPT = ROOT / "examples/picar-beambot/robot/check_stop_on_kill.sh"


@pytest.fixture
def kill_check(tmp_path):
    """Runs check_stop_on_kill.sh with a fake systemctl, id and journalctl in front of a real simulated robot."""
    import subprocess as sp
    import time
    from conftest import ROBOT_DIR, _clean_env, wait_http, wait_robot_ready
    bin_dir, log, pidfile = tmp_path / "bin", tmp_path / "systemctl.log", tmp_path / "pid"
    bin_dir.mkdir()

    def script(name, body):
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    script("id", "echo 0\n")
    script("journalctl", "echo fake journal\n")
    # is-active says whether the robot process is really alive (a zombie counts as dead). kill notes whether the robot
    # was still driving at that moment; FAKE_KILL=noop makes it do nothing. FAKE_ENV is the unit's Environment= line.
    moving = "import json,sys,urllib.request; print('moving_at_kill=%s' % json.load(urllib.request.urlopen(sys.argv[1], timeout=5))['moving'])"
    script("systemctl", f'echo "$@" >> {log}\ncase "$1" in '
                        f'is-active) case "$(ps -o stat= -p "$(cat {pidfile})" 2>/dev/null)" in ""|Z*) s=failed;; *) s=active;; esac; '
                        f'[ "$2" = --quiet ] || echo "$s"; [ "$s" = active ]; exit $?;; '
                        f'show) case "$*" in *Environment*) echo "Environment=$FAKE_ENV";; '
                        f'*) echo "ExecStopPost={{ path=/x ; status=0/SUCCESS }}";; esac;; '
                        f'kill) "$PY" -c "{moving}" "$FAKE_TELEMETRY" >> {log}; '
                        f'[ "$FAKE_KILL" = noop ] || kill -9 "$(cat {pidfile})";; esac\nexit 0\n')
    procs = []

    def run(robot=None, answers="\ny\ny\n", **env):
        port, sport = free_port(), free_port()
        srv = ROOT / "tests/helpers/counting_robot.py" if robot == "counting" else ROBOT_DIR / "robot_mcp.py"
        cmd = [sys.executable, str(srv)] + ([str(port), str(tmp_path / "calls.txt")] if robot == "counting" else [])
        e = _clean_env(SIMULATE=1, ROBOT_PORT=port, STREAM_PORT=sport, SAY_CMD="true")
        p = sp.Popen(cmd, cwd=ROBOT_DIR, env=e, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        procs.append(p)
        if robot == "counting":
            wait_http(f"http://127.0.0.1:{port}/")
            time.sleep(0.6)
        else:
            wait_robot_ready(f"http://127.0.0.1:{sport}")
        pidfile.write_text(str(p.pid))
        if robot == "latched":
            import httpx
            httpx.post(f"http://127.0.0.1:{sport}/stop")
        log.write_text("")
        cmd = ["bash", str(KILL_SCRIPT), *env.pop("args", ["kill"])]
        senv = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "URL": f"http://127.0.0.1:{port}/mcp",
                "PY": sys.executable, "STOP_CMD": env.pop("STOP_CMD", "true"), "DROPIN_ROOT": str(tmp_path / "dropins"),
                "SETTLE_S": "0.2", "FAKE_TELEMETRY": f"http://127.0.0.1:{sport}/telemetry", **env}
        if callable(answers):        # an operator who takes their time: answers(proc) talks to the script, returns its output
            proc = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.STDOUT, env=senv)
            out = answers(proc)
            return proc.wait(60), out, log.read_text()
        r = sp.run(cmd, input=answers, capture_output=True, text=True, timeout=60, env=senv)
        return r.returncode, r.stdout + r.stderr, log.read_text()
    yield run
    for p in procs:
        p.kill()


def test_a_kill_check_that_saw_the_car_move_and_the_wheels_stop_passes(kill_check):
    rc, out, calls = kill_check()
    assert rc == 0 and "RESULT: PASS" in out and "moving=True" in out, out
    assert "kill -s KILL" in calls and "ExecStopPost" in out


def test_a_latched_car_is_an_invalid_test_not_a_pass(kill_check):
    rc, out, calls = kill_check(robot="latched", answers="\ny\n")
    assert rc == 1 and "INVALID" in out and "LATCHED" in out and "RESULT: PASS" not in out, out
    assert "kill" not in calls, "it must not kill anything when the test cannot be valid"


def test_a_car_that_did_not_move_is_an_invalid_test_not_a_pass(kill_check):
    rc, out, calls = kill_check(robot="counting", answers="\ny\n")
    assert rc == 1 and "did NOT move" in out and "RESULT: PASS" not in out, out
    assert "kill -s" not in calls


def test_a_motor_stop_that_fails_at_the_end_says_so(kill_check):
    rc, out, _ = kill_check(STOP_CMD="false")
    assert "WARNING: could not zero the motors" in out and "CUT THE POWER" in out, out


def test_the_control_run_expects_the_wheels_to_keep_turning(kill_check):
    rc, out, _ = kill_check(args=["control"], answers="\ny\nn\n")
    assert rc == 0 and "control behaved as expected" in out, out


def test_the_script_uses_a_temp_file_and_both_stop_flavours():
    text = KILL_SCRIPT.read_text()
    assert "/tmp/zz-kill-test.conf" not in text and "mktemp" in text
    assert "robot_hat_lite.py stop" in text and "picarx import Picarx" in text
