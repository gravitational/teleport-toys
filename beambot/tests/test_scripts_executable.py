"""Every shell script must be runnable straight after a clone: `./start.sh` died with "Permission denied" on a
real beam because a rewrite had dropped the executable bit."""
import os
import subprocess

import pytest

from conftest import ROOT

SCRIPTS = sorted(p for p in ROOT.rglob("*.sh") if ".git" not in p.parts)


def test_there_are_scripts():
    assert len(SCRIPTS) >= 8


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_script_is_executable_and_has_a_shebang(script):
    assert os.access(script, os.X_OK), f"chmod +x {script.relative_to(ROOT)}"
    assert script.read_text().startswith("#!"), "missing shebang"


def test_git_records_the_executable_bit():
    r = subprocess.run(["git", "ls-files", "-s", "--", "*.sh"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        pytest.skip("not inside a git checkout")
    bad = [l.split("\t")[1] for l in r.stdout.splitlines() if not l.startswith("100755")]
    assert not bad, f"committed without +x (git update-index --chmod=+x): {bad}"
