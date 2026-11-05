import * as vscode from 'vscode';
import { classifyTshError, listBeams, tshErrorMessage, TshErrorKind } from './tsh';
import { closeBeamTerminals } from './terminal';

// A disconnected beam usually fails several operations at once (tree refresh, git poll,
// an open editor) and the tree view re-queries on a timer, so the same condition would
// otherwise stack up duplicate notifications. Collapse repeats of the same beam+kind.
const DEDUPE_WINDOW_MS = 30000;
const lastShown = new Map<string, number>();

// A beam only goes away once, so its disconnect notice is shown once and stays suppressed
// until the beam is selected again (resetBeamNotice) — not re-shown every dedupe window
// while the tree and open editors keep retrying against it.
const disconnectNoticed = new Set<string>();

function shouldShow(key: string): boolean {
    const now = Date.now();
    const previous = lastShown.get(key);
    if (previous !== undefined && now - previous < DEDUPE_WINDOW_MS) {
        return false;
    }
    lastShown.set(key, now);
    return true;
}

export function resetBeamNotice(beamId: string): void {
    disconnectNoticed.delete(beamId);
    for (const key of [...lastShown.keys()]) {
        if (key.startsWith(`${beamId}:`)) {
            lastShown.delete(key);
        }
    }
}

export interface ReportOptions {
    /** Beam the failed operation targeted, if any. */
    beamId?: string;
    /** Human-readable description of what failed, e.g. "list files". */
    action: string;
    /** Refresh the beams list after reporting a disconnect. */
    refresh?: () => void;
}

export function reportDisconnected(beamId: string | undefined, refresh?: () => void): void {
    const key = beamId ?? '-';
    if (!disconnectNoticed.has(key)) {
        disconnectNoticed.add(key);
        const label = beamId ? `Beam "${beamId}"` : 'Beam';
        vscode.window.showInformationMessage(
            `${label} is no longer available — it may have expired or been stopped.`
        );
    }
    // Close any open SSH terminal for this beam ourselves, before the underlying `tsh`
    // process notices the dropped connection and exits on its own — that would otherwise
    // trigger VS Code's native "terminal process terminated" alert for what is really just
    // an expected, ephemeral-beam disconnect.
    if (beamId) {
        closeBeamTerminals(beamId);
    }
    refresh?.();
}

export async function beamStillExists(beamId: string): Promise<boolean> {
    try {
        return (await listBeams()).some(b => b.id === beamId);
    } catch {
        // Can't tell — assume it exists so a genuine failure isn't hidden.
        return true;
    }
}

/**
 * Surface a tsh failure at the right severity: an expired or stopped beam is reported as
 * information (it is expected — beams are ephemeral), an expired login as a warning with
 * a next step, and anything genuinely unexpected as an error.
 */
export function reportTshError(err: unknown, options: ReportOptions): TshErrorKind {
    const kind = classifyTshError(err);
    const { beamId, action, refresh } = options;

    if (kind === 'disconnected') {
        reportDisconnected(beamId, refresh);
        return kind;
    }

    if (kind === 'auth') {
        if (shouldShow(`${beamId ?? '-'}:auth`)) {
            void vscode.window.showWarningMessage(
                'Your Teleport session has expired. Log in again to continue.',
                'Login'
            ).then(choice => {
                if (choice === 'Login') {
                    void vscode.commands.executeCommand('beams.login');
                }
            });
        }
        return kind;
    }

    if (kind === 'denied') {
        if (shouldShow(`${beamId ?? '-'}:denied:${action}`)) {
            const label = beamId ? `Beam "${beamId}"` : 'This resource';
            vscode.window.showWarningMessage(
                `${label}: access denied trying to ${action}. Your login is valid, but you may not have permission for this beam.`
            );
        }
        return kind;
    }

    // tsh words a dropped connection many ways, and not all of them match the disconnect
    // patterns. Before showing an error, check whether the beam is simply gone.
    if (beamId && disconnectNoticed.has(beamId)) {
        return 'disconnected';
    }
    void (async () => {
        if (beamId && !(await beamStillExists(beamId))) {
            reportDisconnected(beamId, refresh);
            return;
        }
        if (shouldShow(`${beamId ?? '-'}:other:${action}`)) {
            vscode.window.showErrorMessage(`Failed to ${action}: ${tshErrorMessage(err)}`);
        }
    })();
    return kind;
}
