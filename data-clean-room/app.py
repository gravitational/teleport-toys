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
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

import flask.cli
from flask import Flask, Response, request, send_from_directory, stream_with_context

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


class Worker:
    """One disposable worker Beam. In LOCAL mode, a temp directory stands in."""

    def __init__(self):
        self.beam_id = None
        self.local_dir = None
        self.copied_in = {}
        self.image = "default Beam image"

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
        result = run_cmd(cmd, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"beam provisioning failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        if isinstance(data, list):
            data = data[0]
        self.beam_id = data.get("id") or data.get("ID")
        if not self.beam_id:
            raise RuntimeError(f"could not find beam id in: {result.stdout.strip()}")
        return self.beam_id

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
            raise RuntimeError((r.stderr or r.stdout).strip())
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
        if LOCAL:
            if self.local_dir:
                shutil.rmtree(self.local_dir, ignore_errors=True)
            return
        if self.beam_id:
            run_cmd(["tsh", "beams", "rm", self.beam_id], timeout=60)


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
    return worker.sh(
        f'cd {worker.path("")} && claude -p "$(cat prompt.txt)" --permission-mode bypassPermissions',
        timeout=timeout,
    ).strip()


# --------------------------------------------------------------------------
# Privacy gate (runs in the orchestrator, after the agent has finished)
# --------------------------------------------------------------------------

BASIC_PATTERNS = [
    ("SSN-like pattern", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("bank-account-like pattern", re.compile(r"\bXXXX-\d{3,}\b")),
    ("email address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("long raw digit sequence (9+ digits)", re.compile(r"\b\d{9,}\b")),
]
STRICT_PATTERNS = [
    ("currency amount", re.compile(r"\$\s?\d[\d,]*(\.\d+)?\s?[kKmM]?|\b\d{1,3}(,\d{3})+(\.\d+)?\b|\b\d{5,7}(\.\d+)?\b|\b\d+(\.\d+)?\s?[kK]\b|\bUSD\b")),
    ("employee or manager code", re.compile(r"\b[0-9a-f]{10}\b")),
    ("reference to a very small group",
     re.compile(r"\b(only one|the only|a single|sole|one person|[1-4] (people|person|employees?|managers?|reps?))\b", re.I)),
]

# Phrases that describe a metric's DEFINITION, not a group of people.
# Example: M2 is literally "managers with 1 or 2 direct reports".
# These are removed before the small-group check runs.
ALLOWED_PHRASES = re.compile(
    r"\b(1|one)\s*(-|–|or|to)\s*(2|two)\s+(direct\s+)?reports?\b"
    r"|\b(1|one)\s*(-|–|or|to)\s*(2|two)[- ]person teams?\b"
    r"|\bvery small teams?\b",
    re.I,
)


def gate_text(text, strict):
    """Returns a list of reasons, each with the phrase that triggered it."""
    text = text or ""
    patterns = BASIC_PATTERNS + (STRICT_PATTERNS if strict else [])
    reasons = []
    for name, p in patterns:
        checked = ALLOWED_PHRASES.sub(" ", text) if name == "reference to a very small group" else text
        m = p.search(checked)
        if m:
            reasons.append(f'{name} ("{m.group(0).strip()[:40]}")')
    return reasons


def gate_dashboard(metrics, insights):
    """Structural checks on the numbers plus text checks on the insights."""
    reasons = []
    for org, block in metrics["orgs"].items():
        for mid, m in block.items():
            if isinstance(m, dict) and m.get("value") is not None and m.get("n", 0) < MIN_GROUP:
                reasons.append(f"{mid} for {org} is based on {m['n']} people (minimum {MIN_GROUP})")
    for org, items in insights.items():
        for item in items:
            text = " ".join(str(item.get(k, "")) for k in ("what", "why", "next_step"))
            for r in gate_text(text, strict=True):
                reasons.append(f"{org} insight contains a {r}")
    return reasons


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
- Talk about rates and shares, not head counts. Don't write phrases like
  "only one", "a single", "the only" or "3 people".
- For M2, call it "very small teams" (managers with 1 or 2 direct reports);
  don't count or describe specific teams.

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
        f"{metrics['min_group']} people is suppressed.</p>{''.join(cards)}</div>"
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
        f"cd {worker.path('')} && "
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


def use_phase_toolkit(worker, emit):
    """Phase mode: the playbook and metrics script are already in the image.
    Copy them into the run folder, and warn if they differ from this repo's
    worker/ files (meaning the Phase needs rebuilding with `tsh phase apply`)."""
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
        emit("blocked_warning",
             f"The {PHASE} image is out of date: {', '.join(stale)} differ from this repo. "
             "This run uses the image's version. Rebuild it with: tsh phase apply -f phase.yaml")
        worker.image = f"{PHASE} (Phase, OUT OF DATE: {', '.join(stale)})"
    else:
        worker.image = f"{PHASE} (Phase, toolkit matches repo)"


def playbook_flow(worker, dataset, emit):
    if DATA_SOURCE == "database" and not LOCAL:
        emit("status", f"Pulling the no-names view from {DB_NAME_TELEPORT} through Teleport, inside the beam...")
        pull_from_database(worker)
    else:
        emit("status", "Copying the raw people data into the beam...")
        worker.put(os.path.join(DATA_DIR, dataset["csv"]), "people_coded.csv")
    if PHASE and not LOCAL:
        emit("status", f"Using the playbook and metrics script built into the {PHASE} image...")
        use_phase_toolkit(worker, emit)
    else:
        emit("status", "Copying the playbook and metrics script into the beam...")
        worker.put(os.path.join(WORKER_DIR, "playbook.md"), "playbook.md")
        worker.put(os.path.join(WORKER_DIR, "compute_metrics.py"), "compute_metrics.py")

    emit("status", "Computing org-level metrics inside the beam (raw rows stay in the beam)...")
    worker.sh(f"cd {worker.path('')} && python3 compute_metrics.py people_coded.csv metrics.json", timeout=60)
    run_dir = os.path.join(REPORTS_DIR, uuid.uuid4().hex[:8])
    os.makedirs(run_dir)
    metrics_path = os.path.join(run_dir, "metrics.json")
    worker.get("metrics.json", metrics_path)
    metrics = json.load(open(metrics_path))

    feedback, insights, reasons = "", None, []
    for attempt in (1, 2, 3):
        emit("status", f"Claude is writing insights inside the beam (attempt {attempt})...")
        prompt = INSIGHT_PROMPT.format(min_group=MIN_GROUP, feedback=feedback)
        raw = run_claude(worker, prompt)
        insights = canned_insights(metrics) if raw is None else parse_json(raw)
        emit("status", "Privacy gate: checking group sizes, codes and dollar amounts...")
        reasons = gate_dashboard(metrics, insights)
        if not reasons:
            break
        emit("blocked", reasons)
        feedback = ("\nYour previous answer was REJECTED by the privacy gate. The exact "
                    "phrases that triggered it are in quotes: " + "; ".join(reasons)
                    + ". Rewrite those insights without that kind of phrase "
                    "(use rates or shares instead) and return the full JSON again.")
    if reasons:
        raise RuntimeError("privacy gate blocked the report 3 times; nothing was released")

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
    run_dir = os.path.join(REPORTS_DIR, uuid.uuid4().hex[:8])
    os.makedirs(run_dir)
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
   else if(d.type==='warning')addMsg('blocked','Warning: '+d.msg);
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


@app.route("/analyze", methods=["POST"])
def analyze():
    key = request.json.get("dataset")
    dataset = DATASETS.get(key)

    def sse(kind, **payload):
        return f"data: {json.dumps({'type': kind, **payload})}\n\n"

    def generate():
        if not dataset:
            yield sse("error", msg=f"Unknown dataset: {key}")
            return
        events = []

        def emit(kind, value):
            if kind == "blocked_warning":
                events.append(sse("warning", msg=value))
            elif kind == "blocked":
                events.append(sse("blocked", reasons=value))
            elif kind == "dashboard":
                events.append(sse("dashboard", html=value))
            elif kind == "report":
                events.append(sse("report", content=value))
            else:
                events.append(sse("status", msg=value))

        worker = Worker()
        started = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        run_dir, released, outcome = None, [], "failed"
        try:
            yield sse("status", msg="Provisioning a fresh disposable worker beam...")
            beam_id = worker.provision()
            yield sse("status", msg=f"Beam {beam_id} is up. Waiting for it to be ready...")
            worker.wait_ready()
            flow = playbook_flow if dataset["mode"] == "playbook" else freeform_flow
            # run the flow, streaming its events as they happen
            import threading
            result = {}

            def target():
                try:
                    result["value"] = flow(worker, dataset, emit)
                except Exception as exc:  # surfaced below
                    result["error"] = exc

            t = threading.Thread(target=target)
            t.start()
            while t.is_alive() or events:
                while events:
                    yield events.pop(0)
                time.sleep(0.2)
            if "error" in result:
                raise result["error"]
            run_dir = result["value"]
            released = sorted(os.listdir(run_dir))
            outcome = "released"
            rid = os.path.basename(run_dir)
            yield sse("files", links=[[f"/download/{rid}/{f}", f] for f in released])
        except Exception as exc:
            if "privacy gate" in str(exc):
                outcome = "blocked by privacy gate, nothing released"
            yield sse("error", msg=str(exc))
        finally:
            worker.teardown()
            if worker.beam_id:
                yield sse("status", msg=f"Beam {worker.beam_id} torn down. Raw data, credential and VM are gone.")
            receipt = [
                "RUN RECEIPT",
                f"  beam:        {worker.beam_id}",
                f"  image:       {worker.image}",
                f"  started:     {started}",
                f"  destroyed:   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
                f"  copied in:   " + (", ".join(f"{k} (sha256 {v})" for k, v in worker.copied_in.items()) or "-"),
                f"  released:    " + (", ".join(released) or "nothing"),
                f"  outcome:     {outcome}",
                "  query audit: see the Teleport audit log for this beam's session",
            ]
            yield sse("receipt", text="\n".join(receipt))

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/download/<run_id>/<filename>")
def download(run_id, filename):
    return send_from_directory(os.path.join(REPORTS_DIR, run_id), filename, as_attachment=True)


if __name__ == "__main__":
    print("Clean Room running on http://localhost:8080" + (" (LOCAL DRY RUN)" if LOCAL else ""))
    if PHASE and not LOCAL:
        print(f"Worker beams start from the {PHASE} image (Phase). Rebuild after editing worker/: tsh phase apply -f phase.yaml")
    if BEAM_ALIAS:
        print(f"To publish externally: tsh beams publish {BEAM_ALIAS}")
    app.run(host="0.0.0.0", port=8080, threaded=True)
