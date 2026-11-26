# Manual steps (what `beam_mcp_up.sh` automates)

Placeholders: `PROXY=<your-tenant>.beams.run:443`, `APP=<mcp app name>`.

## 1. Create the beam (laptop)

```bash
tsh login --proxy=$PROXY
tsh version                                   # 18.11.9 or newer
tsh beams add --roles=<role-a,role-b>         # always pass --roles: omitting it hands the beam ALL your roles
tsh beams ls
until tsh beams exec <beam> true; do sleep 5; done
```

## 2. Inside the beam

```bash
tsh beams ssh <beam>
tsh status                                    # the delegated user and the roles the beam carries
tsh mcp ls <key=value> -v                     # servers and the tools this identity may call
mkdir -p ~/work && cd ~/work
echo '{"mcpServers":{}}' > mcp.json
tsh mcp config --labels <key=value> --client-config=$PWD/mcp.json
cp mcp.json .mcp.json
```

`tsh mcp config` writes one `teleport-mcp-<app>` stdio entry per server (`tsh mcp connect <app>`). No
environment variables are needed: `tsh` already uses the beam's identity.

## 3. Permission rules

Write `.claude/settings.json` (see SKILL.md §2 for the glob rules):

```json
{"permissions": {"allow": ["mcp__teleport-mcp-<app>__sense_*"], "deny": ["mcp__teleport-mcp-<app>__admin_*"]},
 "enableAllProjectMcpServers": true}
```

## 4. Handshake

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  | (cat; sleep 6) | TELEPORT_DEBUG=false timeout 25 tsh mcp connect <app>
```

A JSON reply containing `"protocolVersion"` means the whole path works. Then run `claude` in `~/work`.

## Where to look when it doesn't

| Question | Command |
|---|---|
| Who am I, and with which roles? | `tsh status` |
| What may this identity call? | `tsh mcp ls <labels> -v` |
| Is the beam's tsh new enough? | `tsh version` (18.11.9+) |
| What did Teleport record? | Audit log in the web UI: events show the user and the beam's workload identity |
