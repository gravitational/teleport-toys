import * as vscode from 'vscode';
import { Beam, tshBinary } from './tsh';

// Every terminal below runs its real command (tsh, docker) as the terminal's own shell
// process, so any non-zero exit — including entirely expected ones, like a beam
// disappearing mid-session — makes VS Code's own "terminal process terminated with exit
// code" notification fire. This extension's own error reporting never sees it, since that
// only wraps `execFile` calls (see notify.ts), not terminal exit events, and VS Code has no
// supported way to suppress that alert for a process-backed (shellPath/shellArgs) terminal
// short of reimplementing it as an extension-owned Pseudoterminal — which, to preserve
// today's interactive fidelity (resize, raw mode, colors), would need a real local pty via
// `node-pty`, a native dependency this project deliberately doesn't have (it ships zero
// runtime deps and packages with `vsce package --no-dependencies`).
//
// Instead, run the real command through a shell that always reports a clean exit to VS
// Code. The command's own output — including any failure text it prints — still appears in
// the terminal exactly as before; only the extra native popup on top is suppressed. Each
// argument is passed to `sh` as a distinct argv entry (not interpolated into the script
// string), so there's no shell-injection risk from beam ids, phase names, or file paths.
//
// Not yet extended to Windows (no `cmd.exe`/PowerShell wrapping has been verified safe for
// arbitrary arguments) — terminals there still show the native alert as before.
export function suppressExitAlert(cmd: string, args: string[]): { shellPath: string; shellArgs: string[] } {
    if (process.platform === 'win32') {
        return { shellPath: cmd, shellArgs: args };
    }
    return {
        shellPath: '/bin/sh',
        shellArgs: ['-c', '"$@"; exit 0', cmd, cmd, ...args],
    };
}

// Tracks open `beams.ssh` terminals per beam id, so a terminal can be closed proactively
// (via `closeBeamTerminals`) the moment the extension learns its beam is gone — before the
// underlying `tsh beams ssh` process notices the dropped connection and exits on its own.
// Disposing a terminal ourselves is a deliberate closure, not an unexpected process exit,
// so it doesn't trigger VS Code's native "terminal process terminated" alert.
const openTerminals = new Map<string, Set<vscode.Terminal>>();

export function untrackBeamTerminal(terminal: vscode.Terminal): void {
    for (const [beamId, terminals] of openTerminals) {
        if (terminals.delete(terminal) && terminals.size === 0) {
            openTerminals.delete(beamId);
        }
    }
}

export function closeBeamTerminals(beamId: string): void {
    const terminals = openTerminals.get(beamId);
    if (!terminals) {
        return;
    }
    openTerminals.delete(beamId);
    for (const terminal of terminals) {
        terminal.dispose();
    }
}

export function openBeamTerminal(beam: Pick<Beam, 'id'>): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Beam: ${beam.id}`,
        ...suppressExitAlert(tshBinary(), ['beams', 'ssh', beam.id]),
        iconPath: new vscode.ThemeIcon('terminal'),
    });
    let terminals = openTerminals.get(beam.id);
    if (!terminals) {
        terminals = new Set();
        openTerminals.set(beam.id, terminals);
    }
    terminals.add(terminal);
    terminal.show();
    return terminal;
}

export function openPhaseAddTerminal(name?: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: name ? `Phase: ${name}` : 'Phase: new',
        ...suppressExitAlert(tshBinary(), name ? ['phase', 'add', name] : ['phase', 'add']),
        iconPath: new vscode.ThemeIcon('package'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseAddForkTerminal(name: string, forkFrom: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Phase: ${name}`,
        ...suppressExitAlert(tshBinary(), ['phase', 'add', name, `--fork-from=${forkFrom}`]),
        iconPath: new vscode.ThemeIcon('git-branch'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseEditTerminal(name: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Phase: ${name}`,
        ...suppressExitAlert(tshBinary(), ['phase', 'edit', name]),
        iconPath: new vscode.ThemeIcon('edit'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseSnapshotTerminal(name?: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: name ? `Phase: ${name}` : 'Phase: snapshot',
        ...suppressExitAlert(tshBinary(), name ? ['phase', 'snapshot', name] : ['phase', 'snapshot']),
        iconPath: new vscode.ThemeIcon('save'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseApplyTerminal(file: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: 'Phase: apply',
        ...suppressExitAlert(tshBinary(), ['phase', 'apply', `--file=${file}`]),
        iconPath: new vscode.ThemeIcon('cloud-upload'),
    });
    terminal.show();
    return terminal;
}
