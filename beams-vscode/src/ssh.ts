import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execFile } from 'child_process';
import { promisify } from 'util';

const exec = promisify(execFile);

const MARKER_START = '# BEGIN Teleport Beams';
const MARKER_END = '# END Teleport Beams';

function getTshPath(): string {
    if (process.platform === 'win32') {
        return 'tsh.exe';
    }
    if (process.platform === 'darwin') {
        const connectPath = '/Applications/Teleport Connect.app/Contents/MacOS/tsh.app/Contents/MacOS/tsh';
        if (fs.existsSync(connectPath)) {
            return connectPath;
        }
    }
    return 'tsh';
}

function getSshConfigPath(): string {
    return path.join(os.homedir(), '.ssh', 'config');
}

function readSshConfig(): string {
    const configPath = getSshConfigPath();
    if (!fs.existsSync(configPath)) {
        return '';
    }
    return fs.readFileSync(configPath, 'utf-8');
}

/** Backs up the current config, then atomically replaces it via a sibling file. */
function writeSshConfig(content: string): void {
    const configPath = getSshConfigPath();
    const sshDir = path.dirname(configPath);
    if (!fs.existsSync(sshDir)) {
        fs.mkdirSync(sshDir, { mode: 0o700, recursive: true });
    }

    const backupPath = `${configPath}.teleport-beams.bak`;
    const tempPath = `${configPath}.teleport-beams.${process.pid}.tmp`;
    if (fs.existsSync(configPath)) {
        fs.copyFileSync(configPath, backupPath);
        fs.chmodSync(backupPath, 0o600);
    }

    try {
        fs.writeFileSync(tempPath, content, { mode: 0o600 });
        fs.renameSync(tempPath, configPath);
    } finally {
        if (fs.existsSync(tempPath)) {
            fs.unlinkSync(tempPath);
        }
    }
}

// `tsh config` with no --proxy targets whatever cluster happens to be tsh's current
// default profile — not necessarily the one we're configuring here, especially when
// logged into multiple clusters. Passing --proxy pins it to the right one, including
// the IdentityFile/CertificateFile lines that scope SSH auth to that cluster's own
// certificate (without them, ssh falls back to offering every cert in the agent, which
// can exhaust the server's MaxAuthTries before ever trying the right one).
async function getTshConfig(cluster: string): Promise<string | null> {
    try {
        const tshPath = getTshPath();
        const { stdout } = await exec(tshPath, ['config', '--proxy', cluster], { timeout: 10000 });
        return stdout;
    } catch {
        return null;
    }
}

/**
 * Adapts the matching cluster block from `tsh config` for Beam aliases.
 *
 * Beam SSH connections route through Teleport's internal `beams/alias`
 * principal, so the stock ProxyCommand is replaced and its fixed Port is
 * removed. Other cluster blocks are preserved verbatim.
 */
function patchTshConfigForBeams(tshConfig: string, cluster: string): string {
    const lines = tshConfig.split('\n');
    const result: string[] = [];
    let inClusterBlock = false;

    for (const line of lines) {
        if (line.startsWith('Host ') && line.includes(cluster)) {
            inClusterBlock = true;
            result.push(line);
            continue;
        } else if (line.startsWith('Host ') && inClusterBlock) {
            inClusterBlock = false;
        }

        if (inClusterBlock) {
            if (line.trim().startsWith('Port ')) {
                continue;
            }
            if (line.trim().startsWith('ProxyCommand ')) {
                const tshPath = getTshPath();
                result.push(`    StrictHostKeyChecking no`);
                result.push(`    ProxyCommand sh -c '"${tshPath}" proxy ssh --cluster=${cluster} --proxy=${cluster}:443 %r@teleport.internal/beams/alias=$(echo %h | cut -d. -f1)'`);
                continue;
            }
        }

        result.push(line);
    }

    return result.join('\n');
}

/**
 * Builds the wildcard ProxyCommand block for a cluster, preferring `tsh config --proxy`
 * (which includes IdentityFile/CertificateFile pinning so ssh doesn't have to guess which
 * of the user's other logged-in clusters' certs to offer — without it, ssh falls back to
 * offering every cert in the agent, which can exhaust the server's MaxAuthTries before
 * ever trying the right one) and falling back to a minimal hardcoded ProxyCommand-only
 * block if tsh is unavailable or not logged into that cluster.
 */
async function buildWildcardBlock(cluster: string): Promise<string> {
    const tshConfig = await getTshConfig(cluster);
    if (tshConfig && tshConfig.includes(`*.${cluster}`)) {
        return patchTshConfigForBeams(tshConfig, cluster);
    }
    const tshPath = getTshPath();
    return [
        `Host *.${cluster} !${cluster}`,
        '    StrictHostKeyChecking no',
        '    UserKnownHostsFile /dev/null',
        `    ProxyCommand sh -c '"${tshPath}" proxy ssh --cluster=${cluster} --proxy=${cluster}:443 %r@teleport.internal/beams/alias=$(echo %h | cut -d. -f1)'`,
    ].join('\n');
}

function hostPatterns(line: string): string[] | null {
    const match = line.match(/^\s*Host\s+(.+)$/i);
    return match ? match[1].trim().split(/\s+/) : null;
}

function hasHost(config: string, host: string): boolean {
    return config.split('\n').some(line => hostPatterns(line)?.includes(host) ?? false);
}

/**
 * Moves a Beam's Host entry above the first `*.<cluster>` wildcard if it sits
 * below one. OpenSSH takes the first value it sees for each setting, so an
 * entry below tsh's wildcard inherits `UserKnownHostsFile ~/.tsh/known_hosts`
 * instead of `/dev/null`. Beam names get reused across recreated VMs, so a
 * stale key there triggers ssh's MITM guard, which disables port forwarding
 * and breaks Remote-SSH's `-D` tunnel with ECONNREFUSED.
 */
function hoistBeamEntry(config: string, host: string, cluster: string): string {
    const lines = config.split('\n');
    const wildcardIdx = lines.findIndex(line => hostPatterns(line)?.includes(`*.${cluster}`) ?? false);
    const hostIdx = lines.findIndex(line => hostPatterns(line)?.includes(host) ?? false);
    if (wildcardIdx === -1 || hostIdx === -1 || hostIdx < wildcardIdx) {
        return config;
    }

    let end = hostIdx + 1;
    while (end < lines.length && /^\s+\S/.test(lines[end])) {
        end++;
    }
    const entry = lines.splice(hostIdx, end - hostIdx);
    // Drop the blank separator the entry leaves behind.
    if (lines[hostIdx - 1]?.trim() === '' && (lines[hostIdx] === undefined || lines[hostIdx].trim() === '' || lines[hostIdx].startsWith('#'))) {
        lines.splice(hostIdx - 1, 1);
    }

    // Insert above the wildcard and any comments introducing it (e.g. tsh's
    // "# Begin generated Teleport configuration" header), but inside our marker.
    let insertIdx = wildcardIdx;
    while (insertIdx > 0 && /^(#|\s*$)/.test(lines[insertIdx - 1]) && lines[insertIdx - 1] !== MARKER_START) {
        insertIdx--;
    }
    while (lines[insertIdx].trim() === '') {
        insertIdx++;
    }
    lines.splice(insertIdx, 0, ...entry, '');
    return lines.join('\n');
}

/**
 * Migrates aliases created by older extension versions from `vscode+` and
 * `.beams.run` to the current host name.
 *
 * Rewrites are limited to exact Beam/cluster Host patterns and directives in
 * those blocks so unrelated `.beams.run` values in the user's config survive.
 */
function migrateLegacyConfig(config: string, cluster: string, beamId: string, host: string): string {
    const legacyCluster = cluster.replace(/\.beams\.sh$/, '.beams.run');
    const legacyHosts = new Set([
        `vscode+${beamId}.${cluster}`,
        `vscode+${beamId}.${legacyCluster}`,
        `vscode--${beamId}.${legacyCluster}`,
    ]);
    const relevantHosts = new Set([
        host,
        ...legacyHosts,
        cluster,
        legacyCluster,
        `!${cluster}`,
        `!${legacyCluster}`,
        `*.${cluster}`,
        `*.${legacyCluster}`,
    ]);
    let inRelevantBlock = false;

    return config.split('\n').map(line => {
        const hostMatch = line.match(/^(\s*Host\s+)(.+)$/i);
        if (hostMatch) {
            const patterns = hostMatch[2].trim().split(/\s+/);
            inRelevantBlock = patterns.some(pattern => relevantHosts.has(pattern));
            const migrated = patterns.map(pattern => {
                if (legacyHosts.has(pattern)) {
                    return host;
                }
                if (legacyCluster !== cluster && pattern.endsWith(legacyCluster)) {
                    const prefix = pattern.slice(0, -legacyCluster.length);
                    if (prefix === '' || prefix === '!' || prefix === '*.') {
                        return `${prefix}${cluster}`;
                    }
                }
                return pattern;
            });
            return hostMatch[1] + migrated.join(' ');
        }

        if (inRelevantBlock && legacyCluster !== cluster && /^\s*(?:HostName|ProxyCommand)\s+/i.test(line)) {
            return line.split(legacyCluster).join(cluster);
        }
        return line;
    }).join('\n');
}

function buildBeamsBlock(cluster: string, beamId: string): string {
    const tshPath = getTshPath();
    const wildcardHost = `*.${cluster}`;
    const beamHost = `vscode--${beamId}.${cluster}`;

    return [
        MARKER_START,
        `Host ${wildcardHost} !${cluster}`,
        '    StrictHostKeyChecking no',
        '    UserKnownHostsFile /dev/null',
        `    ProxyCommand sh -c '"${tshPath}" proxy ssh --cluster=${cluster} --proxy=${cluster}:443 %r@teleport.internal/beams/alias=$(echo %h | cut -d. -f1)'`,
        '',
        `Host ${beamHost}`,
        `    HostName ${beamId}.${cluster}`,
        '    User beams',
        '    UserKnownHostsFile /dev/null',
        '    RemoteCommand bash',
        MARKER_END,
    ].join('\n');
}

/**
 * Ensures `~/.ssh/config` contains a VS Code-safe alias for a Beam and returns
 * that alias. Specific Beam entries are placed before Teleport wildcards
 * because OpenSSH uses the first value it finds for each setting.
 *
 * Existing extension entries are migrated in place; otherwise the function
 * reuses `tsh config` when available and falls back to a standalone block.
 */
export async function ensureBeamSshConfig(beamId: string, rawCluster: string): Promise<string> {
    const cluster = rawCluster;
    const host = `vscode--${beamId}.${cluster}`;
    let config = readSshConfig();

    const migratedConfig = migrateLegacyConfig(config, cluster, beamId, host);
    const migrated = migratedConfig !== config;
    config = migratedConfig;
    if (hasHost(config, host)) {
        const hoistedConfig = hoistBeamEntry(config, host, cluster);
        if (migrated || hoistedConfig !== config) {
            config = hoistedConfig;
            writeSshConfig(config);
        }
        return host;
    }

    // If there's an existing tsh-generated block for this cluster, patch its ProxyCommand
    if (config.includes(`*.${cluster}`) && !config.includes(MARKER_START)) {
        config = patchTshConfigForBeams(config, cluster);
        const beamEntry = [
            '',
            MARKER_START,
            `Host ${host}`,
            `    HostName ${beamId}.${cluster}`,
            '    User beams',
            '    UserKnownHostsFile /dev/null',
            '    RemoteCommand bash',
            MARKER_END,
        ].join('\n');
        // Insert beam entry BEFORE the wildcard so it matches first
        const wildcardIdx = config.indexOf(`Host *.${cluster}`);
        if (wildcardIdx > 0) {
            config = config.slice(0, wildcardIdx) + beamEntry + '\n\n' + config.slice(wildcardIdx);
        } else {
            config += '\n' + beamEntry + '\n';
        }
        writeSshConfig(config);
        return host;
    }

    // No existing tsh config — generate it
    if (!config.includes(MARKER_START)) {
        const wildcardBlock = await buildWildcardBlock(cluster);
        const block = [
            MARKER_START,
            `Host ${host}`,
            `    HostName ${beamId}.${cluster}`,
            '    User beams',
            '    UserKnownHostsFile /dev/null',
            '    RemoteCommand bash',
            '',
            wildcardBlock,
            MARKER_END,
        ].join('\n');
        if (config.length > 0 && !config.endsWith('\n')) {
            config += '\n';
        }
        config += '\n' + block + '\n';
        writeSshConfig(config);
        return host;
    }

    // Fallback: a Teleport Beams marker block already exists — but possibly only for a
    // different cluster. Always add this beam's Host entry, and if THIS cluster doesn't
    // already have its own wildcard ProxyCommand somewhere in the config, add that too;
    // otherwise the new entry has no ProxyCommand and falls through to a raw, unproxied
    // TCP connection attempt.
    const markerIdx = config.indexOf(MARKER_START);
    if (markerIdx !== -1) {
        const endIdx = config.indexOf(MARKER_END);
        if (endIdx !== -1) {
            const before = config.slice(0, endIdx);
            const after = config.slice(endIdx);
            const entries = [
                '',
                `Host ${host}`,
                `    HostName ${beamId}.${cluster}`,
                '    User beams',
                '    UserKnownHostsFile /dev/null',
                '    RemoteCommand bash',
            ];
            if (!hasHost(config, `*.${cluster}`)) {
                entries.push('', await buildWildcardBlock(cluster));
            }
            config = hoistBeamEntry(before + entries.join('\n') + '\n' + after, host, cluster);
            writeSshConfig(config);
            return host;
        }
    }

    const block = buildBeamsBlock(cluster, beamId);
    if (config.length > 0 && !config.endsWith('\n')) {
        config += '\n';
    }
    config += '\n' + block + '\n';
    writeSshConfig(config);
    return host;
}
