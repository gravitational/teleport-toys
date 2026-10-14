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
