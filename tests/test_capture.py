import os
import signal
import subprocess
import sys
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import cast

import pytest

# Ubuntu builds its PyGObject for its own Python alone: under another, the recorder cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gst", "1.0")
gi.require_version("GstCheck", "1.0")
from gi.repository import (  # noqa: E402
    GLib,
    Gst,
    GstCheck,  # ty: ignore[missing-module-member] - pygobject-stubs has no GstCheck
)

from minutas import capture, recorder, store  # noqa: E402

# A source that captures in real time, as pulsesrc does, a buffer at a time.
LIVE = ["audiotestsrc", "is-live=true"]
# One buffer of a source, 1024 samples at the tracks' 16 kHz, in samples of Opus's 48 kHz.
BUFFER = 3072
PAUSE = 5 * Gst.SECOND
# A test's whole recording takes a fraction of a second, none of it waiting.
GIVE_UP_SECONDS = 10


def tracks(source: list[str]) -> list[str]:
    """The recorder's pipeline, with the source in place of the microphone and the call."""
    return [name for track in store.TRACKS for name in recorder.track_branch(source, track)]


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


class Recording:
    """The recorder's pipeline on a clock that only moves when told to, so a live source
    captures a buffer exactly when the test lets it; the signals are real.
    """

    def __init__(self) -> None:
        Gst.init(None)
        self.pipeline = cast("Gst.Pipeline", Gst.parse_launchv(tracks(LIVE)))
        self.clock = GstCheck.TestClock.new()
        self.pipeline.use_clock(self.clock)

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
        exit_status = capture.run(self.pipeline)
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


def test_the_units_program_ends_both_tracks_properly_on_sigint(folder: Path) -> None:
    # Its first buffer reaches stdout once the pipeline plays, and so listens for signals.
    playing = [*LIVE, "num-buffers=1", "!", "fdsink"]
    with subprocess.Popen(  # noqa: S603 - the test's own command
        [sys.executable, "-m", "minutas.capture", *tracks(LIVE), *playing],
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
