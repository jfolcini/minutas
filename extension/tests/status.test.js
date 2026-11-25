// Run with `gjs -m extension/tests/status.test.js`; exits 1 on any failure.
import System from 'system';

import {
    EMPTY_STATUS,
    elapsedSeconds,
    formatClock,
    hasWarning,
    lastLine,
    notificationsFor,
    parseStatus,
    recentLabel,
} from '../status.js';

/** @typedef {import('../status.js').Recording} Recording */
/** @typedef {import('../status.js').Recent} Recent */

/** @type {{name: string, fn: () => void}[]} */
const tests = [];
/** @type {(name: string, fn: () => void) => void} */
const test = (name, fn) => tests.push({name, fn});

/**
 * @param {unknown} actual
 * @param {unknown} expected
 */
function assertEqual(actual, expected) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    if (a !== e)
        throw new Error(`expected ${e}\n       got ${a}`);
}

/** @type {(fields?: Partial<Recent>) => Recent} */
const entry = (fields = {}) => ({
    id: '2026-11-18T12-00-03',
    name: 'Meeting',
    started_at: '2026-11-18T12:00:03+01:00',
    duration_seconds: 1834,
    incomplete: false,
    transcribe: 'succeeded',
    failed: [],
    ...fields,
});

const ACTIVE = {id: '2026-11-18T13-05-42', started_at: '2026-11-18T13:05:42+01:00'};
/** @type {(recording: Recording | null, recent: Recent[]) => import('../status.js').Status} */
const status = (recording, recent) => ({recording, recent});

test('parseStatus reads the contract example', () => {
    const text = JSON.stringify({version: 1, recording: ACTIVE, recent: [entry()]});
    assertEqual(parseStatus(text), status(ACTIVE, [entry()]));
});

test('parseStatus rejects a half-written file', () => {
    assertEqual(parseStatus('{"version": 1, "recording": {"id": "2026'), null);
});

test('parseStatus rejects another version', () => {
    assertEqual(parseStatus(JSON.stringify({version: 2, recording: null, recent: []})), null);
});

test('parseStatus rejects a recent entry whose failed is not a list', () => {
    // @ts-expect-error: the invalid entry this test is about
    const text = JSON.stringify({version: 1, recording: null, recent: [entry({failed: 'summarize'})]});
    assertEqual(parseStatus(text), null);
});

test('parseStatus rejects an unknown transcribe status', () => {
    // @ts-expect-error: the invalid entry this test is about
    const text = JSON.stringify({version: 1, recording: null, recent: [entry({transcribe: 'done'})]});
    assertEqual(parseStatus(text), null);
});

test('parseStatus rejects a recording whose start time does not parse', () => {
    const recording = {id: ACTIVE.id, started_at: 'yesterday'};
    assertEqual(parseStatus(JSON.stringify({version: 1, recording, recent: []})), null);
});

test('a transcript that finished running is announced', () => {
    const previous = status(null, [entry({transcribe: 'running'})]);
    assertEqual(notificationsFor(previous, status(null, [entry()])), [
        {recordingId: entry().id, title: 'Transcript ready', body: 'Meeting, 2026-11-18 12:00'},
    ]);
});

test('a transcript ready by the first snapshot after stop is announced', () => {
    const stopped = entry({id: ACTIVE.id, started_at: ACTIVE.started_at});
    assertEqual(notificationsFor(status(ACTIVE, []), status(null, [stopped])), [
        {recordingId: ACTIVE.id, title: 'Transcript ready', body: 'Meeting, 2026-11-18 13:05'},
    ]);
});

test('recordings the previous snapshot did not know are not announced', () => {
    const old = entry({failed: ['publish_gitlab']});
    assertEqual(notificationsFor(EMPTY_STATUS, status(null, [old])), []);
});

test('a newly failed action is announced by its display name', () => {
    const previous = status(null, [entry({failed: ['summarize']})]);
    const current = status(null, [entry({failed: ['summarize', 'publish_agaric']})]);
    assertEqual(notificationsFor(previous, current), [
        {recordingId: entry().id, title: 'Publish to Agaric failed', body: 'Meeting, 2026-11-18 12:00'},
    ]);
});

test('a failed Publish to GitHub is announced by its display name', () => {
    const previous = status(null, [entry()]);
    const current = status(null, [entry({failed: ['publish_github']})]);
    assertEqual(notificationsFor(previous, current), [
        {recordingId: entry().id, title: 'Publish to GitHub failed', body: 'Meeting, 2026-11-18 12:00'},
    ]);
});

test('an unchanged snapshot announces nothing again', () => {
    const same = status(null, [entry({failed: ['summarize']})]);
    assertEqual(notificationsFor(same, same), []);
});

test('a recorder that died is announced', () => {
    const died = entry({id: ACTIVE.id, started_at: ACTIVE.started_at, incomplete: true, transcribe: 'queued'});
    assertEqual(notificationsFor(status(ACTIVE, []), status(null, [died])), [
        {
            recordingId: ACTIVE.id,
            title: 'Recording stopped unexpectedly',
            body: 'Meeting, 2026-11-18 13:05: what was captured is kept',
        },
    ]);
});

test('a normal stop announces nothing', () => {
    const stopped = entry({id: ACTIVE.id, started_at: ACTIVE.started_at, transcribe: 'queued'});
    assertEqual(notificationsFor(status(ACTIVE, []), status(null, [stopped])), []);
});

test('formatClock shows minutes and seconds, and hours once there are any', () => {
    assertEqual([formatClock(5), formatClock(754), formatClock(3723)], ['0:05', '12:34', '1:02:03']);
});

test('elapsedSeconds counts from a start time with a UTC offset', () => {
    assertEqual(elapsedSeconds(ACTIVE.started_at, Date.parse('2026-11-18T12:06:47Z')), 65);
});

test('elapsedSeconds never goes below zero when the clock is behind', () => {
    assertEqual(elapsedSeconds(ACTIVE.started_at, Date.parse('2026-11-18T12:00:00Z')), 0);
});

test('recentLabel shows the name, local start and duration', () => {
    assertEqual(recentLabel(entry()), 'Meeting · 2026-11-18 12:00 · 30:34');
});

test('recentLabel hints while the transcript is queued or running', () => {
    assertEqual(
        [recentLabel(entry({transcribe: 'queued'})), recentLabel(entry({transcribe: 'running'}))],
        ['Meeting · 2026-11-18 12:00 · 30:34 · transcribing…', 'Meeting · 2026-11-18 12:00 · 30:34 · transcribing…']
    );
});

test('hasWarning marks a failed run or an incomplete recording', () => {
    assertEqual(
        [hasWarning(entry({failed: ['transcribe']})), hasWarning(entry({incomplete: true})), hasWarning(entry())],
        [true, true, false]
    );
});

test('lastLine takes the last non-empty line of stderr', () => {
    assertEqual([lastLine('warning: x\nerror: already recording\n\n'), lastLine('')], ['error: already recording', '']);
});

let failures = 0;
for (const {name, fn} of tests) {
    try {
        fn();
        print(`ok - ${name}`);
    } catch (e) {
        failures++;
        print(`FAIL - ${name}\n       ${e}`);
    }
}
print(`${tests.length - failures} passed, ${failures} failed`);
if (failures > 0)
    System.exit(1);
