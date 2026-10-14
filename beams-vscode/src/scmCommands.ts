import * as vscode from 'vscode';
import { BeamPoller } from './polling';

// Read-only git surface: the beam's repo state is shown and diffable, but nothing here
// mutates it. Staging, commit, discard, push, and pull-request creation were all removed
// deliberately — run those from a terminal on the beam.
export function registerScmCommands(
    context: vscode.ExtensionContext,
    getPoller: () => BeamPoller | undefined,
): void {
    context.subscriptions.push(
        vscode.commands.registerCommand('beams.showDiff', async (item?: { beamId?: string; path?: string; name?: string; entry?: { beamId?: string; path?: string; name?: string } }) => {
            const poller = getPoller();
            // Inline tree-item commands receive the FileItem itself, whose fields live
            // under `.entry` — fall back to that shape if the flat fields aren't present.
            const beamId = item?.beamId ?? item?.entry?.beamId;
            const path = item?.path ?? item?.entry?.path;
            const name = item?.name ?? item?.entry?.name;
            if (!beamId || !path || !poller?.hasGitRepo()) return;

            const originalUri = vscode.Uri.parse(`beam-git://${beamId}${path}`);
            const workingUri = vscode.Uri.parse(`beam://${beamId}${path}`);
            const title = `${name ?? path.split('/').pop()} (Working Tree)`;
            await vscode.commands.executeCommand('vscode.diff', originalUri, workingUri, title);
        }),

        vscode.commands.registerCommand('beams.gitRefreshScm', () => {
            getPoller()?.pollNow();
        }),
    );
}
