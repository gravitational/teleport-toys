import * as vscode from 'vscode';
import { execOnBeam, classifyTshError, tshErrorMessage } from './tsh';

// VS Code surfaces a raw Error from a FileSystemProvider as-is, so a beam that has gone
// away would show tsh's error text. Map it to Unavailable with a plain explanation.
function toFsError(err: unknown, uri: vscode.Uri): vscode.FileSystemError {
    if (classifyTshError(err) === 'disconnected') {
        return vscode.FileSystemError.Unavailable(`Beam "${uri.authority}" is no longer available.`);
    }
    return vscode.FileSystemError.Unavailable(tshErrorMessage(err));
}

// Each `tsh beams exec` call pays a fixed ~2.3s connection-establishment cost regardless
// of payload, so cutting the number of round trips matters far more than payload size.
// Opening a file normally costs two calls (stat() then readFile()) back to back; caching
// short-term and prefetching content alongside stat() collapses that to effectively one.
const CACHE_TTL_MS = 4000;
// Skip speculative content prefetch above this size — base64'ing multi-MB files on every
// stat() call (most of which are never followed by an open) isn't worth the bandwidth.
const PREFETCH_MAX_BYTES = 2 * 1024 * 1024;
const CONTENT_MARKER = '---CONTENT---';

interface CachedStat {
    stat: vscode.FileStat;
    expires: number;
}

interface CachedContent {
    content: Buffer;
    expires: number;
}

function shellQuote(value: string): string {
    return `'${value.replace(/'/g, `'\\''`)}'`;
}

export class BeamFileSystemProvider implements vscode.FileSystemProvider {
    private _onDidChangeFile = new vscode.EventEmitter<vscode.FileChangeEvent[]>();
    readonly onDidChangeFile = this._onDidChangeFile.event;
    private mtimeCache = new Map<string, number>();
    private statCache = new Map<string, CachedStat>();
    private contentCache = new Map<string, CachedContent>();

    watch(): vscode.Disposable {
        return new vscode.Disposable(() => {});
    }

    handleStatUpdate(beamId: string, stats: Map<string, number>): void {
        const events: vscode.FileChangeEvent[] = [];
        for (const [remotePath, mtime] of stats) {
            const key = `${beamId}:${remotePath}`;
            const cached = this.mtimeCache.get(key);
            if (cached !== undefined && cached !== mtime) {
                events.push({
                    type: vscode.FileChangeType.Changed,
                    uri: vscode.Uri.parse(`beam://${beamId}${remotePath}`),
                });
                // Changed on the beam outside of our own writes — drop any cached stat/content
                // so the next read reflects reality instead of a stale prefetch.
                this.statCache.delete(key);
                this.contentCache.delete(key);
            }
            this.mtimeCache.set(key, mtime);
        }
        if (events.length > 0) {
            this._onDidChangeFile.fire(events);
        }
    }

    async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
        const { beamId, remotePath } = this.parseUri(uri);
        const key = `${beamId}:${remotePath}`;
        const cached = this.statCache.get(key);
        if (cached && cached.expires > Date.now()) {
            return cached.stat;
        }

        try {
            const quoted = shellQuote(remotePath);
            // Single round trip: stat metadata plus (for small files) the content itself,
            // so a readFile() that immediately follows can be served from cache with no
            // additional network call — VS Code always stats a file right before opening it.
            const script = [
                `stat --format='%F_%s_%Y' ${quoted} 2>/dev/null || exit 1`,
                `size=$(stat --format=%s ${quoted} 2>/dev/null)`,
                `if [ -f ${quoted} ] && [ "$size" -le ${PREFETCH_MAX_BYTES} ]; then`,
                `  printf '\\n${CONTENT_MARKER}\\n'`,
                `  base64 ${quoted}`,
                `fi`,
            ].join('\n');
            const output = await execOnBeam(beamId, [script]);

            const markerLine = `\n${CONTENT_MARKER}\n`;
            const sepIdx = output.indexOf(markerLine);
            const statLine = (sepIdx === -1 ? output : output.slice(0, sepIdx)).trim();
            const parts = statLine.split('_');
            const typePart = parts.slice(0, -2).join('_');
            const isDir = typePart === 'directory';
            const size = parseInt(parts[parts.length - 2] ?? '0', 10);
            const mtime = parseInt(parts[parts.length - 1] ?? '0', 10) * 1000;
            const stat: vscode.FileStat = {
                type: isDir ? vscode.FileType.Directory : vscode.FileType.File,
                ctime: mtime,
                mtime,
                size,
            };

            this.statCache.set(key, { stat, expires: Date.now() + CACHE_TTL_MS });
            if (sepIdx !== -1) {
                const b64 = output.slice(sepIdx + markerLine.length).trim();
                this.contentCache.set(key, {
                    content: Buffer.from(b64, 'base64'),
                    expires: Date.now() + CACHE_TTL_MS,
                });
            }
            return stat;
        } catch {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
    }

    async readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
        const { beamId, remotePath } = this.parseUri(uri);
        let output: string;
        try {
            output = await execOnBeam(beamId, ['ls', '-1F', remotePath]);
        } catch (err) {
            throw toFsError(err, uri);
        }
        const entries: [string, vscode.FileType][] = [];
        for (const line of output.trim().split('\n')) {
            if (!line) continue;
            const isDir = line.endsWith('/');
            const isLink = line.endsWith('@');
            const isExec = line.endsWith('*');
            const name = (isDir || isLink || isExec) ? line.slice(0, -1) : line;
            if (!name) continue;
            entries.push([
                name,
                isDir ? vscode.FileType.Directory : vscode.FileType.File,
            ]);
        }
        return entries;
    }

    async readFile(uri: vscode.Uri): Promise<Uint8Array> {
        const { beamId, remotePath } = this.parseUri(uri);
        const key = `${beamId}:${remotePath}`;
        const cached = this.contentCache.get(key);
        if (cached && cached.expires > Date.now()) {
            return cached.content;
        }

        try {
            const output = await execOnBeam(beamId, ['cat', remotePath]);
            const content = Buffer.from(output);
            this.contentCache.set(key, { content, expires: Date.now() + CACHE_TTL_MS });
            return content;
        } catch {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
    }

    async writeFile(uri: vscode.Uri, content: Uint8Array): Promise<void> {
        const { beamId, remotePath } = this.parseUri(uri);
        const encoded = Buffer.from(content).toString('base64');
        try {
            await execOnBeam(beamId, [
                `echo '${encoded}' | base64 -d > ${shellQuote(remotePath)}`
            ]);
        } catch (err) {
            throw toFsError(err, uri);
        }
        const key = `${beamId}:${remotePath}`;
        // Update mtime cache to suppress false-positive change events from our own write
        this.mtimeCache.set(key, Math.floor(Date.now() / 1000));
        this.statCache.delete(key);
        this.contentCache.set(key, { content: Buffer.from(content), expires: Date.now() + CACHE_TTL_MS });
        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Changed, uri }]);
    }

    async delete(uri: vscode.Uri, options: { recursive: boolean }): Promise<void> {
        const { beamId, remotePath } = this.parseUri(uri);
        const args = options.recursive ? ['rm', '-rf', remotePath] : ['rm', remotePath];
        try {
            await execOnBeam(beamId, args);
        } catch (err) {
            throw toFsError(err, uri);
        }
        const key = `${beamId}:${remotePath}`;
        this.statCache.delete(key);
        this.contentCache.delete(key);
        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Deleted, uri }]);
    }

    async rename(oldUri: vscode.Uri, newUri: vscode.Uri): Promise<void> {
        const old = this.parseUri(oldUri);
        const neu = this.parseUri(newUri);
        if (old.beamId !== neu.beamId) {
            throw vscode.FileSystemError.NoPermissions('Cannot move between beams');
        }
        try {
            await execOnBeam(old.beamId, ['mv', old.remotePath, neu.remotePath]);
        } catch (err) {
            throw toFsError(err, oldUri);
        }
        const oldKey = `${old.beamId}:${old.remotePath}`;
        this.statCache.delete(oldKey);
        this.contentCache.delete(oldKey);
        this._onDidChangeFile.fire([
            { type: vscode.FileChangeType.Deleted, uri: oldUri },
            { type: vscode.FileChangeType.Created, uri: newUri },
        ]);
    }

    async createDirectory(uri: vscode.Uri): Promise<void> {
        const { beamId, remotePath } = this.parseUri(uri);
        try {
            await execOnBeam(beamId, ['mkdir', '-p', remotePath]);
        } catch (err) {
            throw toFsError(err, uri);
        }
    }

    private parseUri(uri: vscode.Uri): { beamId: string; remotePath: string } {
        return {
            beamId: uri.authority,
            remotePath: uri.path || '/',
        };
    }
}
