import * as vscode from 'vscode';
import * as path from 'path';
import { Beam, execOnBeam, classifyTshError, tshErrorMessage } from './tsh';
import { reportTshError } from './notify';
import { PollConsumer } from './polling';

interface FileEntry {
    name: string;
    path: string;
    isDirectory: boolean;
    beamId: string;
}

export class FileItem extends vscode.TreeItem {
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

// A non-navigable row for states that aren't worth an intrusive toast — e.g. a beam that's
// visible (listed) but outside the current identity's access scope. Mirrors how
// ClustersProvider reports resource-load failures inline instead of popping a notification.
class FileInfoItem extends vscode.TreeItem {
    constructor(label: string, icon: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.iconPath = new vscode.ThemeIcon(icon);
    }
}

type FileTreeItem = FileItem | FileInfoItem;

export class BeamFileExplorer implements vscode.TreeDataProvider<FileTreeItem>, vscode.TreeDragAndDropController<FileTreeItem>, PollConsumer {
    private _onDidChangeTreeData = new vscode.EventEmitter<FileTreeItem | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    // Dragging in local files from the OS (Finder/Explorer) arrives as a uri-list; we don't
    // support dragging FileItems back out (no dragMimeTypes), only dropping onto the tree.
    readonly dropMimeTypes = ['text/uri-list'];
    readonly dragMimeTypes: string[] = [];

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

    getTreeItem(element: FileTreeItem): vscode.TreeItem {
        return element;
    }

    async getChildren(element?: FileTreeItem): Promise<FileTreeItem[]> {
        if (!this.currentBeam) {
            return [];
        }
        if (element instanceof FileInfoItem) {
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
            // A beam that's listed but outside this identity's access scope (e.g. one owned
            // by the same human but delegated to a different beam's bot identity) fails every
            // file listing attempt the same way. Show it once, inline, instead of a toast per
            // directory the tree tries to expand.
            if (classifyTshError(err) === 'denied') {
                return [new FileInfoItem(`Access denied: ${tshErrorMessage(err)}`, 'lock')];
            }
            reportTshError(err, { beamId, action: 'list files' });
            return [];
        }
    }

    async handleDrop(target: FileTreeItem | undefined, dataTransfer: vscode.DataTransfer): Promise<void> {
        if (target instanceof FileInfoItem) {
            return;
        }
        const transferItem = dataTransfer.get('text/uri-list');
        if (!transferItem) {
            return;
        }
        const raw = await transferItem.asString();
        const uris = raw.split('\n').map(s => s.trim()).filter(Boolean).map(s => vscode.Uri.parse(s));
        if (uris.length === 0) {
            return;
        }

        const targetDir = !target
            ? '/home/beams'
            : target.entry.isDirectory
                ? target.entry.path
                : path.posix.dirname(target.entry.path);
        await this.uploadLocalFiles(targetDir, uris);
    }

    // Shared by the drag-and-drop handler above and the `beams.uploadFiles` command — both
    // just need to know which local files go into which remote directory. `beam://` already
    // implements the full FileSystemProvider surface (readDirectory/readFile/writeFile/
    // createDirectory), so a generic `vscode.workspace.fs.copy` handles recursive directory
    // uploads too, without needing a separate scp-based code path.
    async uploadLocalFiles(targetDir: string, localUris: vscode.Uri[]): Promise<void> {
        if (!this.currentBeam) {
            vscode.window.showWarningMessage('Select a beam first.');
            return;
        }
        const beamId = this.currentBeam.id;
        const dir = targetDir.endsWith('/') ? targetDir : `${targetDir}/`;

        const failures: string[] = [];
        await vscode.window.withProgress(
            { location: vscode.ProgressLocation.Notification, title: 'Uploading files...' },
            async progress => {
                for (const uri of localUris) {
                    const name = path.basename(uri.fsPath);
                    progress.report({ message: name });
                    const destUri = vscode.Uri.parse(`beam://${beamId}${dir}${name}`);
                    try {
                        await vscode.workspace.fs.copy(uri, destUri, { overwrite: true });
                    } catch (err: unknown) {
                        failures.push(`${name}: ${err instanceof Error ? err.message : err}`);
                    }
                }
            }
        );

        this.refresh();
        if (failures.length > 0) {
            vscode.window.showErrorMessage(`Some files failed to upload:\n${failures.join('\n')}`);
        } else {
            vscode.window.showInformationMessage(
                `Uploaded ${localUris.length} item${localUris.length === 1 ? '' : 's'} to ${targetDir}.`
            );
        }
    }
}
