"""Fake `tsh` / `tctl` for testing the shell scripts without a Teleport cluster.

Invoked through tiny wrappers as:  fake_teleport.py <tsh|tctl> <args...>
Behavior comes from the JSON file named by $FAKE_TELEPORT; every call is appended to $FAKE_TELEPORT_LOG.
`tsh beams exec` and `tsh beams scp` act on a local directory (state["beam_home"]), so the scripts under
test really run end to end, just on this machine instead of in a beam.
"""
import json
import os
import shutil
import subprocess
import sys
import time

prog, args = sys.argv[1], sys.argv[2:]
STATE = {
    "logged_in": True, "version": "18.11.9", "user": "alice", "roles": ["picar-driver"],
    "cluster": "example.beams.run", "proxy": "example.beams.run:443",
    "mcp_servers": [{"name": "beambot-sim-arm", "labels": {"robot": "beambot-sim"},
                     "tools": ["sense_arm_pose", "act_arm_home", "safety_stop"]},
                    {"name": "beambot-sim-drive", "labels": {"robot": "beambot-sim"},
                     "tools": ["sense_drive_state", "act_drive", "safety_stop"]},
                    {"name": "other-app", "labels": {"team": "x"}, "tools": ["noop"]}],
    "dbs": ["beambot-postgres"], "connect_denied": False, "tunnel_fail": [], "beam_name": "fake-beam",
    "beam_home": "/tmp/fake-beam-home", "ready_after": 0, "counter_file": "/tmp/fake-teleport-counter",
}
STATE.update(json.load(open(os.environ["FAKE_TELEPORT"])))
with open(os.environ["FAKE_TELEPORT_LOG"], "a") as f:
    f.write(json.dumps({"prog": prog, "args": args,
                        "env": {k: v for k, v in os.environ.items() if k.startswith("TELEPORT_")}}) + "\n")


def out(s=""):
    print(s, flush=True)


def err(s, code=1):
    print(s, file=sys.stderr, flush=True)
    sys.exit(code)


def flag(name, default=None):
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def selected_servers():
    labels = flag("--labels") or next((a for a in args[2:] if "=" in a and not a.startswith("-")), None)
    servers = STATE["mcp_servers"]
    if labels:
        k, v = labels.split("=", 1)
        servers = [s for s in servers if s["labels"].get(k) == v]
    return servers


if prog == "tctl":
    if args[:2] == ["tokens", "add"]:
        out("fake-join-token-0123")
    else:
        out("{}")
    sys.exit(0)

cmd = args[0] if args else ""
sub = args[1] if len(args) > 1 else ""

if cmd == "status":
    if not STATE["logged_in"]:
        err("ERROR: Not logged in.")
    if any(a.startswith("--format") for a in args) or "-f" in args:
        out(json.dumps({"active": {"username": STATE["user"], "roles": STATE["roles"], "cluster": STATE["cluster"],
                                   "valid_until": "2030-01-01T00:00:00Z",
                                   "profile_url": "https://" + STATE["proxy"]}}))
    else:
        out(f"> Profile URL:  https://{STATE['proxy']}\n  Logged in as:  {STATE['user']}\n  Roles:         {', '.join(STATE['roles'])}")
elif cmd == "version":
    out(f"Teleport v{STATE['version']} git:v{STATE['version']}-0-gabcdef go1.24")
elif cmd == "mcp" and sub == "ls":
    out("Name  Description  Command  Type  Labels  Allowed Tools\n----  -----------  -------  ----  ------  -------------")
    for s in selected_servers():
        out(f"{s['name']}  fake  -  stdio  " + ",".join(f"{k}={v}" for k, v in s["labels"].items()) + "  " + str(s["tools"]))
elif cmd == "mcp" and sub == "config":
    path = flag("--client-config")
    cfg = json.load(open(path)) if path and os.path.exists(path) else {"mcpServers": {}}
    for s in selected_servers():
        cfg.setdefault("mcpServers", {})["teleport-mcp-" + s["name"]] = {
            "command": "tsh", "args": ["mcp", "connect", s["name"]]}
    if path:
        json.dump(cfg, open(path, "w"), indent=2)
    out("ok")
elif cmd == "mcp" and sub == "connect":
    if STATE["connect_denied"]:
        err("access denied: this identity may not use that MCP server")
    app = args[2]
    server = next((s for s in STATE["mcp_servers"] if s["name"] == app), None)
    if not server:
        err(f"ERROR: MCP server {app} not found")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            out(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": app, "version": "0"}}}))
        elif msg.get("method") == "tools/list":
            out(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": t, "inputSchema": {"type": "object"}} for t in server["tools"]]}}))
elif cmd == "apps" and sub == "ls":
    out("Application  Description Type Public Address  Labels\n------------ ----------- ---- --------------- ------")
    if STATE.get("llm_apps", True):
        out("anthropic  LLM  anthropic.example.beams.run\nopenai  LLM  openai.example.beams.run")
    for s_ in STATE["mcp_servers"]:
        out(f"{s_['name']}  MCP  {s_['name']}.example.beams.run")
elif cmd == "db" and sub == "ls":
    out("Name  Description  Allowed Users  Labels  Connect\n----  -----------  -------------  ------  -------")
    for d in STATE["dbs"]:
        out(f"{'> ' if d in STATE.get('db_active', []) else ''}{d}  fake  [app]  env=test")
elif cmd == "proxy":
    kind = sub
    if kind in STATE["tunnel_fail"]:
        err(f"ERROR: access denied to {kind} tunnel")
    time.sleep(120)
elif cmd == "beams" and sub == "add":
    out(json.dumps({"alias": STATE["beam_name"], "id": "0000-1111"}))
elif cmd == "beams" and sub == "ls":
    out(json.dumps([{"id": STATE["beam_name"], "expires": "2030-01-01T00:00:00Z"}]))
elif cmd == "beams" and sub == "publish":
    # exact shape of real output (incl. the 4-char suffix on the host)
    out(f'Beam "{args[2]}" successfully published.\n\nURL: https://{args[2]}-4618.example.beams.run')
elif cmd == "beams" and sub == "scp":
    paths = [a for a in args[2:] if not a.startswith("-")]
    src, dst = paths[0], paths[1]
    name, _, remote = dst.partition(":")
    target = os.path.join(STATE["beam_home"], remote.replace("/home/beams", "").lstrip("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy(src, target)
elif cmd == "beams" and sub == "exec":
    command = args[3] if len(args) > 3 else ""
    if command == "true":
        n = int(open(STATE["counter_file"]).read()) if os.path.exists(STATE["counter_file"]) else 0
        open(STATE["counter_file"], "w").write(str(n + 1))
        sys.exit(0 if n >= STATE["ready_after"] else 1)
    if "curl" in command:
        out("200")
        sys.exit(0)
    if "tmux" in command:           # the app itself is not started in script tests
        sys.exit(0)
    home = STATE["beam_home"]
    os.makedirs(home, exist_ok=True)
    r = subprocess.run(["bash", "-c", command.replace("/home/beams", home)],
                       env={**os.environ, "HOME": home, "HANDSHAKE_WAIT": "0"})
    sys.exit(r.returncode)
else:
    pass
