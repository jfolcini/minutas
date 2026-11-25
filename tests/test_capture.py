import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

# Ubuntu builds its PyGObject for its own Python alone: under another, the recorder cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gst", "1.0")
gi.require_version("GstCheck", "1.0")
from gi.repository import (  # noqa: E402
    GLib,
    Gst,
    GstCheck,  # ty: ignore[missing-module-member] - pygobject-stubs has no GstCheck
)

from minutas import capture, recorder, settings, store  # noqa: E402

# A deadlock inside GStreamer holds the main thread, where neither the watchdog below nor the
# default timeout's signal runs: a timer thread ends the suite instead, with every thread's stack.
pytestmark = pytest.mark.timeout(method="thread")

# A source that captures in real time, as pulsesrc does, a buffer at a time.
LIVE = ["audiotestsrc", "is-live=true"]
# One buffer of a source, 1024 samples at the tracks' 16 kHz, in samples of Opus's 48 kHz.
BUFFER = 3072
PAUSE = 5 * Gst.SECOND
# A second of audio a buffer, so minutes of it take a few hundred steps.
QUIET = [*LIVE, "samplesperbuffer=16000", "wave=silence"]
SECOND = 16_000 * 3
# Where the clock starts: what the program takes for the time of day when it starts.
START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
# A test's whole recording takes a fraction of a second, none of it waiting.
GIVE_UP_SECONDS = 10


def tracks(mic: list[str], call: list[str]) -> list[str]:
    """The recorder's pipeline, with the sources in place of the microphone and the call."""
    sources = zip((mic, call), store.TRACKS, strict=True)
    return [name for source, track in sources for name in recorder.track_branch(source, track)]


def audio_in(track: Path) -> tuple[int, bool]:
    """The samples an Ogg Opus file holds, and whether its stream was ended properly."""
    data = track.read_bytes()
    # A page's header holds its flags, 4 on the stream's last page, then where its audio ends.
    last_page = data.rindex(b"OggS")
    end = int.from_bytes(data[last_page + 6 : last_page + 14], "little")
    # Opus's header says how many samples at the start are the encoder's own.
    header = data.index(b"OpusHead")
    pre_skip = int.from_bytes(data[header + 10 : header + 12], "little")
    return end - pre_skip, data[last_page + 5] == 4


def recorded(folder: Path) -> list[tuple[int, bool]]:
    return [audio_in(folder / name) for name in store.TRACKS]


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recording's folder, where the unit runs its program."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def keyboard_interrupt() -> Iterator[None]:
    """GLib leaves SIGINT ending the process once its watch is gone; Ctrl-C interrupts the suite
    again."""
    yield
    signal.signal(signal.SIGINT, signal.default_int_handler)


# The maintainer's laptop microphone through the recorder's track branch, a second at a time,
# in dB of RMS, the plain mic and the echo-cancelled one measured together: the room while
# nobody speaks, from the start of a recording; then soft speech at the usual distance, whose
# breaths fall back to the room.
ROOM = [-47.0, -56.6, -57.2, -58.8, -56.6, -56.1, -56.9, -58.4, -57.7, -56.9, -56.3, -54.9, -58.3]
SOFT_SPEECH = [-46.4, -44.1, -47.9, -57.9, -41.2, -43.2, -55.6, -58.9, -44.4, -41.1, -41.9, -50.0]
SOFT_SPEECH += [-38.9, -41.3, -53.6, -41.2]
CANCELLED_ROOM = [-74.3, -86.6, -77.5, -86.2, -76.7, -82.1, -82.0, -84.0, -84.0, -82.2, -71.2]
CANCELLED_ROOM += [-68.7, -70.5, -83.8, -84.3]
CANCELLED_SOFT_SPEECH = [-47.9, -45.6, -50.0, -66.2, -42.4, -44.4, -61.0, -74.3, -44.8, -41.8]
CANCELLED_SOFT_SPEECH += [-43.0, -51.4, -40.1, -41.1, -56.2, -42.0]
# The call track with nothing playing: digital silence.
NOTHING_PLAYING = -700.0


def levels_measured(source: list[str]) -> list[float]:
    """The RMS the recorder's track branch measures each second of what the source plays."""
    Gst.init(None)
    pipeline = cast("Gst.Pipeline", Gst.parse_launchv(recorder.track_branch(source, "mic.opus")))
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    levels: list[float] = []
    kinds = Gst.MessageType.ELEMENT | Gst.MessageType.EOS | Gst.MessageType.ERROR
    while (message := bus.timed_pop_filtered(GIVE_UP_SECONDS * Gst.SECOND, kinds)) is not None:
        if message.type != Gst.MessageType.ELEMENT:
            break
        reading = cast("Gst.Structure", message.get_structure())
        # The reading the end of the stream flushes covers no time.
        if cast("int", reading.get_value("duration")) > 0:
            levels.append(max(cast("list[float]", reading.get_value("rms"))))
    pipeline.set_state(Gst.State.NULL)
    return levels


def test_a_constant_offset_is_taken_out_before_the_level_is_measured(folder: Path) -> None:
    # A constant offset, as the maintainer's laptop microphone records under all it hears:
    # unfiltered, it reads -30 dB every second and buries soft speech under it.
    offset = ["audiotestsrc", "num-buffers=4", "samplesperbuffer=16000", "wave=square"]

    levels = levels_measured([*offset, "freq=0.01", "volume=0.03"])

    # The first second, the high-pass settling; then only the dither of the 16-bit samples.
    assert [round(level) for level in levels] == [-56, -96, -96, -96]
    assert (folder / "mic.opus").is_file()


def heard_each_second(
    hearing: capture.Hearing, seconds: Sequence[tuple[float, float] | None], first: int = 0
) -> list[int | None]:
    """What hearing says of each reading, the mic's then the call's each second from the first;
    None in seconds is a pause, which takes no running time."""
    said: list[int | None] = []
    second = first
    for levels in seconds:
        if levels is None:
            hearing.pause()
            continue
        start, end = second * Gst.SECOND, (second + 1) * Gst.SECOND
        said.extend(
            hearing.reading(track, start, end, level)
            for track, level in zip(store.TRACKS, levels, strict=True)
        )
        second += 1
    return said


@pytest.mark.parametrize("room", [ROOM, CANCELLED_ROOM], ids=["plain", "cancelled"])
def test_the_rooms_noise_floor_and_its_starting_transient_are_silence(room: list[float]) -> None:
    minutes = room + room[1:] * (10 * 60 // len(room))

    said = heard_each_second(capture.Hearing(), [(level, NOTHING_PLAYING) for level in minutes])

    assert said[-1] == len(minutes) * Gst.SECOND


@pytest.mark.parametrize(
    ("room", "speech", "longest_quiet"),
    [(ROOM, SOFT_SPEECH, 15), (CANCELLED_ROOM, CANCELLED_SOFT_SPEECH, 2)],
    ids=["plain", "cancelled"],
)
def test_soft_speech_over_the_rooms_noise_floor_is_heard_every_few_seconds(
    room: list[float], speech: list[float], longest_quiet: int
) -> None:
    hearing = capture.Hearing()
    heard_each_second(hearing, [(level, NOTHING_PLAYING) for level in room])
    minutes = speech * (10 * 60 // len(speech))

    said = heard_each_second(hearing, [(level, NOTHING_PLAYING) for level in minutes], len(room))

    # Once the first word is heard, its longest stretch without one above the floor: far
    # shorter than any reminder.
    spoken = said[said.index(None) :]
    assert max(quiet or 0 for quiet in spoken) == longest_quiet * Gst.SECOND


def test_a_room_that_grows_louder_is_heard_only_until_it_is_the_floor() -> None:
    hearing = capture.Hearing()
    heard_each_second(hearing, [(-57.0, NOTHING_PLAYING)] * 60)
    # A fan switched on near the microphone.
    fan = [(-35.0, NOTHING_PLAYING)] * 180

    said = heard_each_second(hearing, fan, 60)

    # Heard while a second of the quieter room is among the floor's; quiet from then on.
    quiet_from = capture.FLOOR_READINGS - 1
    assert said[-1] == (len(fan) - quiet_from) * Gst.SECOND


# In half dB steps, which floating point adds exactly: in tenths, a level the margin over its
# floor can land on either side of it once the room is made louder.
LEVELS = st.one_of(st.just(NOTHING_PLAYING), st.integers(-180, -10).map(lambda halves: halves / 2))
SECONDS = st.lists(st.one_of(st.none(), st.tuples(LEVELS, LEVELS)), max_size=400)


@given(SECONDS)
def test_a_second_is_heard_above_its_tracks_floor_and_silence_counts_from_the_last_one(
    seconds: list[tuple[float, float] | None],
) -> None:
    levels: dict[str, list[float]] = {track: [] for track in store.TRACKS}
    expected: list[int | None] = []
    counted_from: int | None = None
    second = 0
    for both in seconds:
        if both is None:
            counted_from = None
            continue
        for track, level in zip(store.TRACKS, both, strict=True):
            levels[track].append(level)
            floor = min(levels[track][-capture.FLOOR_READINGS :])
            if level > floor + capture.SILENCE_MARGIN_DB:
                counted_from = (second + 1) * Gst.SECOND
                expected.append(None)
            else:
                counted_from = second * Gst.SECOND if counted_from is None else counted_from
                expected.append((second + 1) * Gst.SECOND - counted_from)
        second += 1

    assert heard_each_second(capture.Hearing(), seconds) == expected


@given(SECONDS, st.integers(-60, 60).map(lambda halves: halves / 2))
def test_what_is_heard_is_relative_to_the_room_however_loud_it_is(
    seconds: list[tuple[float, float] | None], louder: float
) -> None:
    mic_louder = [None if both is None else (both[0] + louder, both[1]) for both in seconds]

    assert heard_each_second(capture.Hearing(), mic_louder) == heard_each_second(
        capture.Hearing(), seconds
    )


@pytest.fixture
def silent_after_a_minute(monkeypatch: pytest.MonkeyPatch) -> str:
    """A recording in progress, whose folder the unit runs its program in, that the top bar
    reminds of after a minute of silence; its id."""
    settings.set_silence_reminder_minutes(1)
    recording_id = store.create_recording(START)["id"]
    store.write_status()
    monkeypatch.chdir(store.recording_dir(recording_id))
    return recording_id


def silent_since(recording_id: str) -> object:
    """Since when the recording says it has been silent, and status.json with it."""
    status = json.loads((store.data_dir() / "status.json").read_text())
    said = store.load_recording(recording_id).get("silent_since")
    assert status["recording"].get("silent_since") == said
    return said


def after(duration: int) -> str:
    """The time of day the duration after START, as the recording keeps it."""
    return (START + timedelta(microseconds=duration // 1000)).isoformat(timespec="seconds")


class Recording:
    """The recorder's pipeline on a clock that only moves when told to, so a live source
    captures a buffer exactly when the test lets it; the signals are real.
    """

    def __init__(self, mic: list[str] = LIVE, call: list[str] = LIVE) -> None:
        Gst.init(None)
        self.pipeline = cast("Gst.Pipeline", Gst.parse_launchv(tracks(mic, call)))
        self.clock = GstCheck.TestClock.new()
        self.pipeline.use_clock(self.clock)
        # This clock lets a live source hand a buffer on at its start; a microphone hands it on
        # once it holds all of it, so the time of day is a buffer's length ahead of the clock.
        given = [arg.removeprefix("samplesperbuffer=") for arg in mic if "samplesperbuffer=" in arg]
        self.buffer_length = int(given[0] if given else 1024) * Gst.SECOND // 16_000

    def run(self, script: Generator[None]) -> int:
        """Runs the program, with the script taking its steps inside its main loop.

        A program that never pauses, resumes or ends would wait there for good, where no test
        timeout reaches: it is ended as a pipeline that failed instead, with exit status 1.
        """
        gave_up: list[bool] = []

        def give_up() -> bool:
            gave_up.append(True)
            waited = GLib.Error(f"the script was not done after {GIVE_UP_SECONDS} s")
            self.pipeline.post_message(Gst.Message.new_error(self.pipeline, waited, "the test"))
            return GLib.SOURCE_REMOVE

        watchdog = GLib.timeout_add_seconds(GIVE_UP_SECONDS, give_up)
        GLib.idle_add(lambda: not gave_up and next(script, False) is not False)
        # The time of day moves with the clock, as a pause moves it and the running time not.
        exit_status = capture.run(
            self.pipeline,
            now=lambda: (
                START + timedelta(microseconds=(self.clock.get_time() + self.buffer_length) // 1000)
            ),
        )
        if not gave_up:
            GLib.source_remove(watchdog)
        return exit_status

    def until(self, done: Callable[[], bool]) -> Generator[None]:
        """Lets the main loop go on, where the signals arrive, until done."""
        while not done():
            yield

    def waiting(self) -> Generator[None]:
        """Until both sources hold a buffer and wait for the clock to reach its time."""
        yield from self.until(lambda: self.clock.peek_id_count() == len(store.TRACKS))

    def capture(self, buffers: int) -> Generator[None]:
        """Has each source capture as many buffers."""
        for _ in range(buffers):
            yield from self.waiting()
            for _ in store.TRACKS:
                self.clock.crank()

    def pause(self) -> Generator[None, None, int]:
        """Pauses it; returns the time the sources were waiting for."""
        yield from self.waiting()
        held_for = int(self.clock.get_next_entry_time())
        # Where a running clock would be: at the buffers the sources hold.
        self.clock.set_time(held_for)
        os.kill(os.getpid(), recorder.PAUSE_SIGNAL)
        # A paused source waits for no time.
        yield from self.until(lambda: self.clock.peek_id_count() == 0)
        return held_for

    def end(self) -> Generator[None]:
        """Ends it, as Finish does, with no buffer half made."""
        # End-of-stream ends a source's wait for the clock; one still making its buffer would
        # wait after it, on a clock that nothing moves while the program ends.
        yield from self.waiting()
        os.kill(os.getpid(), signal.SIGINT)

    def hear(self, wave: str, name: str = "mic") -> Generator[None]:
        """Has the source of that name capture that wave from its next buffer on."""
        # Each source has made the buffer it waits to capture, so the change can't reach it.
        yield from self.waiting()
        source = cast("Gst.Element", self.pipeline.get_by_name(name))
        Gst.util_set_object_arg(source, "wave", wave)


def test_a_pause_leaves_no_gap_in_the_tracks_and_keeps_them_aligned(folder: Path) -> None:
    recording = Recording()
    resumed_after: list[int] = []

    def script() -> Generator[None]:
        yield from recording.capture(8)
        held_for = yield from recording.pause()
        recording.clock.advance_time(PAUSE)
        os.kill(os.getpid(), recorder.RESUME_SIGNAL)
        yield from recording.waiting()
        resumed_after.append(int(recording.clock.get_next_entry_time()) - held_for)
        yield from recording.capture(8)
        # End-of-stream ends a source's wait for the clock; one still making its buffer would
        # wait after it, on a clock that nothing moves while the program ends.
        yield from recording.waiting()
        os.kill(os.getpid(), signal.SIGINT)

    assert recording.run(script()) == 0

    # The sources go on where they stopped: to the pipeline, the pause took no time.
    assert resumed_after == [PAUSE]
    assert recorded(folder) == [(16 * BUFFER, True), (16 * BUFFER, True)]


def test_a_paused_recording_ends_with_what_it_captured_before_the_pause(folder: Path) -> None:
    recording = Recording()

    def script() -> Generator[None]:
        yield from recording.capture(8)
        yield from recording.pause()
        recording.clock.advance_time(PAUSE)
        os.kill(os.getpid(), signal.SIGINT)

    assert recording.run(script()) == 0

    assert recorded(folder) == [(8 * BUFFER, True), (8 * BUFFER, True)]


def test_a_recording_paused_with_a_page_on_its_way_to_its_file_still_ends_properly(
    folder: Path,
) -> None:
    recording = Recording()
    sinks: list[Gst.Element] = []
    recording.pipeline.iterate_sinks().foreach(sinks.append)
    paused = threading.Event()
    held: list[bool] = []

    def hold(_pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Holds the next page on its way to one track's file until the pipeline has paused."""
        held.append(True)
        paused.wait()
        return Gst.PadProbeReturn.REMOVE

    captured = 0

    def script() -> Generator[None]:
        nonlocal captured
        # Both files have begun, as they have once the recorder has started, and no page is
        # on its way.
        yield from recording.capture(1)
        yield from recording.waiting()
        yield from recording.until(
            lambda: all(sink.get_state(0)[1] == Gst.State.PLAYING for sink in sinks)
        )
        sinks[0].sinkpads[0].add_probe(Gst.PadProbeType.BUFFER, hold)
        captured = 1
        while not held:
            yield from recording.capture(1)
            captured += 1
            yield from recording.until(lambda: bool(held) or recording.clock.peek_id_count() == 2)
        # The source whose page is held waits for the clock no more; the other one waits again.
        yield from recording.until(lambda: recording.clock.peek_id_count() == 1)
        os.kill(os.getpid(), recorder.PAUSE_SIGNAL)
        # The sinks pause before the sources, and a paused source waits for no time.
        yield from recording.until(lambda: recording.clock.peek_id_count() == 0)
        paused.set()
        # The paused file holds the page, and the thread that brought it.
        prerolled = Gst.StateChangeReturn.SUCCESS
        yield from recording.until(lambda: sinks[0].get_state(0)[0] == prerolled)
        os.kill(os.getpid(), signal.SIGINT)

    assert recording.run(script()) == 0

    assert recorded(folder) == [(captured * BUFFER, True)] * len(store.TRACKS)


@pytest.mark.parametrize(("quiet_for", "said"), [(59, None), (60, after(0))])
def test_silence_on_both_tracks_as_long_as_the_reminder_says_since_when(
    silent_after_a_minute: str, quiet_for: int, said: str | None
) -> None:
    recording = Recording(QUIET, QUIET)

    def script() -> Generator[None]:
        yield from recording.capture(quiet_for)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert silent_since(silent_after_a_minute) == said


@pytest.mark.parametrize("track", ["mic", "call"])
def test_sound_on_either_track_is_no_silence(silent_after_a_minute: str, track: str) -> None:
    recording = Recording([*QUIET, "name=mic"], [*QUIET, "name=call"])

    def script() -> Generator[None]:
        yield from recording.capture(1)
        yield from recording.hear("sine", track)
        yield from recording.capture(90)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert silent_since(silent_after_a_minute) is None


def test_sound_ends_a_silence_and_the_next_one_is_said_again(silent_after_a_minute: str) -> None:
    recording = Recording([*QUIET, "name=mic"], QUIET)
    said: list[object] = []

    def saying(silence: object) -> Generator[None]:
        yield from recording.until(lambda: silent_since(silent_after_a_minute) == silence)
        said.append(silence)

    def script() -> Generator[None]:
        yield from recording.capture(60)
        yield from saying(after(0))
        yield from recording.hear("sine")
        # The buffer the microphone had made, still quiet, then one it heard.
        yield from recording.capture(2)
        yield from saying(None)
        yield from recording.hear("silence")
        # One more it heard, then a minute of silence again.
        yield from recording.capture(61)
        yield from saying(after(63 * Gst.SECOND))
        yield from recording.end()

    assert recording.run(script()) == 0

    assert said == [after(0), None, after(63 * Gst.SECOND)]
    assert recorded(Path.cwd()) == [(123 * SECOND, True), (123 * SECOND, True)]


@pytest.mark.parametrize(
    ("quiet_after", "said"), [(59, None), (60, after(PAUSE + 40 * Gst.SECOND))]
)
def test_a_pause_starts_the_silence_again_and_its_own_time_is_no_silence(
    silent_after_a_minute: str, quiet_after: int, said: str | None
) -> None:
    recording = Recording(QUIET, QUIET)

    def script() -> Generator[None]:
        yield from recording.capture(40)
        yield from recording.pause()
        recording.clock.advance_time(PAUSE)
        os.kill(os.getpid(), recorder.RESUME_SIGNAL)
        yield from recording.capture(quiet_after)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert silent_since(silent_after_a_minute) == said


def test_with_the_reminder_off_no_silence_is_said(silent_after_a_minute: str) -> None:
    settings.set_silence_reminder_minutes(None)
    recording = Recording(QUIET, QUIET)

    def script() -> Generator[None]:
        yield from recording.capture(120)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert silent_since(silent_after_a_minute) is None


def test_a_reminder_set_while_recording_counts_the_silence_already_heard(
    silent_after_a_minute: str,
) -> None:
    settings.set_silence_reminder_minutes(None)
    recording = Recording(QUIET, QUIET)

    def script() -> Generator[None]:
        yield from recording.capture(30)
        settings.set_silence_reminder_minutes(1)
        yield from recording.capture(30)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert silent_since(silent_after_a_minute) == after(0)


def test_a_silence_that_cannot_be_said_stops_no_recording(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The folder holds no recording for the silence to be said of.
    settings.set_silence_reminder_minutes(1)
    recording = Recording(QUIET, QUIET)

    def script() -> Generator[None]:
        yield from recording.capture(90)
        yield from recording.end()

    assert recording.run(script()) == 0

    assert recorded(folder) == [(90 * SECOND, True), (90 * SECOND, True)]
    # Said once, to the unit's journal, and not again each second the silence goes on.
    missing = store.recording_dir(folder.name) / "recording.json"
    errors = [line for line in capsys.readouterr().err.splitlines() if "Error" in line]
    assert errors == [f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'"]


def test_the_units_program_ends_both_tracks_properly_on_sigint(folder: Path) -> None:
    # Its first buffer reaches stdout once the pipeline plays, and so listens for signals.
    playing = [*LIVE, "num-buffers=1", "!", "fdsink"]
    with subprocess.Popen(  # noqa: S603 - the test's own command
        [sys.executable, "-m", "minutas.capture", *tracks(LIVE, LIVE), *playing],
        stdout=subprocess.PIPE,
    ) as program:
        assert program.stdout is not None
        assert len(program.stdout.read(1)) == 1
        program.send_signal(signal.SIGINT)

    assert program.returncode == 0
    assert [ended for _, ended in recorded(folder)] == [True, True]


def test_a_pipeline_that_fails_exits_1_and_says_why(
    folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = folder / "missing" / "mic.opus"

    assert capture.main(["audiotestsrc", "!", "filesink", f"location={missing}"]) == 1

    # Then where in GStreamer it failed.
    assert capsys.readouterr().err.splitlines()[:1] == [
        f'Could not open file "{missing}" for writing.'
    ]


def test_a_description_gstreamer_cannot_read_exits_1_and_says_why(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert capture.main(["nosuchelement", "!", "fakesink"]) == 1

    assert capsys.readouterr().err == 'no element "nosuchelement"\n'
