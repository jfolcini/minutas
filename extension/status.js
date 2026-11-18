// The extension's pure logic: no gi:// imports, so plain `gjs -m` can test it.

/** @typedef {null | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'} RunStatus */
/** @typedef {{id: string, started_at: string}} Recording */
/**
 * @typedef {Recording & {name: string, duration_seconds: number, incomplete: boolean,
 *     transcribe: RunStatus, failed: string[]}} Recent
 */
/** @typedef {{recording: Recording | null, recent: Recent[]}} Status */
/** @typedef {{recordingId: string, title: string, body: string}} Notice */

const RUN_STATUSES = [null, 'queued', 'running', 'succeeded', 'failed', 'cancelled'];

/** @type {Record<string, string>} */
const ACTION_NAMES = {
    transcribe: 'Transcribe',
    summarize: 'Summarize',
    publish_agaric: 'Publish to Agaric',
    publish_gitlab: 'Publish to GitLab',
};

// What a missing status.json means: nothing has been recorded yet.
/** @type {Status} */
export const EMPTY_STATUS = {recording: null, recent: []};

// The validators take what JSON.parse returns.
/** @param {any} value */
const isString = value => typeof value === 'string';

/** @param {any} value */
const isTimestamp = value =>
    isString(value) && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(value) && !Number.isNaN(Date.parse(value));

/** @param {any} value */
const isRecording = value => value !== null && typeof value === 'object' &&
    isString(value.id) && isTimestamp(value.started_at);

/** @param {any} value */
const isRecent = value => isRecording(value) &&
    isString(value.name) &&
    Number.isFinite(value.duration_seconds) &&
    typeof value.incomplete === 'boolean' &&
    RUN_STATUSES.includes(value.transcribe) &&
    Array.isArray(value.failed) && value.failed.every(isString);

/**
 * Returns null for anything that is not a whole, valid snapshot, so the caller keeps the last good one.
 * @param {string} text
 * @returns {Status | null}
 */
export function parseStatus(text) {
    let data;
    try {
        data = JSON.parse(text);
    } catch {
        return null;
    }
    if (data?.version !== 1 ||
        !(data.recording === null || isRecording(data.recording)) ||
        !Array.isArray(data.recent) || !data.recent.every(isRecent))
        return null;
    return {recording: data.recording, recent: data.recent};
}

/** @param {string} startedAt */
const localStart = startedAt => `${startedAt.slice(0, 10)} ${startedAt.slice(11, 16)}`;

/** @param {Recent} recording */
const describe = recording => `${recording.name}, ${localStart(recording.started_at)}`;

/**
 * Only recordings the previous snapshot knew about can announce something, so the first
 * snapshot after the Shell starts, and an old one moving back into the list, stay quiet.
 * @param {Status} previous
 * @param {Status} current
 * @returns {Notice[]}
 */
export function notificationsFor(previous, current) {
    /** @type {Notice[]} */
    const notifications = [];
    for (const recording of current.recent) {
        const before = previous.recent.find(r => r.id === recording.id);
        const wasActive = previous.recording?.id === recording.id;
        if (!before && !wasActive)
            continue;
        /** @type {(title: string, body?: string) => void} */
        const announce = (title, body = describe(recording)) =>
            notifications.push({recordingId: recording.id, title, body});
        if (wasActive && recording.incomplete)
            announce('Recording stopped unexpectedly', `${describe(recording)}: what was captured is kept`);
        if (recording.transcribe === 'succeeded' && before?.transcribe !== 'succeeded')
            announce('Transcript ready');
        for (const action of recording.failed) {
            if (!before?.failed.includes(action))
                announce(`${ACTION_NAMES[action] ?? action} failed`);
        }
    }
    return notifications;
}

/** @param {number} n */
const pad = n => String(n).padStart(2, '0');

/** @param {number} seconds */
export function formatClock(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor(seconds / 60) % 60;
    const s = Math.floor(seconds) % 60;
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/**
 * @param {string} startedAt
 * @param {number} nowMs
 */
export function elapsedSeconds(startedAt, nowMs) {
    return Math.max(0, Math.floor((nowMs - Date.parse(startedAt)) / 1000));
}

/** @param {Recent} recording */
export function recentLabel(recording) {
    const parts = [recording.name, localStart(recording.started_at), formatClock(recording.duration_seconds)];
    if (recording.transcribe === 'queued' || recording.transcribe === 'running')
        parts.push('transcribing…');
    return parts.join(' · ');
}

/** @param {Recent} recording */
export function hasWarning(recording) {
    return recording.incomplete || recording.failed.length > 0;
}

/** @param {string} text */
export function lastLine(text) {
    const trimmed = text.trim();
    return trimmed.slice(trimmed.lastIndexOf('\n') + 1).trim();
}
