import * as vscode from 'vscode';
import { PhaseItem } from './phaseItem';
import { listPhases, isTshAvailable, classifyTshError, tshErrorMessage } from './tsh';

export class PhasesProvider implements vscode.TreeDataProvider<PhaseItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<PhaseItem | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    private refreshInterval: NodeJS.Timeout | undefined;

    constructor() {
        this.refreshInterval = setInterval(() => this.refresh(), 30000);
    }

    refresh(): void {
        this._onDidChangeTreeData.fire(undefined);
    }

    dispose(): void {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
        }
    }

    getTreeItem(element: PhaseItem): vscode.TreeItem {
        return element;
    }

    async getChildren(): Promise<PhaseItem[]> {
        const available = await isTshAvailable();
        if (!available) {
            vscode.window.showErrorMessage(
                'tsh is not installed or not in PATH. Install Teleport: https://goteleport.com/docs/installation/'
            );
            return [];
        }

        try {
            const phases = await listPhases();
            return phases.map(p => new PhaseItem(p));
        } catch (err: unknown) {
            if (classifyTshError(err) === 'auth') {
                vscode.window.showWarningMessage('Not logged in to Teleport. Run: tsh login');
            } else {
                console.warn(`Beams: failed to list phases: ${tshErrorMessage(err)}`);
            }
            return [];
        }
    }
}
