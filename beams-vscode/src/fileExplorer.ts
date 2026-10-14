import * as vscode from 'vscode';
import { Beam, execOnBeam } from './tsh';
import { reportTshError } from './notify';
import { PollConsumer } from './polling';

interface FileEntry {
    name: string;
    path: string;
    isDirectory: boolean;
    beamId: string;
}

class FileItem extends vscode.TreeItem {
    constructor(public readonly entry: FileEntry, changed: boolean) {
        super(
            entry.name,
            entry.isDirectory
                ? vscode.TreeItemCollapsibleState.Collapsed
                : vscode.TreeItemCollapsibleState.None
        );

        this.iconPath = entry.isDirectory
            ? new vscode.ThemeIcon('folder')
            : new vscode.ThemeIcon('file');

        this.contextValue = entry.isDirectory
            ? 'beamFolder'
            : (changed ? 'beamFileChanged' : 'beamFile');

        this.resourceUri = vscode.Uri.parse(`beam://${entry.beamId}${entry.path}`);

        if (!entry.isDirectory) {
            this.command = {
                command: 'beams.openFile',
                title: 'Open File',
                arguments: [entry],
            };
        }
    }
}

export class BeamFileExplorer implements vscode.TreeDataProvider<FileItem>, PollConsumer {
    private _onDidChangeTreeData = new vscode.EventEmitter<FileItem | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    private currentBeam: Beam | undefined;
    private changedPaths = new Set<string>();

    setBeam(beam: Beam): void {
        this.currentBeam = beam;
        this.changedPaths.clear();
        this._onDidChangeTreeData.fire(undefined);
    }

    // PollConsumer: track which absolute paths have uncommitted git changes so file rows
    // can show a "Show Diff" inline action — mirrors the parsing in fileDecorations.ts/scm.ts,
    // each consumer keeps its own view of the porcelain output for what it needs.
    onGitStatus(beamId: string, repoRoot: string, porcelain: string): void {
        if (beamId !== this.currentBeam?.id) return;

        const changed = new Set<string>();
        for (const line of porcelain.split('\n')) {
            if (line.length < 4) continue;
            const x = line[0];
            const y = line[1];
            const filePath = line.slice(3).trim();
            if (!filePath || (x === ' ' && y === ' ')) continue;

            const absPath = filePath.startsWith('/') ? filePath : `${repoRoot}/${filePath}`;
            changed.add(absPath);
        }
        this.changedPaths = changed;
        this._onDidChangeTreeData.fire(undefined);
    }

    getBeam(): Beam | undefined {
        return this.currentBeam;
    }

    refresh(): void {
        this._onDidChangeTreeData.fire(undefined);
    }

    getTreeItem(element: FileItem): vscode.TreeItem {
        return element;
    }

    async getChildren(element?: FileItem): Promise<FileItem[]> {
        if (!this.currentBeam) {
            return [];
        }

        const dirPath = element ? element.entry.path : '/home/beams';
        const beamId = this.currentBeam.id;

        try {
            const output = await execOnBeam(beamId, ['ls', '-1F', dirPath]);

            if (!output.trim()) {
                return [];
            }

            const entries: FileItem[] = [];
            for (const line of output.trim().split('\n')) {
                if (!line) continue;
                const isDirectory = line.endsWith('/');
                const isLink = line.endsWith('@');
                const isExec = line.endsWith('*');
                const name = (isDirectory || isLink || isExec)
                    ? line.slice(0, -1)
                    : line;
                if (!name || name.startsWith('.')) continue;

                const path = dirPath.endsWith('/')
                    ? `${dirPath}${name}`
                    : `${dirPath}/${name}`;

                entries.push(new FileItem({
                    name,
                    path,
                    isDirectory,
                    beamId,
                }, this.changedPaths.has(path)));
            }

            entries.sort((a, b) => {
                if (a.entry.isDirectory !== b.entry.isDirectory) {
                    return a.entry.isDirectory ? -1 : 1;
                }
                return a.entry.name.localeCompare(b.entry.name);
            });

            return entries;
        } catch (err) {
            reportTshError(err, { beamId, action: 'list files' });
            return [];
        }
    }
}
