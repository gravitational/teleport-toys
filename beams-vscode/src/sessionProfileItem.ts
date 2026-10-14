import * as vscode from 'vscode';
import { SessionProfile } from './sessionProfiles';

export class SessionProfileItem extends vscode.TreeItem {
    constructor(public readonly profile: SessionProfile) {
        super(profile.label, vscode.TreeItemCollapsibleState.None);

        const tooltip = [`Task: ${profile.taskId}`];

        if (profile.gitBranch) {
            const shortSha = (profile.gitCommitSha ?? '').slice(0, 7);
            this.description = `${profile.gitBranch}@${shortSha}`;
            this.iconPath = new vscode.ThemeIcon('history');
            tooltip.push(`Branch: ${profile.gitBranch}`, `Commit: ${profile.gitCommitSha}`);
        } else {
            this.iconPath = new vscode.ThemeIcon('history');
        }

        if (profile.beamId) {
            tooltip.push(`Saved from beam: ${profile.beamId}`);
        }
        tooltip.push(
            `Created by: ${profile.createdBy}`,
            `Created: ${profile.createdAt}`,
            `Updated: ${profile.updatedAt}`,
        );
        this.tooltip = tooltip.join('\n');
        this.contextValue = 'sessionProfile';

        this.command = {
            command: 'beams.viewSessionProfile',
            title: 'View Session Profile',
            arguments: [this],
        };
    }
}
