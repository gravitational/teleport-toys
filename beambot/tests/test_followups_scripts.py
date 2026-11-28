"""Follow-ups from the PR 14 review: the beam-mcp-setup scripts, the live bypass check, the k8s example's network
policy, test_no_tbot's file walk and the docs. Each fix has a test that fails on d2a71d2 (before the fixes); the two
"does not warn" / "never drops" tests guard against the fixes going too far."""
import http.server
import json
import re
import threading

import pytest
import yaml

import test_beam_mcp_scripts as bms
import test_no_tbot
from conftest import ROOT, free_port
from test_live_check_scripts import run_check, sim  # noqa: F401  (fixtures)

K8S = ROOT / "examples/picar-beambot/k8s"


# --------------------------------------------------------------------------- 1. beam_mcp_up.sh --beam with --roles
def test_up_says_roles_are_ignored_when_reusing_a_beam(teleport, tmp_path):
    """An existing beam keeps the roles it was created with: --roles must not look as if it narrowed it."""
    r = bms.up(teleport, tmp_path, "--beam", "warm-orbit", "--roles", "beam-user,picar-driver", "--labels", "robot=beambot-sim")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "WARN" in r.stderr and "--roles" in r.stderr and "warm-orbit" in r.stderr, r.stderr
    assert teleport.calls("tsh", "beams", "add") == []


def test_up_reusing_a_beam_without_roles_does_not_warn(teleport, tmp_path):
    r = bms.up(teleport, tmp_path, "--beam", "warm-orbit", "--labels", "robot=beambot-sim")
    assert r.returncode == 0 and "--roles" not in r.stderr, r.stderr


# --------------------------------------------------------------------------- 2. re-running beam_mcp_setup.sh
@pytest.mark.parametrize("first", [["--allow", "sense_*,act_*"], []], ids=["wider-allow", "whole-server"])
def test_rerunning_setup_with_a_narrower_allow_drops_the_wider_rules(teleport, tmp_path, first):
    r, work = bms.setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--no-handshake", *first)
    assert r.returncode == 0, r.stdout + r.stderr
    sp = work / ".claude/settings.json"
    s = json.loads(sp.read_text())
    s["permissions"]["allow"] += ["Bash(ls:*)", "mcp__github"]   # rules someone added by hand
    sp.write_text(json.dumps(s))
    cfg = json.loads((work / "mcp.json").read_text())
    cfg["mcpServers"]["github"] = {"type": "stdio", "command": "github-mcp"}   # a server that isn't Teleport's
    (work / "mcp.json").write_text(json.dumps(cfg))

    r, work = bms.setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--no-handshake", "--allow", "sense_*", "--deny", "act_*")
    assert r.returncode == 0, r.stdout + r.stderr
    perms = json.loads(sp.read_text())["permissions"]
    teleport_rules = sorted(a for a in perms["allow"] if a.startswith("mcp__teleport-mcp-"))
    assert teleport_rules == ["mcp__teleport-mcp-beambot-sim-arm__sense_*", "mcp__teleport-mcp-beambot-sim-drive__sense_*"]
    assert "Bash(ls:*)" in perms["allow"] and "mcp__github" in perms["allow"], "other rules are left alone"
    assert "mcp__teleport-mcp-beambot-sim-arm__act_*" in perms["deny"]


def test_rerunning_setup_never_drops_an_earlier_deny(teleport, tmp_path):
    """Deny rules only narrow what the agent may do, so a re-run keeps them."""
    bms.setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--no-handshake", "--deny", "act_*")
    r, work = bms.setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--no-handshake", "--allow", "sense_*")
    assert r.returncode == 0
    assert "mcp__teleport-mcp-beambot-sim-arm__act_*" in json.loads((work / ".claude/settings.json").read_text())["permissions"]["deny"]


# --------------------------------------------------------------------------- 3. k8s: only the Teleport agent reaches the robot
def test_only_the_teleport_agents_namespace_may_reach_the_simulated_robot():
    docs = [d for d in yaml.safe_load_all((K8S / "sim-robot.yaml").read_text()) if d]
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pol = [d for d in docs if d["kind"] == "NetworkPolicy"]
    assert len(pol) == 1, "sim-robot.yaml needs a NetworkPolicy: the robot's /mcp has no tool filter of its own"
    pol = pol[0]
    assert pol["metadata"]["namespace"] == dep["metadata"]["namespace"] == "picar-sim"
    assert pol["spec"]["podSelector"] == {"matchLabels": dep["spec"]["template"]["metadata"]["labels"]}
    assert pol["spec"]["policyTypes"] == ["Ingress"]
    agent_ns = re.search(r"^AGENT_NS=(\S+)$", (K8S / "deploy.sh").read_text(), re.M).group(1)
    (rule,) = pol["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": agent_ns}}}]
    container_ports = {p["containerPort"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]}
    assert {(p["protocol"], p["port"]) for p in rule["ports"]} == {("TCP", p) for p in container_ports}
    head = "\n".join(l for l in (K8S / "sim-robot.yaml").read_text().splitlines() if l.startswith("#"))
    assert "NetworkPolicy" in head and "Reached only through Teleport" not in head, head


# --------------------------------------------------------------------------- 4. check_mcp_bypass.sh
def _calls(path):
    return [line.split("\t", 1) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("mode,forbidden", [("good", "resume"), ("leaky_call", "resume"), ("good", "admin_reset_estop"), ("leaky_call", "admin_reset_estop")])
def test_the_probe_call_can_never_run_the_tool(run_check, tmp_path, mode, forbidden):  # noqa: F811
    """The one call the check makes carries arguments no MCP server accepts, so it can't lift a STOP or move the car,
    whatever answers it. There is no read-the-latch-then-resume window any more."""
    calls = tmp_path / "calls.txt"
    rc, out = run_check(mode, FORBIDDEN=forbidden, EXPECT_TOOLS="get_telemetry,snapshot,stop", FAKE_CALLS=calls)
    assert rc == (0 if mode == "good" else 1), out
    assert _calls(calls) == [[forbidden, '"not-an-object"']], _calls(calls)


@pytest.mark.parametrize("mode", ["expired_init", "odd_denied", "teleport_403", "wrong_id"])
def test_answers_that_are_not_teleports_rbac_denial_are_inconclusive(run_check, mode):  # noqa: F811
    rc, out = run_check(mode)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and "RESULT: PASS" not in out, out


@pytest.mark.parametrize("expect", [".*", "b.r"])
def test_expected_tools_are_matched_as_plain_names(run_check, expect):  # noqa: F811
    rc, out = run_check("wrong_list", EXPECT_TOOLS=expect)
    assert rc == 2 and "shows none of the tools this role should have" in out, out


def test_expected_tools_may_have_spaces_after_the_commas(run_check):  # noqa: F811
    rc, out = run_check("good", EXPECT_TOOLS="snapshot_x, stop")
    assert rc == 0 and "RESULT: PASS" in out, out


def test_the_forbidden_tool_in_another_letter_case_is_a_fail(run_check):  # noqa: F811
    rc, out = run_check("shouty_list")
    assert rc == 1 and "RESULT: FAIL" in out, out


@pytest.mark.parametrize("mode,rc_want", [("leaky_call", 1), ("good", 0)])
def test_sse_answers_are_matched_by_request_id(run_check, mode, rc_want):  # noqa: F811
    """A log line and another request's denial come first in the stream: only the answer to our id counts."""
    rc, out = run_check(mode, FAKE_SSE=1)
    assert rc == rc_want, out


@pytest.fixture
def squatter():
    """Something that isn't tsh listening on a port: answers every POST with 404."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    servers = []

    def start(port):
        s = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.mark.parametrize("offset", [0, 1])
def test_a_port_someone_else_listens_on_is_inconclusive(run_check, squatter, offset):  # noqa: F811
    for _ in range(20):
        port = free_port()
        try:
            squatter(port + offset)
            break
        except OSError:
            continue
    rc, out = run_check("good", PORT=port)
    assert rc == 2 and "RESULT: INCONCLUSIVE" in out and f"port {port + offset}" in out and "already taken" in out, out


def test_a_latched_robot_behind_a_list_only_filter_fails_and_stays_latched(run_check, sim):  # noqa: F811
    """The reviewers' bypass with STOP pressed: the check still proves the call got through, and the STOP holds."""
    sim.stop()
    assert sim.halted() is True
    rc, out = run_check("forward_hide", FAKE_UPSTREAM=sim.mcp, FAKE_STREAM="missing")
    assert rc == 1 and "REACHED THE ROBOT" in out and "Invalid request parameters" in out, out
    assert sim.halted() is True, "running the check cleared the STOP:\n" + out


# --------------------------------------------------------------------------- 5. test_no_tbot skips virtualenvs
def test_the_tbot_scan_skips_virtualenvs_and_ignored_dirs(tmp_path):
    for d in (".venv/lib", "venv/lib", "envs/py312/lib", ".pytest_cache", "logs", "docs"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "envs/py312/pyvenv.cfg").write_text("home = /usr/bin\n")
    for f in (".venv/lib/pkg.py", "venv/lib/pkg.py", "envs/py312/lib/pkg.py", ".pytest_cache/README.md", "logs/run.txt"):
        (tmp_path / f).write_text("tbot start\n")
    (tmp_path / "docs/guide.md").write_text("tbot start\n")
    assert [p.relative_to(tmp_path).as_posix() for p in test_no_tbot.files(tmp_path)] == ["docs/guide.md"]


# --------------------------------------------------------------------------- 6. docs
def test_readme_layout_lists_only_folders_that_exist():
    layout = (ROOT / "README.md").read_text().split("## Layout", 1)[1].split("```")[1]
    line = next(l for l in layout.splitlines() if l.startswith("beam-mcp-setup/"))
    for name in re.findall(r"([\w-]+)/", line.split(" ", 1)[1]):
        assert (ROOT / "beam-mcp-setup" / name).is_dir(), f"README layout lists beam-mcp-setup/{name}/"


def test_readme_gives_the_measured_test_time():
    readme = (ROOT / "README.md").read_text()
    assert "about 5 min" not in readme and "about 8 min" in readme


def test_admin_tools_are_described_by_what_they_do():
    """The five-Pi simulation has one admin tool, admin_reset_estop: nothing changes limits."""
    src = "\n".join(p.read_text() for p in (ROOT / "examples/advanced-simulation/pi/robot_mcp").glob("*.py"))
    assert set(re.findall(r'@tool\("(admin_\w+)"', src)) == {"admin_reset_estop"}
    row = next(l for l in (ROOT / "examples/advanced-simulation/README.md").read_text().splitlines() if l.startswith("| `admin_*`"))
    assert "limit" not in row, row
    assert "limits" not in (ROOT / "examples/advanced-simulation/pi/robot_mcp/registry.py").read_text().split('"""')[1]


def test_live_readme_beams_can_call_models():
    text = (ROOT / "tests/live/README.md").read_text()
    adds = re.findall(r"tsh beams add --roles[ =](\S+)", text)
    assert adds and all("beam-user" in a.split(",") for a in adds), adds
    assert "cd teleport-toys/beambot" in text
    assert "get_telemetry" not in text, "the check no longer reads the latch first"


def test_picar_readme_reports_what_was_observed_about_sharing():
    text = (ROOT / "examples/picar-beambot/README.md").read_text()
    assert "can only open it with two grants" not in text
    assert "The docs don't describe sharing it" not in text
    assert "share your app with others by publishing it" in text


def test_picar_readme_documents_the_mcp_stop_cap():
    app = (ROOT / "examples/picar-beambot/beam-app/app.py").read_text()
    n = re.search(r"^MAX_MCP_STOPS_IN_FLIGHT = (\d+)$", app, re.M).group(1)
    text = (ROOT / "examples/picar-beambot/README.md").read_text()
    assert re.search(rf"`MAX_MCP_STOPS_IN_FLIGHT` \({n}\)", text), "README must name the cap and its value"


# --------------------------------------------------------------------------- the PiCar's MCP SDK floor
def test_the_picar_mcp_floor_is_at_least_1_11():
    """On mcp 1.9 a malformed tool call (check_mcp_bypass.sh sends one) broke the robot's /mcp until a restart."""
    for req in ("examples/picar-beambot/robot/requirements.txt", "examples/picar-beambot/beam-app/requirements.txt",
                "tests/requirements.txt"):
        m = re.search(r"^mcp>=(\d+)\.(\d+)\S*,<2\b", (ROOT / req).read_text(), re.M)
        assert m and (int(m.group(1)), int(m.group(2))) >= (1, 11), f"{req}: the mcp floor must be 1.11 or newer, below 2"
    stale = [f"{p.relative_to(ROOT)}" for p in test_no_tbot.files() if "mcp>=1.9," in p.read_text(errors="ignore")]
    assert not stale, f"still documents mcp>=1.9: {stale}"
    assert '"mcp==1.19.0"' in (ROOT / "examples/advanced-simulation/pi/setup_pi.sh").read_text(), "the five-Pi pin stays"
