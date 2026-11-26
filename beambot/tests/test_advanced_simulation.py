"""The five-Pi simulation example ships its own MCP client test; run it as part of the suite, with both
MCP server implementations (official SDK and the zero-dependency fallback)."""
import subprocess
import sys

import pytest

from conftest import ROOT, _clean_env

EXAMPLE = ROOT / "examples/advanced-simulation"


@pytest.mark.parametrize("impl", ["sdk", "stdlib"])
def test_simulated_arm_and_drive_servers(impl):
    r = subprocess.run([sys.executable, "tests/test_mcp_client.py", impl], cwd=EXAMPLE, env=_clean_env(),
                       capture_output=True, text=True, timeout=150)
    assert "ALL PASSED" in r.stdout, r.stdout[-2000:] + r.stderr[-2000:]
    assert r.returncode == 0
