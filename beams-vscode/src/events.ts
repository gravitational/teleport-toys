import * as vscode from 'vscode';
import { Beam, execOnBeam } from './tsh';

interface AgentEvent {
    timestamp: string;
    type: string;
    summary: string;
    detail?: string;
    icon: string;
}

class EventItem extends vscode.TreeItem {
    constructor(public readonly event: AgentEvent) {
        super(event.summary, vscode.TreeItemCollapsibleState.None);
        this.description = event.timestamp;
        this.iconPath = new vscode.ThemeIcon(event.icon);
        this.tooltip = event.detail ?? event.summary;
        if (event.detail) {
            this.command = {
                command: 'beams.showActivityDetail',
                title: 'Show Detail',
                arguments: [{ label: event.summary, detail: event.detail }],
            };
        }
    }
}

export class AgentEventsProvider implements vscode.TreeDataProvider<EventItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<EventItem | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    private currentBeam: Beam | undefined;
    private events: AgentEvent[] = [];
    private pollInterval: NodeJS.Timeout | undefined;
    private transcriptPath: string | undefined;
    private bytesRead = 0;
    private initialLoadDone = false;

    setBeam(beam: Beam): void {
        this.currentBeam = beam;
        this.events = [];
        this.transcriptPath = undefined;
        this.bytesRead = 0;
        this.initialLoadDone = false;
        this.startPolling();
        this._onDidChangeTreeData.fire(undefined);
    }

    stop(): void {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = undefined;
        }
    }

    private startPolling(): void {
        this.stop();
        this.poll();
        this.pollInterval = setInterval(() => this.poll(), 3000);
    }

    private async poll(): Promise<void> {
        if (!this.currentBeam) return;

        try {
            if (!this.transcriptPath) {
                this.transcriptPath = await this.findTranscript();
                if (!this.transcriptPath) return;
            }

            const sizeOutput = await execOnBeam(this.currentBeam.id, [
                'stat', '--format=%s', this.transcriptPath
            ]);
            const fileSize = parseInt(sizeOutput.trim(), 10);
            if (isNaN(fileSize) || fileSize === 0) {
                if (this.events.length > 0) {
                    this.events = [];
                    this._onDidChangeTreeData.fire(undefined);
                }
                return;
            }

            if (fileSize === this.bytesRead) return;

            let output: string;
            if (!this.initialLoadDone) {
                output = await execOnBeam(this.currentBeam.id, [
                    'cat', this.transcriptPath
                ], 60000);
                this.initialLoadDone = true;
            } else {
                output = await execOnBeam(this.currentBeam.id, [
                    'tail', '-c', `+${this.bytesRead + 1}`, this.transcriptPath
                ]);
            }

            this.bytesRead = fileSize;

            if (!output.trim()) return;

            const lines = output.split('\n').filter(l => l.trim());
            const newEvents = this.parseEvents(lines);

            if (newEvents.length > 0) {
                this.events.push(...newEvents);
                this._onDidChangeTreeData.fire(undefined);
            }
        } catch {
            this.transcriptPath = undefined;
            this.bytesRead = 0;
            this.initialLoadDone = false;
        }
    }

    // Scans every per-tool dotdir under the home dir (~/.claude, ~/.codex, ~/.copilot, etc.)
    // rather than assuming Claude Code specifically, so the panel picks up whichever coding
    // agent most recently wrote a JSONL transcript on this beam. `.vscode-server*` is excluded
    // because VS Code's own remote server writes unrelated JSON-RPC logs there with a .jsonl
    // extension (its internal "Agent Host Process" protocol, not a coding agent transcript).
    private async findTranscript(): Promise<string | undefined> {
        if (!this.currentBeam) return undefined;
        try {
            const output = await execOnBeam(this.currentBeam.id, [
                'find /home/beams/.[!.]* -maxdepth 6 -name "*.jsonl" -not -path "*/subagents/*" ' +
                '-not -path "/home/beams/.vscode-server*/*" -printf "%T@ %p\\n" 2>/dev/null | ' +
                'sort -rn | head -1 | cut -d" " -f2-'
            ]);
            const path = output.trim();
            return path || undefined;
        } catch {
            return undefined;
        }
    }

    private parseEvents(lines: string[]): AgentEvent[] {
        const events: AgentEvent[] = [];

        for (const line of lines) {
            try {
                const parsed = JSON.parse(line);
                const lineEvents = this.toEvents(parsed);
                events.push(...lineEvents);
            } catch {
                continue;
            }
        }

        return events;
    }

    private toEvents(parsed: Record<string, unknown>): AgentEvent[] {
        const ts = extractTimestamp(parsed);
        const msg = parsed.message as Record<string, unknown> | undefined;

        // Claude Code's transcript schema nests role/content under `message`. Other agents'
        // JSONL (if/when present) won't necessarily match this shape, so fall back to a
        // best-effort generic reading rather than silently dropping the line.
        if (!msg) return toGenericEvent(parsed, ts);

        const role = msg.role as string | undefined;
        const content = msg.content;

        if (role === 'user') {
            // Tool result entries have role:'user' but content is [{type:'tool_result',...}]
            if (Array.isArray(content)) {
                for (const block of content) {
                    const b = block as Record<string, unknown>;
                    if (b.type === 'tool_result') {
                        const isError = b.is_error === true;
                        const text = extractToolResultText(b.content);
                        const snippet = text ? truncate(text, 70) : '';
                        return [{
                            timestamp: ts,
                            type: 'tool_result',
                            summary: isError ? `Error: ${snippet || 'tool failed'}` : (snippet || 'Tool completed'),
                            detail: text || undefined,
                            icon: isError ? 'error' : 'check',
                        }];
                    }
                }
            }
            const text = extractText(content);
            if (!text) return [];
            return [{
                timestamp: ts,
                type: 'user',
                summary: truncate(text, 80),
                detail: text,
                icon: 'account',
            }];
        }

        if (role === 'assistant') {
            if (!Array.isArray(content)) return [];
            const events: AgentEvent[] = [];

            for (const block of content) {
                const b = block as Record<string, unknown>;

                if (b.type === 'thinking') {
                    const text = (b.thinking as string) ?? '';
                    if (text.trim()) {
                        events.push({
                            timestamp: ts,
                            type: 'thinking',
                            summary: `Thinking: ${truncate(text, 70)}`,
                            detail: text,
                            icon: 'lightbulb',
                        });
                    }
                }

                if (b.type === 'tool_use') {
                    const name = (b.name as string) ?? 'tool';
                    const args = summarizeToolArgs(b.input);
                    events.push({
                        timestamp: ts,
                        type: 'tool_use',
                        summary: `${name}${args ? ': ' + args : ''}`,
                        detail: JSON.stringify(b.input, null, 2),
                        icon: 'wrench',
                    });
                }

                if (b.type === 'text') {
                    const text = (b.text as string) ?? '';
                    if (text.trim()) {
                        events.push({
                            timestamp: ts,
                            type: 'assistant',
                            summary: truncate(text, 80),
                            detail: text,
                            icon: 'hubot',
                        });
                    }
                }
            }

            return events;
        }

        // Fallback for any other entry type with tool_result content
        if (parsed.type === 'tool_result' || msg.type === 'tool_result') {
            const isError = (msg.is_error === true) || (parsed.is_error === true);
            const resultContent = msg.content ?? parsed.content;
            const text = extractToolResultText(resultContent);
            const snippet = text ? truncate(text, 70) : '';
            return [{
                timestamp: ts,
                type: 'tool_result',
                summary: isError ? `Error: ${snippet || 'tool failed'}` : (snippet || 'Tool completed'),
                detail: text || undefined,
                icon: isError ? 'error' : 'check',
            }];
        }

        return [];
    }

    getTreeItem(element: EventItem): vscode.TreeItem {
        return element;
    }

    getChildren(): EventItem[] {
        if (!this.currentBeam) {
            return [new EventItem({ timestamp: '', type: 'info', summary: 'Select a beam to view events', icon: 'info' })];
        }

        if (this.events.length === 0) {
            const msg = !this.initialLoadDone
                ? 'Loading event history...'
                : this.transcriptPath
                    ? 'Waiting for events...'
                    : 'No agent session found on this beam';
            const icon = !this.initialLoadDone || this.transcriptPath ? 'loading~spin' : 'info';
            return [new EventItem({ timestamp: '', type: 'info', summary: msg, icon })];
        }

        return this.events.slice().reverse().map(e => new EventItem(e));
    }
}

function extractTimestamp(parsed: Record<string, unknown>): string {
    const raw = (parsed.timestamp ?? parsed.time ?? parsed.ts ?? parsed.created_at) as string | number | undefined;
    if (!raw) return '';
    const date = typeof raw === 'number' ? new Date(raw) : new Date(raw);
    return isNaN(date.getTime()) ? '' : date.toLocaleTimeString();
}

// Best-effort event for JSONL lines that don't match Claude Code's `message.role`/`content`
// shape — covers other agents' transcripts by probing a handful of common field names instead
// of a single fixed schema.
function toGenericEvent(parsed: Record<string, unknown>, ts: string): AgentEvent[] {
    const role = (parsed.role ?? parsed.type ?? parsed.event ?? parsed.kind) as string | undefined;
    const text = extractText(parsed.content)
        || (typeof parsed.text === 'string' ? parsed.text : '')
        || (typeof parsed.message === 'string' ? parsed.message : '')
        || (typeof parsed.summary === 'string' ? parsed.summary : '');

    if (!text.trim()) return [];

    const icon = role === 'user' ? 'account' : role === 'assistant' ? 'hubot' : 'circle-outline';
    return [{
        timestamp: ts,
        type: role ?? 'event',
        summary: truncate(text, 80),
        detail: text,
        icon,
    }];
}

function extractText(content: unknown): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
        return content
            .filter((b: Record<string, unknown>) => b.type === 'text')
            .map((b: Record<string, unknown>) => b.text ?? '')
            .join('\n');
    }
    return '';
}

function truncate(s: string, max: number): string {
    const line = s.split('\n')[0] ?? s;
    return line.length > max ? line.slice(0, max) + '...' : line;
}

function extractToolResultText(content: unknown): string {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
        return content
            .map((b: Record<string, unknown>) => {
                if (b.type === 'text') return (b.text as string) ?? '';
                return '';
            })
            .filter(Boolean)
            .join('\n');
    }
    return '';
}

function summarizeToolArgs(input: unknown): string {
    if (!input || typeof input !== 'object') return '';
    const obj = input as Record<string, unknown>;
    if (obj.file_path) return String(obj.file_path).split('/').pop() ?? '';
    if (obj.path) return String(obj.path).split('/').pop() ?? '';
    if (obj.command) {
        const cmd = String(obj.command);
        return cmd.length > 60 ? cmd.slice(0, 60) + '...' : cmd;
    }
    if (obj.query) {
        const q = String(obj.query);
        return q.length > 60 ? q.slice(0, 60) + '...' : q;
    }
    return '';
}
