import { execOnBeam, shellSingleQuote } from './tsh';

// Registry of coding-agent CLIs this extension knows how to drive headlessly for
// session-summary generation, plus the global memory file each one auto-loads —
// so features like sessionProfiles.ts aren't tied to any single agent (mirrors the
// per-tool dotdir generalization in events.ts, but for invoking a CLI rather than
// reading its transcript).
export interface AgentAdapter {
    id: string;
    binary: string;
    globalMemoryFile: string;
    buildSummaryCommand(prompt: string): string;
    launchCommand: string;
}

export const agentAdapters: AgentAdapter[] = [
    {
        id: 'claude',
        binary: 'claude',
        globalMemoryFile: '/home/beams/.claude/CLAUDE.md',
        buildSummaryCommand: (prompt) => `claude --continue -p ${shellSingleQuote(prompt)}`,
        launchCommand: 'claude',
    },
    {
        id: 'codex',
        binary: 'codex',
        globalMemoryFile: '/home/beams/AGENTS.md',
        buildSummaryCommand: (prompt) => `codex exec resume --last ${shellSingleQuote(prompt)}`,
        launchCommand: 'codex',
    },
];

// Every adapter whose binary resolves on the beam, in registry priority order.
export async function detectAgents(beamId: string): Promise<AgentAdapter[]> {
    const checks = agentAdapters.map(a => `command -v ${a.binary} >/dev/null 2>&1 && echo ${a.id}`);
    let output = '';
    try {
        output = await execOnBeam(beamId, [checks.join('; ')], 10000);
    } catch {
        return [];
    }
    const found = new Set(output.split('\n').map(l => l.trim()).filter(Boolean));
    return agentAdapters.filter(a => found.has(a.id));
}

// `--continue` / `resume --last` are scoped to the directory they're invoked from — each
// records a `cwd` field in its transcript, filed under a per-directory project folder (e.g.
// Claude Code's `~/.claude/projects/<encoded-cwd>/*.jsonl`). A session profile's repoRoot
// (the git repo root) isn't necessarily where the real interactive session was running —
// it's common to run a coding agent from the beam's home directory one level up from the
// actual repo checkout — so invoking the continue/resume command from repoRoot can silently
// start a brand-new, contextless session instead of resuming the real one. This finds the
// most recently modified transcript across any `~/.<tool>` dotdir on the beam (same scan
// events.ts uses) and reads back its recorded cwd, so the summary command runs from
// wherever the actual most-recent session was, not just repoRoot.
export async function findMostRecentSessionCwd(beamId: string): Promise<string | undefined> {
    try {
        const transcriptOut = await execOnBeam(beamId, [
            'find /home/beams/.[!.]* -maxdepth 6 -name "*.jsonl" -not -path "*/subagents/*" ' +
            '-not -path "/home/beams/.vscode-server*/*" -printf "%T@ %p\\n" 2>/dev/null | ' +
            'sort -rn | head -1 | cut -d" " -f2-',
        ], 10000);
        const transcriptPath = transcriptOut.trim();
        if (!transcriptPath) return undefined;

        const grepOut = await execOnBeam(beamId, [
            `grep -m1 -o '"cwd":"[^"]*"' ${shellSingleQuote(transcriptPath)} 2>/dev/null`,
        ], 10000);
        const match = grepOut.match(/"cwd":"([^"]*)"/);
        return match?.[1];
    } catch {
        return undefined;
    }
}
