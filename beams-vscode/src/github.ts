import * as vscode from 'vscode';
import { spawn } from 'child_process';
import { execOnBeam, tshBinary } from './tsh';
export const SECRET_KEY = 'beams.githubPat';


export interface GithubSetupOptions {
    beamId: string;
    username: string;
    email: string;
    authMethod: 'pat' | 'oauth' | 'tsh-git';
    pat?: string;
    cloneRepo?: string;
    cloneDir?: string;
}

const GH_INSTALL_SCRIPT = [
    'type -p wget >/dev/null || sudo apt update && sudo apt-get install wget -y',
    'sudo mkdir -p -m 755 /etc/apt/keyrings',
    'wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null',
    'sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg',
    'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli-stable.list > /dev/null',
    'sudo apt update',
    'sudo apt install gh -y',
].join('\n');

function execScriptOnBeam(beamId: string, script: string, timeout: number): Promise<string> {
    return new Promise((resolve, reject) => {
        const child = spawn(tshBinary(), ['beams', 'exec', beamId, '--', 'bash', '-s'], {
            timeout,
            stdio: ['pipe', 'pipe', 'pipe'],
        });
        let stdout = '';
        let stderr = '';
        child.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
        child.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
        child.on('close', (code) => {
            if (code === 0) {
                resolve(stdout);
            } else {
                reject(new Error(`Script failed (exit ${code}): ${stderr || stdout}`));
            }
        });
        child.on('error', reject);
        child.stdin.write(`set -e\n${script}\n`);
        child.stdin.end();
    });
}

export async function setupGithubOnBeam(
    options: GithubSetupOptions,
    progress: vscode.Progress<{ message?: string; increment?: number }>
): Promise<void> {
    const { beamId, username, email, authMethod, pat, cloneRepo, cloneDir } = options;
    const timeout = 120000;

    progress.report({ message: 'Configuring git identity...' });
    await execOnBeam(beamId, ['git', 'config', '--global', 'user.name', username], timeout);
    await execOnBeam(beamId, ['git', 'config', '--global', 'user.email', email], timeout);
    await execOnBeam(beamId, ['git', 'config', '--global', 'pager.branch', 'false'], timeout);

    if (authMethod === 'tsh-git') {
        // tsh git config update is a per-repo command; run it after cloning, not here
    } else {
        progress.report({ message: 'Checking GitHub CLI...' });
        let ghInstalled = false;
        try {
            await execOnBeam(beamId, ['which', 'gh'], timeout);
            ghInstalled = true;
        } catch { /* not installed */ }

        if (!ghInstalled) {
            progress.report({ message: 'Installing GitHub CLI...' });
            await execScriptOnBeam(beamId, GH_INSTALL_SCRIPT, timeout);
        }

        if (authMethod === 'pat' && pat) {
            progress.report({ message: 'Authenticating with GitHub...' });
            const escapedPat = pat.trim().replace(/'/g, "'\\''");
            await execScriptOnBeam(beamId, `printf '%s' '${escapedPat}' | gh auth login -h github.com -p https --with-token`, timeout);
            progress.report({ message: 'Configuring git credential helper...' });
            await execScriptOnBeam(beamId, 'gh auth setup-git', timeout);
        }

        if (authMethod === 'pat') {
            progress.report({ message: 'Configuring Teleport git proxy...' });
            await execScriptOnBeam(beamId, 'tsh git config update 2>/dev/null || true', timeout);
        }
    }

    if (cloneRepo && authMethod !== 'oauth') {
        progress.report({ message: `Cloning ${cloneRepo}...` });
        const ownerRepo = toOwnerRepo(cloneRepo);
        const repoName = ownerRepo.split('/').pop()?.replace(/\.git$/, '') || 'repo';
        const resolvedCloneDir = cloneDir?.trim().replace(/\/+$/, '') || `/home/beams/${repoName}`;
        const cloneCmd = authMethod === 'tsh-git'
            ? `tsh git clone git@github.com:${ownerRepo}.git "${resolvedCloneDir}"`
            : `gh repo clone "${ownerRepo}" "${resolvedCloneDir}"`;
        await execScriptOnBeam(beamId, [
            `if [ -d "${resolvedCloneDir}/.git" ]; then`,
            `  echo "Repo already exists at ${resolvedCloneDir}, skipping clone."`,
            `else`,
            `  ${cloneCmd}`,
            `fi`,
        ].join('\n'), 300000);

        if (authMethod === 'tsh-git') {
            progress.report({ message: 'Configuring Teleport git proxy...' });
            await execScriptOnBeam(beamId, `cd "${resolvedCloneDir}" && tsh git config update`, timeout);
        }
    }
}

export async function isTshGitAvailable(beamId: string): Promise<boolean> {
    try {
        await execOnBeam(beamId, ['tsh', 'git', 'config', 'update'], 10000);
        return true;
    } catch {
        return false;
    }
}

export async function autoSetupGithub(
    beamId: string,
    context: vscode.ExtensionContext,
    progress: vscode.Progress<{ message?: string; increment?: number }>,
    force = false,
): Promise<{ applied: boolean; error?: string }> {
    const config = vscode.workspace.getConfiguration('beams');
    if (!force) {
        const autoSetup = config.get<boolean>('github.autoSetup', true);
        if (!autoSetup) {
            return { applied: false };
        }
    }

    const username = config.get<string>('github.username');
    const email = config.get<string>('github.email');
    const authMethod = config.get<string>('github.authMethod') as 'pat' | 'oauth' | 'tsh-git' | '';
    const defaultCloneRepo = config.get<string>('github.defaultCloneRepo');

    if (!username || !authMethod) {
        return { applied: false };
    }

    const pat = authMethod === 'pat' ? await context.secrets.get(SECRET_KEY) : undefined;
    if (authMethod === 'pat' && !pat) {
        return { applied: false, error: 'Stored PAT not found — run "Setup GitHub on Beam" to re-enter' };
    }

    if (authMethod === 'oauth') {
        try {
            await setupGithubOnBeam({
                beamId,
                username,
                email: email || `${username}@users.noreply.github.com`,
                authMethod: 'oauth',
            }, progress);
            return { applied: true };
        } catch (err) {
            return { applied: false, error: err instanceof Error ? err.message : String(err) };
        }
    }

    try {
        await setupGithubOnBeam({
            beamId,
            username,
            email: email || `${username}@users.noreply.github.com`,
            authMethod,
            pat,
            cloneRepo: defaultCloneRepo || undefined,
        }, progress);
        return { applied: true };
    } catch (err) {
        return { applied: false, error: err instanceof Error ? err.message : String(err) };
    }
}

// Normalise any GitHub URL variant to "owner/repo".
// Accepts: https://github.com/owner/repo[.git], git@github.com:owner/repo[.git], owner/repo
// Only used within this module now that the create-pull-request command is gone.
function toOwnerRepo(input: string): string {
    const m = input.trim().match(/(?:github\.com[/:])([\w.-]+\/[\w.-]+?)(?:\.git)?\/?$/);
    return m ? m[1] : input.replace(/\.git$/, '').replace(/\/+$/, '');
}

