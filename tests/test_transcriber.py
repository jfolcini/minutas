import builtins
import contextlib
import gc
import io
import itertools
import json
import multiprocessing
import os
import pickle
import subprocess
import sys
import tarfile
import tracemalloc
import types
import weakref
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Self

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import transcriber
from minutas.transcriber import (
    MAX_TURN_SAMPLES,
    SAMPLE_RATE,
    Segment,
    TranscriberError,
    Turn,
    VoicedOnly,
    assign_speakers,
    main,
    merge_turns,
    model_file,
    number_speakers,
    read_words,
    segments,
    write_transcript,
)

SECOND = SAMPLE_RATE
REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
PARAKEET = "nvidia/parakeet-tdt-0.6b-v3"
PYANNOTE = "pyannote/speaker-diarization-community-1"
PARAKEET_NOT_FOUND = f"{PARAKEET} not found in ~/.cache/minutas/models; run install.sh"
PYANNOTE_NOT_FOUND = f"{PYANNOTE} not found in ~/.cache/minutas/models; run install.sh"
# Parakeet's archive in miniature: the files NeMo loads it from.
CHECKPOINT = {"model_config.yaml": b"config", "model_weights.ckpt": b"weights"}


def me(start: float, end: float) -> Turn:
    return Turn("Me", int(start * SECOND), int(end * SECOND))


def them(start: float, end: float, speaker: str = "SPEAKER_00") -> Turn:
    return Turn(speaker, int(start * SECOND), int(end * SECOND))


def span(start: float, end: float) -> tuple[int, int]:
    return int(start * SECOND), int(end * SECOND)


def test_a_speaker_who_goes_on_after_a_pause_keeps_the_turn() -> None:
    assert merge_turns([me(0, 1), me(2, 3)]) == [me(0, 3)]


def test_a_turn_grows_to_thirty_seconds_and_no_further() -> None:
    whole = Turn("Me", 20 * SECOND, 30 * SECOND)
    one_sample_over = Turn("Me", 20 * SECOND, 30 * SECOND + 1)

    assert merge_turns([me(0, 19), whole]) == [me(0, 30)]
    assert merge_turns([me(0, 19), one_sample_over]) == [me(0, 19), one_sample_over]


def test_the_thirty_seconds_count_from_the_turn_start_not_from_zero() -> None:
    assert merge_turns([me(100, 110), me(111, 129), me(131, 140)]) == [me(100, 129), me(131, 140)]


def test_the_other_speaker_starting_in_a_pause_ends_the_turn() -> None:
    segments = [me(0, 8), me(8.5, 20), them(8.2, 8.4)]

    assert merge_turns(segments) == [me(0, 8), them(8.2, 8.4), me(8.5, 20)]


def test_the_other_speaker_talking_over_a_turn_does_not_split_it() -> None:
    segments = [me(0, 8), me(8.5, 20), them(9, 9.5)]

    assert merge_turns(segments) == [me(0, 20), them(9, 9.5)]


def test_turns_come_out_in_time_order_across_both_tracks() -> None:
    mic = [me(11, 14), me(40, 42)]
    call = [them(4, 9), them(20, 30)]

    assert merge_turns([*mic, *call]) == [them(4, 9), me(11, 14), them(20, 30), me(40, 42)]


def test_no_segments_make_no_turns() -> None:
    assert merge_turns([]) == []


@st.composite
def track_segments(draw: st.DrawFn, speaker: str) -> list[Turn]:
    """Speech on one track: segments in time order that never overlap, none over the limit."""
    lengths = draw(st.lists(st.integers(1, MAX_TURN_SAMPLES), max_size=8))
    gaps = draw(st.lists(st.integers(0, 2 * MAX_TURN_SAMPLES), min_size=8, max_size=8))
    segments: list[Turn] = []
    position = 0
    for length, gap in zip(lengths, gaps, strict=False):
        segments.append(Turn(speaker, position + gap, position + gap + length))
        position += gap + length
    return segments


@given(track_segments("Me"), track_segments("SPEAKER_00"))
def test_merging_keeps_every_segment_in_order_within_the_limit(
    mic: list[Turn], call: list[Turn]
) -> None:
    turns = merge_turns([*mic, *call])

    assert [turn.start for turn in turns] == sorted(turn.start for turn in turns)
    assert all(turn.end - turn.start <= MAX_TURN_SAMPLES for turn in turns)
    for segment in [*mic, *call]:
        holders = [
            turn
            for turn in turns
            if turn.speaker == segment.speaker
            and turn.start <= segment.start
            and segment.end <= turn.end
        ]
        assert len(holders) == 1
    for earlier, later in itertools.pairwise(turns):
        if earlier.speaker == later.speaker:
            assert later.end - earlier.start > MAX_TURN_SAMPLES


def test_each_stretch_of_the_call_is_whose_pyannote_says_it_is() -> None:
    heard = [them(4, 16, "SPEAKER_01"), them(28, 40, "SPEAKER_00")]

    assert assign_speakers([span(4, 8), span(12, 16), span(28, 40)], heard) == [
        them(4, 8, "SPEAKER_01"),
        them(12, 16, "SPEAKER_01"),
        them(28, 40, "SPEAKER_00"),
    ]


def test_a_stretch_two_people_share_is_cut_half_way_through_the_pause_between_them() -> None:
    heard = [them(44, 47, "SPEAKER_02"), them(49, 52, "SPEAKER_01")]

    assert assign_speakers([span(44, 52)], heard) == [
        them(44, 48, "SPEAKER_02"),
        them(48, 52, "SPEAKER_01"),
    ]


def test_a_stretch_is_cut_once_for_every_change_of_speaker_in_it() -> None:
    heard = [them(0, 4, "A"), them(4, 6, "B"), them(6, 7, "B"), them(8, 20, "A"), them(30, 31, "C")]

    assert assign_speakers([span(1, 19)], heard) == [
        them(1, 4, "A"),
        them(4, 7.5, "B"),
        them(7.5, 19, "A"),
    ]


def test_a_stretch_that_starts_and_ends_where_the_speaker_changes_is_not_cut() -> None:
    heard = [them(0, 4, "A"), them(4, 8, "B"), them(8, 12, "A")]

    # A cut there would leave a turn with no audio in it.
    assert assign_speakers([span(4, 8)], heard) == [them(4, 8, "B")]


def test_speech_pyannote_missed_goes_to_whoever_it_heard_nearest_in_time() -> None:
    heard = [them(10, 20, "A"), them(40, 50, "B")]

    assert assign_speakers([span(1, 2), span(28, 29), span(31, 32), span(58, 59)], heard) == [
        them(1, 2, "A"),
        them(28, 29, "A"),
        them(31, 32, "B"),
        them(58, 59, "B"),
    ]


def test_a_moment_of_someone_else_between_two_speakers_cuts_nothing() -> None:
    quarter_second = SECOND // 4
    blip = Turn("B", 20 * SECOND, 20 * SECOND + quarter_second - 1)
    word = Turn("B", 20 * SECOND, 20 * SECOND + quarter_second)

    assert assign_speakers([span(10, 21)], [them(10, 20, "A"), blip, them(22, 30, "C")]) == [
        them(10, 21, "A")
    ]
    assert assign_speakers([span(10, 21)], [them(10, 20, "A"), word, them(22, 30, "C")]) == [
        them(10, 20, "A"),
        them(20, 21, "B"),
    ]


def test_a_call_on_which_pyannote_heard_nobody_has_one_speaker() -> None:
    nobody = [Turn("A", 3 * SECOND, 3 * SECOND + 100)]

    for heard in ([], nobody):
        assert assign_speakers([span(1, 2), span(5, 9)], heard) == [
            Turn("unheard", SECOND, 2 * SECOND),
            Turn("unheard", 5 * SECOND, 9 * SECOND),
        ]


def distance(turn: Turn, sample: int) -> int:
    return max(turn.start - sample, sample - turn.end, 0)


@given(
    track_segments("call"),
    track_segments("call"),
    st.lists(st.sampled_from(["A", "B", "C"]), min_size=8, max_size=8),
)
def test_assigning_tiles_every_stretch_with_the_nearest_speakers(
    speech: list[Turn], heard_spans: list[Turn], speakers: list[str]
) -> None:
    spans = [(turn.start, turn.end) for turn in speech]
    heard = [
        Turn(speaker, turn.start, turn.end)
        for turn, speaker in zip(heard_spans, speakers, strict=False)
        if turn.end - turn.start >= SECOND // 4
    ]
    blips = [
        Turn("blip", turn.start, turn.end)
        for turn in heard_spans
        if turn.end - turn.start < SECOND // 4
    ]

    turns = assign_speakers(spans, [*heard, *blips])

    tiles: list[Turn] = []
    for start, end in spans:
        pieces = [turn for turn in turns if start <= turn.start and turn.end <= end]
        tiles += pieces
        assert [pieces[0].start, *(piece.end for piece in pieces)] == [
            start,
            *(piece.start for piece in pieces[1:]),
            end,
        ]
        assert all(piece.start < piece.end for piece in pieces)
        assert all(
            earlier.speaker != later.speaker for earlier, later in itertools.pairwise(pieces)
        )
        for piece in pieces:
            assert piece.speaker in ({turn.speaker for turn in heard} or {"unheard"})
            for sample in (piece.start, piece.end - 1):
                nearest = min((distance(turn, sample) for turn in heard), default=0)
                own = [distance(turn, sample) for turn in heard if turn.speaker == piece.speaker]
                # Within a sample: half a pause of an odd length is not a whole number of them.
                assert min(own, default=0) <= nearest + 1
    assert tiles == turns


def test_the_calls_speakers_are_numbered_in_the_order_they_first_speak() -> None:
    speakers = ["SPEAKER_02", "Me", "SPEAKER_00", "SPEAKER_02", "Me", "SPEAKER_01"]

    assert number_speakers(speakers) == {
        "Me": "Me",
        "SPEAKER_02": "Speaker 1",
        "SPEAKER_00": "Speaker 2",
        "SPEAKER_01": "Speaker 3",
    }


def test_with_nobody_on_the_call_there_is_only_me() -> None:
    assert number_speakers(["Me", "Me"]) == {"Me": "Me"}
    assert number_speakers([]) == {"Me": "Me"}


@given(st.lists(st.sampled_from(["Me", "A", "B", "C", "D"])))
def test_numbering_counts_up_from_one_by_first_appearance(speakers: list[str]) -> None:
    numbered = number_speakers(speakers)

    on_the_call = list(dict.fromkeys(speaker for speaker in speakers if speaker != "Me"))
    assert numbered == {
        "Me": "Me",
        **{speaker: f"Speaker {place}" for place, speaker in enumerate(on_the_call, 1)},
    }


def test_segments_are_the_turns_with_words_under_their_speakers_keys() -> None:
    paragraphs = [
        (them(4, 9, "SPEAKER_01"), " Arrancamos con el release. "),
        (Turn("Me", 11 * SECOND, 225_001), "CI is green since this morning."),
        (them(15, 16, "SPEAKER_00"), "Dale."),
        (them(17, 18, "SPEAKER_01"), "Sí."),
    ]

    assert segments(paragraphs) == [
        {"start": 4.0, "end": 9.0, "speaker": "Speaker 1", "text": "Arrancamos con el release."},
        {"start": 11.0, "end": 14.063, "speaker": "Me", "text": "CI is green since this morning."},
        {"start": 15.0, "end": 16.0, "speaker": "Speaker 2", "text": "Dale."},
        {"start": 17.0, "end": 18.0, "speaker": "Speaker 1", "text": "Sí."},
    ]


def test_a_speaker_with_no_words_keeps_their_number_and_shows_no_segment() -> None:
    paragraphs = [
        (them(1, 2, "SPEAKER_00"), ""),
        (them(4, 9, "SPEAKER_01"), "Dale."),
        (me(11, 14), "  "),
    ]

    assert segments(paragraphs) == [
        {"start": 4.0, "end": 9.0, "speaker": "Speaker 2", "text": "Dale."}
    ]


def test_new_words_in_a_turn_leave_every_speakers_number_alone() -> None:
    turns = [them(1, 2, "SPEAKER_00"), them(4, 9, "SPEAKER_01"), them(10, 12, "SPEAKER_02")]

    before = segments(list(zip(turns, ["", "Dale.", "Sí."], strict=True)))
    after = segments(list(zip(turns, ["Eh.", "Dale.", "Sí."], strict=True)))

    assert [segment["speaker"] for segment in before] == ["Speaker 2", "Speaker 3"]
    assert [segment["speaker"] for segment in after] == ["Speaker 1", "Speaker 2", "Speaker 3"]


def test_words_are_one_phrase_per_line_without_blank_lines(tmp_path: Path) -> None:
    words = tmp_path / "glossary.txt"
    words.write_text("Añatuya\n\n  deploy  \n\t\nJavier Folcini\n", encoding="utf-8")

    assert read_words(words) == ["Añatuya", "deploy", "Javier Folcini"]


def test_no_words_file_means_no_words() -> None:
    assert read_words(None) == []


def test_a_words_file_that_is_not_there_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_words(tmp_path / "glossary.txt")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def pack(files: Mapping[str, bytes]) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(f"./{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return archive.getvalue()


def install_models(home: Path, revisions: str | None = None) -> tuple[Path, Path]:
    """Parakeet's weights and pyannote's pipeline, as install.sh leaves them."""
    models = home / ".cache" / "minutas" / "models"
    parakeet = models / "models--nvidia--parakeet-tdt-0.6b-v3" / "snapshots" / REVISION
    parakeet.mkdir(parents=True)
    (parakeet / "parakeet-tdt-0.6b-v3.nemo").write_bytes(pack(CHECKPOINT))
    pyannote = models / "models--pyannote--speaker-diarization-community-1/snapshots/3533c8cf"
    pyannote.mkdir(parents=True)
    (pyannote / "config.yaml").write_bytes(b"pipeline")
    (models / "revisions").write_text(
        revisions
        if revisions is not None
        else f"{PYANNOTE} 3533c8cf *\n{PARAKEET} {REVISION} *.nemo\n",
        encoding="utf-8",
    )
    return parakeet / "parakeet-tdt-0.6b-v3.nemo", pyannote / "config.yaml"


def test_model_file_is_in_the_pinned_revision_install_sh_fetched(home: Path) -> None:
    weights, pipeline = install_models(home)

    assert model_file(PARAKEET, "*.nemo") == weights
    assert model_file(PYANNOTE, "config.yaml") == pipeline


def test_model_file_ignores_a_snapshot_of_another_revision(home: Path) -> None:
    install_models(home, revisions=f"{PARAKEET} 0000000 *.nemo\n")

    with pytest.raises(TranscriberError) as error:
        model_file(PARAKEET, "*.nemo")

    assert str(error.value) == PARAKEET_NOT_FOUND


@pytest.mark.parametrize("revisions", ["", f"{PYANNOTE} 3533c8cf *\n"])
def test_model_file_needs_the_model_among_the_fetched_revisions(home: Path, revisions: str) -> None:
    install_models(home, revisions=revisions)

    with pytest.raises(TranscriberError) as error:
        model_file(PARAKEET, "*.nemo")

    assert str(error.value) == PARAKEET_NOT_FOUND


def test_model_file_needs_install_sh_to_have_finished(home: Path) -> None:
    (install_models(home)[0].parents[3] / "revisions").unlink()

    with pytest.raises(TranscriberError) as error:
        model_file(PARAKEET, "*.nemo")

    assert str(error.value) == PARAKEET_NOT_FOUND


def test_model_file_needs_the_file(home: Path) -> None:
    weights, pipeline = install_models(home)
    weights.unlink()
    pipeline.unlink()

    with pytest.raises(TranscriberError) as error:
        model_file(PARAKEET, "*.nemo")
    assert str(error.value) == PARAKEET_NOT_FOUND

    with pytest.raises(TranscriberError) as error:
        model_file(PYANNOTE, "config.yaml")
    assert str(error.value) == PYANNOTE_NOT_FOUND


SAID: list[Segment] = [
    {"start": 4.0, "end": 9.0, "speaker": "Speaker 1", "text": "Año nuevo."},
    {"start": 11.0, "end": 14.5, "speaker": "Me", "text": "Dale."},
]
SAID_AS_WRITTEN = """[
  {
    "start": 4.0,
    "end": 9.0,
    "speaker": "Speaker 1",
    "text": "Año nuevo."
  },
  {
    "start": 11.0,
    "end": 14.5,
    "speaker": "Me",
    "text": "Dale."
  }
]
"""


def test_transcript_replaces_the_old_one_and_leaves_nothing_else(tmp_path: Path) -> None:
    (tmp_path / "transcript.json").write_text("[]\n", encoding="utf-8")

    write_transcript(tmp_path, SAID)

    assert (tmp_path / "transcript.json").read_text(encoding="utf-8") == SAID_AS_WRITTEN
    assert [path.name for path in tmp_path.iterdir()] == ["transcript.json"]


def test_transcript_arrives_whole_by_a_rename_inside_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "transcript.json").write_text("[]\n", encoding="utf-8")
    renames = []
    rename = Path.replace

    def watched(source: Path, target: Path) -> Path:
        renames.append(
            (source.parent, source.read_text("utf-8"), target.name, target.read_text("utf-8"))
        )
        return rename(source, target)

    monkeypatch.setattr(Path, "replace", watched)

    write_transcript(tmp_path, SAID)

    assert renames == [(tmp_path, SAID_AS_WRITTEN, "transcript.json", "[]\n")]


WINDOW = 512
# What is said on each track of the fake recording, in seconds from its start. Every time is a
# multiple of four seconds, which is a whole number of VAD windows.
SPOKEN = {
    "mic.opus": {(20, 24): "CI is green since this morning.", (60, 64): "Dale."},
    "call.opus": {(4, 8): "", (12, 16): "", (28, 40): "", (44, 52): ""},
}
# Whom pyannote hears on the call. SPEAKER_00 says nothing Parakeet makes words of, yet keeps
# the number of their first turn; the last stretch is two people's.
HEARD = [
    (4.0, 16.0, "SPEAKER_01"),
    (28.0, 40.0, "SPEAKER_00"),
    (44.0, 47.0, "SPEAKER_02"),
    (49.0, 52.0, "SPEAKER_01"),
]
# What Parakeet makes of the turns that are not one stretch: two merged, and one cut in two.
TURNS = {
    ("call.opus", 4, 16): "Arrancamos con el release.",
    ("call.opus", 44, 48): "Sí, dale.",
    ("call.opus", 48, 52): "Perfecto.",
}
SEGMENTS = [
    {"start": 4.0, "end": 16.0, "speaker": "Speaker 1", "text": "Arrancamos con el release."},
    {"start": 20.0, "end": 24.0, "speaker": "Me", "text": "CI is green since this morning."},
    {"start": 44.0, "end": 48.0, "speaker": "Speaker 3", "text": "Sí, dale."},
    {"start": 48.0, "end": 52.0, "speaker": "Speaker 1", "text": "Perfecto."},
    {"start": 60.0, "end": 64.0, "speaker": "Me", "text": "Dale."},
]
MINE = [SEGMENTS[1], SEGMENTS[4]]
# The masks pyannote asks an embedding for: of a chunk nobody speaks in, and of one someone does.
SILENT, VOICED = [[0.0, 0.0, 0.0]], [[0.0, 1.0, 1.0]]


def transcript(folder: Path) -> object:
    return json.loads((folder / "transcript.json").read_text(encoding="utf-8"))


class Checkpoint:
    itself: "Checkpoint"


class CallAudio:
    """A whole track in memory, as heavy as the real one: four bytes a sample."""

    def __init__(self, frames: int) -> None:
        self.samples = bytearray(4 * frames)
        self.batched = False

    def __getitem__(self, _: None) -> Self:
        self.batched = True
        return self


class FakeEmbedding:
    """Stands in for pyannote's embedding model, which the pipeline asks about itself too."""

    dimension = 256
    min_num_samples = 400

    def __init__(self) -> None:
        self.embedded: list[tuple[str, list[list[float]]]] = []

    def __call__(self, waveforms: str, *, masks: list[list[float]]) -> str:
        self.embedded.append((waveforms, masks))
        return f"embeddings of {waveforms}"


class FakePipeline:
    """Stands in for pyannote's pipeline."""

    def __init__(self, stack: "FakeSpeechStack") -> None:
        self.stack = stack
        self.embedding_batch_size = 32
        # community-1's segmentation: a ten-second window, moved a tenth of it at a time.
        self._segmentation = types.SimpleNamespace(duration=10.0, step=1.0)
        self.model = FakeEmbedding()
        self._embedding: Callable[..., object] = self.model

    def __call__(self, file: dict[str, object]) -> types.SimpleNamespace:
        waveform = file["waveform"]
        assert isinstance(waveform, CallAudio)
        # As pyannote does: it asks what the model needs, then for every chunk's embeddings.
        needs = getattr(self._embedding, "min_num_samples", None)
        self.stack.diarizations.append(
            {
                "samples": len(waveform.samples) // 4,
                "batched": waveform.batched,
                "sample_rate": file["sample_rate"],
                "embedding_batch_size": self.embedding_batch_size,
                "window": (self._segmentation.duration, self._segmentation.step),
                "min_num_samples": needs,
                "embeddings": [
                    self._embedding("chunk 1", masks=SILENT),
                    self._embedding("chunk 2", masks=VOICED),
                ],
                "embedded": self.model.embedded,
                "in a process of its own": self.stack.apart,
            }
        )
        return types.SimpleNamespace(exclusive_speaker_diarization=self)

    def itertracks(self, *, yield_label: bool) -> Iterator[tuple[types.SimpleNamespace, str, str]]:
        assert yield_label
        for start, end, speaker in self.stack.heard:
            yield types.SimpleNamespace(start=start, end=end), "A", speaker


class FakeSpeechStack:
    """Stands in for soundfile, torch, silero_vad, pyannote, NeMo and omegaconf; records their use.

    A track's audio is its speech itself: a window is all ones where someone speaks.
    """

    def __init__(self) -> None:
        self.spoken: dict[str, dict[tuple[int, int], str]] = SPOKEN
        self.heard: list[tuple[float, float, str]] = HEARD
        self.seconds = 68
        self.opened: list[str] = []
        self.block_settings: list[dict[str, object]] = []
        self.torch_threads = 8
        self.inference = False
        self.onednn = True
        self.inference_in_vad: set[bool] = set()
        self.threads_in_vad: set[int] = set()
        self.silero_imported = False
        self.vad_rates: set[int] = set()
        self.vad_settings: list[dict[str, object]] = []
        self.telemetry_when_pyannote_is_imported: str | None = None
        self.pipelines: list[Path] = []
        self.processes: list[tuple[int, str]] = []
        self.apart = False
        self.diarizations: list[dict[str, object]] = []
        self.whole_reads: list[tuple[str, str]] = []
        self.restored: list[tuple[Path, str, dict[str, bytes]]] = []
        self.apart_when_parakeet_loads: bool | None = None
        self.traced_when_parakeet_loads = 0
        self.load_config = types.SimpleNamespace(mmap=False)
        self.checkpoint_mapped = False
        self.checkpoint: weakref.ref[Checkpoint] = weakref.ref(Checkpoint())
        self.decoding = types.SimpleNamespace(
            strategy="greedy_batch", greedy=types.SimpleNamespace()
        )
        self.decoding_changes: list[object] = []
        self.reads: list[tuple[str, int, int, str]] = []
        self.clip: list[float] = []
        self.transcriptions: list[dict[str, object]] = []

    # soundfile

    def sound_file(self, track: Path) -> "FakeSpeechStack":
        self.opened.append(track.name)
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.opened.append("process ended" if self.apart else "closed")
        self.apart = False

    @property
    def frames(self) -> int:
        return self.seconds * SECOND

    def blocks(self, **settings: object) -> Iterator[list[float]]:
        self.block_settings.append(settings)
        speech = self.spoken[self.opened[-1]]
        for start in range(0, self.frames, WINDOW):
            speaking = any(first * SECOND <= start < last * SECOND for first, last in speech)
            yield [float(speaking)] * WINDOW

    def read(
        self, track: Path, *, start: int | None = None, stop: int | None = None, dtype: str
    ) -> tuple[list[float] | CallAudio, int]:
        if start is None or stop is None:
            self.whole_reads.append((track.name, dtype))
            return CallAudio(self.frames), SAMPLE_RATE
        self.reads.append((track.name, start, stop, dtype))
        self.clip = [1.0] * (stop - start)
        return self.clip, SAMPLE_RATE

    # torch

    def get_num_threads(self) -> int:
        return self.torch_threads

    def set_num_threads(self, threads: int) -> None:
        self.torch_threads = threads

    @contextlib.contextmanager
    def inference_mode(self) -> Generator[None]:
        self.inference = True
        yield
        self.inference = False

    @contextlib.contextmanager
    def mkldnn_flags(self, *, enabled: bool) -> Generator[None]:
        self.onednn = enabled
        yield
        self.onednn = True

    # silero_vad

    def load_silero_vad(self) -> "FakeSpeechStack":
        return self

    def __call__(self, window: list[float], rate: int) -> types.SimpleNamespace:
        self.inference_in_vad.add(self.inference)
        self.threads_in_vad.add(self.torch_threads)
        self.vad_rates.add(rate)
        return types.SimpleNamespace(item=lambda: window[0])

    def get_speech_timestamps_from_probs(
        self, probabilities: list[float], **settings: object
    ) -> list[dict[str, int]]:
        self.vad_settings.append(settings)
        spans = []
        for window, probability in enumerate(probabilities):
            if probability and not (window and probabilities[window - 1]):
                spans.append({"start": window * WINDOW, "end": window * WINDOW})
            if probability:
                spans[-1]["end"] += WINDOW
        return spans

    # concurrent.futures: the work is done here, between the start and the end of "its process"

    def process_pool(
        self, max_workers: int, *, mp_context: multiprocessing.context.BaseContext
    ) -> "FakeSpeechStack":
        self.processes.append((max_workers, mp_context.get_start_method()))
        self.opened.append("process started")
        self.apart = True
        return self

    def submit(self, work: Callable[..., object], *arguments: object) -> Future[object]:
        done: Future[object] = Future()
        done.set_result(work(*arguments))
        return done

    # pyannote

    def from_pretrained(self, checkpoint: Path) -> FakePipeline:
        self.pipelines.append(checkpoint)
        return FakePipeline(self)

    # NeMo

    def restore_from(
        self,
        path: str,
        *,
        map_location: str,
        save_restore_connector: types.SimpleNamespace | None = None,
    ) -> "FakeSpeechStack":
        # As NeMo does: the files of the unpacked folder its connector names, if it is there;
        # without one, the archive, unpacked into a temporary folder.
        unpacked = Path(
            path if save_restore_connector is None else save_restore_connector.model_extracted_dir
        )
        files = {file.name: file.read_bytes() for file in unpacked.glob("*")}
        self.restored.append((unpacked, map_location, files))
        self.apart_when_parakeet_loads = self.apart
        if tracemalloc.is_tracing():
            self.traced_when_parakeet_loads, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
        self.checkpoint_mapped = self.load_config.mmap
        # As NeMo does, the checkpoint outlives the call in a reference cycle.
        checkpoint = Checkpoint()
        checkpoint.itself = checkpoint
        self.checkpoint = weakref.ref(checkpoint)
        return self

    @property
    def cfg(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(decoding=self.decoding)

    def change_decoding_strategy(self, decoding: object) -> None:
        self.decoding_changes.append(decoding)

    def transcribe(
        self, clips: list[list[float]], **settings: object
    ) -> list[types.SimpleNamespace]:
        assert len(clips) == 1
        assert clips[0] is self.clip
        track, start, stop, _ = self.reads[-1]
        self.transcriptions.append(
            {
                **settings,
                "threads": self.torch_threads,
                "inference": self.inference,
                "oneDNN": self.onednn,
                "checkpoint in memory": self.checkpoint() is not None,
            }
        )
        turn = (start // SECOND, stop // SECOND)
        text = TURNS.get((track, *turn)) or self.spoken[track].get(turn, "Palabra.")
        return [types.SimpleNamespace(text=text)]


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> FakeSpeechStack:
    fake = FakeSpeechStack()
    # The transcriber changes it in this very process; as it was again when the test ends.
    monkeypatch.setenv("PYANNOTE_METRICS_ENABLED", "true")
    monkeypatch.setattr(transcriber, "ProcessPoolExecutor", fake.process_pool)
    serialization = types.SimpleNamespace(config=types.SimpleNamespace(load=fake.load_config))
    modules = {
        "soundfile": {"SoundFile": fake.sound_file, "read": fake.read},
        "torch": {
            "get_num_threads": fake.get_num_threads,
            "set_num_threads": fake.set_num_threads,
            "inference_mode": fake.inference_mode,
            "from_numpy": lambda window: window,
            "any": lambda masks: any(any(mask) for mask in masks),
            "full": lambda shape, value: types.SimpleNamespace(numpy=lambda: (shape, value)),
            "nan": "not a number",
            "backends": types.SimpleNamespace(
                mkldnn=types.SimpleNamespace(flags=fake.mkldnn_flags)
            ),
            "utils": types.SimpleNamespace(serialization=serialization),
        },
        "torch.utils": {},
        "torch.utils.serialization": {},
        "silero_vad": {
            "load_silero_vad": fake.load_silero_vad,
            "get_speech_timestamps_from_probs": fake.get_speech_timestamps_from_probs,
        },
        "pyannote": {},
        "pyannote.audio": {"Pipeline": types.SimpleNamespace(from_pretrained=fake.from_pretrained)},
        "nemo": {},
        "nemo.collections": {},
        "nemo.collections.asr": {},
        "nemo.collections.asr.models": {
            "ASRModel": types.SimpleNamespace(restore_from=fake.restore_from)
        },
        "nemo.core": {},
        "nemo.core.connectors": {},
        "nemo.core.connectors.save_restore_connector": {
            "SaveRestoreConnector": types.SimpleNamespace
        },
        "omegaconf": {"open_dict": contextlib.nullcontext},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        vars(module).update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    real_import = builtins.__import__

    def importing(
        name: str,
        module_globals: Mapping[str, object] | None = None,
        module_locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> types.ModuleType:
        if name == "silero_vad" and not fake.silero_imported:
            # As the real one does, on its first import only: load_silero_vad is too late to read
            # torch's count.
            fake.silero_imported = True
            fake.torch_threads = 1
        if name == "pyannote.audio":
            # The real one reads it on import, and reports its runs unless it says not to.
            fake.telemetry_when_pyannote_is_imported = os.environ.get("PYANNOTE_METRICS_ENABLED")
        return real_import(name, module_globals, module_locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", importing)
    return fake


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    folder = tmp_path / "recordings" / "2026-11-18T13-05-42"
    folder.mkdir(parents=True)
    (folder / "mic.opus").write_bytes(b"OggS mic")
    (folder / "call.opus").write_bytes(b"OggS call")
    return folder


def test_main_writes_who_said_what_in_time_order(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    weights, pipeline = install_models(home)

    assert main([str(recording)]) == 0

    assert transcript(recording) == SEGMENTS
    assert stack.pipelines == [pipeline]
    # Beside the archive, in its revision's snapshot: a new revision is unpacked anew.
    assert stack.restored == [(weights.parent / "parakeet-tdt-0.6b-v3", "cpu", CHECKPOINT)]
    assert stack.decoding_changes == []


def test_later_transcriptions_load_the_unpacked_folder_as_it_is(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    weights, _ = install_models(home)
    main([str(recording)])
    unpacked = weights.parent / "parakeet-tdt-0.6b-v3"
    # A folder already there is loaded as it is, never unpacked over.
    (unpacked / "model_config.yaml").write_bytes(b"changed")

    assert main([str(recording)]) == 0

    assert stack.restored[1] == (unpacked, "cpu", {**CHECKPOINT, "model_config.yaml": b"changed"})


def test_an_unpack_that_fails_half_way_leaves_nothing_behind(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    weights, _ = install_models(home)
    # Its second file would land outside the folder: the unpack stops there, the first one out.
    weights.write_bytes(pack({"model_config.yaml": b"config", "../model_weights.ckpt": b"w"}))
    with pytest.raises(tarfile.OutsideDestinationError):
        main([str(recording)])
    # On a disk that filled, what it left would keep it full.
    assert [path.name for path in weights.parent.iterdir()] == [weights.name]
    weights.write_bytes(pack(CHECKPOINT))

    assert main([str(recording)]) == 0

    assert stack.restored == [(weights.parent / "parakeet-tdt-0.6b-v3", "cpu", CHECKPOINT)]


def test_speech_is_found_window_by_window_in_pieces_within_the_turn_limit(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.opened[:4] == ["mic.opus", "closed", "call.opus", "closed"]
    assert stack.block_settings == [{"blocksize": 512, "dtype": "float32", "fill_value": 0}] * 2
    assert stack.vad_rates == {16000}
    assert (
        stack.vad_settings
        == [
            {
                "sampling_rate": 16000,
                "max_speech_duration_s": 30,
                "audio_length_samples": 68 * SECOND,
            }
        ]
        * 2
    )


def test_pyannote_hears_the_whole_call_read_once(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.whole_reads == [("call.opus", "float32")]
    assert [(run["samples"], run["batched"], run["sample_rate"]) for run in stack.diarizations] == [
        (68 * SECOND, True, 16000)
    ]


def test_speaker_detection_killed_at_the_memory_limit_says_so(
    home: Path,
    stack: FakeSpeechStack,
    recording: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_models(home)

    def killed(_work: Callable[..., object], *_arguments: object) -> Future[object]:
        broken: Future[object] = Future()
        broken.set_exception(BrokenProcessPool("A process was terminated abruptly"))
        return broken

    monkeypatch.setattr(stack, "submit", killed)

    assert main([str(recording)]) == 1

    assert capsys.readouterr().err == "Speaker detection ran out of its memory limit\n"
    assert not (recording / "transcript.json").exists()
    assert stack.restored == []


def test_diarize_reaches_its_own_process_by_name() -> None:
    # A spawned process is sent the function as its module and name: a closure never arrives.
    sent = pickle.dumps(transcriber.diarize)

    assert pickle.loads(sent) is transcriber.diarize  # noqa: S301 - pickled on the line above


def test_a_sliver_a_speaker_change_cuts_off_a_stretch_is_not_transcribed(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    stack.spoken = {"mic.opus": {}, "call.opus": {(4, 12): ""}}
    # The change falls half-way through their pause, 50 ms before the stretch ends.
    stack.heard = [(4.0, 11.9, "SPEAKER_00"), (12.0, 16.0, "SPEAKER_01")]

    assert main([str(recording)]) == 0

    assert stack.reads == [("call.opus", 4 * SECOND, 191_200, "float32")]
    assert transcript(recording) == [
        {"start": 4.0, "end": 11.95, "speaker": "Speaker 1", "text": "Palabra."}
    ]


def test_pyannote_is_told_not_to_report_the_run_before_it_is_imported(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.telemetry_when_pyannote_is_imported == "false"


def test_pyannote_embeds_one_voice_at_a_time_and_only_where_someone_speaks(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    (run,) = stack.diarizations
    assert run["embedding_batch_size"] == 1
    assert run["embedded"] == [("chunk 2", VOICED)]
    assert run["embeddings"] == [((1, 256), "not a number"), "embeddings of chunk 2"]
    assert run["min_num_samples"] == 400


def test_pyannote_moves_its_ten_second_window_three_seconds_at_a_time(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    (run,) = stack.diarizations
    assert run["window"] == (10.0, 3.0)


def test_a_silent_batch_gets_one_empty_embedding_for_each_of_its_masks(
    stack: FakeSpeechStack,
) -> None:
    model = FakeEmbedding()

    embeddings = VoicedOnly(model)("chunk", masks=[*SILENT, *SILENT])

    assert embeddings == ((2, 256), "not a number")
    assert model.embedded == []
    assert stack.diarizations == []


@pytest.mark.usefixtures("stack")
def test_a_batch_in_which_anyone_speaks_goes_to_the_model_whole() -> None:
    model = FakeEmbedding()

    embeddings = VoicedOnly(model)("chunk", masks=[*SILENT, *VOICED])

    assert embeddings == "embeddings of chunk"
    assert model.embedded == [("chunk", [*SILENT, *VOICED])]


def test_pyannote_runs_in_a_process_of_its_own_that_ends_before_parakeet_loads(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    # One process, started afresh: forked, it would share the threads torch already runs here.
    assert stack.processes == [(1, "spawn")]
    assert [run["in a process of its own"] for run in stack.diarizations] == [True]
    assert stack.opened == [
        *("mic.opus", "closed", "call.opus", "closed"),
        *("process started", "process ended"),
    ]
    assert stack.apart_when_parakeet_loads is False


def test_parakeet_hears_one_turn_at_a_time_read_from_its_track(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.reads == [
        ("call.opus", 4 * SECOND, 16 * SECOND, "float32"),
        ("mic.opus", 20 * SECOND, 24 * SECOND, "float32"),
        ("call.opus", 28 * SECOND, 40 * SECOND, "float32"),
        ("call.opus", 44 * SECOND, 48 * SECOND, "float32"),
        ("call.opus", 48 * SECOND, 52 * SECOND, "float32"),
        ("mic.opus", 60 * SECOND, 64 * SECOND, "float32"),
    ]
    assert [(run["batch_size"], run["verbose"]) for run in stack.transcriptions] == [(1, False)] * 6


def test_parakeet_gets_the_threads_torch_had_before_silero_took_them(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert [run["threads"] for run in stack.transcriptions] == [8] * 6


def test_silero_steps_through_both_tracks_on_one_thread(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.threads_in_vad == {1}


def test_the_models_run_without_recording_gradients(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert stack.inference_in_vad == {True}
    assert [run["inference"] for run in stack.transcriptions] == [True] * 6


def test_parakeet_runs_without_the_cache_that_grows_with_every_new_turn_length(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)

    main([str(recording)])

    assert [run["oneDNN"] for run in stack.transcriptions] == [False] * 6


def test_the_checkpoint_is_mapped_and_gone_before_the_first_turn(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    # Only the transcriber's own collection may free the checkpoint.
    gc.disable()

    try:
        main([str(recording)])
    finally:
        gc.enable()

    assert stack.checkpoint_mapped is True
    assert [run["checkpoint in memory"] for run in stack.transcriptions] == [False] * 6


def test_memory_follows_the_longest_turn_not_the_length_of_the_meeting(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    # Ten minutes in which each side speaks four seconds out of every sixteen. The fake audio
    # is 38 MB of turns, 154 MB of VAD windows and 38 MB of call for pyannote, which takes it
    # whole; one turn is half a megabyte.
    stack.seconds = 600
    stack.spoken = {
        "mic.opus": {(start, start + 4): "Dale." for start in range(0, 600, 16)},
        "call.opus": {(start, start + 4): "Sí." for start in range(8, 600, 16)},
    }
    stack.heard = [(0.0, 600.0, "SPEAKER_00")]
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()

    try:
        assert main([str(recording)]) == 0
        # Since Parakeet loaded: the fake starts the peak again there.
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(stack.reads) == 75
    # Nothing of the call pyannote heard came back from its process.
    assert stack.traced_when_parakeet_loads - before < 1_000_000
    assert peak - before < 4_000_000


def test_main_boosts_the_words(home: Path, stack: FakeSpeechStack, recording: Path) -> None:
    install_models(home)
    words = home / "glossary.txt"
    words.write_text("Añatuya\ndeploy\n", encoding="utf-8")

    assert main([str(recording), "--words", str(words)]) == 0

    assert stack.decoding_changes == [stack.decoding]
    assert stack.decoding.strategy == "greedy_batch"
    assert vars(stack.decoding.greedy) == {
        "boosting_tree": {"key_phrases_list": ["Añatuya", "deploy"]},
        "boosting_tree_alpha": 1.0,
    }


def test_an_empty_words_file_boosts_nothing(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    words = home / "glossary.txt"
    words.write_text("\n", encoding="utf-8")

    assert main([str(recording), "--words", str(words)]) == 0

    assert stack.decoding_changes == []
    assert vars(stack.decoding.greedy) == {}


def test_a_recording_without_speech_gets_an_empty_transcript_and_loads_no_model(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    stack.spoken = {"mic.opus": {}, "call.opus": {}}

    assert main([str(recording)]) == 0

    assert (recording / "transcript.json").read_text(encoding="utf-8") == "[]\n"
    assert stack.processes == []
    assert stack.restored == []


def test_a_call_nobody_speaks_on_is_not_given_to_pyannote(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    stack.spoken = {"mic.opus": SPOKEN["mic.opus"], "call.opus": {}}

    assert main([str(recording)]) == 0

    assert transcript(recording) == MINE
    assert stack.processes == []
    assert stack.whole_reads == []


def test_a_track_that_is_missing_or_empty_is_left_out(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_models(home)
    (recording / "call.opus").unlink()

    assert main([str(recording)]) == 0
    assert transcript(recording) == MINE

    (recording / "call.opus").write_bytes(b"")

    assert main([str(recording)]) == 0
    assert transcript(recording) == MINE
    assert stack.opened == ["mic.opus", "closed"] * 2
    assert stack.processes == []


def test_a_folder_without_tracks_gets_an_empty_transcript(
    home: Path, stack: FakeSpeechStack, tmp_path: Path
) -> None:
    install_models(home)

    assert main([str(tmp_path)]) == 0

    assert transcript(tmp_path) == []
    assert stack.opened == []


@pytest.mark.parametrize("missing", [PARAKEET, PYANNOTE])
def test_a_missing_model_fails_with_the_fix_before_any_work(
    home: Path,
    stack: FakeSpeechStack,
    recording: Path,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    weights, pipeline = install_models(home)
    (weights if missing == PARAKEET else pipeline).unlink()

    assert main([str(recording)]) == 1

    assert capsys.readouterr().err.splitlines()[-1] == (
        f"{missing} not found in ~/.cache/minutas/models; run install.sh"
    )
    assert not (recording / "transcript.json").exists()
    assert stack.opened == []


def test_the_command_ends_its_stderr_with_the_reason_when_the_model_is_missing(
    tmp_path: Path, recording: Path
) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "minutas.transcriber", str(recording)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "HOME": str(tmp_path)},
    )

    assert result.returncode == 1
    assert result.stderr.splitlines()[-1] == PARAKEET_NOT_FOUND
    assert not (recording / "transcript.json").exists()
