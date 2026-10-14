import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execOnBeam, shellSingleQuote } from './tsh';
import { AgentAdapter, detectAgents, findMostRecentSessionCwd } from './agents';

// A profile captures a resumable work session: repoRoot/gitBranch/gitCommitSha/remoteUrl,
// plus an AI-drafted (or manually edited) summary, so you can pick up where you left off
// on a fresh beam.
export interface SessionProfile {
    taskId: string;
    label: string;
    beamId: string;
    repoRoot?: string;
    gitBranch?: string;
    gitCommitSha?: string;
    remoteUrl?: string;
    createdBy: string;
    createdAt: string;
    updatedAt: string;
}

function getProfilesRoot(): string {
    return path.join(os.homedir(), '.teleport', 'beams', 'session-profiles');
}

function getProfileDir(taskId: string): string {
    return path.join(getProfilesRoot(), taskId);
}

type RawProfile = Record<string, unknown>;

// "owner/repo" shorthand -> full https clone URL; leave anything already URL-shaped alone.
function toRemoteUrl(repo: string): string {
    return /^[\w.-]+\/[\w.-]+$/.test(repo) ? `https://github.com/${repo}.git` : repo;
}

// The RFD "Beams Task Profile" schema (schema_version: 1) that the real save-session/
// resume-session workflow produces, e.g.:
// { schema_version, task_id, tenant_id, user_id, created_at, updated_at,
//   git: { repo, branch, commit_sha }, summary_object, scan: {...} }
// This is now the default shape we expect on disk.
function isRfdProfileShape(raw: RawProfile): boolean {
    return typeof raw.task_id === 'string' && typeof raw.git === 'object' && raw.git !== null;
}

function fromRfdShape(raw: RawProfile): SessionProfile {
    const git = (raw.git ?? {}) as RawProfile;
    const taskId = String(raw.task_id);
    const repo = typeof git.repo === 'string' ? git.repo : undefined;
    return {
        taskId,
        label: typeof raw.label === 'string' ? raw.label : taskId,
        beamId: typeof raw.beam_id === 'string' ? raw.beam_id : '',
        repoRoot: typeof raw.repo_root === 'string' ? raw.repo_root : '',
        gitBranch: String(git.branch ?? ''),
        gitCommitSha: String(git.commit_sha ?? ''),
        remoteUrl: repo ? toRemoteUrl(repo) : undefined,
        createdBy: String(raw.user_id ?? ''),
        createdAt: String(raw.created_at ?? ''),
        updatedAt: String(raw.updated_at ?? raw.created_at ?? ''),
    };
}

// Best-effort fallback for anything that isn't the RFD shape above (including this
// prototype's own legacy flat/camelCase profile.json) — flattens every leaf value in the
// JSON (regardless of nesting) and matches it against known field names by normalized
// (lowercased, punctuation-stripped) key, so "task_id", "taskId", and "git.branch" /
// "gitBranch" all resolve the same way.
const FIELD_ALIASES: Record<keyof SessionProfile, string[]> = {
    taskId: ['taskid', 'id'],
    label: ['label', 'name', 'title'],
    beamId: ['beamid'],
    repoRoot: ['reporoot', 'root'],
    gitBranch: ['gitbranch', 'branch'],
    gitCommitSha: ['gitcommitsha', 'commitsha', 'sha'],
    remoteUrl: ['remoteurl', 'repo', 'repourl'],
    createdBy: ['createdby', 'userid', 'author'],
    createdAt: ['createdat'],
    updatedAt: ['updatedat'],
};

function normalizeKey(key: string): string {
    return key.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function flattenLeaves(obj: unknown, out: Map<string, unknown> = new Map()): Map<string, unknown> {
    if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
        return out;
    }
    for (const [key, value] of Object.entries(obj as RawProfile)) {
        if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
            flattenLeaves(value, out);
        } else if (!out.has(normalizeKey(key))) {
            out.set(normalizeKey(key), value);
        }
    }
    return out;
}

function fromDynamicMatch(raw: RawProfile): SessionProfile {
    const leaves = flattenLeaves(raw);
    const pick = (aliases: string[]): string => {
        for (const alias of aliases) {
            const value = leaves.get(alias);
            if (typeof value === 'string' && value) {
                return value;
            }
        }
        return '';
    };

    const taskId = pick(FIELD_ALIASES.taskId);
    const remoteUrl = pick(FIELD_ALIASES.remoteUrl);
    return {
        taskId,
        label: pick(FIELD_ALIASES.label) || taskId,
        beamId: pick(FIELD_ALIASES.beamId),
        repoRoot: pick(FIELD_ALIASES.repoRoot) || undefined,
        gitBranch: pick(FIELD_ALIASES.gitBranch) || undefined,
        gitCommitSha: pick(FIELD_ALIASES.gitCommitSha) || undefined,
        remoteUrl: remoteUrl ? toRemoteUrl(remoteUrl) : undefined,
        createdBy: pick(FIELD_ALIASES.createdBy),
        createdAt: pick(FIELD_ALIASES.createdAt),
        updatedAt: pick(FIELD_ALIASES.updatedAt) || pick(FIELD_ALIASES.createdAt),
    };
}

function parseSessionProfileJson(text: string): SessionProfile | undefined {
    const raw = JSON.parse(text) as RawProfile;
    const profile = isRfdProfileShape(raw) ? fromRfdShape(raw) : fromDynamicMatch(raw);
    return profile.taskId ? profile : undefined;
}

export function listSessionProfiles(): SessionProfile[] {
    const root = getProfilesRoot();
    const profiles: SessionProfile[] = [];
    if (fs.existsSync(root)) {
        for (const taskId of fs.readdirSync(root)) {
            const file = path.join(root, taskId, 'profile.json');
            if (!fs.existsSync(file)) continue;
            try {
                const profile = parseSessionProfileJson(fs.readFileSync(file, 'utf-8'));
                if (profile) {
                    profiles.push(profile);
                }
            } catch { /* skip corrupt profile */ }
        }
        profiles.sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? ''));
    }
    return profiles;
}

export function getSessionProfile(taskId: string): SessionProfile | undefined {
    const file = path.join(getProfileDir(taskId), 'profile.json');
    if (!fs.existsSync(file)) {
        return undefined;
    }
    try {
        return parseSessionProfileJson(fs.readFileSync(file, 'utf-8'));
    } catch {
        return undefined;
    }
}

export function getSessionSummaryPath(taskId: string): string {
    return path.join(getProfileDir(taskId), 'summary.md');
}

export function getSessionSummary(taskId: string): string {
    const file = getSessionSummaryPath(taskId);
    return fs.existsSync(file) ? fs.readFileSync(file, 'utf-8') : '';
}

// Prototype storage only: local disk, standing in for the RFD's S3-backed profile
// store. Also skips the RFD's scan-before-write/scan-before-load step — treat any
// summary as trusted input for now until that check exists.
export function saveSessionProfile(profile: SessionProfile, summaryMd: string): void {
    const dir = getProfileDir(profile.taskId);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'profile.json'), JSON.stringify(profile, null, 2), 'utf-8');
    fs.writeFileSync(path.join(dir, 'summary.md'), summaryMd, 'utf-8');
}

export function deleteSessionProfile(taskId: string): void {
    const dir = getProfileDir(taskId);
    if (fs.existsSync(dir)) {
        fs.rmSync(dir, { recursive: true, force: true });
    }
}

export async function captureGitRef(beamId: string, repoRoot: string): Promise<{ branch: string; sha: string }> {
    const output = await execOnBeam(beamId, [
        `cd ${shellSingleQuote(repoRoot)} && git rev-parse --abbrev-ref HEAD && echo "---SEP---" && git rev-parse HEAD`,
    ], 10000);
    const sepIdx = output.indexOf('---SEP---');
    if (sepIdx === -1) {
        throw new Error('Failed to read git ref from beam');
    }
    const branch = output.slice(0, sepIdx).trim();
    const sha = output.slice(sepIdx + '---SEP---'.length).trim();
    return { branch, sha };
}

// `branch`/`sha` may come straight from a saved profile.json (untrusted — profiles are meant
// to be portable/shareable) rather than from input validated at save time, so they must be
// quoted rather than interpolated into the double-quoted command below.
export async function applyGitRef(beamId: string, repoRoot: string, branch: string, sha: string): Promise<void> {
    const root = shellSingleQuote(repoRoot);
    const ref = shellSingleQuote(branch);
    const remoteRef = shellSingleQuote(`origin/${branch}`);
    const commit = shellSingleQuote(sha);
    await execOnBeam(beamId, [
        `cd ${root} && git fetch origin ${ref} 2>/dev/null; ` +
        `(git checkout ${ref} 2>/dev/null || git checkout -b ${ref} ${remoteRef}) && git reset --hard ${commit}`,
    ], 30000);
}

export async function captureRemoteUrl(beamId: string, repoRoot: string): Promise<string | undefined> {
    try {
        const output = await execOnBeam(beamId, [`cd ${shellSingleQuote(repoRoot)} && git config --get remote.origin.url`], 10000);
        const url = output.trim();
        return url || undefined;
    } catch {
        return undefined;
    }
}

// `remoteUrl`/`targetDir` may originate from a saved profile.json (untrusted) when resuming.
export async function cloneRepoOnBeam(beamId: string, remoteUrl: string, targetDir: string): Promise<void> {
    await execOnBeam(beamId, [`git clone ${shellSingleQuote(remoteUrl)} ${shellSingleQuote(targetDir)}`], 180000);
}

export interface SessionSummaryResult {
    text?: string;
    error?: string;
}

// RFD: "Beams summarizes the session into the profile format." The beam already runs
// whichever coding-agent CLI is installed for the agentic session itself, so ask that
// same CLI (in headless mode, resuming its most recent conversation in this repo) to
// draft the summary — detected via agents.ts rather than assumed to be Claude Code, so
// this works on a beam provisioned with any supported agent. `recentActivity` (git
// log/status) is folded into the prompt so the draft stays grounded even when the
// resumed conversation finds no prior session to continue.
export async function generateSessionSummary(
    beamId: string,
    repoRoot: string,
    label: string,
    recentActivity?: string,
): Promise<SessionSummaryResult> {
    const agents = await detectAgents(beamId);
    const agent = agents[0];
    if (!agent) {
        return { error: 'No supported coding agent CLI (claude, codex) found on this beam.' };
    }

    const prompt = [
        `Write a concise session summary for a task-resumption profile called "${label}".`,
        'Use exactly this markdown structure with no extra preamble or closing remarks:',
        '',
        '## What was tried',
        '',
        '## Decisions made',
        '',
        "## What's left",
        '',
        'Fill in terse bullet points under each heading based on this coding session. ' +
        'If a section has nothing to report, leave it with a single bullet saying so.',
        ...(recentActivity ? ['', 'Recent git activity for additional context:', '', recentActivity] : []),
    ].join('\n');

    // Prefer wherever the most recently active session on this beam actually ran (see
    // findMostRecentSessionCwd) over repoRoot, since a beam is normally dedicated to one
    // task at a time and the real conversation may not have run from inside the repo.
    const cwd = await findMostRecentSessionCwd(beamId) ?? repoRoot;
    const cmd = `cd ${shellSingleQuote(cwd)} && ${agent.buildSummaryCommand(prompt)}`;
    try {
        const output = await execOnBeam(beamId, [cmd], 120000);
        const text = output.trim();
        return text ? { text } : { error: `${agent.id} produced no output.` };
    } catch (err: unknown) {
        const stderr = typeof (err as { stderr?: unknown })?.stderr === 'string' ? (err as { stderr: string }).stderr.trim() : '';
        const message = stderr || (err instanceof Error ? err.message : String(err));
        return { error: message };
    }
}

// Lightweight, non-AI fallback seed for the editable summary template when agent-based
// generation above fails — real git signal so the scratch buffer never starts fully blank.
export async function captureRecentActivity(beamId: string, repoRoot: string): Promise<string> {
    let log = '';
    let status = '';
    const root = shellSingleQuote(repoRoot);
    try {
        log = await execOnBeam(beamId, [`cd ${root} && git log --oneline -15 2>/dev/null`], 10000);
    } catch { /* ignore */ }
    try {
        status = await execOnBeam(beamId, [`cd ${root} && git status --porcelain=v1 2>/dev/null`], 10000);
    } catch { /* ignore */ }

    const sections: string[] = [];
    if (log.trim()) {
        sections.push('Recent commits:', '```', log.trim(), '```');
    }
    if (status.trim()) {
        sections.push('Uncommitted changes at save time:', '```', status.trim(), '```');
    }
    return sections.join('\n\n');
}

// Written under the extension's own directory, not any agent's dotdir — this file is
// Beams' own resumable-task state, not a memory format any particular agent auto-loads.
export async function writeSessionSummaryToBeam(
    beamId: string,
    repoRoot: string,
    taskId: string,
    summaryMd: string,
): Promise<string> {
    const remotePath = `${repoRoot}/.beams/session-memory/${taskId}.md`;
    const remoteDir = remotePath.slice(0, remotePath.lastIndexOf('/'));
    const encoded = Buffer.from(summaryMd, 'utf-8').toString('base64');
    await execOnBeam(beamId, [`mkdir -p ${shellSingleQuote(remoteDir)} && echo "${encoded}" | base64 -d > ${shellSingleQuote(remotePath)}`]);
    return remotePath;
}

// Each coding-agent CLI auto-loads its own global memory file into every session on the
// beam without being asked (e.g. Claude Code reads `.claude/CLAUDE.md`, Codex reads
// `AGENTS.md` — see agents.ts) — unlike the per-repo session-memory file above, which
// only gets read if something explicitly tells the agent to go look at it. Appending the
// summary here means a freshly started session already has the context, no initial
// "go read this file" prompt required. Idempotent: re-resuming the same task replaces
// its earlier block instead of appending a duplicate.
export async function appendSessionSummaryToAgentMemory(
    beamId: string,
    agent: AgentAdapter,
    taskId: string,
    label: string,
    summaryMd: string,
): Promise<void> {
    const remotePath = agent.globalMemoryFile;
    const beginMarker = `<!-- BEGIN session-memory:${taskId} -->`;
    const endMarker = `<!-- END session-memory:${taskId} -->`;
    const block = [beginMarker, `## Resumed task: ${label}`, '', summaryMd.trim(), endMarker].join('\n');

    let existing = '';
    try {
        existing = await execOnBeam(beamId, [`cat "${remotePath}" 2>/dev/null || true`]);
    } catch { /* no existing global memory file */ }

    const blockPattern = new RegExp(`${beginMarker}[\\s\\S]*?${endMarker}`);
    const updated = blockPattern.test(existing)
        ? existing.replace(blockPattern, block)
        : `${existing.trim()}${existing.trim() ? '\n\n' : ''}${block}\n`;

    const remoteDir = remotePath.slice(0, remotePath.lastIndexOf('/'));
    const encoded = Buffer.from(updated, 'utf-8').toString('base64');
    await execOnBeam(beamId, [`mkdir -p ${shellSingleQuote(remoteDir)} && echo "${encoded}" | base64 -d > "${remotePath}"`]);
}
