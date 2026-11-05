import * as vscode from 'vscode';
import { Beam } from './tsh';
import { getLocalContainerRecord } from './localContainer';

let _labelStore: vscode.Memento | undefined;
export function setBeamLabelStore(store: vscode.Memento): void { _labelStore = store; }
export function getBeamLabel(beamId: string): string | undefined {
    return _labelStore?.get<string>(`beamLabel:${beamId}`);
}
export function setBeamLabel(beamId: string, label: string | undefined): void {
    _labelStore?.update(`beamLabel:${beamId}`, label);
}

export class BeamDetailItem extends vscode.TreeItem {
    constructor(label: string, value: string, icon?: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.description = value;
        if (icon) {
            this.iconPath = new vscode.ThemeIcon(icon);
        }
    }
}

export class BeamItem extends vscode.TreeItem {
    constructor(public readonly beam: Beam) {
        const customLabel = getBeamLabel(beam.id);
        super(customLabel || beam.id, vscode.TreeItemCollapsibleState.Collapsed);

        const expires = new Date(beam.expires);
        const remaining = Math.max(0, Math.floor((expires.getTime() - Date.now()) / 60000));
        const timeStr = remaining > 60
            ? `${Math.floor(remaining / 60)}h ${remaining % 60}m`
            : `${remaining}m`;

        const hasLocalContainer = getLocalContainerRecord(beam.id)?.enabled === true;

        this.description = `${customLabel ? beam.id + ' · ' : ''}expires in ${timeStr}${hasLocalContainer ? ' · local container' : ''}`;
        this.tooltip = `UUID: ${beam.uuid}\nOwner: ${beam.owner}\nExpires: ${beam.expires}${beam.url ? `\nURL: ${beam.url}` : ''}${hasLocalContainer ? '\nLocal debug container: enabled' : ''}`;
        this.contextValue = (beam.url ? 'beamPublished' : 'beam') + (hasLocalContainer ? '-hasLocalContainer' : '');
        this.iconPath = new vscode.ThemeIcon(beam.url ? 'globe' : 'vm');

        this.command = {
            command: 'beams.select',
            title: 'Select Beam',
            arguments: [this],
        };
    }
}
