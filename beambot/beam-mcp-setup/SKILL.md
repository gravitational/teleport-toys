---
name: beam-mcp-setup
description: >-
  Set up a Teleport Beam so Claude Code (or Codex) running inside it can use Teleport-protected MCP servers, with scoped Claude Code allowedTools/deny rules on top of Teleport RBAC. One command creates the beam (optionally limited to chosen roles), wires mcp.json from `tsh mcp config`, writes `.claude/settings.json` permission rules and proves the connection with a real MCP handshake. Use this whenever someone wants an agent in a beam to reach MCP apps (robots, GitHub, databases over MCP), sees MCP errors inside a beam, asks how to configure `--allowedTools` or `.claude/settings.json` for Teleport MCP servers, or is creating a fresh beam for an MCP demo.
---

# Beam MCP setup

Goal: inside a Teleport Beam, `claude` can call tools on Teleport-enrolled MCP servers. Two layers gate
what it may do: **Teleport RBAC** (`mcp.tools` on the roles the beam carries) is the real security
boundary, and **Claude Code permissions** (allow/deny rules) control what the agent may call without asking.

## How identity works in a beam

A beam acts with an identity **delegated from the person who created it**. Teleport runs its own `tbot`
sidecar in every beam, which keeps its certificates fresh, so:

- `tsh` is preinstalled in the beam and already logged in. There is **nothing to enroll or install**.
- `tsh mcp ls`, `tsh mcp config`, `tsh mcp connect`, `tsh proxy app|db|mcp` all just work, as the delegated identity.
- A beam carries ALL of the creator's roles unless you narrow it, which is almost never what you want (it would
  include `access`, `editor`, `auditor`...). Always pass `--roles` (the scripts insist). `tsh beams add --roles=a,b` narrows it to those roles
  (they must be eligible for delegation). Roles from a temporary Access Request are **not** carried over.
- The audit log attributes each action to both the originating user and the beam's own workload identity.
- Model credentials are injected by Teleport's inference proxy (`$ANTHROPIC_BASE_URL`, `$OPENAI_BASE_URL`).

Needs `tsh` 18.11.9+ on your laptop. Reference: <https://goteleport.com/docs/beams/>.

## Workflow

### 0. Gather inputs (ask only for what you can't infer)

- **Which MCP servers**: a label selector such as `robot=beambot-sim`. Check with `tsh mcp ls -v`.
- **Which roles the beam should carry**: ideally a dedicated least-privilege role (one whose `mcp.tools`
  lists only the tools the agent needs) rather than your admin roles. Check `tctl get roles` first for name clashes.
- **Tool policy**: which tool name patterns Claude Code may auto-allow (e.g. `sense_*,safety_*`) and which
  to deny (e.g. `admin_*`).

A role scoped to some MCP apps (`app_labels` select the servers, `mcp.tools` select the tools):

```yaml
kind: role
version: v8
metadata: {name: beambot-sim-observer}
spec:
  allow:
    app_labels:
      robot: beambot-sim
      teleport.internal/app-sub-kind: mcp
    mcp:
      tools: ["sense_*", "safety_*"]   # Teleport hides everything else from tools/list
```

Avoid `deny` rules in roles meant to be combined with others. Teleport deny is greedy across the
whole identity, so it will also block a later grant.

### 1. One command from the laptop

From a normal `tsh login`, with `tsh` 18.11.9+:

```bash
bash scripts/beam_mcp_up.sh --roles beam-user,beambot-sim-observer \
     --labels robot=beambot-sim --allow 'sense_*,safety_*' --deny 'admin_*' \
     [--beam <existing-beam>] [--dir /home/beams/work]
```

It runs `tsh beams add --roles=...` (or reuses `--beam`), waits until the beam is reachable, copies in
`scripts/beam_mcp_setup.sh` and runs it. The in-beam script:

1. checks `tsh status` and the `tsh` version,
2. lists the servers this identity can reach (`tsh mcp ls -v`),
3. writes `<dir>/mcp.json` and `<dir>/.mcp.json` from `tsh mcp config`,
4. writes `<dir>/.claude/settings.json` allow/deny rules and `<dir>/allowed_tools.txt`,
5. runs a real MCP `initialize` handshake through `tsh mcp connect`.

To do it by hand, see `references/manual-steps.md`.

### 2. Claude Code permission rules: get the syntax right

Claude Code names MCP tools `mcp__<server>__<tool>`. `tsh mcp config` names servers
`teleport-mcp-<app>`, so a tool looks like `mcp__teleport-mcp-beambot-sim-arm__act_arm_home`.

- **Allow** rules: `mcp__<server>` (whole server) or `mcp__<server>__<tool>`. A glob is only valid
  in the tool position after a literal server prefix: `mcp__teleport-mcp-beambot-sim-arm__sense_*` is
  fine, but `act_*` or `mcp__*` alone is rejected with "Wildcard tool name ... is not supported in
  allow rules". That's why the setup script expands each pattern per server.
- **Deny** rules accept wildcards anywhere, e.g. `mcp__teleport-mcp-beambot-*__admin_*`.
- Rules can live in `.claude/settings.json` (`permissions.allow` / `permissions.deny`), which is
  persistent and what the script writes, or be passed as `--allowedTools` / `--disallowedTools`.
- `--allowedTools` is **variadic**. A prompt placed after it gets eaten as a rule ("Input must be
  provided either through stdin or as a prompt argument"). Pipe the prompt on stdin instead:

  ```bash
  echo "Look around, don't move." | claude -p --mcp-config ./mcp.json \
       --allowedTools $(paste -sd' ' allowed_tools.txt)
  ```
- Configure MCP servers with `claude mcp add-json <name> '<json>'` or a JSON file. Avoid
  `claude mcp add -e KEY=VAL ...`: its argument parser mis-consumes the following args.
- `--dangerously-skip-permissions` is defensible in a beam for demos, since Teleport RBAC still
  bounds the agent, but say so explicitly rather than defaulting to it.

### 3. Verify, and show the evidence

Inside the beam: `tsh status` (the delegated user and roles), then `tsh mcp ls <labels> -v`. The Allowed
Tools column should match the roles the beam carries. Then a headless Claude run that lists which tools it
can see. A healthy manual `tsh mcp connect <app>` logs `"Start processing messages" ... transport: stdio`
and sits waiting. That's success.

### 4. Changing what the agent may do

The agent can't grant itself anything. A beam's roles are fixed when it is created, so to change them,
create a new beam with different roles:

```bash
bash scripts/beam_mcp_up.sh --roles beam-user,beambot-sim-observer ...     # eyes only
bash scripts/beam_mcp_up.sh --roles beam-user,beambot-sim-operator ...     # hands too
```

An approved Access Request does not flow into a beam by itself. Beams are single-user: only their creator can attach.

## Gotchas that cost time (check these before debugging anything else)

| Symptom | Cause / fix |
|---|---|
| `tsh: error: unknown short flag '-x'` from `tsh beams exec <id> tar -xzf …` | tsh parses flags after `<command>`. Pass the remote command as **one quoted string**. |
| `tsh mcp ls` is empty or lacks tools you expect | A beam carries only the roles you gave it with `--roles`. Include one that grants the MCP app: the built-in `mcp-user` matches every MCP app, but it grants every tool (for the PiCar that includes `resume`, which lifts an emergency stop); prefer a role that lists tools. Check `tsh status` in the beam. Roles from a temporary Access Request are not included. |
| `claude -p` in the beam returns an HTML page, or a model call gets a 302 to `/web/launch/...` | The beam can't see the `openai` / `anthropic` apps. Create it with `--roles beam-user,...`; `tsh apps ls` inside should list them. |
| `ERROR: You do not have permission to delegate the following roles: [...]` from `tsh beams add --roles` | You can only hand a beam roles your own user holds. A role made with `tctl create` is not yours until you add it (`tctl users update <you> --set-roles <current roles>,<new role>`, which replaces the whole list), then `tsh logout` and log in again. The same message appears for blank or empty entries (`" "`, `a,,b`, `a,`). |
| A beam has far more roles than you asked for (`access`, `editor`, `auditor`...) | `--roles=""` is the same as no `--roles`: the beam inherits ALL your roles. An unset shell variable (`--roles="$ROLES"`) does this silently. Check the list is non-empty before calling `tsh beams add`. The scripts here refuse a blank list. |
| `python3 -m venv` fails: no ensurepip, or `pip install` says "externally managed" | The beam image has no python3-venv and marks system Python as managed. Use `python3 -m pip install --user --break-system-packages <pkg>`. |
| `No module named 'mcp.server.fastmcp'` | `pip install mcp` pulled the 2.x SDK, which renamed `FastMCP`. Install `'mcp>=1.11,<2'`. |
| Everything gone after recreating the beam | Beams are ephemeral (by default they expire after 12 hours). Re-run `beam_mcp_up.sh` for the new beam. |

For the manual procedure and a live verification script, see `references/manual-steps.md` and
`scripts/verify_in_beam.sh`.
