#!/usr/bin/env python3
# Clean Room: a people-data clean room built on Teleport Beams.
#
# Each run creates a disposable worker Beam, brings the data in (a sample CSV,
# or a no-names database view pulled through Teleport), computes org-health
# metrics and Claude-written insights inside the Beam, checks everything with
# a privacy gate before release, then deletes the Beam and prints a receipt.
#
# Real run (see README.md for all settings):
#   CLEAN_ROOM_BEAM_ROLES=beam-user python3 app.py
#
# Practice mode, with no Beams and no Claude (tests the flow and UI):
#   CLEAN_ROOM_LOCAL=1 CLEAN_ROOM_FAKE_AGENT=1 python3 app.py
#
# Database mode: see CONNECT_DATABASE.md.

import hashlib
import html
import json
import logging
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import date

import flask.cli
from flask import Flask, Response, abort, request, send_from_directory

logging.getLogger("werkzeug").setLevel(logging.ERROR)
flask.cli.show_server_banner = lambda *args, **kwargs: None

app = Flask(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
WORKER_DIR = os.path.join(HERE, "worker")
REPORTS_DIR = os.path.join(HERE, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

TELEPORT_CLUSTER = os.environ.get("TELEPORT_CLUSTER", "your-cluster.beams.run")
BEAM_ALIAS = os.environ.get("BEAM_ALIAS", "")
BEAM_ID = os.environ.get("BEAM_ID", "")
LOCAL = os.environ.get("CLEAN_ROOM_LOCAL") == "1"   # simulate worker Beams locally
FAKE_AGENT = os.environ.get("CLEAN_ROOM_FAKE_AGENT") == "1"  # skip Claude, use canned output

# Where the Org Health data comes from (see CONNECT_DATABASE.md):
#   "csv"      (default) copy data/people_coded.csv into the beam with tsh beams scp
#   "database" pull the coded view through Teleport from inside the beam
DATA_SOURCE = os.environ.get("CLEAN_ROOM_DATA_SOURCE", "csv")
DB_NAME_TELEPORT = os.environ.get("CLEAN_ROOM_DB", "hr-demo-db")         # name in `tsh db ls`
DB_USER = os.environ.get("CLEAN_ROOM_DB_USER", "readonly_analyst")       # read-only database user
DB_DATABASE = os.environ.get("CLEAN_ROOM_DB_NAME", "hr_demo")            # database inside Postgres
DB_VIEW = os.environ.get("CLEAN_ROOM_DB_VIEW", "v_people_coded")         # the no-names view
# Roles the worker beam gets, e.g. "beam-user,clean-room-reader". Empty = all of the user's roles.
BEAM_ROLES = os.environ.get("CLEAN_ROOM_BEAM_ROLES", "")
# Prebuilt worker image (a Teleport Beams Phase built from phase.yaml), e.g. "clean-room-worker".
# Empty = default Beam image, and the toolkit is copied in on every run.
PHASE = os.environ.get("CLEAN_ROOM_PHASE", "")
PHASE_TOOLKIT_DIR = "/opt/clean-room"     # where phase.yaml puts the worker/ folder
TOOLKIT_FILES = ["playbook.md", "compute_metrics.py"]

REMOTE_DIR = "/home/beams/cleanroom"
MIN_GROUP = 5

# Reference date for "last 12 months" measures (YYYY-MM-DD). It must be the
# first day of a quarter (Jan 1, Apr 1, Jul 1 or Oct 1): two runs on nearby
# dates differ only by the few people who joined, left or were promoted in
# between, which singles them out. Default: the latest quarter start for
# database data, and 2026-10-01 for the bundled sample CSV (the date its
# planted patterns were built around). Events after this date are ignored.
REFERENCE_DATE = os.environ.get("CLEAN_ROOM_REFERENCE_DATE", "")
SAMPLE_REFERENCE_DATE = "2026-10-01"
QUARTER_MONTHS = (1, 4, 7, 10)


def is_quarter_start(d):
    return d.day == 1 and d.month in QUARTER_MONTHS


if REFERENCE_DATE:
    try:
        _ref = date.fromisoformat(REFERENCE_DATE)
    except ValueError:
        raise SystemExit(f"CLEAN_ROOM_REFERENCE_DATE must be a real date like 2026-10-01, got {REFERENCE_DATE!r}")
    if not is_quarter_start(_ref):
        raise SystemExit(f"CLEAN_ROOM_REFERENCE_DATE must be the first day of a quarter "
                         f"(01-01, 04-01, 07-01 or 10-01), got {REFERENCE_DATE!r}")
    # Pass on YYYY-MM-DD only: "20261001" or "2026-W40-4" parse here, but older
    # Pythons in the beam reject them.
    REFERENCE_DATE = _ref.isoformat()


def latest_quarter_start(today=None):
    today = today or date.today()
    month = max(m for m in QUARTER_MONTHS if m <= today.month)
    return date(today.year, month, 1)


def reference_date(from_database):
    if REFERENCE_DATE:
        return REFERENCE_DATE
    return latest_quarter_start().isoformat() if from_database else SAMPLE_REFERENCE_DATE

# The app has no login, so by default only this machine can reach it. Set
# CLEAN_ROOM_HOST=0.0.0.0 only on a network you control (for example inside a
# Beam you share with `tsh beams publish`, where Teleport handles access).
HOST = os.environ.get("CLEAN_ROOM_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLEAN_ROOM_PORT", "8080"))


def new_run_dir():
    run_dir = os.path.join(REPORTS_DIR, uuid.uuid4().hex[:8])
    os.makedirs(run_dir)
    return run_dir

FORMAT_INSTRUCTION = (
    " Format your entire response as clean Markdown: a top-level '# ' "
    "title, '## ' section headers, and '- ' bullet points. Do not include "
    "any preamble or closing remarks outside the report itself."
)

DATASETS = {
    "orghealth": {
        "label": "Org Health Dashboard (300 people, playbook v0)",
        "csv": "people_coded.csv",
        "mode": "playbook",
    },
    "naive": {
        "label": "Naive HR report on the 8-person 'redacted' view",
        "csv": "redacted_salaries_8.csv",
        "mode": "freeform",
        "strict_gate": True,      # same rules as the org-health report
        "prompt": (
            "You are analyzing anonymized HR comp data for a company. The rows "
            "below already exclude names, SSNs, and bank account numbers. "
            "Produce an org-level summary by department and title: identify "
            "pay patterns and flag notable outliers in 3-5 bullet points. "
            "Do not invent any data not present below." + FORMAT_INSTRUCTION
        ),
    },
    "arr": {
        "label": "Customer ARR & Pipeline Summary",
        "csv": "customer_arr_summary.csv",
        "mode": "freeform",
        "strict_gate": False,     # dollar amounts are expected here
        "prompt": (
            "You are analyzing customer ARR and sales pipeline data. Summarize "
            "total ARR, expansion opportunity, concentration risk among top "
            "accounts, and notable stage trends in 3-5 bullet points. Do not "
            "invent any data not present below." + FORMAT_INSTRUCTION
        ),
    },
}

# --------------------------------------------------------------------------
# Worker Beam plumbing
# --------------------------------------------------------------------------


def run_cmd(args, timeout, input_text=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_text)


def list_beam_ids():
    """IDs from `tsh beams ls --all`, or None if the list can't be read."""
    try:
        # --all: inside a Beam, plain `ls` shows only that Beam identity's own beams.
        r = run_cmd(["tsh", "beams", "ls", "--all", "-f", "json"], timeout=60)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or "[]")
        if data is None:
            data = []      # tsh prints null when there are no beams
        if not isinstance(data, list):
            return None    # unknown shape: never "confirm" a beam gone from it
        return {str(b.get("id")) for b in data if isinstance(b, dict) and b.get("id")}
    except (subprocess.TimeoutExpired, ValueError, TypeError, OSError):
        return None


TEARDOWN_CONFIRM_SECONDS = 30


class Worker:
    """One disposable worker Beam. In LOCAL mode, a temp directory stands in."""

    def __init__(self):
        self.beam_id = None
        self.local_dir = None
        self.copied_in = {}
        self.image = "default Beam image"
        # Beams that may exist but that we couldn't identify for sure. The
        # receipt lists them so the user can check and remove them.
        self.unaccounted = []
        self.teardown_status = "no beam was created"

    def provision(self):
        if LOCAL:
            self.local_dir = tempfile.mkdtemp(prefix="cleanroom-")
            self.beam_id = "local-" + uuid.uuid4().hex[:6]
            return self.beam_id
        cmd = ["tsh", "beams", "add", "--no-console", "-f", "json"]
        if BEAM_ROLES:
            cmd.append(f"--roles={BEAM_ROLES}")   # delegate only these roles to the beam
        if PHASE:
            cmd.append(f"--phase={PHASE}")        # start from the prebuilt worker image
            self.image = f"{PHASE} (Phase)"
        before = list_beam_ids()
        try:
            result = run_cmd(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            # `add` may or may not have created a beam. Never guess which one
            # it was: other tools (VS Code, the Slack bot, a terminal, another
            # copy of this app) can add beams at the same time.
            self.unaccounted = ["unknown (tsh beams add timed out; check tsh beams ls --all)"]
            raise RuntimeError("beam provisioning timed out")
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    data = data[0]
                beam_id = data.get("id") or data.get("ID")
                self.beam_id = str(beam_id) if beam_id else None   # tsh may print a number
            except (ValueError, IndexError, AttributeError):
                pass
            if self.beam_id:
                return self.beam_id
            error = f"could not find beam id in: {result.stdout.strip()[:200]}"
        else:
            error = f"beam provisioning failed: {result.stderr.strip()}"
        # `add` failed (it may still have created a beam) or its output had no
        # ID we could read. The list diff shows what may be ours, but a beam
        # created elsewhere at the same time looks the same, so report the
        # candidates instead of deleting any.
        after = list_beam_ids()
        if before is None or after is None:
            self.unaccounted = ["unknown (couldn't list beams to check)"]
        elif after - before:
            self.unaccounted = sorted(after - before)
        elif result.returncode != 0:
            self.teardown_status = "nothing removed: tsh beams add failed and no new beam appeared in tsh beams ls"
        else:
            self.unaccounted = ["unknown (no new beam in tsh beams ls)"]
        raise RuntimeError(error)

    def wait_ready(self, max_wait=90, interval=3):
        if LOCAL:
            return
        waited = 0
        while waited < max_wait:
            r = run_cmd(["tsh", "beams", "exec", self.beam_id, "--", "echo", "ready"], timeout=interval + 5)
            if r.returncode == 0 and "ready" in r.stdout:
                return
            time.sleep(interval)
            waited += interval
        raise RuntimeError(f"beam {self.beam_id} did not become SSH-ready within {max_wait}s")

    def sh(self, script, timeout=60):
        """Run a bash script inside the worker, from REMOTE_DIR."""
        if LOCAL:
            r = run_cmd(["bash", "-c", script], timeout=timeout)
            # in LOCAL mode REMOTE_DIR is rewritten to the temp dir
        else:
            # `tsh beams exec` joins its arguments into one command line for the
            # beam's shell (like ssh), so the script must be passed pre-quoted.
            r = run_cmd(["tsh", "beams", "exec", self.beam_id, "--", "bash -c " + shlex.quote(script)],
                        timeout=timeout)
        if r.returncode != 0:
            err = RuntimeError((r.stderr or r.stdout).strip())
            err.returncode = r.returncode
            raise err
        return r.stdout

    def path(self, name):
        return os.path.join(self.local_dir if LOCAL else REMOTE_DIR, name)

    def put(self, local_path, name):
        if name.endswith(".csv"):
            self.copied_in[name] = sha256(local_path)
        if LOCAL:
            shutil.copy(local_path, self.path(name))
            return
        self.sh(f"mkdir -p {REMOTE_DIR}")
        r = run_cmd(["tsh", "beams", "scp", local_path, f"{self.beam_id}:{self.path(name)}"], timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"copy into beam failed: {r.stderr.strip()}")

    def get(self, name, local_path):
        if LOCAL:
            shutil.copy(self.path(name), local_path)
            return
        r = run_cmd(["tsh", "beams", "scp", f"{self.beam_id}:{self.path(name)}", local_path], timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"copy out of beam failed: {r.stderr.strip()}")

    def teardown(self):
        """Delete the beam, then check it's gone. Sets self.teardown_status and
        returns True only if removal is confirmed."""
        if LOCAL:
            if self.local_dir:
                shutil.rmtree(self.local_dir, ignore_errors=True)
            gone = not (self.local_dir and os.path.exists(self.local_dir))
            self.teardown_status = "removed" if gone else f"NOT removed: delete {self.local_dir}"
            return gone
        if not self.beam_id:
            if self.unaccounted:
                # Unknown whether a beam was created: don't say none was.
                self.teardown_status = "nothing removed: couldn't tell which beam, if any, this run created (see CHECK)"
            return not self.unaccounted
        try:
            r = run_cmd(["tsh", "beams", "rm", self.beam_id], timeout=60)
            rm_error = None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200]
        except subprocess.TimeoutExpired:
            rm_error = "tsh beams rm timed out"
        remaining = list_beam_ids()
        if rm_error:
            self.teardown_status = f"NOT removed ({rm_error}). Run: tsh beams rm {self.beam_id}"
            return False
        # Deletion can take a few seconds to show in `tsh beams ls`.
        deadline = time.time() + TEARDOWN_CONFIRM_SECONDS
        while remaining is not None and self.beam_id in remaining and time.time() < deadline:
            time.sleep(3)
            remaining = list_beam_ids()
        if remaining is None:
            self.teardown_status = (f"rm succeeded but couldn't confirm with tsh beams ls. "
                                    f"Check: tsh beams ls --all")
            return False
        if self.beam_id in remaining:
            self.teardown_status = f"NOT removed (still listed). Run: tsh beams rm {self.beam_id}"
            return False
        self.teardown_status = "removed (confirmed: no longer in tsh beams ls)"
        return True


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def run_claude(worker, prompt_text, timeout=240):
    """Write the prompt into the worker and run Claude there."""
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt")
    tmp.write(prompt_text)
    tmp.close()
    worker.put(tmp.name, "prompt.txt")
    os.unlink(tmp.name)
    if FAKE_AGENT or (LOCAL and not shutil.which("claude")):
        return None   # caller falls back to a canned response for dry runs
    if LOCAL:
        # Practice mode runs Claude on THIS machine (useful inside a build Beam,
        # see CUSTOMIZING.md), so never bypass permissions here. --tools makes
        # Read, Glob and Grep the only built-in tools, --strict-mcp-config
        # drops MCP servers from the local config, and dontAsk denies anything
        # else instead of prompting. --restricted confines Read, Glob and Grep
        # to the run folder (the working directory), refuses bypassPermissions
        # and ignores user, project and local settings files. Claude can't
        # write files, run commands or read outside the run folder. A Claude
        # Code too old to know --restricted exits with an error, so the run
        # fails instead of running unconfined.
        flags = ('--tools "Read,Glob,Grep" --allowedTools "Read,Glob,Grep" '
                 '--strict-mcp-config --permission-mode dontAsk --restricted')
    else:
        # Inside a disposable worker Beam, which is the sandbox.
        flags = "--permission-mode bypassPermissions"
    try:
        out = worker.sh(f'cd {worker.path("")} && claude -p "$(cat prompt.txt)" {flags}', timeout=timeout)
    except RuntimeError as exc:
        # Claude's own words (stdout or stderr) never leave the beam.
        raise RuntimeError(f"Claude failed inside the beam (exit {getattr(exc, 'returncode', '?')})") from None
    return out.strip()


# --------------------------------------------------------------------------
# Privacy gate (runs in the orchestrator, after the agent has finished)
# --------------------------------------------------------------------------

BASIC_PATTERNS = [
    ("SSN-like pattern", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("bank-account-like pattern", re.compile(r"\bXXXX-\d{3,}\b")),
    # Start only where a run of local-part characters starts, and cap each part
    # at the lengths a real address allows (64 before the @, 63 per domain
    # label). Without that, a long run like "a-a-a-..." is rescanned from every
    # position: 28 s on 100,000 characters.
    ("email address", re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")),
    ("long raw digit sequence (9+ digits)", re.compile(r"\b\d{9,}\b")),
]
PEOPLE_NOUNS = (
    r"people|persons?|individuals?|employees?|staff(?:ers?)?|members?|folks?|"
    r"workers?|colleagues?|managers?|leaders?|directors?|execs?|executives?|"
    r"engineers?|developers?|designers?|reps?|representatives?|analysts?|"
    r"hires?|joiners?|leavers?|departures?|starters?|"
    r"resignations?|exits?|terminations?|promotions?|separations?|"
    r"men|women|man|woman|ICs?|reports?|performers?|"
    r"heads?|seniors?|juniors?|interns?|contractors?|"
    r"(?:\w+ )?(?:who|that) (?:left|quit|resigned|joined)"
)
SMALL_NUMBER = r"(?:[1-4]|one|two|three|four|a couple(?: of)?|a few|a handful(?: of)?|a pair of|both)"
# Words that say people left, joined or were promoted, and the words that may
# sit between a number and them ("3 have since resigned", "Two are leaving").
LEAVE_FILLERS = r"(?:have|has|had|are|is|were|was|been|being|got|getting|recently|since|already)"
LEAVE_VERBS = (r"(?:left|leaving|quit|quitting|resigned|resigning|departed|departing|joined|joining|retired|retiring"
               r"|transferred|transferring|promoted|hired|fired|let\s+go|laid\s+off"
               r"|terminated|exited|exiting|dismissed|moved\s+on)")
LEAVER_NOUNS = r"(?:leavers?|departures?|resignations?|exits?|terminations?|promotions?|separations?)"
# These patterns catch mistakes, not an agent that is deliberately evading
# them: the no-names view and the Beam are the real controls.
STRICT_PATTERNS = [
    ("currency amount", re.compile(r"\$\s?\d[\d,]*(\.\d+)?\s?[kKmM]?|\b\d{1,3}(,\d{3})+(\.\d+)?\b|\b\d{5,7}(\.\d+)?\b|\b\d+(\.\d+)?\s?[kK]\b|\bUSD\b")),
    # Not \b: "_" is a word character, so \b misses "emp_56121ed67d". Hex-only
    # edges, so letters around the code ("emp56121ed67d") don't hide it, and
    # {10,} so a hex character touching it ("a56121ed67d") doesn't either.
    ("employee or manager code", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{10,}(?![0-9a-fA-F])")),
    ("reference to a very small group", re.compile(
        r"\b(only one|the only|a single|sole|lone|solitary|one person|nobody else|no one else|just one)\b"
        # "both new hires and tenured engineers" is coordination, not a count,
        # unless the words before "and" say someone left ("Both engineers quit and ...",
        # "Both regretted leavers and ...").
        rf"|(?<![\d.,])\b(?!both(?:\s+(?!(?:{LEAVE_FILLERS}|{LEAVE_VERBS}|{LEAVER_NOUNS})\b)[\w-]+){{1,3}}\s+and\b)"
        rf"{SMALL_NUMBER}\s+(?:\w+[- ]){{0,2}}?(?:{PEOPLE_NOUNS})\b"
        rf"|\banother\s+(?:\w+[- ]){{0,2}}?(?:{PEOPLE_NOUNS})\b"
        rf"|\bteams?\s+of\s+(?:just\s+|only\s+)?{SMALL_NUMBER}\b"
        rf"|(?<![\d.,])\b{SMALL_NUMBER}[- ](?:person|people|member|strong)[- ]teams?\b"
        # Bare counts ("2 of 4 left", "1 in 20 quit"): the count itself is a small group.
        rf"|(?<![\d.,])\b{SMALL_NUMBER}\s+(?:in|of|out\s+of)\s+(?:every\s+)?(?:\d{{1,3}}|one|two|three|four|five|six|seven|eight|nine|ten)\b"
        # A count with no people word ("Two quit", "3 have since resigned", "Two are leaving").
        rf"|(?<![\d.,])\b{SMALL_NUMBER}\s+(?:{LEAVE_FILLERS}\s+){{0,3}}{LEAVE_VERBS}\b"
        # "Two of them left", "2 of the 9 new reps left", "lost 3 of its top performers".
        rf"|(?<![\d.,])\b{SMALL_NUMBER}\s+of\s+(?:them|the|these|those|its|their|our|his|her)\b"
        # Group words ("A duo of managers quit", "the pair left") and slash counts ("2/9").
        r"|\b(?:duos?|trios?)\b|\b(?:a|the|this|that)\s+pair\b"
        r"|(?<![\d.,])\b[1-4]\s*/\s*\d+\b",
        re.I)),
]

# Phrases that describe a metric's DEFINITION or a job level, not a group of
# people. Example: M2 is literally "managers with 1 or 2 direct reports", and
# "level 3 engineers" names a level. These are removed before the small-group
# check runs. Counts like "1 of 3 managers" or "1 in 4 engineers" are NOT
# allowed: with a small group they say exactly how many people. Insights use
# percentages instead. Only the exact M2 definition (1 or 2 reports, 1-2 person
# teams) and single levels are allowed: "reports" is also a people word, so
# "3-4 direct reports quit" or "level 5 and 2 engineers" must still be checked.
# The report range needs "with", "having", "have" or "has" in front ("managers
# with 1-2 direct reports") and no leaving word after it, so "1-2 direct reports
# quit" and "has 1 or 2 reports leaving" are checked too. No "band N":
# C4 is "Out of band", so "Out of band 2 engineers are paid" would slip through.
ALLOWED_PHRASES = re.compile(
    r"\b(?:with|having|have|has)\s+(?:only\s+)?(?:1|one)-?\s*(?:-|–|to|or)\s*(?:2|two)\s+(?:direct\s+)?reports?\b"
    rf"(?!\s+(?:(?:who|that)\s+)?(?:{LEAVE_FILLERS}\s+){{0,3}}{LEAVE_VERBS}\b)"   # not "has 1 or 2 reports leaving"
    r"|\b(?:1|one)-?\s*(?:-|–|to|or)\s*(?:2|two)[- ]person teams?\b"
    r"|\b(?:levels?|grades?|tiers?)\s*\d+\b"
    r"|\b(?-i:IC|M|L)\d{1,2}\b",
    re.I,
)


def gate_hits(text, strict):
    """Returns (pattern name, matched phrase) pairs. The matched phrase may be
    the sensitive value itself, so it must only go back to the agent inside the
    beam, never to the browser, the receipt or a released file."""
    # Fold look-alike forms (full-width digits) and drop invisible format
    # characters (zero-width space), so neither can hide a phrase.
    text = "".join(c for c in unicodedata.normalize("NFKC", text or "") if unicodedata.category(c) != "Cf")
    patterns = BASIC_PATTERNS + (STRICT_PATTERNS if strict else [])
    hits = []
    for name, p in patterns:
        checked = ALLOWED_PHRASES.sub(" ", text) if name == "reference to a very small group" else text
        m = p.search(checked)
        if m:
            hits.append((name, m.group(0).strip()[:40]))
    return hits


def gate_text(text, strict):
    """Reasons safe to show outside the beam: pattern names only."""
    return [name for name, _ in gate_hits(text, strict)]


# The only things the dashboard may contain. Anything else the agent or the
# metrics file adds is dropped before the gate runs, so nothing unchecked is
# rendered or written.
ORGS = ["Engineering", "Product", "Sales", "Marketing", "G&A"]
METRIC_IDS = ["R1", "R2", "R3", "R4", "M1", "M2", "M3", "P1", "P2", "P3", "C1", "C2", "C3", "C4"]
METRIC_STATUSES = {"green", "yellow", "red", "suppressed"}
INSIGHT_TEXT_KEYS = ("what", "why", "next_step")
MAX_INSIGHTS = 3


def _number(v):
    # json.load reads NaN and Infinity; they compare False with everything, so
    # they would slip past the size checks. Treat them as missing. (Ints are
    # always finite but math.isfinite can't take very large ones. No real count
    # or value is over 10**9, and a huge float n would make the count check
    # below loop for a very long time, so cap floats too.)
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and (abs(v) <= 10**9 if isinstance(v, int) else math.isfinite(v) and abs(v) <= 1e9))


def clean_metrics(raw):
    """Rebuild metrics.json from known orgs, metric IDs and keys. Returns
    (metrics, reasons)."""
    reasons = []
    if not isinstance(raw, dict):
        return None, ["metrics file is not a JSON object"]

    def block(src, where, with_headcount):
        src = src if isinstance(src, dict) else {}
        out = {}
        if with_headcount:
            hc = src.get("headcount")
            if not _number(hc):
                reasons.append(f"{where}: headcount missing or not a number")
                hc = 0
            elif 0 < hc < MIN_GROUP:
                reasons.append(f"{where}: headcount under {MIN_GROUP}")
            out["headcount"] = hc
        for mid in METRIC_IDS:
            m = src.get(mid)
            if not isinstance(m, dict):
                # Keep a suppressed placeholder so later checks and the
                # dashboard always find every ID (an older worker image may not
                # produce every measure). The reason still blocks the release.
                reasons.append(f"{where} {mid}: missing")
                out[mid] = {"label": mid, "value": None, "n": None, "unit": "", "status": "suppressed"}
                continue
            value, n, status = m.get("value"), m.get("n"), m.get("status")
            if isinstance(value, float) and not math.isfinite(value):
                reasons.append(f"{where} {mid}: value is not a finite number")
            clean = {
                "label": str(m.get("label", "")),
                "value": value if _number(value) else None,
                "n": n if _number(n) else None,
                "unit": str(m.get("unit", "")),
                "status": status if status in METRIC_STATUSES else "suppressed",
            }
            if clean["value"] is None:
                clean["status"], clean["n"] = "suppressed", None
            elif clean["status"] == "suppressed":
                clean["value"], clean["n"] = None, None
            elif clean["n"] is None or clean["n"] < MIN_GROUP:
                reasons.append(f"{where} {mid}: based on fewer than {MIN_GROUP} people")
            elif mid in PLAIN_RATES:
                # The worker's rule again: the people a plain rate counts, and
                # the rest, must each be 0 or at least MIN_GROUP. The value is
                # rounded to 0.1%, so at large n several counts fit it; block
                # only if every count that fits is a small group (or none fits).
                if not 0 <= clean["value"] <= 100:
                    reasons.append(f"{where} {mid}: value is not a percentage")
                else:
                    size = int(clean["n"])
                    centre = clean["value"] * size / 100
                    lo, hi = max(0, math.floor(centre - size / 2000) - 1), min(size, math.ceil(centre + size / 2000) + 1)
                    fits = (k for k in range(lo, hi + 1) if round(100 * k / size, 1) == clean["value"])
                    if all(0 < k < MIN_GROUP or 0 < size - k < MIN_GROUP for k in fits):
                        reasons.append(f"{where} {mid}: its count or the rest is a group under {MIN_GROUP}")
            for k in ("label", "unit"):
                for name in gate_text(clean[k], strict=True):
                    reasons.append(f"{where} {mid} {k}: {name}")
            out[mid] = clean
        return out

    ref = raw.get("reference_date")
    try:
        if not is_quarter_start(date.fromisoformat(ref)):
            reasons.append("reference date is not the first day of a quarter")
    except (TypeError, ValueError):
        reasons.append("reference date missing or not a date")
        ref = "unknown"
    metrics = {
        "reference_date": ref,
        "min_group": MIN_GROUP,
        "company": block(raw.get("company"), "company", with_headcount=False),
        "orgs": {org: block((raw.get("orgs") or {}).get(org), org, with_headcount=True) for org in ORGS},
    }
    reasons += subtraction_check(metrics)
    # n is for the checks above only. Released, the n values give small groups
    # away by subtraction (C2 n minus R4 n is the number of top performers hired
    # in the last 2 years), so nothing after the gate ever sees them.
    for blk in [metrics["company"], *metrics["orgs"].values()]:
        for mid in METRIC_IDS:
            blk[mid].pop("n", None)
    return metrics, reasons


# Measures whose n adds up across orgs (people or managers), and the subset
# that are plain "count / n" rates, so their counts add up too.
ADDITIVE_N = ["R1", "R2", "R3", "R4", "M1", "M2", "M3", "P1", "P2", "P3", "C1", "C2", "C4"]
PLAIN_RATES = ["R2", "R3", "R4", "M2", "M3", "P3", "C2", "C4"]
# Measures whose n is the active headcount (same list as compute_metrics.py).
HEADCOUNT_N = ["R1", "M3", "P1", "P2", "P3", "C1", "C4"]


def subtraction_check(metrics):
    """Try the subtraction attack on what's about to be released: company
    figure minus the visible orgs. If that reveals a hidden group of 1 to
    MIN_GROUP-1 people (or a count or remainder that small), block. The hidden
    group includes rows whose org isn't in ORGS, so the check runs even when
    every org is visible. compute_metrics.py already prevents this; this
    catches an out-of-date worker image or a changed script."""
    reasons = []
    small = lambda c: 0 < c < MIN_GROUP  # noqa: E731
    for mid in ADDITIVE_N:
        co = metrics["company"][mid]
        if co["value"] is None:
            continue
        orgs = [metrics["orgs"][o][mid] for o in ORGS]
        visible = [m for m in orgs if m["value"] is not None]
        if co["n"] is None or any(m["n"] is None for m in visible):
            continue   # already blocked by the n check in clean_metrics
        hidden_n = co["n"] - sum(m["n"] for m in visible)
        if hidden_n == 0:
            continue   # nobody hidden; any count difference is rounding
        bad = small(hidden_n)
        if not bad and mid in PLAIN_RATES:
            hidden_num = round(co["value"] * co["n"] / 100) - sum(round(m["value"] * m["n"] / 100) for m in visible)
            bad = small(hidden_num) or small(hidden_n - hidden_num)
        if bad:
            reasons.append(f"company {mid}: with the org figures, reveals a suppressed group under {MIN_GROUP}")
    # Org headcounts are always shown. For measures whose n is the active
    # headcount, company n minus the org headcounts is the number of people
    # outside ORGS, whatever else is hidden.
    for mid in HEADCOUNT_N:
        co = metrics["company"][mid]
        if co["value"] is not None and co["n"] is not None:
            outside = co["n"] - sum(metrics["orgs"][o]["headcount"] for o in ORGS)
            if small(outside):
                reasons.append(f"company {mid}: n minus the org headcounts reveals a group under {MIN_GROUP}")
    # P1 is each org's top-performer share minus the COMPANY share, and the
    # company share includes every org. So if one org's P1 is hidden, the
    # visible orgs' P1 values and headcounts still give its count back. P1 is
    # released for every org or for none. An org with nobody active doesn't
    # count as hidden: it has no P1 and hides nobody. If the company P1 is
    # hidden, the org values would give the company share back.
    shown = [o for o in ORGS if metrics["orgs"][o]["P1"]["value"] is not None]
    hidden = [o for o in ORGS if metrics["orgs"][o]["P1"]["value"] is None and metrics["orgs"][o]["headcount"] != 0]
    if shown and hidden:
        reasons.append("P1: suppressed for some orgs but shown for others, which reveals the hidden ones")
    if shown and metrics["company"]["P1"]["value"] is None:
        reasons.append("P1: shown for orgs while the company P1 is suppressed, which reveals it")
    # R4's n (tenured top performers) is part of C2's n (all top performers),
    # so the two give the top performers hired in the last 2 years.
    for where, blk in [("company", metrics["company"]), *metrics["orgs"].items()]:
        c2, r4 = blk["C2"], blk["R4"]
        if c2["value"] is not None and r4["value"] is not None and c2["n"] is not None and r4["n"] is not None:
            if small(c2["n"] - r4["n"]):
                reasons.append(f"{where} C2 and R4: their n values differ by a group under {MIN_GROUP}")
    return reasons


def clean_insights(raw, metrics):
    """Rebuild the insights from known orgs and keys only, and gate every
    string in the result. Returns (insights, reasons, hits). `reasons` hold
    pattern names only (safe for the browser); `hits` add the matched phrase
    for the retry prompt, which stays inside the beam."""
    insights, reasons, hits = {}, [], []
    if not isinstance(raw, dict):
        raw = {}
    for org in ORGS:
        items = raw.get(org)
        if not isinstance(items, list) or not items:
            reasons.append(f"{org}: no insights returned")
            hits.append(f"{org}: no insights returned")
            insights[org] = []
            continue
        clean_items = []
        for idx, item in enumerate(items[:MAX_INSIGHTS], 1):
            where = f"{org} insight {idx}"
            if not isinstance(item, dict):
                reasons.append(f"{where}: not an object")
                hits.append(f"{where}: not an object")
                continue
            mid = item.get("metric", "-")
            if mid not in METRIC_IDS and mid != "-":
                reasons.append(f"{where}: unknown metric ID")
                hits.append(f"{where}: metric must be one of {', '.join(METRIC_IDS)} or \"-\"")
                continue
            if mid != "-" and metrics["orgs"][org][mid]["status"] == "suppressed":
                reasons.append(f"{where}: refers to a suppressed metric")
                hits.append(f"{where}: {mid} is suppressed for {org}; don't mention it")
            clean = {"metric": mid}
            for k in INSIGHT_TEXT_KEYS:
                v = item.get(k, "")
                if not isinstance(v, str):
                    reasons.append(f"{where} {k}: not text")
                    hits.append(f"{where} {k}: must be a string")
                    v = ""
                for name, phrase in gate_hits(v, strict=True):
                    reasons.append(f"{where} {k}: {name}")
                    hits.append(f'{where} {k}: {name} ("{phrase}")')
                clean[k] = v
            clean_items.append(clean)
        insights[org] = clean_items
    return insights, reasons, hits


# --------------------------------------------------------------------------
# Playbook flow
# --------------------------------------------------------------------------

INSIGHT_PROMPT = """You are an org-health analyst working inside a secure, disposable environment.

Read playbook.md (the analysis playbook) and metrics.json (org-level metrics
already computed from the raw data in people_coded.csv, which is also in this
directory if you need to check a pattern).

For EACH org in metrics.json, write the top things its leader should look at,
following playbook section 4 and its interpretation notes. Use only red, then
yellow metrics, up to 3 items. Never turn a healthy metric into a concern to
fill the list. If an org has no red or yellow metrics, return ONE item with
"metric": "-" saying it is healthy across all measures and naming one thing
worth keeping.

Hard rules (a privacy gate will reject your output if you break them):
- Never mention an individual, an employee code, a manager code, or a dollar amount.
- Never describe a group of fewer than {min_group} people. Skip suppressed metrics.
- Refer to metrics by ID (e.g. "R3") and use only the numbers in metrics.json.
- Talk about rates and shares, not head counts, and never quote a metric's
  "n" (how many people it is based on). Don't write phrases like
  "only one", "a single", "the only", "3 people", "1 in 4", "3 of 14" or
  "one of the"; use percentages instead.
- For M2, call it "very small teams" (managers with 1 or 2 direct reports);
  don't count or describe specific teams.
- Don't name or describe teams, job families, levels or locations inside an
  org; keep every insight at the org level.

Return ONLY a JSON object, no prose, shaped exactly like:
{{"Engineering": [{{"metric": "R4", "what": "...", "why": "...", "next_step": "..."}}, ...up to 3 items], "Product": [...], ...}}
{feedback}"""


def parse_json(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("agent did not return JSON")
    return json.loads(m.group(0))


def canned_insights(metrics):
    """LOCAL dry-run stand-in for Claude: lists the worst metrics per org."""
    out = {}
    for org, block in metrics["orgs"].items():
        ranked = sorted(
            [(k, v) for k, v in block.items() if isinstance(v, dict) and v["status"] in ("red", "yellow")],
            key=lambda kv: 0 if kv[1]["status"] == "red" else 1,
        )[:3]
        out[org] = [
            {"metric": k, "what": f"{v['label']} is {v['value']}{v['unit']} ({v['status']}).",
             "why": "(dry run: Claude writes this part)", "next_step": "(dry run)"}
            for k, v in ranked
        ] or [{"metric": "-", "what": "No red or yellow measures.", "why": "", "next_step": "Keep monitoring."}]
    return out


STATUS_COLORS = {"green": "#169984", "yellow": "#D4A017", "red": "#C62828", "suppressed": "#90A4AE"}
STATUS_LABEL = {"green": "Healthy", "yellow": "Watch", "red": "Act", "suppressed": "Suppressed (&lt;5)"}


def fmt(m):
    if m["value"] is None:
        return "&mdash;"
    return html.escape(f"{m['value']}{m['unit']}")


def render_dashboard(metrics, insights):
    company = metrics["company"]
    cards = []
    for org, block in metrics["orgs"].items():
        rows = []
        for mid, m in block.items():
            if not isinstance(m, dict):
                continue
            c = company[mid]
            rows.append(
                f"<tr><td>{mid}</td><td>{html.escape(m['label'])}</td><td><b>{fmt(m)}</b></td>"
                f"<td class='co'>{fmt(c)}</td>"
                f"<td><span class='pill' style='background:{STATUS_COLORS[m['status']]}'>"
                f"{STATUS_LABEL[m['status']]}</span></td></tr>"
            )
        ins = "".join(
            f"<li>{'' if i.get('metric', '-') in ('-', '') else '<b>' + html.escape(i['metric']) + '</b> '}{html.escape(i.get('what', ''))}"
            f"<div class='why'>{html.escape(i.get('why', ''))}</div>"
            f"<div class='next'>Next step: {html.escape(i.get('next_step', ''))}</div></li>"
            for i in insights.get(org, [])
        )
        cards.append(
            f"<section class='card'><h2>{html.escape(org)} <span class='hc'>{block['headcount']} people</span></h2>"
            f"<table><tr><th></th><th>Measure</th><th>Org</th><th>Company</th><th>Status</th></tr>{''.join(rows)}</table>"
            f"<h3>Top things to look at</h3><ol>{ins}</ol></section>"
        )
    return (
        "<div class='dash'><p class='meta'>Reference date "
        f"{html.escape(metrics['reference_date'])}. Any number based on fewer than "
        f"{metrics['min_group']} people is suppressed. A company figure is hidden "
        "when, together with the org figures, it would reveal a suppressed number.</p>"
        f"{''.join(cards)}</div>"
    )


DASH_CSS = """
.dash{font-family:Lato,sans-serif;color:#324148}
.dash .meta{font-size:12px;color:#607D8B;margin-bottom:8px}
.card{background:#fff;border:1px solid #CFD8DC;border-radius:10px;padding:14px;margin-bottom:14px}
.card h2{font-size:17px;color:#512FC9;margin-bottom:8px}.card .hc{font-size:12px;color:#607D8B;font-weight:400}
.card table{border-collapse:collapse;width:100%;font-size:13px}
.card td,.card th{padding:4px 6px;border-bottom:1px solid #EFF1FE;text-align:left}
.card td.co{color:#607D8B}.pill{color:#fff;border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700}
.card h3{font-size:14px;margin:10px 0 4px}.card ol{padding-left:18px;font-size:13px}
.card li{margin-bottom:6px}.why{color:#455A64}.next{color:#512FC9}
"""


def pull_from_database(worker):
    """Runs INSIDE the worker beam: query the no-names view through Teleport
    and save it as people_coded.csv in the beam. The rows never touch the
    machine running this app."""
    script = (
        f"mkdir -p {REMOTE_DIR} && cd {worker.path('')} && "
        f"tsh db connect {shlex.quote(DB_NAME_TELEPORT)} "
        f"--db-user={shlex.quote(DB_USER)} --db-name={shlex.quote(DB_DATABASE)} <<'SQL'\n"
        f"\\copy (SELECT * FROM {DB_VIEW}) TO 'people_coded.csv' WITH CSV HEADER\n"
        "SQL\n"
        "test -s people_coded.csv && "
        "echo \"rows=$(($(wc -l < people_coded.csv) - 1)) sha=$(sha256sum people_coded.csv | cut -c1-16)\""
    )
    out = worker.sh(script, timeout=90)
    m = re.search(r"rows=(\d+) sha=(\w+)", out)
    if not m:
        raise RuntimeError(
            "database pull returned no data. Inside a beam, check `tsh db ls` and "
            "`tsh -d db connect ...` (see CONNECT_DATABASE.md, Troubleshooting)."
        )
    worker.copied_in[f"{DB_NAME_TELEPORT}/{DB_VIEW} via Teleport, {m.group(1)} rows"] = m.group(2)


def full_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def use_phase_toolkit(worker):
    """Phase mode: the playbook and metrics script are already in the image.
    Copy them into the run folder, and stop the run if they differ from this
    repo's worker/ files (meaning the Phase needs rebuilding with `tsh phase apply`)."""
    files = " ".join(f"{PHASE_TOOLKIT_DIR}/{f}" for f in TOOLKIT_FILES)
    try:
        out = worker.sh(f"mkdir -p {REMOTE_DIR} && cp {files} {REMOTE_DIR}/ && sha256sum {files}", timeout=60)
    except RuntimeError as exc:
        hint = ""
        if "Permission denied" in str(exc):
            hint = (" The image's files aren't readable by the beams user. On your laptop, run "
                    "`chmod -R a+rX worker` in the repo folder, then `tsh phase apply -f phase.yaml`.")
        elif "No such file" in str(exc):
            hint = f" The {PHASE} image has no toolkit in {PHASE_TOOLKIT_DIR}. Build it with `tsh phase apply -f phase.yaml`."
        raise RuntimeError(f"couldn't read the worker toolkit from the {PHASE} image: {exc}.{hint}")
    in_image = {os.path.basename(line.split()[1]): line.split()[0] for line in out.strip().splitlines() if line.strip()}
    stale = [f for f in TOOLKIT_FILES if in_image.get(f) != full_sha256(os.path.join(WORKER_DIR, f))]
    if stale:
        # Fail closed: an older metrics script may not apply today's privacy rules.
        worker.image = f"{PHASE} (Phase, OUT OF DATE: {', '.join(stale)})"
        raise RuntimeError(f"The {PHASE} image is out of date: {', '.join(stale)} differ from this repo, "
                           "so nothing was computed or released. Rebuild it with: tsh phase apply -f phase.yaml")
    worker.image = f"{PHASE} (Phase, toolkit matches repo)"


def playbook_flow(worker, dataset, emit):
    from_database = DATA_SOURCE == "database" and not LOCAL
    if from_database:
        emit("status", f"Pulling the no-names view from {DB_NAME_TELEPORT} through Teleport, inside the beam...")
        pull_from_database(worker)
    else:
        emit("status", "Copying the raw people data into the beam...")
        worker.put(os.path.join(DATA_DIR, dataset["csv"]), "people_coded.csv")
    if PHASE and not LOCAL:
        emit("status", f"Using the playbook and metrics script built into the {PHASE} image...")
        use_phase_toolkit(worker)
    else:
        emit("status", "Copying the playbook and metrics script into the beam...")
        worker.put(os.path.join(WORKER_DIR, "playbook.md"), "playbook.md")
        worker.put(os.path.join(WORKER_DIR, "compute_metrics.py"), "compute_metrics.py")

    emit("status", "Computing org-level metrics inside the beam (raw rows stay in the beam)...")
    ref = reference_date(from_database)
    worker.sh(f"cd {worker.path('')} && python3 compute_metrics.py people_coded.csv metrics.json {ref}", timeout=60)
    with tempfile.TemporaryDirectory() as tmp:
        worker.get("metrics.json", os.path.join(tmp, "metrics.json"))
        with open(os.path.join(tmp, "metrics.json")) as f:
            metrics, reasons = clean_metrics(json.load(f))
    if reasons:
        emit("blocked", reasons)
        raise RuntimeError("privacy gate blocked the metrics; nothing was released")
    # Claude's copy in the beam becomes the cleaned one too, so it has no n to quote.
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "metrics.json"), "w") as f:
            json.dump(metrics, f, indent=2)
        worker.put(os.path.join(tmp, "metrics.json"), "metrics.json")

    feedback, insights, reasons = "", None, []
    for attempt in (1, 2, 3):
        emit("status", f"Claude is writing insights inside the beam (attempt {attempt})...")
        prompt = INSIGHT_PROMPT.format(min_group=MIN_GROUP, feedback=feedback)
        raw = run_claude(worker, prompt)
        try:
            raw_insights = canned_insights(metrics) if raw is None else parse_json(raw)
        except ValueError:   # includes json.JSONDecodeError
            raw_insights = None
        emit("status", "Privacy gate: checking group sizes, codes and dollar amounts...")
        if raw_insights is None:
            reasons, hits = ["agent did not return valid JSON"], ["your answer was not valid JSON"]
        else:
            insights, reasons, hits = clean_insights(raw_insights, metrics)
        if not reasons:
            break
        emit("blocked", reasons)
        # The matched phrases go only into the prompt, which stays in the beam.
        feedback = ("\nYour previous answer was REJECTED by the privacy gate. The exact "
                    "phrases that triggered it are in quotes: " + "; ".join(hits)
                    + ". Rewrite those insights without that kind of phrase "
                    "(use rates or shares instead) and return the full JSON again.")
    if reasons:
        raise RuntimeError("privacy gate blocked the report 3 times; nothing was released")

    # Only gated, rebuilt objects are written; the run folder exists only once
    # everything has passed.
    run_dir = new_run_dir()
    with open(os.path.join(run_dir, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(run_dir, "insights.json"), "w") as f:
        json.dump(insights, f, indent=2)
    dash = render_dashboard(metrics, insights)
    with open(os.path.join(run_dir, "dashboard.html"), "w") as f:
        f.write(f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>Org Health Dashboard</title>"
                f"<style>{DASH_CSS}</style></head><body>{dash}</body></html>")
    emit("dashboard", dash)
    return run_dir


def freeform_flow(worker, dataset, emit):
    emit("status", f"Copying {dataset['csv']} into the beam...")
    worker.put(os.path.join(DATA_DIR, dataset["csv"]), "data.csv")
    emit("status", "Claude is analyzing the data inside the beam...")
    data_text = open(os.path.join(DATA_DIR, dataset["csv"])).read()
    raw = run_claude(worker, dataset["prompt"] + "\n\nData (CSV):\n" + data_text)
    if raw is None:   # LOCAL dry run without Claude: show what a naive report looks like
        rows = [l.split(",") for l in data_text.strip().splitlines()[1:]]
        raw = "# Dry-run report\n\n" + "\n".join(f"- {r[0]} / {r[1]}: ${int(float(r[2])):,}" for r in rows[:5])
    emit("status", "Privacy gate: scanning the report before release...")
    reasons = gate_text(raw, strict=dataset.get("strict_gate", False))
    if reasons:
        emit("blocked", reasons)
        raise RuntimeError("privacy gate blocked the report; nothing was released")
    run_dir = new_run_dir()
    with open(os.path.join(run_dir, "report.md"), "w") as f:
        f.write(raw)
    emit("report", raw)
    return run_dir


# --------------------------------------------------------------------------
# Web UI
# --------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Clean Room: Sensitive Data Analysis</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Lato,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#F0F2F4;display:flex;flex-direction:column;height:100vh;color:#324148}
header{background:#fff;border-bottom:1px solid #CFD8DC;padding:14px 20px;font-weight:900;font-size:15px;color:#512FC9}
header small{display:block;font-weight:400;color:#607D8B;font-size:12px;margin-top:2px}
#messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:90%;padding:10px 14px;border-radius:10px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-break:break-word;align-self:flex-start}
.status{background:#EFF1FE;border:1px solid #512FC9;font-family:'Ubuntu Mono',monospace;font-weight:700;font-size:12px;color:#512FC9}
.report{background:#fff;border:1px solid #CFD8DC}
.error{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}
.blocked{background:#fffbeb;border:1px solid #fde68a;color:#92400e;font-weight:700}
.receipt{background:#fff;border:1px dashed #512FC9;font-family:'Ubuntu Mono',monospace;font-size:12px}
.dashwrap{max-width:100%;white-space:normal;background:transparent;padding:0}
footer{background:#fff;border-top:1px solid #CFD8DC;padding:14px 20px;display:flex;gap:10px;flex-wrap:wrap}
button.run{padding:10px 18px;background:#651FFF;color:#fff;border:none;border-radius:10px;font-size:14px;cursor:pointer;font-weight:700}
button.run.alt{background:#fff;color:#651FFF;border:1px solid #651FFF}
button.run:disabled{background:#EFF1FE;color:#607D8B;cursor:default}
__DASH_CSS__
</style></head><body>
<header>Clean Room: raw data never leaves the beam; only org-level dashboards do
<small>__MODE__</small></header>
<div id="messages"></div>
<footer>
  <button class="run" data-dataset="orghealth">Run Org Health Dashboard</button>
  <button class="run alt" data-dataset="naive">Run naive report (watch the gate)</button>
  <button class="run alt" data-dataset="arr">Run Customer ARR Summary</button>
</footer>
<script>
const messages=document.getElementById('messages');
const buttons=document.querySelectorAll('button.run');
function addMsg(cls,text,isHtml){const d=document.createElement('div');d.className='msg '+cls;
 if(isHtml){d.innerHTML=text}else{d.textContent=text} messages.appendChild(d);messages.scrollTop=messages.scrollHeight;return d;}
async function runAnalysis(dataset){
 buttons.forEach(b=>b.disabled=true);
 const resp=await fetch('/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset})});
 const reader=resp.body.getReader();const dec=new TextDecoder();let buf='';
 while(true){const {done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});
  const lines=buf.split('\\n');buf=lines.pop();
  for(const line of lines){if(!line.startsWith('data: '))continue;const d=JSON.parse(line.slice(6));
   if(d.type==='status')addMsg('status',d.msg);
   else if(d.type==='report')addMsg('report',d.content);
   else if(d.type==='dashboard')addMsg('dashwrap',d.html,true);
   else if(d.type==='error')addMsg('error',d.msg);
   else if(d.type==='blocked')addMsg('blocked','Privacy gate blocked this output: '+d.reasons.join('; '));
   else if(d.type==='receipt')addMsg('receipt',d.text);
   else if(d.type==='files'){const div=document.createElement('div');div.className='msg status';
     d.links.forEach(([href,label])=>{const a=document.createElement('a');a.href=href;a.textContent='Download '+label;
       a.style.marginRight='12px';a.setAttribute('download','');div.appendChild(a)});messages.appendChild(div);}
  }}
 buttons.forEach(b=>b.disabled=false);}
buttons.forEach(b=>b.addEventListener('click',()=>runAnalysis(b.dataset.dataset)));
</script></body></html>"""


@app.route("/")
def index():
    mode = "LOCAL DRY RUN: no beams are created" if LOCAL else f"Worker beams on {TELEPORT_CLUSTER}"
    return HTML.replace("__DASH_CSS__", DASH_CSS).replace("__MODE__", mode)


def sse(kind, **payload):
    return f"data: {json.dumps({'type': kind, **payload})}\n\n"


def run_job(dataset, send):
    """One full run: provision, flow, teardown, receipt. Runs in its own thread
    so it always finishes (and always tears the beam down), even if the
    browser disconnects. `send(kind, **payload)` queues an event for the page."""
    last_status = ["Starting the run"]   # names the step if an error has to be withheld

    def emit(kind, value):
        if kind == "blocked":
            send("blocked", reasons=value)
        elif kind == "dashboard":
            send("dashboard", html=value)
        elif kind == "report":
            send("report", content=value)
        else:
            last_status[0] = value
            send("status", msg=value)

    worker = Worker()
    started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    released, outcome = [], "failed"
    try:
        emit("status", "Provisioning a fresh disposable worker beam...")
        beam_id = worker.provision()
        emit("status", f"Beam {beam_id} is up. Waiting for it to be ready...")
        worker.wait_ready()
        flow = playbook_flow if dataset["mode"] == "playbook" else freeform_flow
        run_dir = flow(worker, dataset, emit)
        released = sorted(os.listdir(run_dir))
        outcome = "released"
        rid = os.path.basename(run_dir)
        send("files", links=[[f"/download/{rid}/{f}", f] for f in released])
    except Exception as exc:
        if "privacy gate" in str(exc):
            outcome = "blocked by privacy gate, nothing released"
        msg = str(exc)
        if gate_hits(msg, strict=True):
            # Error text can carry what the agent or a beam command printed
            # (worker.sh raises with their output), so it must pass the gate too.
            msg = f'This step failed: "{last_status[0]}" The details were withheld by the privacy gate.'
        send("error", msg=msg)
    finally:
        gone = worker.teardown()
        if worker.beam_id and gone:
            send("status", msg=f"Beam {worker.beam_id} torn down and confirmed gone. "
                               "Raw data, credential and VM are gone.")
        elif worker.beam_id:
            send("error", msg=f"Beam {worker.beam_id} was NOT confirmed removed: {worker.teardown_status}")
        if worker.unaccounted:
            send("error", msg="A beam may have been created that this app couldn't identify: "
                              + ", ".join(worker.unaccounted) + ". Check `tsh beams ls --all` and remove it "
                              "only if it is from this run: another tool may have added it.")
        receipt = [
            "RUN RECEIPT",
            f"  beam:        {worker.beam_id or '-'}",
            f"  image:       {worker.image}",
            f"  started:     {started}",
            f"  teardown:    {worker.teardown_status}",
        ]
        if gone and worker.beam_id:
            receipt.append(f"  destroyed:   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        if worker.unaccounted:
            receipt.append("  CHECK:       possible leftover beam(s): " + ", ".join(worker.unaccounted))
        receipt += [
            f"  copied in:   " + (", ".join(f"{k} (sha256 {v})" for k, v in worker.copied_in.items()) or "-"),
            f"  released:    " + (", ".join(released) or "nothing"),
            f"  outcome:     {outcome}",
            "  query audit: see the Teleport audit log for this beam's session",
        ]
        text = "\n".join(receipt)
        print(text, flush=True)   # kept in the console even if the browser has gone
        send("receipt", text=text)


@app.route("/analyze", methods=["POST"])
def analyze():
    key = (request.get_json(silent=True) or {}).get("dataset")
    dataset = DATASETS.get(key)
    if not dataset:
        return Response(sse("error", msg=f"Unknown dataset: {key}"), mimetype="text/event-stream")

    events = queue.Queue()
    DONE = object()

    def job():
        try:
            run_job(dataset, lambda kind, **payload: events.put(sse(kind, **payload)))
        finally:
            events.put(DONE)

    threading.Thread(target=job, daemon=False).start()

    def generate():
        # Only streams events. If the browser disconnects, this generator is
        # closed, but the job thread keeps going and still tears down the beam.
        while True:
            event = events.get()
            if event is DONE:
                return
            yield event

    return Response(generate(), mimetype="text/event-stream")


RUN_ID = re.compile(r"[0-9a-f]{8}")
RELEASED_FILES = {"metrics.json", "insights.json", "dashboard.html", "report.md"}


@app.route("/download/<run_id>/<filename>")
def download(run_id, filename):
    # Only the 8-hex-character run IDs new_run_dir() makes, and only the file
    # names a run releases. Anything else is a 404, so no path can escape
    # reports/.
    if not RUN_ID.fullmatch(run_id) or filename not in RELEASED_FILES:
        abort(404)
    return send_from_directory(os.path.join(REPORTS_DIR, run_id), filename, as_attachment=True)


if __name__ == "__main__":
    print(f"Clean Room running on http://{HOST}:{PORT}" + (" (LOCAL DRY RUN)" if LOCAL else ""))
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: listening on {HOST} with no authentication. Anyone who can reach "
              "this address can start runs and download reports.")
    if PHASE and not LOCAL:
        print(f"Worker beams start from the {PHASE} image (Phase). Rebuild after editing worker/: tsh phase apply -f phase.yaml")
    if BEAM_ALIAS:
        print(f"To publish externally: tsh beams publish {BEAM_ALIAS}")
    app.run(host=HOST, port=PORT, threaded=True)
