import * as vscode from 'vscode';
import { execOnBeam } from './tsh';
import { BeamPoller } from './polling';

export class BeamGitOriginalProvider implements vscode.FileSystemProvider {
    private _onDidChangeFile = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
    readonly onDidChangeFile = this._onDidChangeFile.event;

    private cache = new Map<string, { sha: string; content: Uint8Array }>();
    private maxCacheSize = 50;

    constructor(private poller: BeamPoller) {}

    watch(): vscode.Disposable {
        return new vscode.Disposable(() => {});
    }

    async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
        const { beamId, remotePath } = this.parseUri(uri);
        const repoRoot = this.poller.getRepoRoot();
        if (!repoRoot) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }

        const relPath = this.relativePath(remotePath, repoRoot);
        try {
            const output = await execOnBeam(beamId, [
                'git', '-C', repoRoot, 'cat-file', '-s', `HEAD:${relPath}`,
            ], 10000);
            const size = parseInt(output.trim(), 10) || 0;
            return { type: vscode.FileType.File, ctime: 0, mtime: 0, size };
        } catch {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
    }

    async readFile(uri: vscode.Uri): Promise<Uint8Array> {
        const { beamId, remotePath } = this.parseUri(uri);
        const repoRoot = this.poller.getRepoRoot();
        if (!repoRoot) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }

        const relPath = this.relativePath(remotePath, repoRoot);
        const headSha = this.poller.getHeadSha();
        const cacheKey = `${beamId}:${relPath}`;

        const cached = this.cache.get(cacheKey);
        if (cached && cached.sha === headSha) {
            return cached.content;
        }

        try {
            const output = await execOnBeam(beamId, [
                'git', '-C', repoRoot, 'show', `HEAD:${relPath}`,
            ], 15000);
            const content = Buffer.from(output);

            if (this.cache.size >= this.maxCacheSize) {
                const firstKey = this.cache.keys().next().value;
                if (firstKey) this.cache.delete(firstKey);
            }
            this.cache.set(cacheKey, { sha: headSha, content });

            return content;
        } catch {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
    }

    readDirectory(): [string, vscode.FileType][] {
        throw vscode.FileSystemError.NoPermissions('Read-only provider');
    }

    createDirectory(): void {
        throw vscode.FileSystemError.NoPermissions('Read-only provider');
    }

    writeFile(): void {
        throw vscode.FileSystemError.NoPermissions('Read-only provider');
    }

    delete(): void {
        throw vscode.FileSystemError.NoPermissions('Read-only provider');
    }

    rename(): void {
        throw vscode.FileSystemError.NoPermissions('Read-only provider');
    }

    invalidateCache(): void {
        this.cache.clear();
    }

    private parseUri(uri: vscode.Uri): { beamId: string; remotePath: string } {
        return { beamId: uri.authority, remotePath: uri.path };
    }

    private relativePath(absPath: string, repoRoot: string): string {
        if (absPath.startsWith(repoRoot + '/')) {
            return absPath.slice(repoRoot.length + 1);
        }
        if (absPath.startsWith('/')) {
            return absPath.slice(1);
        }
        return absPath;
    }
}
