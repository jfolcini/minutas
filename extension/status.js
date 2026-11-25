// The extension's pure logic: no gi:// imports, so plain `gjs -m` can test it.

/** @typedef {null | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'} RunStatus */
/** @typedef {{id: string, started_at: string}} Recording */
/** @typedef {Recording & {paused_at?: string, paused_seconds?: number, continued?: boolean}} Active */
/** @typedef {{phase: string, percent: number}} Progress */
/**
 * @typedef {Recording & {name: string, duration_seconds: number, incomplete: boolean,
 *     transcribe: RunStatus, progress?: Progress | null, failed: string[], continuable?: boolean}} Recent
 */
/** @typedef {{recording: Active | null, recent: Recent[]}} Status */
/** @typedef {{recordingId: string, title: string, body: string}} Notice */
/** @typedef {{label: string, args: string[], failureTitle: string}} Control */

const RUN_STATUSES = [null, 'queued', 'running', 'succeeded', 'failed', 'cancelled'];

/** @type {Record<string, string>} */
const ACTION_NAMES = {
    transcribe: 'Transcribe',
    summarize: 'Summarize',
    publish_agaric: 'Publish to Agaric',
    publish_gitlab: 'Publish to GitLab',
    publish_github: 'Publish to GitHub',
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

// Left out, a flag is not set: so it is in a status.json written before the flag was.
/** @param {any} value */
const isFlag = value => value === undefined || typeof value === 'boolean';

// paused_at is there only while it is paused, paused_seconds only once it has been, continued
// only once it has been stopped and continued.
/** @param {any} value */
const isActive = value => isRecording(value) &&
    (value.paused_at === undefined || isTimestamp(value.paused_at)) &&
    (value.paused_seconds === undefined ||
        (Number.isInteger(value.paused_seconds) && value.paused_seconds >= 0)) &&
    isFlag(value.continued);

// Absent from a status.json written before it was.
/** @param {any} value */
const isProgress = value => value === undefined || value === null ||
    (typeof value === 'object' && isString(value.phase) &&
        Number.isInteger(value.percent) && value.percent >= 0 && value.percent <= 100);

/** @param {any} value */
const isRecent = value => isRecording(value) &&
    isString(value.name) &&
    Number.isFinite(value.duration_seconds) &&
    typeof value.incomplete === 'boolean' &&
    RUN_STATUSES.includes(value.transcribe) &&
    isProgress(value.progress) &&
    Array.isArray(value.failed) && value.failed.every(isString) &&
    isFlag(value.continuable);

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
        !(data.recording === null || isActive(data.recording)) ||
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
 * How long it has recorded for: its pauses left out, standing still while it is paused.
 * @param {Active} recording
 * @param {number} nowMs
 */
export function elapsedSeconds(recording, nowMs) {
    const until = recording.paused_at ? Date.parse(recording.paused_at) : nowMs;
    const sinceStart = Math.floor((until - Date.parse(recording.started_at)) / 1000);
    return Math.max(0, sinceStart - (recording.paused_seconds ?? 0));
}

/** @param {Recent} recording */
export function recentLabel(recording) {
    const parts = [recording.name, localStart(recording.started_at), formatClock(recording.duration_seconds)];
    if (recording.transcribe === 'running' && recording.progress)
        parts.push(`${recording.progress.phase}… ${recording.progress.percent}%`);
    else if (recording.transcribe === 'queued' || recording.transcribe === 'running')
        parts.push('transcribing…');
    else if (recording.transcribe === 'failed' || recording.transcribe === 'cancelled')
        parts.push(`transcription ${recording.transcribe}`);
    return parts.join(' · ');
}

/**
 * What the menu offers for its transcription: to cancel one that is queued or running, to
 * transcribe again after one that failed or was cancelled, else nothing.
 * @param {Recent} recording
 * @returns {Control | null}
 */
export function transcriptionControl(recording) {
    switch (recording.transcribe) {
    case 'queued':
    case 'running':
        return {
            label: 'Cancel transcription',
            args: ['transcribe', '--cancel', recording.id],
            failureTitle: "Couldn't cancel the transcription",
        };
    case 'failed':
    case 'cancelled':
        return {
            label: 'Transcribe again',
            args: ['transcribe', recording.id],
            failureTitle: "Couldn't start transcribing",
        };
    default:
        return null;
    }
}

/**
 * What the menu offers under a recent recording: what its transcription allows, then to record
 * into it again while the program says it can be continued.
 * @param {Recent} recording
 * @returns {Control[]}
 */
export function recentControls(recording) {
    const transcription = transcriptionControl(recording);
    const controls = transcription ? [transcription] : [];
    if (recording.continuable) {
        controls.push({
            label: 'Continue recording',
            args: ['record', 'continue', recording.id],
            failureTitle: "Couldn't continue the recording",
        });
    }
    return controls;
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
