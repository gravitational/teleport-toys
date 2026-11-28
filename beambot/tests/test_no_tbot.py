"""Production Beams carry their own delegated identity, so nothing here should still ask people to run tbot,
create a Machine ID bot, or mint an identity file. This keeps the beta-era flow from creeping back in."""
import os
import re
from pathlib import Path

from conftest import ROOT

FORBIDDEN = re.compile(
    r"tbot (start|configure)|tbot\.yaml|allow_reissue: true|TELEPORT_IDENTITY_FILE|tctl bots (add|update|instances|rm)"
    r"|machine-id/out|BOT_JOIN_TOKEN|ROBOT_VIA_BOT|BEAM_BOT|--bot\b|via_bot|18\.8\b|bot-picar|bot-beambot"
    r"|not allowed to reissue|pre-production|mcp-client-read-roles|Enable Opus", re.I)
# Not ours, or git-ignored: an installed package could match by chance, and reading .venv took 17 s.
SKIP_DIRS = {".git", "tests", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", "logs"}
TEXT = {".md", ".sh", ".py", ".yaml", ".yml", ".html", ".txt", ".conf", ".example", ".ini", ""}


def files(root=ROOT):
    for d, dirs, names in os.walk(root):
        # prune, don't filter: never walk into a skipped folder or a virtualenv under any name (it has pyvenv.cfg)
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not os.path.exists(os.path.join(d, x, "pyvenv.cfg"))]
        for n in names:
            p = Path(d) / n
            if p.is_file() and p.suffix in TEXT and n != "conftest.py":
                yield p


def test_no_beta_era_bot_flow_anywhere():
    hits = []
    for p in files():
        for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if FORBIDDEN.search(line):
                hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:100]}")
    assert not hits, "beta-era tbot/bot flow still referenced:\n" + "\n".join(hits)


def test_documented_tsh_minimum_is_the_production_one():
    readme = (ROOT / "README.md").read_text()
    assert "18.11.9" in readme
