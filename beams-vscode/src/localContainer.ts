import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execFile } from 'child_process';
import { promisify } from 'util';

const exec = promisify(execFile);

export type LocalContainerSyncMode = 'manual' | 'automatic';

// Written exactly once, by the "Replicate to Local Debug Container" action. No
// function in this module (or anywhere else in the extension) ever edits
// `enabled`/`syncMode` on an existing record — the only way to change the
// decision is `deleteLocalContainerRecord` (full teardown) and replicating again.
export interface LocalContainerRecord {
    beamId: string;
    enabled: boolean;
    syncMode: LocalContainerSyncMode;
    repoRoot: string;
    containerName: string;
    imageTag: string;
    createdAt: string;
    lastSyncSignature?: string;
}

function getLocalContainersRoot(): string {
    return path.join(os.homedir(), '.teleport', 'beams', 'local-containers');
}

function getRecordDir(beamId: string): string {
    return path.join(getLocalContainersRoot(), beamId);
}

function getRecordPath(beamId: string): string {
    return path.join(getRecordDir(beamId), 'meta.json');
}

export function getDockerfilePath(beamId: string): string {
    return path.join(getRecordDir(beamId), 'Dockerfile');
}

export function getWorkspaceDir(beamId: string): string {
    return path.join(getRecordDir(beamId), 'workspace');
}

export function getLocalContainerRecord(beamId: string): LocalContainerRecord | undefined {
    const file = getRecordPath(beamId);
    if (!fs.existsSync(file)) {
        return undefined;
    }
    try {
        return JSON.parse(fs.readFileSync(file, 'utf-8'));
    } catch {
        return undefined;
    }
}

function writeRecord(record: LocalContainerRecord): void {
    fs.writeFileSync(getRecordPath(record.beamId), JSON.stringify(record, null, 2), 'utf-8');
}

// Called once per opt-in, from beams.container.create only. `enabled` is always
// `true` here — a record simply does not exist for beams that didn't opt in.
export function createLocalContainerRecord(
    beamId: string,
    repoRoot: string,
    syncMode: LocalContainerSyncMode,
): LocalContainerRecord {
    const dir = getRecordDir(beamId);
    fs.mkdirSync(dir, { recursive: true });
    const workspaceDir = getWorkspaceDir(beamId);
    fs.mkdirSync(workspaceDir, { recursive: true });
    // The container runs as a non-host uid (10001); loosen perms on just this
    // one synced directory so that uid can write into the bind mount. Nothing
    // else on the host is affected.
    fs.chmodSync(workspaceDir, 0o777);

    const record: LocalContainerRecord = {
        beamId,
        enabled: true,
        syncMode,
        repoRoot,
        containerName: `beam-local-${beamId}`,
        imageTag: `beam-local-${beamId}:latest`,
        createdAt: new Date().toISOString(),
    };
    writeRecord(record);
    return record;
}

// The only field this function is allowed to update after creation — purely
// informational sync bookkeeping, not one of the immutable opt-in fields.
export function updateLastSyncSignature(beamId: string, signature: string): void {
    const record = getLocalContainerRecord(beamId);
    if (!record) {
        return;
    }
    record.lastSyncSignature = signature;
    writeRecord(record);
}

export function deleteLocalContainerRecord(beamId: string): void {
    const dir = getRecordDir(beamId);
    if (fs.existsSync(dir)) {
        fs.rmSync(dir, { recursive: true, force: true });
    }
}

const DEFAULT_BASE_IMAGE = 'debian:bookworm-slim';

// Deliberately minimal: no beam credentials, environment variables, systemd units, or bin
// scripts are ever baked into the local debug container image. This guarantee used to be
// enforced by sanitizing a template's env snapshot; with templates gone it holds because
// there is no input to leak. Keep it that way if per-beam customization returns.
export function generateDockerfile(): string {
    const baseImage = vscode.workspace.getConfiguration('beams').get<string>('container.baseImage') || DEFAULT_BASE_IMAGE;
    const lines = [
        `FROM ${baseImage}`,
        'RUN useradd -m -u 10001 -s /bin/bash beamdebug',
        'WORKDIR /workspace',
        'USER beamdebug',
    ];
    return lines.join('\n') + '\n';
}

export function writeDockerfile(beamId: string, content: string): void {
    fs.writeFileSync(getDockerfilePath(beamId), content, 'utf-8');
}

// Courtesy generation for the Dev Containers extension — never required.
// `runArgs` mirrors ensureContainerRunning's hardening exactly (network none,
// caps dropped, etc.) so "Reopen in Container" gets the same sandboxed
// container this feature runs, not a fresh default-networked one, and
// `workspaceMount`/`workspaceFolder` point at the actual synced directory
// rather than whatever folder happens to contain this devcontainer.json.
export function writeDevcontainerJson(record: LocalContainerRecord): void {
    const file = path.join(getRecordDir(record.beamId), 'devcontainer.json');
    const workspaceDir = getWorkspaceDir(record.beamId);
    const content = {
        name: `Beam ${record.beamId} (local debug)`,
        image: record.imageTag,
        workspaceFolder: '/workspace',
        workspaceMount: `source=${workspaceDir},target=/workspace,type=bind`,
        remoteUser: 'beamdebug',
        runArgs: [
            '--read-only',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
            '--cap-drop=ALL',
            '--security-opt', 'no-new-privileges',
            '--pids-limit=256',
            '--memory=2g', '--memory-swap=2g',
            '--cpus=2',
            '--network', 'none',
        ],
    };
    fs.writeFileSync(file, JSON.stringify(content, null, 2), 'utf-8');
}

async function runDocker(args: string[], options?: { timeout?: number }): Promise<string> {
    const { stdout } = await exec('docker', args, {
        timeout: options?.timeout ?? 60000,
        maxBuffer: 50 * 1024 * 1024,
    });
    return stdout;
}

export async function isDockerAvailable(): Promise<boolean> {
    try {
        await exec('docker', ['version', '--format', '{{.Server.Version}}'], { timeout: 5000 });
        return true;
    } catch {
        return false;
    }
}

async function imageExists(imageTag: string): Promise<boolean> {
    try {
        await runDocker(['image', 'inspect', imageTag], { timeout: 10000 });
        return true;
    } catch {
        return false;
    }
}

// The only step in this feature's lifecycle where the container's filesystem
// is assembled with network access (the `docker build` step runs on the host,
// which has network) — the running container itself never gets one (see
// ensureContainerRunning's `--network none`).
export async function buildContainerImage(record: LocalContainerRecord, options?: { noCache?: boolean }): Promise<void> {
    const dir = getRecordDir(record.beamId);
    const args = ['build', '-t', record.imageTag];
    if (options?.noCache) {
        args.push('--no-cache');
    }
    args.push(dir);
    await runDocker(args, { timeout: 600000 });
}

async function containerState(containerName: string): Promise<'running' | 'stopped' | 'absent'> {
    try {
        const output = await runDocker(['inspect', '-f', '{{.State.Running}}', containerName], { timeout: 10000 });
        return output.trim() === 'true' ? 'running' : 'stopped';
    } catch {
        return 'absent';
    }
}

// Hardened invocation: read-only rootfs, all capabilities dropped, no
// privilege escalation, strict resource limits, and — the load-bearing
// control — no network of any kind. Only the one synced workspace directory
// is bind-mounted; nothing else on the host (SSH keys, Teleport certs,
// Docker socket) is ever exposed to this container.
export async function ensureContainerRunning(record: LocalContainerRecord): Promise<void> {
    const state = await containerState(record.containerName);
    if (state === 'running') {
        return;
    }
    if (state === 'stopped') {
        await runDocker(['start', record.containerName], { timeout: 30000 });
        return;
    }

    if (!(await imageExists(record.imageTag))) {
        await buildContainerImage(record);
    }

    const workspaceDir = getWorkspaceDir(record.beamId);
    await runDocker([
        'run', '-d',
        '--name', record.containerName,
        '--read-only',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
        '--cap-drop=ALL',
        '--security-opt', 'no-new-privileges',
        '--pids-limit=256',
        '--memory=2g', '--memory-swap=2g',
        '--cpus=2',
        '--network', 'none',
        '--user', '10001:10001',
        '--mount', `type=bind,source=${workspaceDir},target=/workspace`,
        record.imageTag,
        'sleep', 'infinity',
    ], { timeout: 30000 });
}

export async function stopContainer(record: LocalContainerRecord): Promise<void> {
    try {
        await runDocker(['stop', record.containerName], { timeout: 30000 });
    } catch { /* already stopped or absent */ }
}

export async function removeContainer(record: LocalContainerRecord): Promise<void> {
    try {
        await runDocker(['rm', '-f', record.containerName], { timeout: 30000 });
    } catch { /* already absent */ }
}

export function openContainerTerminal(record: LocalContainerRecord): vscode.Terminal {
    const terminal = vscode.window.createTerminal({
        name: `Local: ${record.beamId}`,
        shellPath: 'docker',
        shellArgs: ['exec', '-it', record.containerName, 'bash'],
        iconPath: new vscode.ThemeIcon('debug-console'),
    });
    terminal.show();
    return terminal;
}
