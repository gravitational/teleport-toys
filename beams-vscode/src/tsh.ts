import { execFile } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { promisify } from 'util';

const exec = promisify(execFile);

// VS Code launched from the Dock/Finder can inherit a minimal PATH without /usr/local/bin
// or Homebrew, so fall back to the standard install locations when tsh isn't on PATH.
const TSH_FALLBACK_PATHS = process.platform === 'darwin'
    ? [
        '/usr/local/bin/tsh',
        '/opt/homebrew/bin/tsh',
        '/Applications/tsh.app/Contents/MacOS/tsh',
        '/Applications/Teleport Connect.app/Contents/MacOS/tsh.app/Contents/MacOS/tsh',
    ]
    : process.platform === 'win32' ? [] : ['/usr/local/bin/tsh', '/usr/bin/tsh'];

let resolvedTsh: string | undefined;

/** Absolute path to tsh when found, else the bare name so the OS reports it missing. */
export function tshBinary(): string {
    if (resolvedTsh) {
        return resolvedTsh;
    }
    const name = process.platform === 'win32' ? 'tsh.exe' : 'tsh';
    const onPath = (process.env.PATH ?? '').split(path.delimiter).filter(Boolean).map(dir => path.join(dir, name));
    const found = [...onPath, ...TSH_FALLBACK_PATHS].find(candidate => {
        try {
            return fs.statSync(candidate).isFile();
        } catch {
            return false;
        }
    });
    // Only cache a real hit, so installing tsh later is picked up without a reload.
    if (found) {
        resolvedTsh = found;
    }
    return found ?? name;
}

export interface Beam {
    id: string;
    uuid: string;
    owner: string;
    expires: string;
    url: string;
}

export interface TshStatus {
    loggedIn: boolean;
    user: string;
    cluster: string;
    validUntil: string;
}

export interface AppResource {
    name: string;
    cluster: string;
    proxy: string;
    subKind: string;
    publicAddr: string;
    uri: string;
    description: string;
    labels: Record<string, string>;
}

export interface DbResource {
    name: string;
    cluster: string;
    proxy: string;
    protocol: string;
    description: string;
    labels: Record<string, string>;
}

export interface KubeResource {
    name: string;
    cluster: string;
    proxy: string;
    labels: Record<string, string>;
}

export interface NodeResource {
    name: string;
    hostname: string;
    cluster: string;
    proxy: string;
    addr: string;
    labels: Record<string, string>;
}

// tsh colourises its diagnostics, so raw error text arrives with SGR escapes embedded
// (e.g. "\x1b[31mERROR: \x1b[0m..."). Strip them before matching or displaying.
export function stripAnsi(text: string): string {
    // eslint-disable-next-line no-control-regex
    return text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '');
}

export type TshErrorKind = 'disconnected' | 'auth' | 'other';

// A beam that has expired, been deleted, or stopped answering is a normal, expected
// end-of-life condition rather than a failure the user needs to act on. `tsh` reports
// it through several different wordings depending on how far the connection got.
const DISCONNECT_PATTERNS = [
    'does not exist',
    'not found',
    'is not running',
    'connection refused',
    'connection reset',
    'context deadline exceeded',
    'i/o timeout',
    'no route to host',
    'broken pipe',
    'unexpected eof',
    'ssh: handshake failed',
    'failed to dial',
    'dial tcp',
    'connection closed',
    'use of closed network connection',
];

const AUTH_PATTERNS = [
    'not logged in',
    'relogin',
    'certificate has expired',
    'access denied',
];

export function tshErrorMessage(err: unknown): string {
    const raw = err instanceof Error ? err.message : String(err);
    const message = stripAnsi(raw)
        .replace(/^Command failed:[^\n]*\n?/, '')
        .replace(/^ERROR:\s*/gm, '')
        .trim();
    if (message) {
        return message;
    }
    // tsh wrote nothing to stderr — say how it failed rather than showing an empty reason.
    const { code, killed, signal } = (err ?? {}) as { code?: unknown; killed?: unknown; signal?: unknown };
    if (code === 'ETIMEDOUT' || killed === true) {
        return `tsh timed out${signal ? ` (${signal})` : ''}`;
    }
    if (typeof code === 'number') {
        return `tsh exited with code ${code} without an error message`;
    }
    return stripAnsi(raw).trim();
}

// Disconnect patterns are checked before auth ones on purpose: when a beam is gone,
// `tsh` first tries to re-resolve it and emits "cannot relogin in non-interactive
// session" alongside "does not exist". Classifying that as an auth problem would send
// the user off to `tsh login` for what is really just a dead beam.
export function classifyTshError(err: unknown): TshErrorKind {
    const msg = tshErrorMessage(err).toLowerCase();
    if (DISCONNECT_PATTERNS.some(p => msg.includes(p))) {
        return 'disconnected';
    }
    if (AUTH_PATTERNS.some(p => msg.includes(p))) {
        return 'auth';
    }
    const code = (err as { code?: unknown } | undefined)?.code;
    const killed = (err as { killed?: unknown } | undefined)?.killed;
    if (code === 'ETIMEDOUT' || killed === true) {
        return 'disconnected';
    }
    return 'other';
}

async function runTsh(args: string[], options?: { timeout?: number }): Promise<string> {
    const { stdout } = await exec(tshBinary(), args, {
        timeout: options?.timeout ?? 30000,
        maxBuffer: 50 * 1024 * 1024,
    });
    return stdout;
}

export async function listBeams(): Promise<Beam[]> {
    const output = await runTsh(['beams', 'ls', '-f', 'json']);
    const parsed = JSON.parse(output);
    if (!Array.isArray(parsed)) {
        return [];
    }
    return parsed;
}

export async function addBeam(): Promise<Beam> {
    // Provisioning can fall back to another region, so allow more than the default 30s.
    const output = await runTsh(['beams', 'add', '-f', 'json'], { timeout: 120000 });
    return JSON.parse(output);
}

export async function removeBeam(id: string): Promise<void> {
    await runTsh(['beams', 'rm', id]);
}

export async function publishBeam(id: string, tcp = false): Promise<string> {
    const args = ['beams', 'publish'];
    if (tcp) {
        args.push('--tcp');
    }
    args.push(id);
    const output = await runTsh(args);
    const urlMatch = output.match(/https?:\/\/\S+/);
    return urlMatch ? urlMatch[0] : output.trim();
}

export async function unpublishBeam(id: string): Promise<void> {
    await runTsh(['beams', 'unpublish', id]);
}

export async function execOnBeam(id: string, command: string[], timeout?: number): Promise<string> {
    const output = await runTsh(['beams', 'exec', id, '--', ...command], { timeout: timeout ?? 30000 });
    return output;
}

export async function waitForBeamReady(id: string, timeoutMs = 60000): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        try {
            await execOnBeam(id, ['true'], 5000);
            return;
        } catch {
            await new Promise(r => setTimeout(r, 2000));
        }
    }
    throw new Error(`Beam "${id}" did not become ready within ${timeoutMs / 1000}s`);
}

export async function detectRepoRoot(beamId: string): Promise<string | undefined> {
    try {
        const output = await execOnBeam(beamId, ['git', '-C', '/home/beams', 'rev-parse', '--show-toplevel'], 10000);
        const root = output.trim();
        if (root) {
            return root;
        }
    } catch { /* no repo at /home/beams */ }

    try {
        // NOTE: tsh beams exec joins the argv into one remote command line rather than
        // preserving argv boundaries, so a `['bash', '-c', '<compound command>']` wrapper
        // has its script truncated to just the first word by the outer shell. Pass
        // compound commands as a single string element instead — no bash -c wrapper.
        const output = await execOnBeam(beamId, [
            'find /home/beams -maxdepth 2 -name .git -type d -print -quit',
        ], 10000);
        const gitDir = output.trim();
        if (gitDir) {
            return gitDir.replace(/\/\.git$/, '');
        }
    } catch { /* nothing found */ }

    return undefined;
}

// `tsh beams exec` joins its argv into a single remote command line rather than preserving
// argument boundaries (confirmed empirically — a multi-element `command` array arrives on the
// beam as one space-joined string re-parsed by the remote shell). Any value that isn't a fixed
// literal — file paths, commit messages, branch names, anything sourced from user input or a
// stored profile — must be quoted with this before being interpolated into a command string,
// or it can break argument boundaries or inject shell syntax on the beam.
export function shellSingleQuote(value: string): string {
    return `'${value.replace(/'/g, `'\\''`)}'`;
}

export async function checkStatus(): Promise<TshStatus> {
    try {
        const output = await runTsh(['status']);
        const user = output.match(/Logged in as:\s+(\S+)/)?.[1] ?? '';
        const cluster = output.match(/Cluster:\s+(\S+)/)?.[1] ?? '';
        const validUntil = output.match(/Valid until:\s+(.+?)(?:\s+\[|$)/)?.[1] ?? '';
        return { loggedIn: true, user, cluster, validUntil };
    } catch {
        return { loggedIn: false, user: '', cluster: '', validUntil: '' };
    }
}

export interface RawClusterProfile {
    profile_url?: string;
    username?: string;
    cluster?: string;
    roles?: string[];
    logins?: string[];
    valid_until?: string;
}

export async function listClusterProfiles(): Promise<{ active?: RawClusterProfile; profiles?: RawClusterProfile[] }> {
    const output = await runTsh(['status', '--format=json'], { timeout: 10000 });
    return JSON.parse(output);
}

// `tsh <kind> ls -f json` returns a bare array of resource objects; `--all` wraps each in
// {proxy, cluster, <kind>: {...}} so items from federated/leaf clusters carry their origin.
// The wrapper key names for db/kube couldn't be confirmed against a live cluster with such
// resources in this environment, so we fall back to a few plausible keys, then to the raw
// object itself, rather than assuming a single name and risking a silent empty result.
function unwrapResource(raw: Record<string, unknown>, wrapperKeys: string[]): { inner: Record<string, unknown>; cluster: string; proxy: string } {
    let inner = raw;
    for (const key of wrapperKeys) {
        if (raw[key] && typeof raw[key] === 'object') {
            inner = raw[key] as Record<string, unknown>;
            break;
        }
    }
    return {
        inner,
        cluster: (raw.cluster as string) ?? '',
        proxy: (raw.proxy as string) ?? '',
    };
}

function labelsOf(inner: Record<string, unknown>): Record<string, string> {
    const metadata = (inner.metadata as Record<string, unknown>) ?? {};
    return (metadata.labels as Record<string, string>) ?? {};
}

function nameOf(inner: Record<string, unknown>): string {
    const metadata = (inner.metadata as Record<string, unknown>) ?? {};
    return (metadata.name as string) ?? '';
}

function descriptionOf(inner: Record<string, unknown>): string {
    const metadata = (inner.metadata as Record<string, unknown>) ?? {};
    return (metadata.description as string) ?? '';
}

async function listResources(subcommand: string[], wrapperKeys: string[], proxy?: string): Promise<Record<string, unknown>[]> {
    const args = [...subcommand, '-f', 'json', '--all'];
    if (proxy) {
        args.push(`--proxy=${proxy}`);
    }
    const output = await runTsh(args, { timeout: 15000 });
    const parsed = JSON.parse(output);
    if (!Array.isArray(parsed)) {
        return [];
    }
    return parsed.map((raw: Record<string, unknown>) => {
        const { inner, cluster, proxy: itemProxy } = unwrapResource(raw, wrapperKeys);
        return { ...inner, cluster, proxy: itemProxy };
    });
}

export async function listApps(proxy?: string): Promise<AppResource[]> {
    const items = await listResources(['apps', 'ls'], ['app'], proxy);
    return items.map(item => {
        const spec = (item.spec as Record<string, unknown>) ?? {};
        return {
            name: nameOf(item),
            cluster: item.cluster as string,
            proxy: item.proxy as string,
            subKind: (item.sub_kind as string) ?? '',
            publicAddr: (spec.public_addr as string) ?? '',
            uri: (spec.uri as string) ?? '',
            description: descriptionOf(item),
            labels: labelsOf(item),
        };
    });
}

export async function listDatabases(proxy?: string): Promise<DbResource[]> {
    const items = await listResources(['db', 'ls'], ['database', 'db'], proxy);
    return items.map(item => {
        const spec = (item.spec as Record<string, unknown>) ?? {};
        return {
            name: nameOf(item),
            cluster: item.cluster as string,
            proxy: item.proxy as string,
            protocol: (spec.protocol as string) ?? '',
            description: descriptionOf(item),
            labels: labelsOf(item),
        };
    });
}

export async function listKubeClusters(proxy?: string): Promise<KubeResource[]> {
    const items = await listResources(['kube', 'ls'], ['kubernetes_cluster', 'kube_cluster'], proxy);
    return items.map(item => ({
        name: nameOf(item),
        cluster: item.cluster as string,
        proxy: item.proxy as string,
        labels: labelsOf(item),
    }));
}

export async function listNodes(proxy?: string): Promise<NodeResource[]> {
    const items = await listResources(['ls'], ['node'], proxy);
    return items.map(item => {
        const spec = (item.spec as Record<string, unknown>) ?? {};
        return {
            name: nameOf(item),
            hostname: (spec.hostname as string) ?? '',
            cluster: item.cluster as string,
            proxy: item.proxy as string,
            addr: (spec.addr as string) ?? '',
            labels: labelsOf(item),
        };
    });
}

export async function scpFromBeam(id: string, remotePath: string, localPath: string): Promise<void> {
    await runTsh(['beams', 'scp', `${id}:${remotePath}`, localPath], { timeout: 300000 });
}

export async function isTshAvailable(): Promise<boolean> {
    try {
        // --client skips the proxy round trip, so a slow network can't make tsh look missing.
        await exec(tshBinary(), ['version', '--client'], { timeout: 10000 });
        return true;
    } catch (err: unknown) {
        // Only a missing binary means tsh isn't installed; a timeout or non-zero exit still
        // means it's there, and the real failure will surface from the command that follows.
        return (err as { code?: unknown } | undefined)?.code !== 'ENOENT';
    }
}
