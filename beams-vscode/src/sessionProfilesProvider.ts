import * as vscode from 'vscode';
import { SessionProfileItem } from './sessionProfileItem';
import { listSessionProfiles } from './sessionProfiles';

export class SessionProfilesProvider implements vscode.TreeDataProvider<SessionProfileItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<SessionProfileItem | undefined | null>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    refresh(): void {
        this._onDidChangeTreeData.fire(undefined);
    }

    getTreeItem(element: SessionProfileItem): vscode.TreeItem {
        return element;
    }

    getChildren(): SessionProfileItem[] {
        return listSessionProfiles().map(p => new SessionProfileItem(p));
    }
}
