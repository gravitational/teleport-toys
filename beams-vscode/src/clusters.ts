import * as vscode from 'vscode';
import {
    listClusterProfiles,
    listApps,
    listDatabases,
    listKubeClusters,
    listNodes,
    RawClusterProfile,
    AppResource,
    DbResource,
    KubeResource,
    NodeResource,
} from './tsh';

interface ClusterProfile {
    profileUrl: string;
    username: string;
    cluster: string;
    roles: string[];
    logins: string[];
    validUntil: string;
    active: boolean;
}

class ClusterItem extends vscode.TreeItem {
    constructor(public readonly profile: ClusterProfile) {
        super(profile.cluster, vscode.TreeItemCollapsibleState.Collapsed);

        const remaining = Math.max(0, Math.floor((new Date(profile.validUntil).getTime() - Date.now()) / 60000));
        const timeStr = remaining > 60
            ? `${Math.floor(remaining / 60)}h ${remaining % 60}m`
            : `${remaining}m`;

        this.description = profile.active ? `active • ${timeStr}` : timeStr;
        this.tooltip = [
            `Cluster: ${profile.cluster}`,
            `User: ${profile.username}`,
            `Roles: ${profile.roles.join(', ')}`,
            `Logins: ${profile.logins.join(', ')}`,
            `Valid until: ${profile.validUntil}`,
        ].join('\n');
        this.iconPath = new vscode.ThemeIcon(profile.active ? 'plug' : 'circle-outline');
        this.contextValue = profile.active ? 'clusterActive' : 'cluster';
    }
}

class ClusterDetailItem extends vscode.TreeItem {
    constructor(label: string, value: string, icon?: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.description = value;
        if (icon) {
            this.iconPath = new vscode.ThemeIcon(icon);
        }
    }
}

type ResourceKind = 'apps' | 'databases' | 'kube' | 'nodes';

const RESOURCE_ICONS: Record<ResourceKind, string> = {
    apps: 'globe',
    databases: 'database',
    kube: 'circuit-board',
    nodes: 'server',
};

const RESOURCE_LABELS: Record<ResourceKind, string> = {
    apps: 'Apps',
    databases: 'Databases',
    kube: 'Kubernetes Clusters',
    nodes: 'Nodes',
};

class ResourceCategoryItem extends vscode.TreeItem {
    constructor(public readonly kind: ResourceKind, public readonly clusterProfile: ClusterProfile) {
        super(RESOURCE_LABELS[kind], vscode.TreeItemCollapsibleState.Collapsed);
        this.iconPath = new vscode.ThemeIcon(RESOURCE_ICONS[kind]);
        this.contextValue = `resourceCategory-${kind}`;
    }
}

class SettingsCategoryItem extends vscode.TreeItem {
    constructor(public readonly clusterProfile: ClusterProfile) {
        super('Settings', vscode.TreeItemCollapsibleState.Collapsed);
        this.iconPath = new vscode.ThemeIcon('gear');
        this.contextValue = 'clusterSettings';
    }
}

type AnyResource = AppResource | DbResource | KubeResource | NodeResource;

function hasBeamAliasLabel(labels: Record<string, string>): string | undefined {
    return labels['teleport.internal/beams/alias'];
}

export class ResourceLeafItem extends vscode.TreeItem {
    constructor(kind: ResourceKind, resource: AnyResource, rootCluster: string) {
        const beamAlias = hasBeamAliasLabel(resource.labels);
        const label = kind === 'nodes' && beamAlias ? `${resource.name} (${beamAlias})` : resource.name;
        super(label, vscode.TreeItemCollapsibleState.None);

        const isLeaf = resource.cluster !== rootCluster;
        const detail = ResourceLeafItem.detailFor(kind, resource);
        this.description = isLeaf ? `${detail} (leaf: ${resource.cluster})` : detail;

        this.tooltip = [
            `Name: ${resource.name}`,
            `Cluster: ${resource.cluster}`,
            `Proxy: ${resource.proxy}`,
            ...(('description' in resource && resource.description) ? [`Description: ${resource.description}`] : []),
            `Labels: ${Object.entries(resource.labels).map(([k, v]) => `${k}=${v}`).join(', ') || 'none'}`,
        ].join('\n');

        this.iconPath = new vscode.ThemeIcon(kind === 'nodes' && beamAlias ? 'vm' : RESOURCE_ICONS[kind]);
        this.contextValue = `resource-${kind}`;
        this.resourceIdentifier = ('publicAddr' in resource && resource.publicAddr) ? resource.publicAddr : resource.name;
    }

    resourceIdentifier: string;

    private static detailFor(kind: ResourceKind, resource: AnyResource): string {
        switch (kind) {
            case 'apps':
                return (resource as AppResource).publicAddr || (resource as AppResource).uri || '';
            case 'databases':
                return (resource as DbResource).protocol || '';
            case 'kube':
                return '';
            case 'nodes':
                return (resource as NodeResource).addr || (resource as NodeResource).hostname || '';
        }
    }
}

type Element = ClusterItem | ClusterDetailItem | ResourceCategoryItem | ResourceLeafItem | SettingsCategoryItem;

export class ClustersProvider implements vscode.TreeDataProvider<Element> {
    private _onDidChangeTreeData = new vscode.EventEmitter<Element | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    private profiles: ClusterProfile[] = [];
    private pollInterval: NodeJS.Timeout | undefined;

    constructor() {
        this.startPolling();
    }

    refresh(): void {
        this.poll();
    }

    dispose(): void {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = undefined;
        }
    }

    private startPolling(): void {
        this.poll();
        this.pollInterval = setInterval(() => this.poll(), 30000);
    }

    private async poll(): Promise<void> {
        try {
            const data = await listClusterProfiles();
            const profiles: ClusterProfile[] = [];

            if (data.active) {
                profiles.push(this.parseProfile(data.active, true));
            }

            if (Array.isArray(data.profiles)) {
                for (const p of data.profiles) {
                    profiles.push(this.parseProfile(p, false));
                }
            }

            this.profiles = profiles;
            this._onDidChangeTreeData.fire(undefined);
        } catch {
            this.profiles = [];
            this._onDidChangeTreeData.fire(undefined);
        }
    }

    private parseProfile(raw: RawClusterProfile, active: boolean): ClusterProfile {
        return {
            profileUrl: raw.profile_url ?? '',
            username: raw.username ?? '',
            cluster: raw.cluster ?? '',
            roles: raw.roles ?? [],
            logins: raw.logins ?? [],
            validUntil: raw.valid_until ?? '',
            active,
        };
    }

    getTreeItem(element: Element): vscode.TreeItem {
        return element;
    }

    async getChildren(element?: Element): Promise<Element[]> {
        if (!element) {
            if (this.profiles.length === 0) {
                return [new ClusterDetailItem('Not logged in', 'run tsh login', 'warning')];
            }
            return this.profiles.map(p => new ClusterItem(p));
        }

        if (element instanceof ClusterItem) {
            const p = element.profile;
            return [
                new SettingsCategoryItem(p),
                new ResourceCategoryItem('apps', p),
                new ResourceCategoryItem('databases', p),
                new ResourceCategoryItem('kube', p),
                new ResourceCategoryItem('nodes', p),
            ];
        }

        if (element instanceof SettingsCategoryItem) {
            const p = element.clusterProfile;
            return [
                new ClusterDetailItem('User', p.username, 'account'),
                new ClusterDetailItem('Roles', p.roles.join(', '), 'shield'),
                new ClusterDetailItem('Logins', p.logins.join(', '), 'terminal'),
                new ClusterDetailItem('Valid until', new Date(p.validUntil).toLocaleString(), 'clock'),
                new ClusterDetailItem('URL', p.profileUrl, 'link'),
            ];
        }

        if (element instanceof ResourceCategoryItem) {
            return this.loadResources(element);
        }

        return [];
    }

    private async loadResources(category: ResourceCategoryItem): Promise<Element[]> {
        const profile = category.clusterProfile;
        const proxy = profile.active ? undefined : profile.cluster;

        try {
            let resources: AnyResource[];
            switch (category.kind) {
                case 'apps':
                    resources = await listApps(proxy);
                    break;
                case 'databases':
                    resources = await listDatabases(proxy);
                    break;
                case 'kube':
                    resources = await listKubeClusters(proxy);
                    break;
                case 'nodes':
                    resources = await listNodes(proxy);
                    break;
            }

            if (resources.length === 0) {
                return [new ClusterDetailItem('None', `no ${RESOURCE_LABELS[category.kind].toLowerCase()} accessible`)];
            }

            return resources.map(r => new ResourceLeafItem(category.kind, r, profile.cluster));
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            return [new ClusterDetailItem('Error', msg, 'warning')];
        }
    }
}
