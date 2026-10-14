import * as vscode from 'vscode';
import { BeamsProvider } from './beamsProvider';
import { BeamFileExplorer } from './fileExplorer';
import { BeamFileSystemProvider } from './beamFs';
import { AgentEventsProvider } from './events';
import { ClustersProvider, ResourceLeafItem } from './clusters';
import { SessionProfilesProvider } from './sessionProfilesProvider';
import { registerCommands } from './commands';
import { BeamPoller } from './polling';
import { BeamGitOriginalProvider } from './gitOriginal';
import { BeamGitDecorationProvider } from './fileDecorations';
import { BeamGitScmProvider } from './scm';
import { registerScmCommands } from './scmCommands';
import { ContainerSyncEngine } from './containerSync';
import { setBeamLabelStore } from './beamItem';

export function activate(context: vscode.ExtensionContext): void {
    setBeamLabelStore(context.globalState);
    const provider = new BeamsProvider();
    const fileExplorer = new BeamFileExplorer();
    const eventsProvider = new AgentEventsProvider();
    const clustersProvider = new ClustersProvider();
    const sessionProfilesProvider = new SessionProfilesProvider();

    const poller = new BeamPoller();
    const fsProvider = new BeamFileSystemProvider();
    const gitOriginalProvider = new BeamGitOriginalProvider(poller);
    const decorationProvider = new BeamGitDecorationProvider();
    let scmProvider: BeamGitScmProvider | undefined;

    // Wire file stat updates from poller into the filesystem provider
    poller.addConsumer({
        onFileStats(beamId, stats) {
            fsProvider.handleStatUpdate(beamId, stats);
        },
        onGitStatus(_beamId, _repoRoot, _porcelain, _headSha) {
            // Invalidate git original cache when HEAD changes
            gitOriginalProvider.invalidateCache();
        },
    });
    poller.addConsumer(decorationProvider);
    poller.addConsumer(fileExplorer);

    const containerSyncEngine = new ContainerSyncEngine();
    poller.addConsumer(containerSyncEngine);

    vscode.window.createTreeView('beamClusters', {
        treeDataProvider: clustersProvider,
        showCollapseAll: true,
    });

    context.subscriptions.push(
        vscode.commands.registerCommand('beams.refreshClusters', () => {
            clustersProvider.refresh();
        }),
        vscode.commands.registerCommand('beams.copyResourceIdentifier', async (item: ResourceLeafItem) => {
            if (!item?.resourceIdentifier) {
                return;
            }
            await vscode.env.clipboard.writeText(item.resourceIdentifier);
            vscode.window.showInformationMessage('Copied to clipboard.');
        }),
    );

    vscode.window.createTreeView('beamsList', {
        treeDataProvider: provider,
        showCollapseAll: false,
    });

    vscode.window.createTreeView('beamFiles', {
        treeDataProvider: fileExplorer,
        showCollapseAll: true,
    });

    vscode.window.createTreeView('beamEvents', {
        treeDataProvider: eventsProvider,
        showCollapseAll: false,
    });

    vscode.window.createTreeView('beamSessionProfiles', {
        treeDataProvider: sessionProfilesProvider,
        showCollapseAll: false,
    });

    context.subscriptions.push(
        vscode.workspace.registerFileSystemProvider('beam', fsProvider, {
            isCaseSensitive: true,
        })
    );

    context.subscriptions.push(
        vscode.workspace.registerFileSystemProvider('beam-git', gitOriginalProvider, {
            isCaseSensitive: true,
            isReadonly: true,
        })
    );

    // Track open beam:// editors for file stat polling
    context.subscriptions.push(
        vscode.window.onDidChangeVisibleTextEditors(editors => {
            poller.clearTrackedFiles();
            for (const editor of editors) {
                if (editor.document.uri.scheme === 'beam') {
                    poller.trackFile(editor.document.uri.path);
                }
            }
        })
    );

    registerCommands(context, provider, fileExplorer, eventsProvider, sessionProfilesProvider, poller, () => scmProvider, containerSyncEngine);
    registerScmCommands(context, () => poller);

    // Hook beam selection to start SCM integration
    context.subscriptions.push(
        vscode.commands.registerCommand('beams.selectScm', async () => {
            // Called internally after beam selection to set up SCM
            scmProvider?.dispose();
            scmProvider = undefined;
            if (poller.hasGitRepo() && poller.getBeamId()) {
                scmProvider = new BeamGitScmProvider(poller.getBeamId()!, poller.getRepoRoot()!);
                poller.addConsumer(scmProvider);
                poller.pollNow();
            }
        })
    );

    context.subscriptions.push({ dispose: () => provider.dispose() });
    context.subscriptions.push({ dispose: () => eventsProvider.stop() });
    context.subscriptions.push({ dispose: () => clustersProvider.dispose() });
    context.subscriptions.push({ dispose: () => poller.dispose() });
    context.subscriptions.push({ dispose: () => scmProvider?.dispose() });
    context.subscriptions.push({ dispose: () => decorationProvider.dispose() });
    context.subscriptions.push({ dispose: () => containerSyncEngine.dispose() });
}

export function deactivate(): void {}
