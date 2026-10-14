import * as vscode from 'vscode';
import { BeamItem, setBeamLabel } from './beamItem';
import { BeamsProvider } from './beamsProvider';
import { BeamFileExplorer } from './fileExplorer';
import { addBeam, removeBeam, publishBeam, unpublishBeam, execOnBeam, scpFromBeam, checkStatus, listBeams, waitForBeamReady, detectRepoRoot, tshErrorMessage, tshBinary } from './tsh';
import { openBeamTerminal } from './terminal';
import { reportTshError } from './notify';
import { setupGithubOnBeam, autoSetupGithub, SECRET_KEY } from './github';
import { ensureBeamSshConfig } from './ssh';
import { AgentEventsProvider } from './events';
import { SessionProfilesProvider } from './sessionProfilesProvider';
import { SessionProfileItem } from './sessionProfileItem';
import {
    SessionProfile,
    listSessionProfiles,
    getSessionSummary,
    getSessionSummaryPath,
    saveSessionProfile,
    deleteSessionProfile,
    captureGitRef,
    captureRemoteUrl,
    captureRecentActivity,
    generateSessionSummary,
    cloneRepoOnBeam,
    applyGitRef,
    writeSessionSummaryToBeam,
    appendSessionSummaryToAgentMemory,
} from './sessionProfiles';
import { detectAgents } from './agents';
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
    fileExplorer: BeamFileExplorer,
    eventsProvider: AgentEventsProvider,
    sessionProfilesProvider: SessionProfilesProvider,
    poller?: import('./polling').BeamPoller,
    _getScm?: () => import('./scm').BeamGitScmProvider | undefined,
    containerSync?: ContainerSyncEngine,
): void {
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
            const cluster = await vscode.window.showInputBox({
                prompt: 'Teleport cluster proxy address',
                placeHolder: 'example.teleport.sh',
            });
            if (!cluster) {
                return;
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
                        const b = await addBeam();
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
                const doc = await vscode.workspace.openTextDocument(uri);
                await vscode.window.showTextDocument(doc);
            } catch (err: unknown) {
                reportTshError(err, { beamId: entry.beamId, action: 'open file' });
            }
        }),

        vscode.commands.registerCommand('beams.refreshFiles', () => {
            fileExplorer.refresh();
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
        }),

        vscode.commands.registerCommand('beams.refreshSessionProfiles', () => {
            sessionProfilesProvider.refresh();
        }),

        vscode.commands.registerCommand('beams.saveSessionProfile', async (item?: BeamItem) => {
            let beamId = item?.beam?.id;
            if (!beamId) {
                const beams = await listBeams();
                if (beams.length === 0) {
                    vscode.window.showErrorMessage('No beams available.');
                    return;
                }
                const picked = await vscode.window.showQuickPick(
                    beams.map(b => ({ label: b.id, description: b.owner ? `Owner: ${b.owner}` : undefined })),
                    { placeHolder: 'Select a beam to save a session profile from', ignoreFocusOut: true }
                );
                if (!picked) {
                    return;
                }
                beamId = picked.label;
            }

            const repoRoot = await detectRepoRoot(beamId);

            const existing = listSessionProfiles();
            const existingForBeam = existing.find(p => p.beamId === beamId);
            const taskId = await vscode.window.showInputBox({
                prompt: 'Task/profile id (used to resume this session later)',
                placeHolder: 'fix-auth-bug',
                value: existingForBeam?.taskId,
                ignoreFocusOut: true,
                validateInput: v => /^[a-z0-9][a-z0-9-_]*$/i.test(v) ? undefined : 'Use letters, numbers, - and _ only',
            });
            if (!taskId) {
                return;
            }

            const existingProfile = existing.find(p => p.taskId === taskId);
            const label = await vscode.window.showInputBox({
                prompt: 'Short label for this profile',
                value: existingProfile?.label ?? taskId,
                ignoreFocusOut: true,
            });
            if (label === undefined) {
                return;
            }

            await runSaveSessionProfileFlow(sessionProfilesProvider, beamId, repoRoot, taskId, label, existingProfile);
        }),

        vscode.commands.registerCommand('beams.viewSessionProfile', async (item?: SessionProfileItem) => {
            if (!item?.profile) {
                return;
            }
            const uri = vscode.Uri.file(getSessionSummaryPath(item.profile.taskId));
            const doc = await vscode.workspace.openTextDocument(uri);
            await vscode.window.showTextDocument(doc, { preview: true });
        }),

        vscode.commands.registerCommand('beams.resumeSessionProfile', async (item?: SessionProfileItem) => {
            let profile = item?.profile;
            if (!profile) {
                const all = listSessionProfiles();
                if (all.length === 0) {
                    vscode.window.showInformationMessage('No saved session profiles.');
                    return;
                }
                const picked = await vscode.window.showQuickPick(
                    all.map(p => ({
                        label: p.label,
                        description: p.gitBranch ? `${p.gitBranch}@${(p.gitCommitSha ?? '').slice(0, 7)}` : undefined,
                        detail: `Updated ${new Date(p.updatedAt).toLocaleString()}`,
                        profile: p,
                    })),
                    { placeHolder: 'Select a session profile to resume', ignoreFocusOut: true }
                );
                if (!picked) {
                    return;
                }
                profile = picked.profile;
            }

            const beams = await listBeams();
            const target = await vscode.window.showQuickPick(
                [
                    { label: '$(add) Create a new beam', beamId: undefined as string | undefined },
                    ...beams.map(b => ({ label: b.id, description: b.owner ? `Owner: ${b.owner}` : undefined, beamId: b.id })),
                ],
                { placeHolder: `Resume "${profile.label}" into which beam?`, ignoreFocusOut: true }
            );
            if (!target) {
                return;
            }

            let beamId = target.beamId;
            let repoRoot: string | undefined;
            let wroteMemoryFile = false;
            let memoryAgents: import('./agents').AgentAdapter[] = [];
            try {
                await vscode.window.withProgress(
                    { location: vscode.ProgressLocation.Notification, title: `Resuming "${profile.label}"...` },
                    async (progress) => {
                        if (!beamId) {
                            progress.report({ message: 'Creating beam...' });
                            const b = await addBeam();
                            beamId = b.id;
                            progress.report({ message: 'Waiting for beam to be ready...' });
                            await waitForBeamReady(beamId);
                        }

                        if (!profile!.gitBranch) {
                            return;
                        }

                        progress.report({ message: 'Locating git repository on beam...' });
                        repoRoot = await detectRepoRoot(beamId);
                        if (!repoRoot) {
                            if (!profile!.remoteUrl) {
                                throw new Error('No git repository found on this beam, and this profile has no remote URL recorded to clone from. Clone the repo (Setup GitHub on Beam) first.');
                            }
                            const dirName = profile!.remoteUrl.replace(/\.git$/, '').split('/').filter(Boolean).pop() || 'project';
                            const targetDir = `/home/beams/${dirName}`;

                            progress.report({ message: 'Cloning repository...' });
                            try {
                                await cloneRepoOnBeam(beamId, profile!.remoteUrl, targetDir);
                            } catch {
                                // Likely a private repo with no credentials on this fresh beam yet —
                                // fall back to whatever GitHub auth prefs the user already has stored.
                                const cfg = vscode.workspace.getConfiguration('beams');
                                const username = cfg.get<string>('github.username');
                                const authMethod = cfg.get<string>('github.authMethod') as 'pat' | 'oauth' | 'tsh-git' | '' | undefined;
                                if (!username || !authMethod) {
                                    throw new Error(
                                        `Could not clone ${profile!.remoteUrl} (likely a private repo with no GitHub credentials on this beam yet). ` +
                                        'Run "Setup GitHub on Beam" first, then resume again.'
                                    );
                                }
                                const email = cfg.get<string>('github.email') || `${username}@users.noreply.github.com`;
                                const pat = authMethod === 'pat' ? await context.secrets.get(SECRET_KEY) : undefined;
                                progress.report({ message: 'Applying stored GitHub credentials...' });
                                await setupGithubOnBeam({
                                    beamId,
                                    username,
                                    email,
                                    authMethod,
                                    pat,
                                    cloneRepo: profile!.remoteUrl,
                                    cloneDir: targetDir,
                                }, progress);
                            }
                            repoRoot = targetDir;
                        }

                        progress.report({ message: `Checking out ${profile!.gitBranch}...` });
                        await applyGitRef(beamId, repoRoot, profile!.gitBranch, profile!.gitCommitSha ?? '');

                        progress.report({ message: 'Loading session memory...' });
                        const summaryMd = getSessionSummary(profile!.taskId);
                        await writeSessionSummaryToBeam(beamId, repoRoot, profile!.taskId, summaryMd);
                        memoryAgents = await detectAgents(beamId);
                        for (const agent of memoryAgents) {
                            await appendSessionSummaryToAgentMemory(beamId, agent, profile!.taskId, profile!.label, summaryMd);
                        }
                        wroteMemoryFile = true;
                    }
                );
            } catch (err: unknown) {
                vscode.window.showErrorMessage(`Failed to resume "${profile.label}": ${err instanceof Error ? err.message : err}`);
                return;
            }

            provider.refresh();
            const resultMessage = `"${profile.label}" resumed on beam "${beamId}".`;
            if (wroteMemoryFile && beamId && repoRoot) {
                if (memoryAgents.length > 0) {
                    // The summary was also appended to each detected agent's global memory
                    // file (e.g. .claude/CLAUDE.md, AGENTS.md — see agents.ts), which that
                    // agent auto-loads into every session on this beam — no need to
                    // explicitly tell it to go read a file.
                    const terminal = openBeamTerminal({ id: beamId });
                    terminal.sendText(`cd "${repoRoot}" && ${memoryAgents[0].launchCommand}`);
                    const openAction = await vscode.window.showInformationMessage(
                        `${resultMessage} Started a ${memoryAgents[0].id} session with the saved memory loaded.`,
                        'Open Session Memory File'
                    );
                    if (openAction === 'Open Session Memory File') {
                        const uri = vscode.Uri.parse(`beam://${beamId}${repoRoot}/.beams/session-memory/${profile.taskId}.md`);
                        const doc = await vscode.workspace.openTextDocument(uri);
                        await vscode.window.showTextDocument(doc);
                    }
                } else {
                    vscode.window.showInformationMessage(
                        `${resultMessage} No supported coding agent CLI (claude, codex) was detected on this beam — session memory was still written to .beams/session-memory/${profile.taskId}.md.`
                    );
                }
            } else {
                vscode.window.showInformationMessage(resultMessage);
            }
        }),

        vscode.commands.registerCommand('beams.updateSessionProfile', async (item?: SessionProfileItem) => {
            let profile = item?.profile;
            if (!profile) {
                const all = listSessionProfiles();
                if (all.length === 0) {
                    vscode.window.showInformationMessage('No saved session profiles.');
                    return;
                }
                const picked = await vscode.window.showQuickPick(
                    all.map(p => ({ label: p.label, description: p.taskId, profile: p })),
                    { placeHolder: 'Select a session profile to update', ignoreFocusOut: true }
                );
                if (!picked) {
                    return;
                }
                profile = picked.profile;
            }

            const beams = await listBeams();
            let beamId = profile.beamId && beams.some(b => b.id === profile!.beamId) ? profile.beamId : undefined;
            if (!beamId) {
                const picked = await vscode.window.showQuickPick(
                    beams.map(b => ({ label: b.id, description: b.owner ? `Owner: ${b.owner}` : undefined })),
                    {
                        placeHolder: `"${profile.label}" was saved from beam "${profile.beamId || '?'}", which is no longer available — select a beam to update from`,
                        ignoreFocusOut: true,
                    }
                );
                if (!picked) {
                    return;
                }
                beamId = picked.label;
            }

            const repoRoot = await detectRepoRoot(beamId) ?? profile.repoRoot;

            await runSaveSessionProfileFlow(sessionProfilesProvider, beamId, repoRoot, profile.taskId, profile.label, profile);
        }),

        vscode.commands.registerCommand('beams.deleteSessionProfile', async (item?: SessionProfileItem) => {
            let profile = item?.profile;
            if (!profile) {
                const all = listSessionProfiles();
                if (all.length === 0) {
                    vscode.window.showInformationMessage('No saved session profiles.');
                    return;
                }
                const picked = await vscode.window.showQuickPick(
                    all.map(p => ({ label: p.label, taskId: p.taskId })),
                    { placeHolder: 'Select a session profile to delete', ignoreFocusOut: true }
                );
                if (!picked) {
                    return;
                }
                profile = all.find(p => p.taskId === picked.taskId);
            }
            if (!profile) {
                return;
            }

            const confirm = await vscode.window.showWarningMessage(
                `Delete session profile "${profile.label}"?`,
                { modal: true },
                'Delete'
            );
            if (confirm !== 'Delete') {
                return;
            }
            deleteSessionProfile(profile.taskId);
            sessionProfilesProvider.refresh();
            vscode.window.showInformationMessage(`Session profile "${profile.label}" deleted.`);
        })
    );
}

// Shared by beams.saveSessionProfile (new profile, beamId/repoRoot/taskId/label freshly
// prompted) and beams.updateSessionProfile (reuses the stored beamId/taskId/label so the
// existing profile.json/summary.md are simply overwritten in place).
async function runSaveSessionProfileFlow(
    sessionProfilesProvider: SessionProfilesProvider,
    beamId: string,
    repoRoot: string | undefined,
    taskId: string,
    label: string,
    existingProfile: SessionProfile | undefined,
): Promise<void> {
    let gitRef: { branch: string; sha: string } | undefined;
    let remoteUrl: string | undefined;
    let recentActivity = '';
    let llmSummary: string | undefined;
    let llmSummaryError: string | undefined;
    try {
        const captured = await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: `${existingProfile ? 'Updating' : 'Saving'} session profile "${label}"...`,
            },
            async (progress) => {
                let ref: { branch: string; sha: string } | undefined;
                let url: string | undefined;
                let activity = '';
                if (repoRoot) {
                    progress.report({ message: 'Reading git state from beam...' });
                    ref = await captureGitRef(beamId, repoRoot);
                    url = await captureRemoteUrl(beamId, repoRoot);
                    activity = await captureRecentActivity(beamId, repoRoot);
                }
                progress.report({ message: 'Drafting summary with the detected coding agent...' });
                const summary = await generateSessionSummary(beamId, repoRoot ?? '/home/beams', label, activity);
                return { ref, url, activity, summary };
            }
        );
        gitRef = captured.ref;
        remoteUrl = captured.url;
        recentActivity = captured.activity;
        llmSummary = captured.summary.text;
        llmSummaryError = captured.summary.error;
    } catch (err: unknown) {
        vscode.window.showErrorMessage(`Failed to read git state: ${err instanceof Error ? err.message : err}`);
        return;
    }

    let summaryMd: string;
    if (llmSummary) {
        summaryMd = [`# Session Summary: ${label}`, '', llmSummary].join('\n');
    } else {
        summaryMd = [
            `# Session Summary: ${label}`,
            '',
            '## What was tried',
            '',
            '## Decisions made',
            '',
            "## What's left",
            '',
            ...(llmSummaryError ? [`_Auto-draft failed: ${llmSummaryError}_`, ''] : []),
            ...(recentActivity ? ['---', '', '_Reference below, fill in manually:_', '', recentActivity, ''] : []),
        ].join('\n');
    }

    const now = new Date().toISOString();
    const status = await checkStatus();
    const profile: SessionProfile = {
        taskId,
        label,
        beamId,
        repoRoot,
        gitBranch: gitRef?.branch,
        gitCommitSha: gitRef?.sha,
        remoteUrl,
        createdBy: existingProfile?.createdBy ?? (status.user || os.userInfo().username),
        createdAt: existingProfile?.createdAt ?? now,
        updatedAt: now,
    };
    saveSessionProfile(profile, summaryMd);
    sessionProfilesProvider.refresh();
    await vscode.commands.executeCommand('beamSessionProfiles.focus');
    const gitSuffix = gitRef ? ` (${gitRef.branch}@${gitRef.sha.slice(0, 7)})` : ' (no git repository detected on this beam)';
    vscode.window.showInformationMessage(
        `Session profile "${label}" ${existingProfile ? 'updated' : 'saved'}${gitSuffix}.`
    );

    // Open the saved summary file itself (not a scratch buffer) so further edits save
    // straight back to disk — no separate confirm-before-save step.
    const summaryDoc = await vscode.workspace.openTextDocument(vscode.Uri.file(getSessionSummaryPath(taskId)));
    await vscode.window.showTextDocument(summaryDoc, { preview: false });
}
