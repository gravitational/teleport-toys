# CLAUDE.md

Guidance for AI coding agents (Claude Code, Codex, …) working on this project.
`AGENTS.md` is a symlink to this file. User-facing setup is in `README.md`.

## What this is

A Slack bot ("Teleport Beams Bot") plus a `/beams` slash command that does
work in Teleport Beams as the requesting user. It is Teleport's Slack access
plugin (`integrations/access/slack` in `gravitational/teleport`) with a Beams
app added. The code here is **not** a standalone Go module:

- `plugin/*.go` are new files in Teleport's `package slack`.
- `upstream.patch` is the small diff to Teleport's own files in that package
  (`bot.go`, `config.go`, `socketmode.go`): config field, app registration, and
  Socket Mode routing for `slash_commands` and `events_api` envelopes.
- The Teleport release is pinned by `TELEPORT_REF` and `TSH_VERSION` in
  `Dockerfile`; the `tbot` image tag in `docker-compose.yml` and
  `terraform/variables.tf` should match.

## Commands

```bash
make test     # prepare .teleport/ (clone on first run) and go test the package
make vet
make build    # bin/teleport-slack
make fmt      # gofmt plugin/
make patch    # regenerate upstream.patch after editing Teleport's own files
make image    # docker build -t beams-slack-plugin:local .
```

- `scripts/teleport-tree.sh` resets `.teleport/integrations/access/slack`,
  applies `upstream.patch`, and **symlinks** `plugin/*.go` in. Edit `plugin/`
  directly; a new file in `plugin/` needs `make tree` to get linked.
- `TELEPORT_TREE=/path` reuses an existing Teleport checkout.
- Go follows Teleport's `go.mod`; an older local Go downloads the required
  toolchain automatically.
- Tests use a fake `tsh` shell script and fake Slack clients; no cluster or
  workspace is needed. Run `make fmt vet test` before calling a change done.

## Layout

| File | Role |
| --- | --- |
| `beams_app.go` | The `common.App`: Socket Mode loop, slash-command and message handlers, dedupe (`recentSet`), one-run-per-thread queue (`threadQueue`) |
| `beams_commands.go` | `/beams` parsing, Slack→Teleport user resolution, delegation (`userIdentity`), authorization prompt, `tsh` runner with retry |
| `beams_bot.go` | Bot request detection, beam/agent selection, agent prompt, `BEAMS_BOT_ACTION` handling, plan approval, delete/list shortcuts, intro |
| `beams_chat.go` | Quick answers through the tenant's `anthropic`/`openai` apps; `HANDOFF` to a beam |
| `beams_incident.go` | Scripted "live website is down" flow (`incident_response`) |
| `beams_tell.go` | `tell <person> :: <message>` actions and Slack people lookup |
| `beams_files.go` | Slack attachments: download, images to chat, `tsh beams scp` into beams |
| `beams_audit.go` | Structured `Beams bot request` log line; Q&A echo into a beam for audit |
| `beams_slack.go` | Extra `Bot` methods: `response_url` replies, thread posts, file download, `auth.test`, people search |
| `beams_socketmode_test.go` | Socket Mode routing of slash commands and Events API messages |
| `beams_types.go` | Socket Mode event types for slash commands and Events API messages |
| `beams_config.go` | `[beams]` config and its defaults/validation |

## How a bot request flows

1. `socketmode.go` (patched) turns `events_api` envelopes into
   `BeamsMessageEvent` and `slash_commands` into `SlashCommandEvent`.
2. `botRequest` decides if a message is for the bot: `app_mention`, DMs,
   messages starting with "Teleport Beams Bot" (`botPrefix`), or replies in a
   thread the same user already has with the bot. Only the bot's own mention
   is stripped (`BotUserID` from `auth.test`), so `<@someone>` reaches the model.
3. Instant replies first (no auth): the "what is a beam" intro.
4. The user must be connected (delegation). If not, the request is saved to
   `<profile>/pending-request`, the user is DMed the `tsh delegation
   create-session ...` command, and pasting the session ID resumes the request
   in its original thread.
5. Order inside `Handle`: incident flow → delete/list shortcuts → quick chat
   (if the thread has no beam yet) → work in a beam.
6. In a beam: `claude -p --permission-mode plan` (or `codex exec`) over
   `tsh beams exec`, as one shell-quoted string ending in `< /dev/null`.
   The agent ends its reply with `BEAMS_BOT_ACTION: publish|unpublish|create_beam|approve|tell ...`
   lines, which the plugin strips and executes.
7. Plan approval: `approve` writes `threads/<key>.approval`; a whole-message
   yes (`yesReply`) reruns with `--dangerously-skip-permissions --continue`,
   no (`noReply`) cancels, anything else revises the plan.

Per-user state lives in `profiles_dir/<team>/<user>/`: `delegation-session`,
`pending-request`, `threads/<channel>-<ts>` (beam + agent for the thread, `-`
for chat-only), and `.chat`, `.approval`, `.incident-fix`, `.incident-owner`
sidecars.

## Teleport facts the design depends on

Confirmed in Teleport v18 source. Don't "simplify" around them.

- **SSO users cannot be impersonated**, and an impersonated cert can't
  impersonate again. So the bot can't just `tctl auth sign` for users.
- **Delegation sessions** can only be created by the user themself, with MFA,
  max 7 days. There is no list API and no consent web page, so the bot has to
  ask the user to run `tsh delegation create-session` and paste the ID.
- Delegation `GenerateCerts` requires the caller to be a **bot**, which is why
  the plugin identity comes from `tbot`. Both tbot and delegated certs carry
  `disallow-reissue`, so `tsh proxy app` doesn't work; quick answers mint an
  app cert with `RouteToApp` and call the app's public address directly.
- **Never act on beams as the bot.** Published apps are labelled
  `teleport.internal/beams/owner`, and `beam-user` only grants access to your
  own, so bot-owned beams' URLs are unreachable for the human.
- `tsh beams publish` always publishes port 8080. `tsh beams exec` runs one
  string over SSH. A brand-new beam rejects SSH until it gets a node, which
  `tsh beams ls` doesn't expose, so the runner retries that error every 5s for
  up to 3 minutes.
- Plugins can't call `EmitAuditEvent` (built-in servers only), and
  `app.session.llm_request` has no prompt. Hence the structured log line and
  the `printf` Q&A echo into a beam.
- The tenant's OpenAI app is served on the Responses API, not Chat
  Completions; Codex quick answers POST `/v1/responses`.

## Gotchas

- `claude -p` waits 3s for stdin unless it's closed: keep `< /dev/null`.
- In `-p` mode `ExitPlanMode` is disabled, so the plan comes back as text and
  the agent signals it with `BEAMS_BOT_ACTION: approve`.
- Models sometimes write a sentence before `HANDOFF`; `isHandoff` checks for a
  line that is just `HANDOFF` and drops the rest.
- Slash-command args go through `shellwords`, except `claude`/`codex` prompts
  (`agentWords`), which are kept as typed so apostrophes work.
- Slack code formatting is trimmed from beam names (`trimSlackCode`).
- Delete is handled by the plugin only when beam names directly follow
  delete/remove/rm (`removeTargets`), so "remove the header from the page"
  still goes to the agent.
- Command-line args are clipped to 32 KiB (Linux allows 128 KiB per argument).
- Slack returns an HTML login page instead of a file when `files:read` is
  missing; `DownloadSlackFile` turns that into a clear error.

## Conventions

- Keep Teleport's AGPL header on every Go file.
- Keep `upstream.patch` small: put new code in `plugin/`, and only touch
  Teleport's files for hooks. Regenerate it with `make patch`, never by hand.
- Bot copy is plain, friendly, and short. Call a beam an "ephemeral runtime",
  not a "sandbox".
- No tenant names, hostnames, Slack IDs, emails, or tokens in code, tests,
  docs, or screenshots. Use `example-beams-tenant.beams.sh` and made-up
  people.
- Update `README.md` when behaviour or config keys change, and `beamsIntro`
  (`beams_bot.go`) if the Beams product docs change.
