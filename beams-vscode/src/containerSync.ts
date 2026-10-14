import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execFile } from 'child_process';
import { promisify } from 'util';
import { execOnBeam, scpFromBeam } from './tsh';
import { PollConsumer } from './polling';
import { getLocalContainerRecord, updateLastSyncSignature, getWorkspaceDir } from './localContainer';

const exec = promisify(execFile);

// Above this raw size, base64-inlining the changed-file tarball through
// execOnBeam risks tripping tsh.ts's 50MB maxBuffer once base64 inflates it
// (~33%) — fall back to the scp-based transport instead.
const MAX_INLINE_ARCHIVE_BYTES = 30 * 1024 * 1024;

interface BeamSyncState {
    remoteMtimes: Map<string, number>;
    writtenLocalMtimes: Map<string, number>;
    busy: boolean;
    lastRunAt: number;
}

function hashString(s: string): number {
    let h = 0;
    for (let i = 0; i < s.length; i++) {
        h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
    }
    return h;
}

// Beam -> local container, one-directional pull only. This engine only ever
// writes into the host-side workspace directory that the (network-less)
// container has bind-mounted — it never reads anything back out of the
// container, and the container has no credentials/network to sync on its
// own. Real changes still go through normal git commit/push on the beam.
export class ContainerSyncEngine implements PollConsumer, vscode.Disposable {
    private state = new Map<string, BeamSyncState>();
    private output = vscode.window.createOutputChannel('Beams: Local Sync');

    onGitStatus(beamId: string, repoRoot: string, porcelain: string, headSha: string): void {
        const record = getLocalContainerRecord(beamId);
        if (!record?.enabled || record.syncMode !== 'automatic') {
            return;
        }
        if (!vscode.workspace.getConfiguration('beams').get<boolean>('container.enabled', true)) {
            return;
        }

        const signature = `${headSha}:${porcelain.length}:${hashString(porcelain)}`;
        if (signature === record.lastSyncSignature) {
            return;
        }

        const st = this.getState(beamId);
        const minIntervalMs = vscode.workspace.getConfiguration('beams').get<number>('container.syncMinInterval', 3) * 1000;
        if (Date.now() - st.lastRunAt < minIntervalMs) {
            return;
        }

        void this.runSync(beamId, repoRoot, signature, false);
    }

    async syncNow(beamId: string, repoRoot: string): Promise<void> {
        const record = getLocalContainerRecord(beamId);
        if (!record?.enabled) {
            throw new Error('This beam does not have a local debug container enabled.');
        }
        await this.runSync(beamId, repoRoot, undefined, false);
    }

    private getState(beamId: string): BeamSyncState {
        let st = this.state.get(beamId);
        if (!st) {
            st = { remoteMtimes: new Map(), writtenLocalMtimes: new Map(), busy: false, lastRunAt: 0 };
            this.state.set(beamId, st);
        }
        return st;
    }

    private log(beamId: string, message: string): void {
        this.output.appendLine(`[${new Date().toISOString()}] ${beamId}: ${message}`);
    }

    private async runSync(
        beamId: string,
        repoRoot: string,
        signature: string | undefined,
        force: boolean,
    ): Promise<void> {
        const st = this.getState(beamId);
        if (st.busy) {
            return;
        }
        st.busy = true;
        st.lastRunAt = Date.now();

        try {
            const workspaceDir = getWorkspaceDir(beamId);

            // 1. Enumerate tracked + untracked-but-not-gitignored files (one round trip).
            const listOutput = await execOnBeam(beamId, [
                `cd "${repoRoot}" && git ls-files --cached --others --exclude-standard`,
            ], 15000);
            const paths = listOutput.split('\n').map(l => l.trim()).filter(Boolean);
            const pathSet = new Set(paths);

            let deletedCount = 0;
            for (const known of [...st.remoteMtimes.keys()]) {
                if (!pathSet.has(known)) {
                    const localPath = path.join(workspaceDir, known);
                    if (fs.existsSync(localPath)) {
                        fs.rmSync(localPath, { force: true });
                    }
                    st.remoteMtimes.delete(known);
                    st.writtenLocalMtimes.delete(known);
                    deletedCount++;
                }
            }
            if (deletedCount > 0) {
                this.log(beamId, `removed ${deletedCount} file(s) no longer present on beam`);
            }

            if (paths.length === 0) {
                if (signature) updateLastSyncSignature(beamId, signature);
                return;
            }

            // 2. Stat-diff (one round trip, batched — mirrors polling.ts's pollFileStats idiom).
            const statOutput = await execOnBeam(beamId, [
                `cd "${repoRoot}" && stat --format='%n %Y' ${paths.map(p => `"${p}"`).join(' ')} 2>/dev/null || true`,
            ], 20000);

            const changed: string[] = [];
            for (const line of statOutput.split('\n')) {
                const lastSpace = line.lastIndexOf(' ');
                if (lastSpace === -1) continue;
                const p = line.slice(0, lastSpace);
                const mtime = parseInt(line.slice(lastSpace + 1), 10);
                if (!p || isNaN(mtime)) continue;
                if (st.remoteMtimes.get(p) !== mtime) {
                    changed.push(p);
                }
                st.remoteMtimes.set(p, mtime);
            }

            if (changed.length === 0) {
                if (signature) updateLastSyncSignature(beamId, signature);
                return;
            }

            await this.fetchAndApply(beamId, repoRoot, workspaceDir, changed, st, force);

            if (signature) {
                updateLastSyncSignature(beamId, signature);
            }
        } catch (err: unknown) {
            this.log(beamId, `sync failed — ${err instanceof Error ? err.message : err}`);
        } finally {
            st.busy = false;
        }
    }

    private async fetchAndApply(
        beamId: string,
        repoRoot: string,
        workspaceDir: string,
        changed: string[],
        st: BeamSyncState,
        force: boolean,
    ): Promise<void> {
        const tmpArchive = path.join(os.tmpdir(), `beam-local-sync-${beamId}-${Date.now()}.tar.gz`);
        const remoteArchive = `/tmp/beam-local-sync-${Date.now()}.tar.gz`;
        try {
            // 3. Fetch content for changed files only (one round trip).
            await execOnBeam(beamId, [
                `cd "${repoRoot}" && tar -czf ${remoteArchive} ${changed.map(p => `"${p}"`).join(' ')}`,
            ], 60000);

            const sizeOutput = await execOnBeam(beamId, [`stat --format='%s' ${remoteArchive}`], 10000);
            const remoteSize = parseInt(sizeOutput.trim(), 10) || 0;

            if (remoteSize > MAX_INLINE_ARCHIVE_BYTES) {
                await scpFromBeam(beamId, remoteArchive, tmpArchive);
            } else {
                const b64Output = await execOnBeam(beamId, [`base64 -w0 ${remoteArchive}`], 60000);
                fs.writeFileSync(tmpArchive, Buffer.from(b64Output.trim(), 'base64'));
            }
            await execOnBeam(beamId, ['rm', '-f', remoteArchive]);

            // Extracts the already-downloaded archive, skipping (and restoring)
            // any file whose local mtime moved since we last wrote it — i.e. the
            // developer edited it inside the container — unless forceApply is
            // set. Callable twice against the same tmpArchive so "Force
            // Overwrite All" doesn't need a second remote round trip.
            const applyOnce = async (forceApply: boolean): Promise<string[]> => {
                const applied: string[] = [];
                const skipped: string[] = [];
                for (const p of changed) {
                    const localPath = path.join(workspaceDir, p);
                    if (!forceApply && fs.existsSync(localPath)) {
                        const localMtime = Math.floor(fs.statSync(localPath).mtimeMs / 1000);
                        const lastWritten = st.writtenLocalMtimes.get(p);
                        if (lastWritten !== undefined && localMtime !== lastWritten) {
                            skipped.push(p);
                            continue;
                        }
                    }
                    applied.push(p);
                }

                if (applied.length > 0) {
                    // tar has no reliable "extract all except N" flag — extract
                    // everything, then restore the skipped files' prior local
                    // content so a locally-edited file is never clobbered.
                    const skipBackup = new Map<string, Buffer | undefined>();
                    for (const p of skipped) {
                        const localPath = path.join(workspaceDir, p);
                        skipBackup.set(p, fs.existsSync(localPath) ? fs.readFileSync(localPath) : undefined);
                    }

                    await exec('tar', ['-xzf', tmpArchive, '-C', workspaceDir], { timeout: 60000 });

                    for (const [p, content] of skipBackup) {
                        if (content === undefined) continue;
                        const localPath = path.join(workspaceDir, p);
                        fs.mkdirSync(path.dirname(localPath), { recursive: true });
                        fs.writeFileSync(localPath, content);
                    }

                    for (const p of applied) {
                        const localPath = path.join(workspaceDir, p);
                        if (fs.existsSync(localPath)) {
                            st.writtenLocalMtimes.set(p, Math.floor(fs.statSync(localPath).mtimeMs / 1000));
                        }
                    }
                }
                return skipped;
            };

            const skipped = await applyOnce(force);

            if (skipped.length > 0) {
                this.log(beamId, `kept ${skipped.length} locally-modified file(s) as-is: ${skipped.join(', ')}`);
                const action = await vscode.window.showInformationMessage(
                    `Local debug container (${beamId}): ${skipped.length} locally-modified file(s) kept as-is during sync.`,
                    'Show', 'Force Overwrite All',
                );
                if (action === 'Show') {
                    this.output.show(true);
                } else if (action === 'Force Overwrite All') {
                    await applyOnce(true);
                }
            }
        } finally {
            if (fs.existsSync(tmpArchive)) {
                fs.rmSync(tmpArchive, { force: true });
            }
        }
    }

    dispose(): void {
        this.output.dispose();
        this.state.clear();
    }
}
