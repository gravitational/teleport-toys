import * as vscode from 'vscode';
import { PollConsumer } from './polling';

export class BeamGitDecorationProvider implements vscode.FileDecorationProvider, PollConsumer, vscode.Disposable {
    private _onDidChangeFileDecorations = new vscode.EventEmitter<vscode.Uri | vscode.Uri[] | undefined>();
    readonly onDidChangeFileDecorations = this._onDidChangeFileDecorations.event;

    private decorationMap = new Map<string, vscode.FileDecoration>();
    private registration: vscode.Disposable;

    constructor() {
        this.registration = vscode.window.registerFileDecorationProvider(this);
    }

    provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
        if (uri.scheme !== 'beam') return undefined;
        return this.decorationMap.get(uri.toString());
    }

    onGitStatus(_beamId: string, repoRoot: string, porcelain: string): void {
        const oldKeys = new Set(this.decorationMap.keys());
        this.decorationMap.clear();

        for (const line of porcelain.split('\n')) {
            if (line.length < 4) continue;
            const x = line[0];
            const y = line[1];
            const filePath = line.slice(3).trim();
            if (!filePath) continue;

            const absPath = filePath.startsWith('/')
                ? filePath
                : `${repoRoot}/${filePath}`;
            const uri = vscode.Uri.parse(`beam://${_beamId}${absPath}`);

            const code = y !== ' ' && y !== '?' ? y : x;
            const decoration = this.decorationForCode(code, x === '?' && y === '?');
            if (decoration) {
                this.decorationMap.set(uri.toString(), decoration);
                this.propagateToParents(_beamId, absPath);
            }
        }

        const allKeys = new Set([...oldKeys, ...this.decorationMap.keys()]);
        const uris = [...allKeys].map(k => vscode.Uri.parse(k));
        if (uris.length > 0) {
            this._onDidChangeFileDecorations.fire(uris);
        }
    }

    private propagateToParents(beamId: string, absPath: string): void {
        const parts = absPath.split('/');
        for (let i = parts.length - 1; i > 1; i--) {
            const parentPath = parts.slice(0, i).join('/');
            const parentUri = `beam://${beamId}${parentPath}`;
            if (!this.decorationMap.has(parentUri)) {
                this.decorationMap.set(parentUri, {
                    color: new vscode.ThemeColor('gitDecoration.modifiedResourceForeground'),
                    propagate: false,
                });
            }
        }
    }

    private decorationForCode(code: string, isUntracked: boolean): vscode.FileDecoration | undefined {
        if (isUntracked) {
            return {
                badge: 'U',
                color: new vscode.ThemeColor('gitDecoration.untrackedResourceForeground'),
                tooltip: 'Untracked',
            };
        }
        switch (code) {
            case 'M':
                return {
                    badge: 'M',
                    color: new vscode.ThemeColor('gitDecoration.modifiedResourceForeground'),
                    tooltip: 'Modified',
                };
            case 'A':
                return {
                    badge: 'A',
                    color: new vscode.ThemeColor('gitDecoration.addedResourceForeground'),
                    tooltip: 'Added',
                };
            case 'D':
                return {
                    badge: 'D',
                    color: new vscode.ThemeColor('gitDecoration.deletedResourceForeground'),
                    tooltip: 'Deleted',
                };
            case 'R':
                return {
                    badge: 'R',
                    color: new vscode.ThemeColor('gitDecoration.modifiedResourceForeground'),
                    tooltip: 'Renamed',
                };
            case 'U':
                return {
                    badge: '!',
                    color: new vscode.ThemeColor('gitDecoration.conflictingResourceForeground'),
                    tooltip: 'Conflict',
                };
            default:
                return undefined;
        }
    }

    dispose(): void {
        this.registration.dispose();
        this._onDidChangeFileDecorations.dispose();
    }
}
