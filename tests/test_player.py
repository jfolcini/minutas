import array
import itertools
import math
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

# Ubuntu builds its PyGObject for its own Python alone: under another, the player cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from minutas import player  # noqa: E402

# The tracks' rate, and how long each test track plays: 20 buffers of a tenth of a second.
RATE = 16000
SECONDS = 2.0
MIC_HZ, CALL_HZ = 440, 1000
# What a sound card would hear, handed to the test as 16-bit samples, as fast as it can take them;
# one buffer at a time, so the pipeline plays no further ahead than the test has listened.
LISTENER = [
    f"audio/x-raw,format=S16LE,rate={RATE},channels=1",
    "!",
    "appsink",
    "name=out",
    "sync=false",
    "max-buffers=1",
]
# scaletempo stretches in strides of a few tens of milliseconds; Opus pads by a frame or two.
SLACK_SECONDS = 0.05
SLACK_HZ = 5


def write(description: list[str]) -> None:
    pipeline = cast("Gst.Pipeline", Gst.parse_launchv(description))
    pipeline.set_state(Gst.State.PLAYING)
    ended = pipeline.get_bus().timed_pop_filtered(
        10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    pipeline.set_state(Gst.State.NULL)
    assert ended is not None
    assert ended.type == Gst.MessageType.EOS


def sine(file: Path, frequency: int) -> Path:
    """An Opus track as the recorder writes one, holding a sine for SECONDS."""
    write(
        [
            *("audiotestsrc", f"freq={frequency}", "samplesperbuffer=1600", "num-buffers=20"),
            *("!", f"audio/x-raw,rate={RATE},channels=1", "!", "opusenc", "bitrate=32000"),
            *("!", "oggmux", "!", "filesink", f"location={file}"),
        ]
    )
    return file


@pytest.fixture
def tracks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    Gst.init(None)
    monkeypatch.setattr(player, "SINK", LISTENER)
    # A space in the folder, as a home folder can hold one.
    folder = tmp_path / "a recording"
    folder.mkdir()
    return sine(folder / "mic.opus", MIC_HZ), sine(folder / "call.opus", CALL_HZ)


def prerolled(playing: player.Player) -> None:
    """Waits until the pipeline has done what it was last told, as a sound card would let it."""
    changed, _, _ = playing.pipeline.get_state(10 * Gst.SECOND)
    assert changed == Gst.StateChangeReturn.SUCCESS


def heard(playing: player.Player) -> list[tuple[float, array.array[int]]]:
    """What reaches the listener until the stream ends: each buffer's place in the recording,
    in seconds, and its samples."""
    out = playing.pipeline.get_by_name("out")
    assert out is not None
    buffers: list[tuple[float, array.array[int]]] = []
    while (sample := out.emit("try-pull-sample", 10 * Gst.SECOND)) is not None:
        buffer = sample.get_buffer()
        segment = sample.get_segment()
        samples = array.array("h")
        samples.frombytes(buffer.extract_dup(0, buffer.get_size()))
        place = segment.to_stream_time(Gst.Format.TIME, buffer.pts) / Gst.SECOND
        buffers.append((place, samples))
    return buffers


def joined(buffers: list[tuple[float, array.array[int]]]) -> array.array[int]:
    return array.array("h", [value for _, samples in buffers for value in samples])


def pitch(samples: array.array[int]) -> float:
    """The frequency of a sine, from how often it crosses zero, away from its edges."""
    middle = samples[len(samples) // 4 : -len(samples) // 4]
    pairs = itertools.pairwise(middle)
    crossings = sum(1 for before, after in pairs if (before < 0) != (after < 0))
    return crossings / 2 / (len(middle) / RATE)


def power(samples: array.array[int], frequency: int) -> float:
    """How much of the samples is at the frequency (Goertzel)."""
    coefficient = 2 * math.cos(2 * math.pi * frequency / RATE)
    previous = before = 0.0
    for value in samples:
        previous, before = value + coefficient * previous - before, previous
    return previous**2 + before**2 - coefficient * previous * before


def until(happened: Callable[[], bool]) -> None:
    """Runs the main loop, where the player says how it ended, until something has happened.

    It waits on the loop's own events, never a sleep, and gives up, failing, after far longer than
    any of them takes.
    """
    context = GLib.MainContext.default()
    gave_up: list[bool] = []

    def give_up() -> bool:
        gave_up.append(True)
        return GLib.SOURCE_REMOVE

    deadline = GLib.timeout_add_seconds(10, give_up)
    while not happened() and not gave_up:
        context.iteration(may_block=True)
    if not gave_up:
        GLib.source_remove(deadline)
    assert happened()


def test_a_track_plays_to_its_end_and_says_so(tracks: tuple[Path, Path]) -> None:
    mic, _ = tracks
    ended: list[str | None] = []
    playing = player.Player([mic], ended.append)

    playing.play()
    samples = joined(heard(playing))
    until(lambda: ended != [])

    assert len(samples) / RATE == pytest.approx(SECONDS, abs=SLACK_SECONDS)
    assert pitch(samples) == pytest.approx(MIC_HZ, abs=SLACK_HZ)
    assert ended == [None]
    assert playing.pipeline.get_state(0)[1] == Gst.State.NULL


def test_at_twice_the_speed_a_track_plays_in_half_the_time_at_its_own_pitch(
    tracks: tuple[Path, Path],
) -> None:
    mic, _ = tracks
    playing = player.Player([mic], [].append)
    playing.pause()
    prerolled(playing)

    playing.set_speed(2.0)
    prerolled(playing)
    playing.play()
    buffers = heard(playing)
    samples = joined(buffers)

    # The whole recording went by, in half the time it takes at normal speed; a voice sped up by
    # playing its samples faster would be an octave up, at twice the frequency.
    assert buffers[-1][0] == pytest.approx(SECONDS, abs=SLACK_SECONDS * 2)
    assert len(samples) / RATE == pytest.approx(SECONDS / 2, abs=SLACK_SECONDS)
    assert pitch(samples) == pytest.approx(MIC_HZ, abs=SLACK_HZ)


def test_a_change_of_speed_mid_play_carries_on_where_it_was(tracks: tuple[Path, Path]) -> None:
    mic, _ = tracks
    playing = player.Player([mic], [].append)
    playing.play()
    out = playing.pipeline.get_by_name("out")
    assert out is not None
    first_half = 0
    while first_half < RATE * SECONDS / 2:
        sample = out.emit("try-pull-sample", 10 * Gst.SECOND)
        assert sample is not None
        first_half += sample.get_buffer().get_size() // 2
    at, length = playing.position()

    playing.set_speed(1.5)
    after = heard(playing)

    assert length == pytest.approx(SECONDS, abs=SLACK_SECONDS)
    assert at == pytest.approx(SECONDS / 2, abs=SLACK_SECONDS * 2)
    # The first buffer at the new speed is where the old one left off, and what was left plays
    # in two thirds of its time.
    assert after[0][0] == pytest.approx(at, abs=SLACK_SECONDS)
    assert len(joined(after)) / RATE == pytest.approx((SECONDS - at) / 1.5, abs=SLACK_SECONDS * 2)
    assert pitch(joined(after)) == pytest.approx(MIC_HZ, abs=SLACK_HZ)


def test_a_seek_goes_to_the_place_at_the_speed_chosen(tracks: tuple[Path, Path]) -> None:
    mic, _ = tracks
    playing = player.Player([mic], [].append)
    playing.pause()
    prerolled(playing)
    playing.set_speed(2.0)
    prerolled(playing)

    playing.seek(1.5)
    prerolled(playing)

    assert playing.position()[0] == pytest.approx(1.5, abs=SLACK_SECONDS)
    playing.play()
    buffers = heard(playing)
    assert buffers[0][0] == pytest.approx(1.5, abs=SLACK_SECONDS)
    assert len(joined(buffers)) / RATE == pytest.approx(0.25, abs=SLACK_SECONDS)


def test_both_tracks_play_mixed_together(tracks: tuple[Path, Path]) -> None:
    playing = player.Player(tracks, [].append)

    playing.play()
    samples = joined(heard(playing))

    assert len(samples) / RATE == pytest.approx(SECONDS, abs=SLACK_SECONDS)
    # Each is there, far above a frequency neither holds.
    silence = power(samples, 700)
    assert power(samples, MIC_HZ) > 1000 * silence
    assert power(samples, CALL_HZ) > 1000 * silence


def test_a_paused_player_holds_its_place(tracks: tuple[Path, Path]) -> None:
    mic, _ = tracks
    playing = player.Player([mic], [].append)
    playing.pause()
    prerolled(playing)
    playing.seek(0.5)
    prerolled(playing)

    assert playing.pipeline.get_state(0)[1] == Gst.State.PAUSED
    assert playing.position() == pytest.approx((0.5, SECONDS), abs=SLACK_SECONDS)


def test_a_stopped_player_lets_go_and_says_nothing(tracks: tuple[Path, Path]) -> None:
    mic, _ = tracks
    ended: list[str | None] = []
    playing = player.Player([mic], ended.append)
    playing.play()
    prerolled(playing)

    playing.stop()
    playing.stop()
    for _ in range(100):
        GLib.MainContext.default().iteration(may_block=False)

    assert playing.pipeline.get_state(0)[1] == Gst.State.NULL
    assert ended == []


def test_a_track_that_cannot_be_played_says_why(tmp_path: Path) -> None:
    Gst.init(None)
    broken = tmp_path / "mic.opus"
    broken.write_bytes(b"OggS not really")
    ended: list[str | None] = []
    playing = player.Player([broken], ended.append)

    playing.play()
    until(lambda: ended != [])

    [said] = ended
    assert said is not None
    assert "decode" in said.lower() or "stream" in said.lower()
    assert playing.pipeline.get_state(0)[1] == Gst.State.NULL


def opus_head(file: Path) -> tuple[int, int]:
    """The channels and input rate an Ogg Opus file's header gives."""
    data = file.read_bytes()
    header = data.index(b"OpusHead")
    channels = data[header + 9]
    rate = int.from_bytes(data[header + 12 : header + 16], "little")
    return channels, rate


def test_save_mix_writes_both_tracks_as_publish_to_agaric_mixes_them(
    tracks: tuple[Path, Path], tmp_path: Path
) -> None:
    saved: list[str | None] = []
    copy = tmp_path / "a copy" / "mixed.opus"
    copy.parent.mkdir()

    player.save_mix(tracks, copy, saved.append)
    until(lambda: saved != [])

    assert saved == [None]
    # The tracks' own format, as the published mix has it.
    assert opus_head(copy) == (1, RATE)
    decoded = player.Player([copy], [].append)
    decoded.play()
    samples = joined(heard(decoded))
    assert len(samples) / RATE == pytest.approx(SECONDS, abs=SLACK_SECONDS)
    silence = power(samples, 700)
    assert power(samples, MIC_HZ) > 1000 * silence
    assert power(samples, CALL_HZ) > 1000 * silence


def test_a_mix_that_cannot_be_written_says_why(tracks: tuple[Path, Path], tmp_path: Path) -> None:
    saved: list[str | None] = []

    player.save_mix(tracks, tmp_path / "missing" / "mixed.opus", saved.append)
    until(lambda: saved != [])

    [said] = saved
    assert said is not None
    assert "mixed.opus" in said
