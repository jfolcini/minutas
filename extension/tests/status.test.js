import {expect, test} from 'vitest';

import {
    EMPTY_STATUS,
    elapsedSeconds,
    formatClock,
    hasWarning,
    lastLine,
    notificationsFor,
    parseStatus,
    recentControls,
    recentLabel,
    transcriptionControl,
} from '../status.js';

/** @typedef {import('../status.js').Active} Active */
/** @typedef {import('../status.js').Recent} Recent */

/** @type {(fields?: Partial<Recent>) => Recent} */
const entry = (fields = {}) => ({
    id: '2026-11-18T12-00-03',
    name: 'Meeting',
    started_at: '2026-11-18T12:00:03+01:00',
    duration_seconds: 1834,
    incomplete: false,
    transcribe: 'succeeded',
    progress: null,
    failed: [],
    ...fields,
});

const SPEAKERS = {phase: 'identifying speakers', percent: 40};

const ACTIVE = {id: '2026-11-18T13-05-42', started_at: '2026-11-18T13:05:42+01:00'};
/** @type {(recording: Active | null, recent: Recent[]) => import('../status.js').Status} */
const status = (recording, recent) => ({recording, recent});

test('parseStatus reads the contract example', () => {
    const text = JSON.stringify({version: 1, recording: ACTIVE, recent: [entry()]});
    expect(parseStatus(text)).toStrictEqual(status(ACTIVE, [entry()]));
});

test("parseStatus reads a running transcription's progress", () => {
    const running = entry({transcribe: 'running', progress: SPEAKERS});
    const text = JSON.stringify({version: 1, recording: null, recent: [running]});
    expect(parseStatus(text)).toStrictEqual(status(null, [running]));
});

test('parseStatus reads a status.json written before progress was in it', () => {
    const older = entry();
    delete older.progress;
    const text = JSON.stringify({version: 1, recording: null, recent: [older]});
    expect(parseStatus(text)).toStrictEqual(status(null, [older]));
});

test('parseStatus rejects a progress that is not a phase and a whole percent', () => {
    const rejected = [
        {phase: 'transcribing', percent: 101},
        {phase: 'transcribing', percent: -1},
        {phase: 'transcribing', percent: 40.5},
        {phase: 'transcribing', percent: '40'},
        {phase: 7, percent: 40},
        {percent: 40},
        40,
    ].map(progress => {
        // @ts-expect-error: the invalid entries this test is about
        const text = JSON.stringify({version: 1, recording: null, recent: [entry({progress})]});
        return parseStatus(text);
    });
    expect(rejected).toStrictEqual([null, null, null, null, null, null, null]);
});

test('parseStatus reads a recording that is paused, and one that was', () => {
    const paused = {...ACTIVE, paused_at: '2026-11-18T13:06:42+01:00'};
    const resumed = {...ACTIVE, paused_seconds: 30};
    expect(
        [paused, resumed].map(recording =>
            parseStatus(JSON.stringify({version: 1, recording, recent: []}))
        )
    ).toStrictEqual([status(paused, []), status(resumed, [])]);
});

test('parseStatus rejects a pause that is not a time, or not whole seconds', () => {
    const rejected = [
        {paused_at: 'yesterday'},
        {paused_at: null},
        {paused_seconds: -1},
        {paused_seconds: 1.5},
        {paused_seconds: '30'},
    ].map(pause =>
        parseStatus(JSON.stringify({version: 1, recording: {...ACTIVE, ...pause}, recent: []}))
    );
    expect(rejected).toStrictEqual([null, null, null, null, null]);
});

test('parseStatus reads a recording that can be continued, and one that was', () => {
    const continued = {...ACTIVE, paused_seconds: 40, continued: true};
    const text = JSON.stringify({
        version: 1,
        recording: continued,
        recent: [entry({continuable: true})],
    });
    expect(parseStatus(text)).toStrictEqual(status(continued, [entry({continuable: true})]));
});

test('parseStatus rejects a continued or a continuable that is not true or false', () => {
    const rejected = [
        {version: 1, recording: {...ACTIVE, continued: 'yes'}, recent: []},
        {version: 1, recording: {...ACTIVE, continued: null}, recent: []},
        {version: 1, recording: null, recent: [{...entry(), continuable: 1}]},
        {version: 1, recording: null, recent: [{...entry(), continuable: null}]},
    ].map(written => parseStatus(JSON.stringify(written)));
    expect(rejected).toStrictEqual([null, null, null, null]);
});

test('parseStatus rejects a half-written file', () => {
    expect(parseStatus('{"version": 1, "recording": {"id": "2026')).toBeNull();
});

test('parseStatus rejects another version', () => {
    expect(parseStatus(JSON.stringify({version: 2, recording: null, recent: []}))).toStrictEqual(
        null
    );
});

test('parseStatus rejects a recent entry whose failed is not a list', () => {
    const text = JSON.stringify({
        version: 1,
        recording: null,
        // @ts-expect-error: the invalid entry this test is about
        recent: [entry({failed: 'summarize'})],
    });
    expect(parseStatus(text)).toBeNull();
});

test('parseStatus rejects an unknown transcribe status', () => {
    const text = JSON.stringify({
        version: 1,
        recording: null,
        // @ts-expect-error: the invalid entry this test is about
        recent: [entry({transcribe: 'done'})],
    });
    expect(parseStatus(text)).toBeNull();
});

test('parseStatus rejects a recording whose start time does not parse', () => {
    const recording = {id: ACTIVE.id, started_at: 'yesterday'};
    expect(parseStatus(JSON.stringify({version: 1, recording, recent: []}))).toBeNull();
});

test('a transcript that finished running is announced', () => {
    const previous = status(null, [entry({transcribe: 'running'})]);
    expect(notificationsFor(previous, status(null, [entry()]))).toStrictEqual([
        {recordingId: entry().id, title: 'Transcript ready', body: 'Meeting, 2026-11-18 12:00'},
    ]);
});

test('a transcript ready by the first snapshot after stop is announced', () => {
    const stopped = entry({id: ACTIVE.id, started_at: ACTIVE.started_at});
    expect(notificationsFor(status(ACTIVE, []), status(null, [stopped]))).toStrictEqual([
        {recordingId: ACTIVE.id, title: 'Transcript ready', body: 'Meeting, 2026-11-18 13:05'},
    ]);
});

test('recordings the previous snapshot did not know are not announced', () => {
    const old = entry({failed: ['publish_gitlab']});
    expect(notificationsFor(EMPTY_STATUS, status(null, [old]))).toStrictEqual([]);
});

test('a newly failed action is announced by its display name', () => {
    const previous = status(null, [entry({failed: ['summarize']})]);
    const current = status(null, [entry({failed: ['summarize', 'publish_agaric']})]);
    expect(notificationsFor(previous, current)).toStrictEqual([
        {
            recordingId: entry().id,
            title: 'Publish to Agaric failed',
            body: 'Meeting, 2026-11-18 12:00',
        },
    ]);
});

test('a failed Publish to GitHub is announced by its display name', () => {
    const previous = status(null, [entry()]);
    const current = status(null, [entry({failed: ['publish_github']})]);
    expect(notificationsFor(previous, current)).toStrictEqual([
        {
            recordingId: entry().id,
            title: 'Publish to GitHub failed',
            body: 'Meeting, 2026-11-18 12:00',
        },
    ]);
});

test('an unchanged snapshot announces nothing again', () => {
    const same = status(null, [entry({failed: ['summarize']})]);
    expect(notificationsFor(same, same)).toStrictEqual([]);
});

test('a recorder that died is announced', () => {
    const died = entry({
        id: ACTIVE.id,
        started_at: ACTIVE.started_at,
        incomplete: true,
        transcribe: 'queued',
    });
    expect(notificationsFor(status(ACTIVE, []), status(null, [died]))).toStrictEqual([
        {
            recordingId: ACTIVE.id,
            title: 'Recording stopped unexpectedly',
            body: 'Meeting, 2026-11-18 13:05: what was captured is kept',
        },
    ]);
});

test('a normal stop announces nothing', () => {
    const stopped = entry({id: ACTIVE.id, started_at: ACTIVE.started_at, transcribe: 'queued'});
    expect(notificationsFor(status(ACTIVE, []), status(null, [stopped]))).toStrictEqual([]);
});

test('formatClock shows minutes and seconds, and hours once there are any', () => {
    expect([formatClock(5), formatClock(754), formatClock(3723)]).toStrictEqual([
        '0:05',
        '12:34',
        '1:02:03',
    ]);
});

test('elapsedSeconds counts from a start time with a UTC offset', () => {
    expect(elapsedSeconds(ACTIVE, Date.parse('2026-11-18T12:06:47Z'))).toStrictEqual(65);
});

test('elapsedSeconds never goes below zero when the clock is behind', () => {
    expect(elapsedSeconds(ACTIVE, Date.parse('2026-11-18T12:00:00Z'))).toStrictEqual(0);
});

test('elapsedSeconds leaves out the time a recording was paused', () => {
    expect(
        elapsedSeconds({...ACTIVE, paused_seconds: 30}, Date.parse('2026-11-18T12:06:47Z'))
    ).toStrictEqual(35);
});

test('elapsedSeconds stands still while a recording is paused', () => {
    const paused = {...ACTIVE, paused_seconds: 30, paused_at: '2026-11-18T13:06:42+01:00'};
    expect([
        elapsedSeconds(paused, Date.parse('2026-11-18T12:06:47Z')),
        elapsedSeconds(paused, Date.parse('2026-11-18T13:00:00Z')),
    ]).toStrictEqual([30, 30]);
});

test('recentLabel shows the name, local start and duration', () => {
    expect(recentLabel(entry())).toStrictEqual('Meeting · 2026-11-18 12:00 · 30:34');
});

test('recentLabel hints while the transcript is queued or running', () => {
    expect([
        recentLabel(entry({transcribe: 'queued'})),
        recentLabel(entry({transcribe: 'running'})),
    ]).toStrictEqual([
        'Meeting · 2026-11-18 12:00 · 30:34 · transcribing…',
        'Meeting · 2026-11-18 12:00 · 30:34 · transcribing…',
    ]);
});

test('recentLabel says how far a running transcription has got', () => {
    expect(recentLabel(entry({transcribe: 'running', progress: SPEAKERS}))).toStrictEqual(
        'Meeting · 2026-11-18 12:00 · 30:34 · identifying speakers… 40%'
    );
});

test('recentLabel says a transcription failed or was cancelled', () => {
    expect([
        recentLabel(entry({transcribe: 'failed', failed: ['transcribe']})),
        recentLabel(entry({transcribe: 'cancelled'})),
    ]).toStrictEqual([
        'Meeting · 2026-11-18 12:00 · 30:34 · transcription failed',
        'Meeting · 2026-11-18 12:00 · 30:34 · transcription cancelled',
    ]);
});

test('transcriptionControl cancels a transcription that is queued or running', () => {
    const cancel = {
        label: 'Cancel transcription',
        args: ['transcribe', '--cancel', '2026-11-18T12-00-03'],
        failureTitle: "Couldn't cancel the transcription",
    };
    expect([
        transcriptionControl(entry({transcribe: 'queued'})),
        transcriptionControl(entry({transcribe: 'running'})),
    ]).toStrictEqual([cancel, cancel]);
});

test('transcriptionControl transcribes again after a transcription failed or was cancelled', () => {
    const again = {
        label: 'Transcribe again',
        args: ['transcribe', '2026-11-18T12-00-03'],
        failureTitle: "Couldn't start transcribing",
    };
    expect([
        transcriptionControl(entry({transcribe: 'failed'})),
        transcriptionControl(entry({transcribe: 'cancelled'})),
    ]).toStrictEqual([again, again]);
});

test('transcriptionControl offers nothing once transcribed, or before any transcription', () => {
    expect([
        transcriptionControl(entry()),
        transcriptionControl(entry({transcribe: null})),
    ]).toStrictEqual([null, null]);
});

const CONTINUE = {
    label: 'Continue recording',
    args: ['record', 'continue', '2026-11-18T12-00-03'],
    failureTitle: "Couldn't continue the recording",
};

test('recentControls offers Continue recording after what the transcription offers', () => {
    const controls = [
        entry({transcribe: 'queued', continuable: true}),
        entry({transcribe: 'failed', continuable: true}),
        entry({transcribe: null, continuable: true}),
    ].map(recentControls);
    expect(controls.map(offered => offered.map(control => control.label))).toStrictEqual([
        ['Cancel transcription', 'Continue recording'],
        ['Transcribe again', 'Continue recording'],
        ['Continue recording'],
    ]);
    expect(controls[2]).toStrictEqual([CONTINUE]);
});

test('recentControls offers no Continue recording for a recording that cannot be continued', () => {
    const older = entry({transcribe: 'cancelled'});
    expect([
        recentControls(entry({transcribe: 'running', continuable: false})),
        recentControls(older),
        recentControls(entry()),
    ]).toStrictEqual([
        [transcriptionControl(entry({transcribe: 'running'}))],
        [transcriptionControl(older)],
        [],
    ]);
});

test('hasWarning marks a failed run or an incomplete recording', () => {
    expect([
        hasWarning(entry({failed: ['transcribe']})),
        hasWarning(entry({incomplete: true})),
        hasWarning(entry()),
    ]).toStrictEqual([true, true, false]);
});

test('lastLine takes the last non-empty line of stderr', () => {
    expect([lastLine('warning: x\nerror: already recording\n\n'), lastLine('')]).toStrictEqual([
        'error: already recording',
        '',
    ]);
});
