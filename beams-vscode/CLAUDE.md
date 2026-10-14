# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
npm run compile        # one-shot TypeScript compile → out/
npm run dev            # watch mode, recompiles on save
npm run package        # compile + vsce package → teleport-beams-<version>.vsix
npm run install-local  # install the built .vsix into VS Code
npm test                # compile + run test/*.test.js (node:test)
```

There is no linter configured. The test suite covers SSH config repair (`test/ssh.test.js`) and tsh error classification (`test/tshError.test.js`) — TypeScript compilation (`tsc`) remains the primary correctness check for everything else.

### Error reporting

Beams are ephemeral, so a beam vanishing mid-session is an expected condition, not a fault. Never report a raw tsh failure with `showErrorMessage` directly — route it through `reportTshError` in `notify.ts` so a disconnect surfaces as information instead of an error dialog. Note that `tsh` emits `cannot relogin in non-interactive session` *alongside* `does not exist` for a gone beam, which is why `classifyTshError` checks disconnect patterns before auth ones.

Press **F5** in VS Code to launch the Extension Development Host (reads `.vscode/launch.json`).

## Architecture

This is a VS Code extension that manages Teleport Beams — ephemeral sandbox VMs for agentic workloads. All source is in `src/`, compiled to `out/` (no bundler, just `tsc`).

### Core data flow

`tsh.ts` is the only file that shells out to the `tsh` CLI. Everything goes through two primitives:
- `execOnBeam(id, cmd[])` → runs `tsh beams exec <id> -- <cmd>` and returns stdout
- Direct `tsh` subcommands (`listBeams`, `addBeam`, `removeBeam`, etc.)

`BeamPoller` (`polling.ts`) is the real-time hub. On a selected beam it polls:
1. `git rev-parse HEAD && git status --porcelain=v1` on a configurable interval (default 5s)
2. `stat --format='%n %Y'` for any open `beam://` files (default 3s)

It notifies registered `PollConsumer` implementors. Polling pauses when VS Code loses focus and restarts on configuration changes.

### Key modules

| File | Role |
|------|------|
| `extension.ts` | Activation entry point — wires all providers together and registers the `beam://` and `beam-git://` filesystem schemes |
| `tsh.ts` | All tsh CLI interactions; defines the `Beam` and `TshStatus` types, plus `classifyTshError` which sorts failures into `disconnected` / `auth` / `other` |
| `notify.ts` | `reportTshError` — picks notification severity from the error class (a gone beam is info, expired login is a warning with a Login action, anything else is an error). A disconnect notice shows once per beam until it's reselected; an unclassified failure first checks `tsh beams ls` and is reported as a disconnect if the beam is gone; other repeats are deduped within 30s |
| `beamsProvider.ts` | `TreeDataProvider` for the Beams panel list |
| `clusters.ts` | `ClustersProvider` — Clusters panel listing logged-in Teleport profiles (`active` + `profiles[]` from `tsh status --format=json`). Note this lists *profiles you have logged into*, not clusters reachable through the proxy — with a single login it renders exactly one row |
| `beamFs.ts` | `FileSystemProvider` for `beam://` URIs — reads/writes remote files via `tsh beams exec` |
| `polling.ts` | `BeamPoller` — polls git status + file mtimes, fans out to consumers |
| `scm.ts` | `BeamGitScmProvider` — VS Code SCM panel, consumes porcelain output from BeamPoller |
| `scmCommands.ts` | Stage/unstage/show-diff/refresh git commands over `tsh beams exec`. Commit, discard, push, and create-pull-request were deliberately removed — do not reintroduce history-writing or remote-publishing operations here |
| `events.ts` | Streams chronological events parsed from the most recently modified JSONL transcript under any `~/.<tool>` dotdir (e.g. `.claude`, `.codex`) — parses Claude Code's schema specifically, with a best-effort generic fallback for other agents' JSONL shapes |
| `agents.ts` | Registry of coding-agent CLI adapters (`claude`, `codex`) — binary detection on a beam, the headless/non-interactive command to draft a summary, and the global memory file each agent auto-loads (`.claude/CLAUDE.md`, `AGENTS.md`). Used by `sessionProfiles.ts` so summary generation isn't tied to one agent |
| `sessionProfiles.ts` | Save/resume workflow for resumable task memory — captures a beam's git ref plus an agent-drafted summary, writes it to `.beams/session-memory/` on the beam, and appends it into whichever agent's global memory file `agents.ts` detected |
| `ssh.ts` | Manages `~/.ssh/config` between `# BEGIN Teleport Beams` / `# END Teleport Beams` markers for Remote-SSH |
| `commands.ts` | Registers all `beams.*` VS Code commands |
| `fileDecorations.ts` | Git status badge decorations on files in the file explorer |

### Virtual filesystem schemes

- **`beam://<beamId><remotePath>`** — writable; reads/writes go through `tsh beams exec cat` / `echo | base64 -d`
- **`beam-git://<beamId><remotePath>`** — read-only; serves HEAD version of files as the QuickDiff baseline

The `BeamGitScmProvider` implements `QuickDiffProvider.provideOriginalResource()` to map `beam://` URIs to their `beam-git://` counterparts, enabling VS Code's inline diff gutter.

### SSH config management

`ensureBeamSshConfig()` in `ssh.ts` writes per-beam entries into `~/.ssh/config`. It:
1. Uses the `.beams.sh` cluster domain reported by `tsh status`
2. Patches the `ProxyCommand` in any existing `tsh config` output to use `tsh proxy ssh` with the beam alias
3. Inserts specific `vscode--<beamId>.<cluster>` `Host` entries before wildcard entries so they match first

### GitHub integration

Three auth methods are supported: `pat` (Personal Access Token stored in VS Code secrets), `oauth` (GitHub CLI browser flow), `tsh-git` (Teleport-managed proxy, no token needed). Preferences are stored in VS Code global configuration under `beams.github.*`.
