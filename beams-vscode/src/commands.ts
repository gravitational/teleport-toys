import * as vscode from 'vscode';
import { BeamItem, setBeamLabel } from './beamItem';
import { BeamsProvider } from './beamsProvider';
import { PhasesProvider } from './phasesProvider';
import { PhaseItem } from './phaseItem';
import { BeamFileExplorer, FileItem } from './fileExplorer';
import { addBeam, removeBeam, publishBeam, unpublishBeam, execOnBeam, scpFromBeam, checkStatus, listBeams, waitForBeamReady, detectRepoRoot, tshErrorMessage, tshBinary, listClusterProfiles, Phase, listPhases, listFailedPhaseSnapshots, removePhase } from './tsh';
import { openBeamTerminal, openPhaseAddTerminal, openPhaseAddForkTerminal, openPhaseEditTerminal, openPhaseSnapshotTerminal, openPhaseApplyTerminal } from './terminal';
import { reportTshError } from './notify';
import { setupGithubOnBeam, autoSetupGithub, SECRET_KEY } from './github';
import { ensureBeamSshConfig } from './ssh';
import { AgentEventsProvider } from './events';
import {
    LocalContainerSyncMode,
    isDockerAvailable,
    createLocalContainerRecord,
    getLocalContainerRecord,
    deleteLocalContainerRecord,
    generateDockerfile,
    writeDockerfile,
    writeDevcontainerJson,
    buildContainerImage,
    ensureContainerRunning,
    stopContainer,
    removeContainer,
    openContainerTerminal,
} from './localContainer';
import { ContainerSyncEngine } from './containerSync';
import * as path from 'path';
import * as os from 'os';

export function registerCommands(
    context: vscode.ExtensionContext,
    provider: BeamsProvider,
    phasesProvider: PhasesProvider,
    fileExplorer: BeamFileExplorer,
    eventsProvider: AgentEventsProvider,
    poller?: import('./polling').BeamPoller,
    _getScm?: () => import('./scm').BeamGitScmProvider | undefined,
    containerSync?: ContainerSyncEngine,
): void {
    async function deletePhaseWithConfirm(name: string): Promise<void> {
        const confirm = await vscode.window.showWarningMessage(
            `Delete Phase "${name}"?`,
            { modal: true },
            'Delete'
        );
        if (confirm !== 'Delete') {
            return;
        }
        try {
            await removePhase(name);
            vscode.window.showInformationMessage(`Phase "${name}" deleted.`);
            phasesProvider.refresh();
        } catch (err: unknown) {
            reportTshError(err, { action: 'delete Phase', refresh: () => phasesProvider.refresh() });
        }
    }

    // A Phase's `parent` field names the image it was built from, as "<name>:<version>" or
    // "<name>@sha256:<digest>" — matching the "Phase name or parent@sha256:digest" format
    // `--fork-from` documents. Used to find a Phase's children.
    function isChildOfPhase(parent: string, name: string): boolean {
        return parent === name || parent.startsWith(`${name}:`) || parent.startsWith(`${name}@`);
    }

    async function forkPhaseFlow(parentName: string): Promise<void> {
        const name = await vscode.window.showInputBox({
            prompt: `Name for the new Phase forked from "${parentName}"`,
            placeHolder: 'e.g. dev-tools-v2',
            validateInput: value => value.trim() ? undefined : 'A name is required.',
        });
        if (!name) {
            return;
        }
        const addTerminal = openPhaseAddForkTerminal(name, parentName);
        const sub = vscode.window.onDidCloseTerminal(closed => {
            if (closed !== addTerminal) {
                return;
            }
            sub.dispose();
            openPhaseSnapshotTerminal(name);
        });
        context.subscriptions.push(sub);
    }

    // `tsh phase edit` opens a build console against the Phase's last successful snapshot.
    // A Phase that's still building or whose last build failed has no such snapshot, and
    // the CLI fails with "has no successful snapshot" — but that failure only ever shows up
    // in the integrated terminal's transient exit banner, which is easy to miss. Catch the
    // known-bad states up front and explain why, instead of letting the terminal fail silently.
    //
    // A Phase that already has a snapshot-strategy child (one forked from its build Beam's
    // filesystem with `--fork`/`--fork-from`) can't be edited in place — re-snapshotting it
    // would invalidate what the child captured. Fork a new Phase from it instead.
    async function editPhase(phase: Phase): Promise<void> {
        if (phase.state === 'building') {
            vscode.window.showWarningMessage(
                `Phase "${phase.name}" is still building, so it has no successful snapshot to edit yet. Try again once the build finishes.`
            );
            return;
        }
        if (phase.state === 'failed') {
            vscode.window.showWarningMessage(
                `Phase "${phase.name}" has no successful build (last build failed), so there's no snapshot to edit.`
            );
            return;
        }

        let allPhases: Phase[] = [];
        try {
            allPhases = await listPhases();
        } catch (err: unknown) {
            console.warn(`Beams: failed to list phases: ${tshErrorMessage(err)}`);
        }
        const hasSnapshotChild = allPhases.some(p => p.strategy === 'snapshot' && isChildOfPhase(p.parent, phase.name));
        if (hasSnapshotChild) {
            vscode.window.showInformationMessage(
                `Phase "${phase.name}" has a snapshot Phase forked from it, so it can't be edited in place. Forking a new Phase from it instead.`
            );
            await forkPhaseFlow(phase.name);
            return;
        }

        openPhaseEditTerminal(phase.name);
    }

    async function managePhase(phase: Phase): Promise<void> {
        const choice = await vscode.window.showQuickPick(
            [
                { label: '$(edit) Edit', description: 'Open build console to edit', action: 'edit' as const },
                { label: '$(save) Snapshot', description: 'Save a snapshot to this Phase', action: 'snapshot' as const },
                { label: '$(trash) Delete', description: 'Delete this Phase image', action: 'delete' as const },
            ],
            { placeHolder: `Phase: ${phase.name}` }
        );
        if (!choice) {
            return;
        }
        switch (choice.action) {
            case 'edit':
                await editPhase(phase);
                break;
            case 'snapshot':
                openPhaseSnapshotTerminal(phase.name);
                break;
            case 'delete':
                await deletePhaseWithConfirm(phase.name);
                break;
        }
    }

    async function createPhaseFlow(): Promise<void> {
        // A name is required (rather than left to the server to generate) so we know what
        // to pass to the follow-up `phase snapshot` below — without one, `tsh phase add`
        // leaves the Phase stuck in "building" until something snapshots it.
        const name = await vscode.window.showInputBox({
            prompt: 'Name for the new Phase',
            placeHolder: 'e.g. dev-tools',
            validateInput: value => value.trim() ? undefined : 'A name is required.',
        });
        if (!name) {
            return;
        }
        const addTerminal = openPhaseAddTerminal(name);
        const sub = vscode.window.onDidCloseTerminal(closed => {
            if (closed !== addTerminal) {
                return;
            }
            sub.dispose();
            openPhaseSnapshotTerminal(name);
        });
        context.subscriptions.push(sub);
    }

    context.subscriptions.push(
        vscode.commands.registerCommand('beams.select', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            fileExplorer.setBeam(item.beam);
            eventsProvider.setBeam(item.beam);
            if (poller) {
                await poller.setBeam(item.beam.id);
                vscode.commands.executeCommand('beams.selectScm');
            }
        }),

        vscode.commands.registerCommand('beams.refresh', () => {
            provider.refresh();
        }),

        vscode.commands.registerCommand('beams.refreshPhases', () => {
            phasesProvider.refresh();
        }),

        vscode.commands.registerCommand('beams.rename', async (item?: BeamItem) => {
            if (!item?.beam) { return; }
            const newName = await vscode.window.showInputBox({
                prompt: 'Enter a custom name for this beam (leave empty to reset)',
                placeHolder: item.beam.id,
                value: (item.label as string) !== item.beam.id ? (item.label as string) : '',
            });
            if (newName === undefined) { return; }
            setBeamLabel(item.beam.id, newName || undefined);
            provider.refresh();
        }),

        vscode.commands.registerCommand('beams.login', async () => {
            const NEW_CLUSTER = { label: '$(add) Enter a new cluster...', isNew: true as const, cluster: undefined as string | undefined };

            let knownClusters: { cluster: string; active: boolean }[] = [];
            try {
                const data = await listClusterProfiles();
                const seen = new Map<string, boolean>();
                if (data.active?.cluster) {
                    seen.set(data.active.cluster, true);
                }
                for (const p of data.profiles ?? []) {
                    if (p.cluster && !seen.has(p.cluster)) {
                        seen.set(p.cluster, false);
                    }
                }
                knownClusters = [...seen].map(([cluster, active]) => ({ cluster, active }));
            } catch {
                // No known profiles (or tsh status failed) — fall straight through to manual entry.
            }

            let cluster: string | undefined;
            if (knownClusters.length > 0) {
                const choice = await vscode.window.showQuickPick(
                    [
                        ...knownClusters.map(c => ({
                            label: `$(server) ${c.cluster}`,
                            description: c.active ? 'active' : undefined,
                            isNew: false as const,
                            cluster: c.cluster as string | undefined,
                        })),
                        NEW_CLUSTER,
                    ],
                    { placeHolder: 'Select a Teleport cluster to log in to' }
                );
                if (!choice) {
                    return;
                }
                cluster = choice.cluster;
            }

            if (!cluster) {
                cluster = await vscode.window.showInputBox({
                    prompt: 'Teleport cluster proxy address',
                    placeHolder: 'example.teleport.sh',
                });
                if (!cluster) {
                    return;
                }
            }

            const terminal = vscode.window.createTerminal({
                name: 'tsh login',
                shellPath: tshBinary(),
                shellArgs: ['login', `--proxy=${cluster}`],
                iconPath: new vscode.ThemeIcon('key'),
            });
            terminal.show();
        }),

        vscode.commands.registerCommand('beams.create', async () => {
            let phases: Phase[] = [];
            try {
                phases = await listPhases();
            } catch (err: unknown) {
                console.warn(`Beams: failed to list phases: ${tshErrorMessage(err)}`);
            }

            const choice = await vscode.window.showQuickPick(
                [
                    { label: '$(vm) Beam', description: 'Beam without state, with no ability to save snapshots', createKind: 'beam' as const, phase: undefined as string | undefined },
                    { label: '$(package) Phase Beams with state', description: 'Create a new Phase and its build beam', createKind: 'phase' as const, phase: undefined as string | undefined },
                    ...phases.flatMap(p => [
                        {
                            label: `$(edit) Beam from Phase: ${p.name} (with state)`,
                            description: `${p.state} · ${p.region} · changes can be snapshotted back to the Phase`,
                            createKind: 'editPhase' as const,
                            phase: p.name as string | undefined,
                        },
                        {
                            label: `$(layers) Beam from Phase: ${p.name} (without state)`,
                            description: `${p.state} · ${p.region} · changes won't be saved`,
                            createKind: 'beam' as const,
                            phase: p.name as string | undefined,
                        },
                    ]),
                ],
                { placeHolder: 'What would you like to create?' }
            );
            if (!choice) {
                return;
            }

            if (choice.createKind === 'phase') {
                await createPhaseFlow();
                return;
            }

            if (choice.createKind === 'editPhase') {
                const phase = phases.find(p => p.name === choice.phase);
                if (phase) {
                    await editPhase(phase);
                }
                return;
            }

            const phaseName = choice.phase;

            // Ask whether to apply saved GitHub credentials
            let applyGithubCredentials = false;
            {
                const cfg = vscode.workspace.getConfiguration('beams');
                const savedUsername = cfg.get<string>('github.username');
                const savedAuthMethod = cfg.get<string>('github.authMethod');
                if (savedUsername && savedAuthMethod) {
                    const savedEmail = cfg.get<string>('github.email') || `${savedUsername}@users.noreply.github.com`;
                    const choice = await vscode.window.showQuickPick(
                        [
                            { label: '$(mark-github) Apply saved GitHub credentials', description: `${savedUsername} · ${savedEmail}`, apply: true },
                            { label: '$(dash) Skip', description: 'Set up GitHub later via Setup GitHub on Beam', apply: false },
                        ],
                        { placeHolder: 'Set up GitHub credentials on the new beam?' }
                    );
                    if (choice === undefined) {
                        return;
                    }
                    applyGithubCredentials = choice.apply;
                }
            }

            try {
                const beam = await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: 'Creating beam...' },
                    async (progress) => {
                        const b = await addBeam({ phase: phaseName });
                        progress.report({ message: 'Waiting for beam to be ready...' });
                        await waitForBeamReady(b.id);
                        try {
                            const status = await checkStatus();
                            if (status.loggedIn && status.cluster) {
                                await ensureBeamSshConfig(b.id, status.cluster);
                                if (applyGithubCredentials) {
                                    const result = await autoSetupGithub(b.id, context, progress, true);
                                    if (result.error) {
                                        vscode.window.showWarningMessage(`GitHub auto-setup: ${result.error}`);
                                    }
                                }
                            }
                        } catch { /* non-fatal */ }

                        return { beam: b };
                    }
                );
                vscode.window.showInformationMessage(`Beam "${beam.beam.id}" created.`);
                provider.refresh();
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Failed to create beam: ${tshErrorMessage(err)}`);
            }
        }),

        vscode.commands.registerCommand('beams.phase.create', async () => {
            await createPhaseFlow();
        }),

        vscode.commands.registerCommand('beams.phase.manage', async (item: PhaseItem) => {
            if (!item?.phase) {
                return;
            }
            await managePhase(item.phase);
        }),

        vscode.commands.registerCommand('beams.phase.edit', async (item: PhaseItem) => {
            if (!item?.phase) {
                return;
            }
            await editPhase(item.phase);
        }),

        vscode.commands.registerCommand('beams.phase.snapshot', (item: PhaseItem) => {
            if (!item?.phase) {
                return;
            }
            openPhaseSnapshotTerminal(item.phase.name);
        }),

        vscode.commands.registerCommand('beams.phase.delete', async (item: PhaseItem) => {
            if (!item?.phase) {
                return;
            }
            await deletePhaseWithConfirm(item.phase.name);
        }),

        vscode.commands.registerCommand('beams.phase.apply', async () => {
            const files = await vscode.window.showOpenDialog({
                canSelectMany: false,
                filters: { 'YAML': ['yaml', 'yml'] },
                title: 'Select a Phase configuration YAML file',
            });
            if (!files || files.length === 0) {
                return;
            }
            openPhaseApplyTerminal(files[0].fsPath);
        }),

        vscode.commands.registerCommand('beams.phase.viewFailedSnapshots', async () => {
            try {
                const failed = await listFailedPhaseSnapshots();
                if (failed.length === 0) {
                    vscode.window.showInformationMessage('No failed Phase build snapshots.');
                    return;
                }
                await vscode.window.showQuickPick(
                    failed.map(f => ({
                        label: typeof f === 'object' && f && 'name' in f ? String((f as Record<string, unknown>).name) : JSON.stringify(f),
                        description: typeof f === 'object' && f && 'last_error' in f ? String((f as Record<string, unknown>).last_error) : undefined,
                    })),
                    { placeHolder: 'Failed Phase build snapshots' }
                );
            } catch (err: unknown) {
                reportTshError(err, { action: 'list failed Phase snapshots', refresh: () => phasesProvider.refresh() });
            }
        }),

        vscode.commands.registerCommand('beams.delete', async (item: BeamItem) => {
            if (!item) {
                return;
            }
            const confirm = await vscode.window.showWarningMessage(
                `Delete beam "${item.beam.id}"?`,
                { modal: true },
                'Delete'
            );
            if (confirm !== 'Delete') {
                return;
            }
            try {
                await removeBeam(item.beam.id);
                vscode.window.showInformationMessage(`Beam "${item.beam.id}" deleted.`);
                provider.refresh();
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Failed to delete beam: ${err instanceof Error ? err.message : err}`);
            }

            // A local debug container must never outlive its beam. Cleanup
            // failures here are surfaced but must never block beam deletion,
            // which has already succeeded above.
            const record = getLocalContainerRecord(item.beam.id);
            if (record) {
                try {
                    await stopContainer(record);
                    await removeContainer(record);
                    deleteLocalContainerRecord(item.beam.id);
                } catch (err: unknown) {
                    vscode.window.showWarningMessage(
                        `Beam deleted, but cleaning up its local debug container failed: ${err instanceof Error ? err.message : err}. ` +
                        `You may need to run "docker rm -f ${record.containerName}" manually.`
                    );
                }
            }
        }),

        vscode.commands.registerCommand('beams.connect', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            try {
                const status = await checkStatus();
                if (!status.loggedIn || !status.cluster) {
                    vscode.window.showErrorMessage('Not logged in to Teleport. Use "Beams: Login" first.');
                    return;
                }

                const detectedRoot = await detectRepoRoot(item.beam.id);
                let folder = await vscode.window.showInputBox({
                    prompt: 'Directory to open as the workspace root (e.g. the repo root, so Source Control diffs/commits are scoped correctly)',
                    value: detectedRoot ?? '/home/beams',
                    ignoreFocusOut: true,
                    validateInput: v => v.startsWith('/') ? undefined : 'Must be an absolute path (starting with /)',
                });
                if (!folder) {
                    return;
                }
                folder = folder.replace(/\/+$/, '') || '/';

                const host = await ensureBeamSshConfig(item.beam.id, status.cluster);
                const config = vscode.workspace.getConfiguration('remote.SSH');
                if (config.inspect<boolean>('enableRemoteCommand') && !config.get<boolean>('enableRemoteCommand')) {
                    await config.update('enableRemoteCommand', true, vscode.ConfigurationTarget.Global);
                }
                const remoteUri = vscode.Uri.parse(`vscode-remote://ssh-remote+${host}${folder}`);
                await vscode.commands.executeCommand('vscode.openFolder', remoteUri, { forceNewWindow: true });
            } catch (err: unknown) {
                reportTshError(err, { beamId: item.beam.id, action: 'connect', refresh: () => provider.refresh() });
            }
        }),

        vscode.commands.registerCommand('beams.ssh', (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            openBeamTerminal(item.beam);
        }),

        vscode.commands.registerCommand('beams.openFiles', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            fileExplorer.setBeam(item.beam);
            if (poller) {
                await poller.setBeam(item.beam.id);
                vscode.commands.executeCommand('beams.selectScm');
            }
            vscode.commands.executeCommand('beamFiles.focus');
        }),

        vscode.commands.registerCommand('beams.openFile', async (entry: { beamId: string; path: string; name: string }) => {
            if (!entry) {
                return;
            }
            const uri = vscode.Uri.parse(`beam://${entry.beamId}${entry.path}`);
            try {
                await vscode.commands.executeCommand('vscode.open', uri);
            } catch (err: unknown) {
                reportTshError(err, { beamId: entry.beamId, action: 'open file' });
            }
        }),

        vscode.commands.registerCommand('beams.refreshFiles', () => {
            fileExplorer.refresh();
        }),

        vscode.commands.registerCommand('beams.uploadFiles', async (item?: FileItem) => {
            if (!fileExplorer.getBeam()) {
                vscode.window.showWarningMessage('Select a beam first.');
                return;
            }
            const targetDir = item?.entry.path ?? '/home/beams';
            const files = await vscode.window.showOpenDialog({
                canSelectMany: true,
                canSelectFiles: true,
                canSelectFolders: true,
                title: targetDir === '/home/beams' ? 'Select files to upload' : `Select files to upload to ${targetDir}`,
            });
            if (!files || files.length === 0) {
                return;
            }
            await fileExplorer.uploadLocalFiles(targetDir, files);
        }),

        vscode.commands.registerCommand('beams.downloadFile', async (item: FileItem) => {
            if (!item?.entry) {
                return;
            }
            const beamUri = vscode.Uri.parse(`beam://${item.entry.beamId}${item.entry.path}`);
            const homeDir = process.env.HOME || process.env.USERPROFILE || '';

            if (item.entry.isDirectory) {
                const destFolder = await vscode.window.showOpenDialog({
                    canSelectFiles: false,
                    canSelectFolders: true,
                    canSelectMany: false,
                    title: `Select a destination for "${item.entry.name}"`,
                });
                if (!destFolder || destFolder.length === 0) {
                    return;
                }
                const destUri = vscode.Uri.joinPath(destFolder[0], item.entry.name);
                try {
                    await vscode.window.withProgress(
                        { location: vscode.ProgressLocation.Notification, title: `Downloading "${item.entry.name}"...` },
                        () => vscode.workspace.fs.copy(beamUri, destUri, { overwrite: true })
                    );
                    vscode.window.showInformationMessage(`Downloaded to ${destUri.fsPath}.`);
                } catch (err: unknown) {
                    reportTshError(err, { beamId: item.entry.beamId, action: 'download folder' });
                }
                return;
            }

            const saveUri = await vscode.window.showSaveDialog({
                defaultUri: vscode.Uri.file(path.join(homeDir, 'Downloads', item.entry.name)),
                title: `Save "${item.entry.name}" as...`,
            });
            if (!saveUri) {
                return;
            }
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Downloading "${item.entry.name}"...` },
                    () => vscode.workspace.fs.copy(beamUri, saveUri, { overwrite: true })
                );
                vscode.window.showInformationMessage(`Downloaded to ${saveUri.fsPath}.`);
            } catch (err: unknown) {
                reportTshError(err, { beamId: item.entry.beamId, action: 'download file' });
            }
        }),

        vscode.commands.registerCommand('beams.publish', async (item: BeamItem) => {
            if (!item) {
                return;
            }
            try {
                const url = await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: 'Publishing beam...' },
                    () => publishBeam(item.beam.id)
                );
                const action = await vscode.window.showInformationMessage(
                    `Beam published: ${url}`,
                    'Copy URL'
                );
                if (action === 'Copy URL') {
                    await vscode.env.clipboard.writeText(url);
                }
                provider.refresh();
            } catch (err: unknown) {
                reportTshError(err, { beamId: item.beam.id, action: 'publish', refresh: () => provider.refresh() });
            }
        }),

        vscode.commands.registerCommand('beams.unpublish', async (item: BeamItem) => {
            if (!item) {
                return;
            }
            try {
                await unpublishBeam(item.beam.id);
                vscode.window.showInformationMessage(`Beam "${item.beam.id}" unpublished.`);
                provider.refresh();
            } catch (err: unknown) {
                reportTshError(err, { beamId: item.beam.id, action: 'unpublish', refresh: () => provider.refresh() });
            }
        }),

        vscode.commands.registerCommand('beams.copyUrl', async (item: BeamItem) => {
            if (!item?.beam.url) {
                return;
            }
            await vscode.env.clipboard.writeText(item.beam.url);
            vscode.window.showInformationMessage('URL copied to clipboard.');
        }),


        vscode.commands.registerCommand('beams.setupGithub', async (item?: BeamItem) => {
            let beamId = item?.beam?.id;
            if (!beamId) {
                const beams = await listBeams();
                if (beams.length === 0) {
                    vscode.window.showErrorMessage('No beams available. Create a beam first.');
                    return;
                }
                const picked = await vscode.window.showQuickPick(
                    beams.map(b => ({
                        label: b.id,
                        description: b.owner ? `Owner: ${b.owner}` : undefined,
                    })),
                    { placeHolder: 'Select a beam to set up GitHub on', ignoreFocusOut: true }
                );
                if (!picked) {
                    return;
                }
                beamId = picked.label;
            }

            // Offer the settings saved by "Remember these settings" instead of re-prompting.
            const savedCfg = vscode.workspace.getConfiguration('beams');
            const savedUsername = savedCfg.get<string>('github.username');
            const savedAuthMethod = savedCfg.get<string>('github.authMethod');
            if (savedUsername && savedAuthMethod) {
                const savedEmail = savedCfg.get<string>('github.email') || `${savedUsername}@users.noreply.github.com`;
                const choice = await vscode.window.showQuickPick(
                    [
                        { label: '$(mark-github) Use saved GitHub settings', description: `${savedUsername} · ${savedEmail} · ${savedAuthMethod}`, useSaved: true },
                        { label: '$(edit) Enter different settings', useSaved: false },
                    ],
                    { placeHolder: 'Set up GitHub on this beam', ignoreFocusOut: true }
                );
                if (choice === undefined) {
                    return;
                }
                if (choice.useSaved) {
                    const targetBeam = beamId;
                    const result = await vscode.window.withProgress(
                        { location: vscode.ProgressLocation.Notification, title: 'Setting up GitHub on beam...', cancellable: false },
                        progress => autoSetupGithub(targetBeam, context, progress, true)
                    );
                    if (result.error) {
                        vscode.window.showErrorMessage(`GitHub setup failed: ${result.error}`);
                    } else if (savedAuthMethod === 'oauth') {
                        vscode.window.showInformationMessage(
                            'GitHub CLI installed. Open a terminal on the beam and run: gh auth login'
                        );
                    } else {
                        vscode.window.showInformationMessage('GitHub setup complete on beam.');
                    }
                    return;
                }
            }

            const username = await vscode.window.showInputBox({
                prompt: 'GitHub username',
                placeHolder: 'octocat',
                value: savedUsername || undefined,
                ignoreFocusOut: true,
            });
            if (!username) {
                return;
            }

            const email = await vscode.window.showInputBox({
                prompt: 'Git email',
                placeHolder: `${username}@users.noreply.github.com`,
                value: (username === savedUsername && savedCfg.get<string>('github.email')) || `${username}@users.noreply.github.com`,
                ignoreFocusOut: true,
            });
            if (email === undefined) {
                return;
            }

            const authChoice = await vscode.window.showQuickPick(
                [
                    // { label: '$(shield) Teleport Git Proxy (tsh git)', description: 'Use Teleport-managed GitHub access — no token needed', method: 'tsh-git' as const },
                    { label: '$(globe) Full account access (OAuth)', description: 'Authenticate via browser — grants access to all your repos', method: 'oauth' as const },
                    { label: '$(key) Fine-grained token (PAT)', description: 'Paste a token scoped to specific repos', method: 'pat' as const },
                ],
                { placeHolder: 'How would you like to authenticate with GitHub?', ignoreFocusOut: true }
            );
            if (!authChoice) {
                return;
            }

            let pat: string | undefined;
            if (authChoice.method === 'pat') {
                pat = await vscode.window.showInputBox({
                    prompt: 'Paste your GitHub Personal Access Token',
                    password: true,
                    placeHolder: 'ghp_... or github_pat_...',
                    ignoreFocusOut: true,
                });
                if (!pat) {
                    return;
                }
            }

            let cloneRepo: string | undefined;
            let cloneDir: string | undefined;
            if (authChoice.method !== 'oauth') {
                cloneRepo = await vscode.window.showInputBox({
                    prompt: 'Repository to clone (or leave empty to skip)',
                    placeHolder: 'owner/repo',
                    ignoreFocusOut: true,
                }) || undefined;

                if (cloneRepo) {
                    cloneDir = await vscode.window.showInputBox({
                        prompt: 'Clone directory (or leave empty for default)',
                        placeHolder: '/home/beams/my-project',
                        ignoreFocusOut: true,
                    }) || undefined;
                }
            }

            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: 'Setting up GitHub on beam...', cancellable: false },
                    async (progress) => {
                        await setupGithubOnBeam({
                            beamId,
                            username,
                            email: email || `${username}@users.noreply.github.com`,
                            authMethod: authChoice.method,
                            pat,
                            cloneRepo,
                            cloneDir,
                        }, progress);
                    }
                );

                if (authChoice.method === 'oauth') {
                    vscode.window.showInformationMessage(
                        'GitHub CLI installed. Open a terminal on the beam and run: gh auth login'
                    );
                } else {
                    vscode.window.showInformationMessage('GitHub setup complete on beam.');
                }

                const remember = await vscode.window.showInformationMessage(
                    'Remember these settings for future beams?',
                    'Yes', 'No'
                );
                if (remember === 'Yes') {
                    const cfg = vscode.workspace.getConfiguration('beams');
                    await cfg.update('github.username', username, vscode.ConfigurationTarget.Global);
                    await cfg.update('github.email', email || `${username}@users.noreply.github.com`, vscode.ConfigurationTarget.Global);
                    await cfg.update('github.authMethod', authChoice.method, vscode.ConfigurationTarget.Global);
                    if (pat) {
                        await context.secrets.store(SECRET_KEY, pat);
                    }
                    if (cloneRepo) {
                        await cfg.update('github.defaultCloneRepo', cloneRepo, vscode.ConfigurationTarget.Global);
                    }
                }
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`GitHub setup failed: ${err instanceof Error ? err.message : err}`);
            }
        }),

        vscode.commands.registerCommand('beams.run', async (item?: BeamItem) => {
            if (!item?.beam) {
                vscode.window.showErrorMessage('Select a beam first.');
                return;
            }
            if (item.beam.url) {
                vscode.env.openExternal(vscode.Uri.parse(item.beam.url));
            } else {
                const action = await vscode.window.showInformationMessage(
                    'This beam is not published yet. Publish it first?',
                    'Publish'
                );
                if (action === 'Publish') {
                    try {
                        const url = await vscode.window.withProgress(
                            { location: vscode.ProgressLocation.Notification, title: 'Publishing beam...' },
                            () => publishBeam(item.beam.id)
                        );
                        provider.refresh();
                        vscode.env.openExternal(vscode.Uri.parse(url));
                    } catch (err: unknown) {
                        reportTshError(err, { beamId: item.beam.id, action: 'publish', refresh: () => provider.refresh() });
                    }
                }
            }
        }),

        vscode.commands.registerCommand('beams.showActivityDetail', (item: { detail?: string; label?: string | vscode.TreeItemLabel }) => {
            if (!item?.detail) {
                return;
            }
            const channel = vscode.window.createOutputChannel('Beam Activity Detail');
            channel.clear();
            const title = typeof item.label === 'string' ? item.label : item.label?.label ?? 'Detail';
            channel.appendLine(`═══ ${title} ═══`);
            channel.appendLine('');
            channel.appendLine(item.detail);
            channel.show(true);
        }),

        vscode.commands.registerCommand('beams.events.expand', () => {
            vscode.commands.executeCommand('beamEventsPanel.focus');
        }),

        vscode.commands.registerCommand('beams.events.collapse', () => {
            vscode.commands.executeCommand('beamEvents.focus');
        }),

        vscode.commands.registerCommand('beams.export', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }

            const remotePath = await vscode.window.showInputBox({
                prompt: 'Remote path on beam to export',
                value: '/home/beams',
                placeHolder: '/home/beams/my-project',
            });
            if (!remotePath) {
                return;
            }

            const homeDir = process.env.HOME || process.env.USERPROFILE || '';
            const saveUri = await vscode.window.showSaveDialog({
                defaultUri: vscode.Uri.file(path.join(homeDir, 'Downloads', `${item.beam.id}-export.tar.gz`)),
                filters: { 'Tar files': ['tar.gz', 'tgz'] },
                title: 'Save exported archive to...',
            });
            if (!saveUri) {
                return;
            }

            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: 'Exporting beam files...' },
                    async () => {
                        const remoteArchive = '/tmp/beam-export.tar.gz';
                        await execOnBeam(item.beam.id, [
                            'tar', '-czf', remoteArchive,
                            '--exclude=./node_modules',
                            '--exclude=./.git',
                            '--exclude=./.claude',
                            '-C', remotePath, '.'
                        ]);
                        await scpFromBeam(item.beam.id, remoteArchive, saveUri.fsPath);
                        await execOnBeam(item.beam.id, ['rm', '-f', remoteArchive]);
                    }
                );
                const action = await vscode.window.showInformationMessage(
                    `Exported to ${saveUri.fsPath}`,
                    'Open Folder'
                );
                if (action === 'Open Folder') {
                    const dir = vscode.Uri.file(path.dirname(saveUri.fsPath));
                    vscode.commands.executeCommand('revealFileInOS', dir);
                }
            } catch (err: unknown) {
                reportTshError(err, { beamId: item.beam.id, action: 'export beam files', refresh: () => provider.refresh() });
            }
        }),

        // Local debug container commands. beams.container.create is the only way to opt a
        // beam in; there is deliberately no command that edits an existing container's
        // settings — delete it (beams.container.teardown) and replicate again instead.
        vscode.commands.registerCommand('beams.container.create', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            const beamId = item.beam.id;
            if (getLocalContainerRecord(beamId)) {
                vscode.window.showInformationMessage(`Beam "${beamId}" already has a local debug container.`);
                return;
            }
            if (!(await isDockerAvailable())) {
                vscode.window.showErrorMessage('Docker is not available. Install or start Docker to replicate this beam locally.');
                return;
            }
            const syncChoice = await vscode.window.showQuickPick(
                [
                    { label: '$(sync) Automatic', description: 'Syncs shortly after git status changes on the beam', mode: 'automatic' as LocalContainerSyncMode },
                    { label: '$(circle-outline) Manual', description: 'Only syncs when you run "Sync Local Debug Container Now"', mode: 'manual' as LocalContainerSyncMode },
                ],
                { placeHolder: `How should the local debug container for "${beamId}" sync from the beam?` }
            );
            if (syncChoice === undefined) {
                return;
            }
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Replicating "${beamId}" to a local debug container...` },
                    async (progress) => {
                        progress.report({ message: 'Locating repository on beam...' });
                        const repoRoot = (await detectRepoRoot(beamId)) ?? '/home/beams';
                        const record = createLocalContainerRecord(beamId, repoRoot, syncChoice.mode);
                        writeDockerfile(beamId, generateDockerfile());
                        writeDevcontainerJson(record);
                        provider.refresh();
                        progress.report({ message: 'Building container image...' });
                        await buildContainerImage(record);
                    }
                );
                vscode.window.showInformationMessage(`Local debug container for "${beamId}" is ready. Use "Open Local Debug Container" to start it.`);
            } catch (err: unknown) {
                reportTshError(err, { beamId, action: 'replicate beam to a local debug container', refresh: () => provider.refresh() });
            }
        }),

        vscode.commands.registerCommand('beams.container.open', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            const record = getLocalContainerRecord(item.beam.id);
            if (!record?.enabled) {
                vscode.window.showErrorMessage('This beam does not have a local debug container enabled.');
                return;
            }
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Preparing local debug container for "${item.beam.id}"...` },
                    async () => {
                        await ensureContainerRunning(record);
                        if (containerSync) {
                            await containerSync.syncNow(item.beam.id, record.repoRoot);
                        }
                    }
                );
                openContainerTerminal(record);
                vscode.window.showInformationMessage(
                    `Tip: for full IntelliSense, you can also open ${path.join(os.homedir(), '.teleport', 'beams', 'local-containers', item.beam.id, 'workspace')} directly as a VS Code folder.`
                );
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Failed to open local debug container: ${err instanceof Error ? err.message : err}`);
            }
        }),

        vscode.commands.registerCommand('beams.container.syncNow', async (item: BeamItem) => {
            if (!item?.beam || !containerSync) {
                return;
            }
            const record = getLocalContainerRecord(item.beam.id);
            if (!record?.enabled) {
                vscode.window.showErrorMessage('This beam does not have a local debug container enabled.');
                return;
            }
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Syncing local debug container for "${item.beam.id}"...` },
                    () => containerSync.syncNow(item.beam.id, record.repoRoot)
                );
                vscode.window.showInformationMessage(`Local debug container for "${item.beam.id}" synced.`);
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Sync failed: ${err instanceof Error ? err.message : err}`);
            }
        }),

        vscode.commands.registerCommand('beams.container.rebuild', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            const record = getLocalContainerRecord(item.beam.id);
            if (!record?.enabled) {
                vscode.window.showErrorMessage('This beam does not have a local debug container enabled.');
                return;
            }
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Rebuilding local debug container image for "${item.beam.id}"...` },
                    async () => {
                        await buildContainerImage(record, { noCache: true });
                        await removeContainer(record);
                        await ensureContainerRunning(record);
                    }
                );
                vscode.window.showInformationMessage(`Local debug container for "${item.beam.id}" rebuilt.`);
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Rebuild failed: ${err instanceof Error ? err.message : err}`);
            }
        }),

        vscode.commands.registerCommand('beams.container.teardown', async (item: BeamItem) => {
            if (!item?.beam) {
                return;
            }
            const record = getLocalContainerRecord(item.beam.id);
            if (!record) {
                vscode.window.showInformationMessage('This beam does not have a local debug container.');
                return;
            }
            const confirm = await vscode.window.showWarningMessage(
                `Delete the local debug container and synced files for "${item.beam.id}"? The beam itself is not affected.`,
                { modal: true },
                'Delete'
            );
            if (confirm !== 'Delete') {
                return;
            }
            try {
                await stopContainer(record);
                await removeContainer(record);
                deleteLocalContainerRecord(item.beam.id);
                provider.refresh();
                vscode.window.showInformationMessage(`Local debug container for "${item.beam.id}" removed.`);
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Teardown failed: ${err instanceof Error ? err.message : err}`);
            }
        })
    );
}
