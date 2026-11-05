import * as vscode from 'vscode';
import { Phase } from './tsh';

export class PhaseItem extends vscode.TreeItem {
    constructor(public readonly phase: Phase) {
        super(phase.name, vscode.TreeItemCollapsibleState.None);

        this.description = `${phase.state} · ${phase.region}`;
        this.tooltip = `Owner: ${phase.owner}\nState: ${phase.state}\nRegion: ${phase.region}\nParent: ${phase.parent}${phase.last_error ? `\nLast error: ${phase.last_error}` : ''}`;
        this.contextValue = phase.state === 'failed' ? 'phaseFailed' : 'phase';
        this.iconPath = phase.state === 'failed'
            ? new vscode.ThemeIcon('error', new vscode.ThemeColor('errorForeground'))
            : phase.state === 'building'
                ? new vscode.ThemeIcon('sync')
                : new vscode.ThemeIcon('package');

        this.command = {
            command: 'beams.phase.manage',
            title: 'Manage Phase',
            arguments: [this],
        };
    }
}
