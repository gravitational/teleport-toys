"""beam-mcp-setup scripts, run for real against a fake `tsh` (see helpers/fake_teleport.py).

What these prove: the scripts call the Teleport CLI the way production Beams expect (delegated identity, no bot,
no tbot), and produce the Claude Code config they promise. What they can't prove: that a real cluster answers the
same way. tests/live/ and beam-mcp-setup/scripts/verify_in_beam.sh do that."""
import json
import subprocess

import pytest

from conftest import SCRIPTS

SETUP, UP = SCRIPTS / "beam_mcp_setup.sh", SCRIPTS / "beam_mcp_up.sh"


def run(script, tp, *args, **env):
    return subprocess.run(["bash", str(script), *args], env=tp.env(**env), capture_output=True, text=True, timeout=120)


def setup(tp, tmp_path, *args, **env):
    work = tmp_path / "work"
    r = run(SETUP, tp, "--dir", str(work), *args, **env)
    return r, work


def test_setup_writes_everything_claude_code_needs(teleport, tmp_path):
    r, work = setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--allow", "sense_*,safety_stop", "--deny", "act_*")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((work / "mcp.json").read_text())
    assert set(cfg["mcpServers"]) == {"teleport-mcp-beambot-sim-arm", "teleport-mcp-beambot-sim-drive"}
    assert (work / ".mcp.json").read_text() == (work / "mcp.json").read_text()

    perms = json.loads((work / ".claude/settings.json").read_text())["permissions"]
    for app in ("beambot-sim-arm", "beambot-sim-drive"):
        assert f"mcp__teleport-mcp-{app}__sense_*" in perms["allow"]
        assert f"mcp__teleport-mcp-{app}__safety_stop" in perms["allow"]
        assert f"mcp__teleport-mcp-{app}__act_*" in perms["deny"]
    assert "other-app" not in json.dumps(perms)
    allowed = (work / "allowed_tools.txt").read_text()
    assert "mcp__teleport-mcp-beambot-sim-arm__sense_*" in allowed


def test_setup_needs_no_bot_no_tbot_no_identity_file(teleport, tmp_path):
    r, _ = setup(teleport, tmp_path, "--labels", "robot=beambot-sim")
    assert r.returncode == 0, r.stdout + r.stderr
    assert teleport.calls("tctl") == []
    called = {tuple(c["args"][:2]) for c in teleport.calls("tsh")}
    assert ("status",) in called and ("mcp", "ls") in called and ("mcp", "config") in called
    assert all("TELEPORT_IDENTITY_FILE" not in c["env"] for c in teleport.calls("tsh")), "the beam's own tsh session is used"
    assert "tbot" not in (r.stdout + r.stderr).lower() and "machine id" not in (r.stdout + r.stderr).lower()


def test_setup_without_allow_grants_the_whole_server(teleport, tmp_path):
    r, work = setup(teleport, tmp_path, "--labels", "robot=beambot-sim")
    assert r.returncode == 0
    perms = json.loads((work / ".claude/settings.json").read_text())["permissions"]
    assert "mcp__teleport-mcp-beambot-sim-arm" in perms["allow"]


def test_setup_handshake_failure_names_the_cause(teleport, tmp_path):
    teleport.set(connect_denied=True)
    r, _ = setup(teleport, tmp_path, "--labels", "robot=beambot-sim")
    assert r.returncode != 0 and "handshake" in (r.stdout + r.stderr).lower()
    assert "access denied" in (r.stdout + r.stderr)          # the fake's own words are shown, not swallowed


def test_setup_can_skip_the_handshake(teleport, tmp_path):
    teleport.set(connect_denied=True)
    r, work = setup(teleport, tmp_path, "--labels", "robot=beambot-sim", "--no-handshake")
    assert r.returncode == 0 and (work / "mcp.json").exists()


def test_setup_fails_clearly_when_nothing_matches(teleport, tmp_path):
    r, _ = setup(teleport, tmp_path, "--labels", "robot=nonexistent")
    assert r.returncode != 0 and "nothing matched" in (r.stdout + r.stderr)


def test_setup_fails_clearly_when_not_logged_in(teleport, tmp_path):
    teleport.set(logged_in=False)
    r, _ = setup(teleport, tmp_path, "--labels", "robot=beambot-sim")
    assert r.returncode != 0 and "no active session" in (r.stdout + r.stderr)


def test_setup_warns_about_old_tsh(teleport, tmp_path):
    teleport.set(version="18.8.0")
    r, _ = setup(teleport, tmp_path, "--labels", "robot=beambot-sim")
    assert r.returncode == 0 and "18.11.9" in (r.stdout + r.stderr) and "WARN" in (r.stdout + r.stderr)


# --------------------------------------------------------------------------- laptop side
def up(tp, tmp_path, *args, **env):
    roles = [] if ("--roles" in args or "--inherit-roles" in args or "--beam" in args) else ["--roles", "picar-viewer"]
    return run(UP, tp, "--dir", "/home/beams/work", *roles, *args, **{"POLL_SECONDS": "0", **env})


def test_up_creates_a_beam_with_the_requested_roles_and_configures_it(teleport, tmp_path):
    r = up(teleport, tmp_path, "--roles", "picar-viewer", "--labels", "robot=beambot-sim", "--allow", "sense_*")
    assert r.returncode == 0, r.stdout + r.stderr
    add = teleport.calls("tsh", "beams", "add")
    assert len(add) == 1 and "--roles=picar-viewer" in add[0]["args"]
    assert len(teleport.calls("tsh", "beams", "scp")) == 1
    execs = teleport.calls("tsh", "beams", "exec")
    last = execs[-1]["args"]
    assert len(last) == 4 and last[2] == "fake-beam", "the whole remote command is ONE quoted argument"
    # the setup really ran, inside the fake beam home
    work = teleport.beam_home / "work"
    assert (work / "mcp.json").exists() and (work / ".claude/settings.json").exists()
    assert teleport.calls("tctl") == []


def test_up_refuses_to_inherit_every_role(teleport, tmp_path):
    """A beam with no --roles holds ALL of the creator's roles (access, editor, auditor...). Never the default."""
    r = run(UP, teleport, "--labels", "robot=beambot-sim", POLL_SECONDS="0")
    assert r.returncode == 2 and "--roles" in r.stderr
    assert teleport.calls("tsh", "beams", "add") == []


@pytest.mark.parametrize("blank", ["", " ", ",", " , "])
def test_up_treats_a_blank_role_list_as_not_given(teleport, tmp_path, blank):
    """`--roles=" "` may give the beam every role you hold, so a blank list must never reach tsh."""
    r = up(teleport, tmp_path, "--roles", blank, "--labels", "robot=beambot-sim")
    assert r.returncode == 2 and "--roles" in r.stderr
    assert teleport.calls("tsh", "beams", "add") == []


def test_up_rejects_empty_role_entries_and_strips_spaces(teleport, tmp_path):
    assert up(teleport, tmp_path, "--roles", "beam-user,,picar-viewer", "--labels", "x=y").returncode == 2
    assert teleport.calls("tsh", "beams", "add") == []
    assert up(teleport, tmp_path, "--roles", " beam-user, picar-viewer ", "--labels", "robot=beambot-sim").returncode == 0
    assert "--roles=beam-user,picar-viewer" in teleport.calls("tsh", "beams", "add")[0]["args"]


def test_up_can_inherit_when_explicitly_asked(teleport, tmp_path):
    assert up(teleport, tmp_path, "--inherit-roles", "--labels", "robot=beambot-sim").returncode == 0
    assert not any(a.startswith("--roles") for a in teleport.calls("tsh", "beams", "add")[0]["args"])


def test_up_reuses_an_existing_beam(teleport, tmp_path):
    r = up(teleport, tmp_path, "--beam", "warm-orbit", "--labels", "robot=beambot-sim")
    assert r.returncode == 0 and teleport.calls("tsh", "beams", "add") == []
    assert all(c["args"][2] == "warm-orbit" for c in teleport.calls("tsh", "beams", "exec"))


def test_up_waits_for_the_beam_to_be_ready(teleport, tmp_path):
    teleport.set(ready_after=3)
    assert up(teleport, tmp_path, "--labels", "robot=beambot-sim").returncode == 0
    probes = [c for c in teleport.calls("tsh", "beams", "exec") if c["args"][3] == "true"]
    assert len(probes) == 4


def test_up_has_no_bot_flag_any_more(teleport, tmp_path):
    r = up(teleport, tmp_path, "--bot", "x")
    assert r.returncode != 0


def test_verify_warns_when_the_beam_holds_admin_roles(teleport, tmp_path):
    teleport.set(roles=["beam", "mcp-user", "editor", "auditor"])
    r = run(SCRIPTS / "verify_in_beam.sh", teleport)
    assert "WARN" in r.stdout and "editor,auditor" in r.stdout and "--roles" in r.stdout


def test_verify_is_quiet_about_roles_for_a_narrow_beam(teleport, tmp_path):
    teleport.set(roles=["beam", "mcp-user"])
    r = run(SCRIPTS / "verify_in_beam.sh", teleport)
    assert "broad roles" not in r.stdout


def test_verify_flags_a_beam_that_cannot_reach_a_model(teleport, tmp_path):
    """Seen on a real cluster: a beam without beam-user could not see the model apps."""
    teleport.set(llm_apps=False)
    r = run(SCRIPTS / "verify_in_beam.sh", teleport)
    assert "WARN  no LLM apps visible" in r.stdout and "beam-user" in r.stdout


def test_verify_passes_the_model_check_when_apps_are_visible(teleport, tmp_path):
    r = run(SCRIPTS / "verify_in_beam.sh", teleport)
    assert "PASS  model apps" in r.stdout
