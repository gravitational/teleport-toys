# Beams - VS Code Extension

Powered by [Teleport](https://goteleport.com/). Manage and connect to Teleport Beams directly from VS Code. Create, monitor, and SSH into ephemeral agentic runtimes.

This directory contains the source for the extension — there is no separate server component. Everything is driven through the `tsh` CLI (see `src/tsh.ts`), which the extension shells out to for beam lifecycle, file access, git operations, and cluster status.

This project lives in [`gravitational/teleport-toys`](https://github.com/gravitational/teleport-toys), Teleport's collection of small open-source projects and experiments. 

## Features

- **Beams Runtimes sidebar** — list, create, rename, and delete beams; each row expands into a detail dropdown (ID, URL, region, phase, roles, expires) instead of relying on a tooltip
- **Phase Snapshots panel** — build and manage reusable Phase images (`tsh phase ls/add/edit/snapshot/rm/apply/ls-failed`): edit a Phase's last successful snapshot, save a new snapshot, apply a Phase configuration YAML, inspect failed build snapshots, or base a new beam directly on an existing Phase
- **Clusters panel** — shows all logged-in Teleport profiles (active + inactive), with roles, logins, and session expiry
- **File explorer** — browse and edit beam files without SSH, with live change indicators as files are modified on the remote beam; upload files via the toolbar button, per-folder, or drag-and-drop, and download files/folders back to your machine
- **Agent Events** — chronological stream of agent session events, auto-detected from the most recently modified JSONL transcript under any `~/.<tool>` dotdir (Claude Code's schema is parsed natively; other agents get a best-effort generic view)
- **Remote-SSH** — one-click VS Code Remote-SSH connection to any beam, with automatic `~/.ssh/config` management
- **Terminal** — open a `tsh` SSH session in the integrated terminal
- **Source Control** — native SCM panel backed by the beam's git with QuickDiff gutter indicators against `HEAD`.
- **File decorations** — git status badges (modified/added/untracked) on files in the beam file explorer
- **Local Debug Containers** — mirror a beam's workspace into a local Docker container, kept in sync automatically while you work, with rebuild/teardown controls
- **Publish / Unpublish** — expose a beam's port 8080 app publicly and copy its URL
- **Run on Beam** — run a command on a beam, publish its port, and open it in the browser
- **Export** — download beam files as a `tar.gz` archive
- **Login** — pick from any cluster you've already logged into, or enter a new proxy address, from a single Login command (surfaced in both the Clusters and Beams panels)
- **GitHub credentials** — PAT, OAuth (`gh` browser flow), or Teleport Git Proxy authentication for git operations on a beam, with saved preferences for auto-setup on new beams

## Prerequisites

- A Beams account — sign up at [beams.run](https://www.beams.run/)
- [Teleport](https://goteleport.com/docs/installation/) (`tsh` CLI) installed and in your PATH
- Logged in to your Teleport cluster: `tsh login --proxy=<cluster>.beams.sh`
- Docker (only required if you use Local Debug Containers)
- VS Code 1.85+

## Install

Install the **Beams** extension from the VS Code Marketplace, or in VS Code: Extensions panel > search "Beams" > Install.

To install from a `.vsix` file instead (e.g. a pre-release build someone sent you): Extensions panel > `...` menu > "Install from VSIX..." and select the file, or `code --install-extension <file>.vsix`.

## Usage

1. Open the **Beams** panel in the activity bar (left sidebar). It has views for **Beams Runtimes**, **Phase Snapshots**, **Files**, **Agent Events**, and **Clusters**.
2. Click **Login** (key icon) if not already authenticated — pick a cluster you've logged into before, or enter a new proxy address.
3. Click **Create Beam** (+) to spin up a new VM, optionally basing it on a saved Phase, applying your saved GitHub credentials, and enabling a local debug container.
4. Click a beam to select it — the Files and Agent Events panels populate, and SCM/git integration activates if the beam has a git repo.
5. Expand a beam row to see its details (ID, URL, region, phase, roles, expires).
6. Use the inline buttons on each beam:
   - `$(remote)` — open a Remote-SSH session in a new VS Code window
   - `$(globe)` — open the beam's published app in the browser
   - `$(terminal)` — open a `tsh` SSH terminal
7. Right-click a beam for more actions: open files, publish/unpublish, export, setup GitHub, rename, delete, and local debug container controls (when enabled for that beam).
8. In the Files view, upload files via the toolbar button, by right-clicking a folder, or by dragging files from your OS onto a folder; right-click a file or folder to download it.
9. Use the Phase Snapshots view to build reusable Phase images and manage their snapshots.

## Commands

| Command | Description |
|---------|-------------|
| Beams: Login | Authenticate with Teleport |
| Beams: Refresh | Refresh the beams list |
| Beams: Create Beam | Create a new beam |
| Beams: Rename Beam | Rename a beam |
| Beams: Delete Beam | Delete a beam |
| Beams: Open in VS Code (Remote-SSH) | Connect via Remote-SSH |
| Beams: SSH into Beam | Open terminal session |
| Beams: Open Beam Files | Browse files remotely |
| Beams: Refresh Files | Refresh the file explorer |
| Beams: Upload Files | Upload local files to a beam folder |
| Beams: Download | Download a remote file or folder |
| Beams: Open in Browser | Run a command on a beam, publish its port, and open it |
| Beams: Publish / Unpublish Beam | Make a beam accessible via URL |
| Beams: Copy URL | Copy a published beam's URL |
| Beams: Export as Zip | Download beam contents as a `tar.gz` archive |
| Beams: Setup GitHub on Beam | Configure git identity and GitHub auth (PAT / OAuth / Teleport Git Proxy) |
| Beams: Show Diff | Show a file's diff against `HEAD` |
| Beams: Stage / Unstage File | Git staging from the SCM panel |
| Beams: Refresh Git Status | Force a git status poll |
| Beams: Create Phase | Build a new reusable Phase image |
| Beams: Manage Phase | Edit, snapshot, or delete a Phase |
| Beams: Apply Phase Configuration (YAML) | Create/update a Phase from a YAML config file |
| Beams: View Failed Phase Snapshots | Inspect Phase builds that failed |
| Beams: Local Debug Container — Open | Open a local Docker mirror of a beam's workspace |
| Beams: Local Debug Container — Sync Now | Force an immediate sync pass |
| Beams: Local Debug Container — Rebuild Image | Rebuild the container image |
| Beams: Local Debug Container — Delete | Tear down the local container |

See the Command Palette (`Cmd+Shift+P` → "Beams:") for the full set.

## Configuration

All settings live under the `beams.*` namespace (see `package.json` → `contributes.configuration` for the full schema):

| Setting | Default | Description |
|---------|---------|--------------|
| `beams.github.username` | `""` | GitHub username for automatic credential setup on new beams |
| `beams.github.email` | `""` | Git commit email for new beams |
| `beams.github.authMethod` | `""` | Preferred GitHub auth method (`pat`, `oauth`, `tsh-git`); empty asks every time |
| `beams.github.autoSetup` | `true` | Automatically configure GitHub on new beams using stored preferences |
| `beams.github.defaultCloneRepo` | `""` | Repository to clone automatically on new beams (`owner/repo`) |
| `beams.git.enabled` | `true` | Enable Git/SCM integration for beams with git repositories |
| `beams.git.statusPollInterval` | `5` | Seconds between git status polls |
| `beams.git.fileStatPollInterval` | `3` | Seconds between open-file mtime polls |
| `beams.container.enabled` | `true` | Global kill-switch for local debug container sync |
| `beams.container.syncMinInterval` | `3` | Minimum seconds between automatic sync passes |
| `beams.container.baseImage` | `debian:bookworm-slim` | Base Docker image for generated local debug containers |
