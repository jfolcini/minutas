import os
import subprocess
import sys
from array import array
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

# Ubuntu builds its PyGObject for its own Python alone: under another, the joiner cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from minutas import join, recorder, store  # noqa: E402

# A deadlock inside GStreamer holds the main thread, where the default timeout's signal never
# runs: a timer thread ends the suite instead, with every thread's stack.
pytestmark = pytest.mark.timeout(method="thread")

# One buffer of a test source: 1024 samples at the tracks' 16 kHz, 3072 of Opus's 48 kHz.
BUFFER = 1024
OPUS_BUFFER = 3072
BEFORE, AFTER = 80, 40
SILENCE, TONE = "silence", "sine"


def run(description: list[str]) -> None:
    Gst.init(None)
    pipeline = cast("Gst.Pipeline", Gst.parse_launchv(description))
    pipeline.set_state(Gst.State.PLAYING)
    ended = pipeline.get_bus().timed_pop_filtered(
        Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    pipeline.set_state(Gst.State.NULL)
    assert ended is not None
    assert ended.type == Gst.MessageType.EOS


def record(name: str, buffers: int, wave: str) -> bytes:
    """An Ogg Opus file as the recorder writes one; returns its bytes."""
    source = ["audiotestsrc", f"num-buffers={buffers}", f"wave={wave}"]
    run(recorder.track_branch(source, name))
    return Path(name).read_bytes()


def pages(data: bytes) -> list[tuple[int, int, bytes]]:
    """Every whole Ogg page's flags, where its audio ends, and its stream's serial number."""
    whole: list[tuple[int, int, bytes]] = []
    at = 0
    while data[at : at + 4] == b"OggS":
        segments = data[at + 26]
        size = 27 + segments + sum(data[at + 27 : at + 27 + segments])
        if at + size > len(data):
            break
        end = int.from_bytes(data[at + 6 : at + 14], "little")
        whole.append((data[at + 5], end, data[at + 14 : at + 18]))
        at += size
    return whole


def audio_in(name: str) -> tuple[int, int, bool]:
    """The samples an Ogg Opus file holds in whole pages, how many streams it is made of, and
    whether the last was ended properly."""
    data = Path(name).read_bytes()
    whole = pages(data)
    # Opus's header says how many samples at the start are the encoder's own.
    header = data.index(b"OpusHead")
    pre_skip = int.from_bytes(data[header + 10 : header + 12], "little")
    flags, end, _ = whole[-1]
    return end - pre_skip, len({serial for _, _, serial in whole}), flags == 4


def first_loud_buffer(name: str) -> int:
    """The source buffer a file's tone starts in."""
    raw = "audio/x-raw,format=S16LE,rate=16000,channels=1"
    decoded = ["filesrc", f"location={name}", "!", "oggdemux", "!", "opusdec"]
    run([*decoded, "!", "audioconvert", "!", raw, "!", "filesink", "location=decoded.raw"])
    samples = array("h", Path("decoded.raw").read_bytes())
    Path("decoded.raw").unlink()
    return next(at for at, sample in enumerate(samples) if abs(sample) > 10000) // BUFFER


def files() -> list[str]:
    return sorted(path.name for path in Path().iterdir())


@pytest.fixture(autouse=True)
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recording's folder, where the worker runs the joiner."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def stopped_and_continued() -> dict[str, bytes]:
    """Both tracks, silent, and what each recorded after Continue, a tone."""
    return {
        **{name: record(name, BEFORE, SILENCE) for name in store.TRACKS},
        **{name: record(name, AFTER, TONE) for name in store.PARTS},
    }


def test_each_track_becomes_one_stream_of_what_it_held_and_then_its_part() -> None:
    stopped_and_continued()

    assert join.main() == 0

    assert files() == ["call.opus", "mic.opus"]
    for track in store.TRACKS:
        assert audio_in(track) == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)
        assert first_loud_buffer(track) == BEFORE


def test_a_part_cut_short_is_joined_as_far_as_it_decodes() -> None:
    recorded = stopped_and_continued()
    part = recorded["mic.continued.opus"]
    Path("mic.continued.opus").write_bytes(part[: len(part) * 2 // 3])
    kept, _, ended = audio_in("mic.continued.opus")
    assert (0 < kept < AFTER * OPUS_BUFFER, ended) == (True, False)

    assert join.main() == 0

    assert files() == ["call.opus", "mic.opus"]
    assert audio_in("mic.opus") == (BEFORE * OPUS_BUFFER + kept, 1, True)
    assert audio_in("call.opus") == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)


def headers_alone(data: bytes) -> bytes:
    """What a recorder killed before it wrote a page of audio leaves."""
    return data[: data.index(b"OggS", data.index(b"OpusTags"))]


@pytest.mark.parametrize(
    "spoil", [headers_alone, lambda _: os.urandom(5000)], ids=["headers alone", "garbage"]
)
def test_a_part_none_of_which_decodes_is_kept_and_its_track_transcribed_without_it(
    capfd: pytest.CaptureFixture[str], spoil: Callable[[bytes], bytes]
) -> None:
    recorded = stopped_and_continued()
    spoilt = spoil(recorded["call.continued.opus"])
    Path("call.continued.opus").write_bytes(spoilt)

    assert join.main() == 0

    assert files() == ["call.continued.opus", "call.opus", "mic.opus"]
    assert Path("call.opus").read_bytes() == recorded["call.opus"]
    assert Path("call.continued.opus").read_bytes() == spoilt
    assert audio_in("mic.opus") == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)
    assert capfd.readouterr().err.splitlines()[-1] == (
        "None of call.continued.opus decodes: call.opus is transcribed without it, "
        "and it is kept in the recording's folder."
    )


def test_no_track_is_replaced_until_the_joins_of_both_check_out(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    recorded = stopped_and_continued()
    decodes = join.length
    # The mic's join goes well, and the call's writes a file that does not decode.
    monkeypatch.setattr(
        join, "length", lambda name: 0 if name == "call.joined.opus" else decodes(name)
    )

    assert join.main() == 1

    assert {name: Path(name).read_bytes() for name in files()} == recorded
    assert capfd.readouterr().err.splitlines()[-1] == (
        "Could not join what was recorded after Continue onto call.opus: "
        "the joined file plays for 0.0 s, its parts for 7.7 s. "
        "Both are kept in the recording's folder."
    )


def test_a_track_that_ended_first_is_made_up_with_silence_so_the_parts_line_up() -> None:
    # A recorder that died wrote neither track's last page; the call's ended a page earlier.
    record("mic.opus", BEFORE, SILENCE)
    record("call.opus", BEFORE - 10, SILENCE)
    for name in store.PARTS:
        record(name, AFTER, TONE)

    assert join.main() == 0

    for track in store.TRACKS:
        assert audio_in(track) == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)
        assert first_loud_buffer(track) == BEFORE


def test_a_track_none_of_which_decodes_becomes_silence_and_then_its_part() -> None:
    recorded = stopped_and_continued()
    Path("call.opus").write_bytes(headers_alone(recorded["call.opus"]))

    assert join.main() == 0

    assert files() == ["call.opus", "mic.opus"]
    assert audio_in("call.opus") == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)
    assert first_loud_buffer("call.opus") == BEFORE


def test_a_joined_file_that_is_not_as_long_as_its_parts_replaces_nothing(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    recorded = stopped_and_continued()
    # Without its identity, the Ogg muxer drops the part: the join ends well, and short.
    monkeypatch.setattr(join, "END_TO_END", ["concat", "name=joined"])

    assert join.main() == 1

    assert {name: Path(name).read_bytes() for name in files()} == recorded
    assert capfd.readouterr().err.splitlines()[-1] == (
        "Could not join what was recorded after Continue onto mic.opus: "
        "the joined file plays for 5.1 s, its parts for 7.7 s. "
        "Both are kept in the recording's folder."
    )


def test_a_track_whose_part_holds_no_audio_stays_as_it_is() -> None:
    recorded = stopped_and_continued()
    # What a recorder that could not start leaves, and a join of it that was cancelled.
    Path("call.continued.opus").write_bytes(b"")
    Path("call.joined.opus").write_bytes(recorded["call.opus"][:1000])

    assert join.main() == 0

    assert files() == ["call.opus", "mic.opus"]
    assert Path("call.opus").read_bytes() == recorded["call.opus"]
    assert audio_in("mic.opus") == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)


def test_the_joiner_runs_as_a_program_in_the_recordings_folder(folder: Path) -> None:
    stopped_and_continued()

    joiner = subprocess.run(
        [sys.executable, "-m", "minutas.join"], cwd=folder, capture_output=True, check=False
    )

    assert (joiner.returncode, joiner.stderr) == (0, b"")
    assert files() == ["call.opus", "mic.opus"]
    assert audio_in("mic.opus") == ((BEFORE + AFTER) * OPUS_BUFFER, 1, True)
