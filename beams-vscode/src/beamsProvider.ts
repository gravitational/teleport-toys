import * as vscode from 'vscode';
import { BeamItem, BeamDetailItem } from './beamItem';
import { listBeams, isTshAvailable, classifyTshError, tshErrorMessage } from './tsh';

type BeamTreeItem = BeamItem | BeamDetailItem;

export class BeamsProvider implements vscode.TreeDataProvider<BeamTreeItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<BeamTreeItem | undefined | null>();
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

    getTreeItem(element: BeamTreeItem): vscode.TreeItem {
        return element;
    }

    async getChildren(element?: BeamTreeItem): Promise<BeamTreeItem[]> {
        if (element instanceof BeamItem) {
            const beam = element.beam;
            return [
                new BeamDetailItem('ID', beam.id, 'tag'),
                new BeamDetailItem('URL', beam.url || 'not published', 'link'),
                new BeamDetailItem('Region', beam.region ?? beam.requested_region ?? '', 'globe'),
                new BeamDetailItem('Phase', beam.phase ?? '', 'package'),
                new BeamDetailItem('Roles', beam.roles ? beam.roles.join(', ') : '*', 'shield'),
                new BeamDetailItem('Expires', new Date(beam.expires).toLocaleString(), 'clock'),
            ];
        }
        if (element instanceof BeamDetailItem) {
            return [];
        }

        const available = await isTshAvailable();
        if (!available) {
            vscode.window.showErrorMessage(
                'tsh is not installed or not in PATH. Install Teleport: https://goteleport.com/docs/installation/'
            );
            return [];
        }

        try {
            const beams = await listBeams();
            return beams.map(b => new BeamItem(b));
        } catch (err: unknown) {
            if (classifyTshError(err) === 'auth') {
                vscode.window.showWarningMessage('Not logged in to Teleport. Run: tsh login');
            } else {
                console.warn(`Beams: failed to list beams: ${tshErrorMessage(err)}`);
            }
            return [];
        }
    }
}
