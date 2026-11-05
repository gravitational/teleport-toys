import * as vscode from 'vscode';
import { Beam, tshBinary } from './tsh';

export function openBeamTerminal(beam: Pick<Beam, 'id'>): vscode.Terminal {
    const tshPath = tshBinary();
    const terminal = vscode.window.createTerminal({
        name: `Beam: ${beam.id}`,
        shellPath: tshPath,
        shellArgs: ['beams', 'ssh', beam.id],
        iconPath: new vscode.ThemeIcon('terminal'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseAddTerminal(name?: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: name ? `Phase: ${name}` : 'Phase: new',
        shellPath: tshBinary(),
        shellArgs: name ? ['phase', 'add', name] : ['phase', 'add'],
        iconPath: new vscode.ThemeIcon('package'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseAddForkTerminal(name: string, forkFrom: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Phase: ${name}`,
        shellPath: tshBinary(),
        shellArgs: ['phase', 'add', name, `--fork-from=${forkFrom}`],
        iconPath: new vscode.ThemeIcon('git-branch'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseEditTerminal(name: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Phase: ${name}`,
        shellPath: tshBinary(),
        shellArgs: ['phase', 'edit', name],
        iconPath: new vscode.ThemeIcon('edit'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseSnapshotTerminal(name?: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: name ? `Phase: ${name}` : 'Phase: snapshot',
        shellPath: tshBinary(),
        shellArgs: name ? ['phase', 'snapshot', name] : ['phase', 'snapshot'],
        iconPath: new vscode.ThemeIcon('save'),
    });
    terminal.show();
    return terminal;
}

export function openPhaseApplyTerminal(file: string): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: 'Phase: apply',
        shellPath: tshBinary(),
        shellArgs: ['phase', 'apply', `--file=${file}`],
        iconPath: new vscode.ThemeIcon('cloud-upload'),
    });
    terminal.show();
    return terminal;
}
