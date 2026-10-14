import * as vscode from 'vscode';
import { PollConsumer } from './polling';

interface ParsedStatus {
    staged: vscode.SourceControlResourceState[];
    changes: vscode.SourceControlResourceState[];
    untracked: vscode.SourceControlResourceState[];
}

export class BeamGitScmProvider implements PollConsumer, vscode.QuickDiffProvider, vscode.Disposable {
    private sourceControl: vscode.SourceControl;
    private stagedGroup: vscode.SourceControlResourceGroup;
    private changesGroup: vscode.SourceControlResourceGroup;
    private untrackedGroup: vscode.SourceControlResourceGroup;

    constructor(
        private beamId: string,
        private repoRoot: string,
    ) {
        const rootUri = vscode.Uri.parse(`beam://${beamId}${repoRoot}`);
        this.sourceControl = vscode.scm.createSourceControl('beam-git', 'Beam Git', rootUri);
        this.sourceControl.quickDiffProvider = this;
        // No commit command exists, so the message box would be a dead end.
        this.sourceControl.inputBox.visible = false;

        this.stagedGroup = this.sourceControl.createResourceGroup('staged', 'Staged Changes');
        this.changesGroup = this.sourceControl.createResourceGroup('changes', 'Changes');
        this.untrackedGroup = this.sourceControl.createResourceGroup('untracked', 'Untracked Files');

        this.stagedGroup.hideWhenEmpty = true;
        this.changesGroup.hideWhenEmpty = true;
        this.untrackedGroup.hideWhenEmpty = true;
    }

    provideOriginalResource(uri: vscode.Uri): vscode.Uri | undefined {
        if (uri.scheme !== 'beam' || uri.authority !== this.beamId) {
            return undefined;
        }
        return uri.with({ scheme: 'beam-git' });
    }

    onGitStatus(_beamId: string, _repoRoot: string, porcelain: string): void {
        const parsed = this.parsePorcelain(porcelain);
        this.stagedGroup.resourceStates = parsed.staged;
        this.changesGroup.resourceStates = parsed.changes;
        this.untrackedGroup.resourceStates = parsed.untracked;
    }

    private parsePorcelain(output: string): ParsedStatus {
        const staged: vscode.SourceControlResourceState[] = [];
        const changes: vscode.SourceControlResourceState[] = [];
        const untracked: vscode.SourceControlResourceState[] = [];

        for (const line of output.split('\n')) {
            if (line.length < 4) continue;
            const x = line[0];
            const y = line[1];
            const filePath = line.slice(3).trim();
            if (!filePath) continue;

            const absPath = filePath.startsWith('/')
                ? filePath
                : `${this.repoRoot}/${filePath}`;
            const workingUri = vscode.Uri.parse(`beam://${this.beamId}${absPath}`);
            const originalUri = vscode.Uri.parse(`beam-git://${this.beamId}${absPath}`);

            if (x === '?' && y === '?') {
                untracked.push({
                    resourceUri: workingUri,
                    decorations: { faded: true, tooltip: 'Untracked' },
                    command: {
                        command: 'vscode.open',
                        title: 'Open File',
                        arguments: [workingUri],
                    },
                });
                continue;
            }

            // Staged changes (index)
            if (x !== ' ' && x !== '?') {
                const strikeThrough = x === 'D';
                staged.push({
                    resourceUri: workingUri,
                    decorations: { strikeThrough, tooltip: statusLabel(x) },
                    command: {
                        command: 'vscode.diff',
                        title: 'Show Diff',
                        arguments: [originalUri, workingUri, `${fileName(filePath)} (Staged)`],
                    },
                });
            }

            // Working tree changes
            if (y !== ' ' && y !== '?') {
                const strikeThrough = y === 'D';
                changes.push({
                    resourceUri: workingUri,
                    decorations: { strikeThrough, tooltip: statusLabel(y) },
                    command: {
                        command: 'vscode.diff',
                        title: 'Show Diff',
                        arguments: [originalUri, workingUri, `${fileName(filePath)} (Working Tree)`],
                    },
                });
            }
        }

        return { staged, changes, untracked };
    }

    dispose(): void {
        this.sourceControl.dispose();
    }
}

function statusLabel(code: string): string {
    switch (code) {
        case 'M': return 'Modified';
        case 'A': return 'Added';
        case 'D': return 'Deleted';
        case 'R': return 'Renamed';
        case 'C': return 'Copied';
        case 'U': return 'Conflict';
        default: return 'Changed';
    }
}

function fileName(filePath: string): string {
    return filePath.split('/').pop() ?? filePath;
}
