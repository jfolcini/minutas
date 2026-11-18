import contextlib
import gc
import itertools
import os
import subprocess
import sys
import tracemalloc
import types
import weakref
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Self

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas.transcriber import (
    MAX_TURN_SAMPLES,
    SAMPLE_RATE,
    TranscriberError,
    Turn,
    format_timestamp,
    format_transcript,
    main,
    merge_turns,
    model_file,
    read_words,
    write_transcript,
)

SECOND = SAMPLE_RATE
REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
MODEL_NOT_FOUND = "Parakeet model not found in ~/.cache/minutas/models; run install.sh"


def me(start: float, end: float) -> Turn:
    return Turn("Me", int(start * SECOND), int(end * SECOND))


def them(start: float, end: float) -> Turn:
    return Turn("Speaker 1", int(start * SECOND), int(end * SECOND))


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


@given(track_segments("Me"), track_segments("Speaker 1"))
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


@pytest.mark.parametrize(
    ("samples", "stamp"),
    [
        (0, "00:00:00"),
        (4 * SECOND, "00:00:04"),
        (5 * SECOND - 1, "00:00:04"),
        (59 * SECOND, "00:00:59"),
        (60 * SECOND, "00:01:00"),
        (3599 * SECOND, "00:59:59"),
        (3661 * SECOND, "01:01:01"),
        (100 * 3600 * SECOND, "100:00:00"),
    ],
)
def test_timestamp_is_whole_seconds_from_the_recording_start(samples: int, stamp: str) -> None:
    assert format_timestamp(samples) == stamp


def test_transcript_is_one_paragraph_per_turn_with_a_blank_line_between() -> None:
    paragraphs = [
        (them(4, 9), "Arrancamos con el release."),
        (me(11, 14), "CI is green since this morning."),
    ]

    assert format_transcript(paragraphs) == (
        "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
        "\n"
        "[00:00:11] **Me:** CI is green since this morning.\n"
    )


def test_transcript_leaves_out_turns_with_no_words() -> None:
    paragraphs = [(me(1, 2), ""), (them(4, 9), " Dale. "), (me(11, 14), "  ")]

    assert format_transcript(paragraphs) == "[00:00:04] **Speaker 1:** Dale.\n"


def test_transcript_of_nothing_is_empty() -> None:
    assert format_transcript([]) == ""


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


def install_model(home: Path, revisions: str | None = None) -> Path:
    models = home / ".cache" / "minutas" / "models"
    snapshot = models / "models--nvidia--parakeet-tdt-0.6b-v3" / "snapshots" / REVISION
    snapshot.mkdir(parents=True)
    (snapshot / "parakeet-tdt-0.6b-v3.nemo").write_bytes(b"weights")
    pyannote = models / "models--pyannote--speaker-diarization-community-1/snapshots/3533c8cf"
    pyannote.mkdir(parents=True)
    (pyannote / "config.yaml").write_bytes(b"pipeline")
    (models / "revisions").write_text(
        revisions
        if revisions is not None
        else "pyannote/speaker-diarization-community-1 3533c8cf *\n"
        f"nvidia/parakeet-tdt-0.6b-v3 {REVISION} *.nemo\n",
        encoding="utf-8",
    )
    return snapshot / "parakeet-tdt-0.6b-v3.nemo"


def test_model_file_is_the_pinned_revision_install_sh_fetched(home: Path) -> None:
    installed = install_model(home)

    assert model_file() == installed


def test_model_file_ignores_a_snapshot_of_another_revision(home: Path) -> None:
    install_model(home, revisions="nvidia/parakeet-tdt-0.6b-v3 0000000 *.nemo\n")

    with pytest.raises(TranscriberError) as error:
        model_file()

    assert str(error.value) == MODEL_NOT_FOUND


@pytest.mark.parametrize("revisions", ["", "pyannote/speaker-diarization-community-1 3533c8cf *\n"])
def test_model_file_needs_parakeet_among_the_fetched_revisions(home: Path, revisions: str) -> None:
    install_model(home, revisions=revisions)

    with pytest.raises(TranscriberError) as error:
        model_file()

    assert str(error.value) == MODEL_NOT_FOUND


def test_model_file_needs_install_sh_to_have_finished(home: Path) -> None:
    (install_model(home).parents[3] / "revisions").unlink()

    with pytest.raises(TranscriberError) as error:
        model_file()

    assert str(error.value) == MODEL_NOT_FOUND


def test_model_file_needs_the_weights(home: Path) -> None:
    install_model(home).unlink()

    with pytest.raises(TranscriberError) as error:
        model_file()

    assert str(error.value) == MODEL_NOT_FOUND


def test_transcript_replaces_the_old_one_and_leaves_nothing_else(tmp_path: Path) -> None:
    (tmp_path / "transcript.md").write_text("old\n", encoding="utf-8")

    write_transcript(tmp_path, "[00:00:04] **Me:** Año nuevo.\n")

    assert (tmp_path / "transcript.md").read_text(encoding="utf-8") == (
        "[00:00:04] **Me:** Año nuevo.\n"
    )
    assert [path.name for path in tmp_path.iterdir()] == ["transcript.md"]


def test_transcript_arrives_whole_by_a_rename_inside_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "transcript.md").write_text("old\n", encoding="utf-8")
    renames = []
    rename = Path.replace

    def watched(source: Path, target: Path) -> Path:
        renames.append((source.parent, source.read_text(), target.name, target.read_text()))
        return rename(source, target)

    monkeypatch.setattr(Path, "replace", watched)

    write_transcript(tmp_path, "new\n")

    assert renames == [(tmp_path, "new\n", "transcript.md", "old\n")]


WINDOW = 512
# What is said on each track of the fake recording, in seconds from its start. Every time is a
# multiple of four seconds, which is a whole number of VAD windows.
SPOKEN = {
    "mic.opus": {(20, 24): "CI is green since this morning.", (60, 64): "Dale."},
    "call.opus": {(4, 8): "Arrancamos", (12, 16): "con el release.", (28, 40): ""},
}
# The call's first two stretches make one turn.
SPOKEN["call.opus"][4, 16] = "Arrancamos con el release."
TRANSCRIPT = (
    "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
    "\n"
    "[00:00:20] **Me:** CI is green since this morning.\n"
    "\n"
    "[00:01:00] **Me:** Dale.\n"
)


class Checkpoint:
    itself: "Checkpoint"


class FakeSpeechStack:
    """Stands in for soundfile, torch, silero_vad, NeMo and omegaconf; records how they are used.

    A track's audio is its speech itself: a window is all ones where someone speaks.
    """

    def __init__(self) -> None:
        self.spoken: dict[str, dict[tuple[int, int], str]] = SPOKEN
        self.seconds = 68
        self.opened: list[str] = []
        self.block_settings: list[dict[str, object]] = []
        self.torch_threads = 8
        self.inference = False
        self.onednn = True
        self.inference_in_vad: set[bool] = set()
        self.vad_rates: set[int] = set()
        self.vad_settings: list[dict[str, object]] = []
        self.restored: list[tuple[str, str]] = []
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
        self.opened.append("closed")

    @property
    def frames(self) -> int:
        return self.seconds * SECOND

    def blocks(self, **settings: object) -> Iterator[list[float]]:
        self.block_settings.append(settings)
        speech = self.spoken[self.opened[-1]]
        for start in range(0, self.frames, WINDOW):
            speaking = any(first * SECOND <= start < last * SECOND for first, last in speech)
            yield [float(speaking)] * WINDOW

    def read(self, track: Path, *, start: int, stop: int, dtype: str) -> tuple[list[float], int]:
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
        # The real silero_vad does this when it is imported.
        self.torch_threads = 1
        return self

    def __call__(self, window: list[float], rate: int) -> types.SimpleNamespace:
        self.inference_in_vad.add(self.inference)
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

    # NeMo

    def restore_from(self, path: str, *, map_location: str) -> "FakeSpeechStack":
        self.restored.append((path, map_location))
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
        text = self.spoken[track].get((start // SECOND, stop // SECOND), "Palabra.")
        return [types.SimpleNamespace(text=text)]


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> FakeSpeechStack:
    fake = FakeSpeechStack()
    serialization = types.SimpleNamespace(config=types.SimpleNamespace(load=fake.load_config))
    modules = {
        "soundfile": {"SoundFile": fake.sound_file, "read": fake.read},
        "torch": {
            "get_num_threads": fake.get_num_threads,
            "set_num_threads": fake.set_num_threads,
            "inference_mode": fake.inference_mode,
            "from_numpy": lambda window: window,
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
        "nemo": {},
        "nemo.collections": {},
        "nemo.collections.asr": {},
        "nemo.collections.asr.models": {
            "ASRModel": types.SimpleNamespace(restore_from=fake.restore_from)
        },
        "omegaconf": {"open_dict": contextlib.nullcontext},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        vars(module).update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
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
    installed = install_model(home)

    assert main([str(recording)]) == 0

    assert (recording / "transcript.md").read_text(encoding="utf-8") == TRANSCRIPT
    assert stack.restored == [(str(installed), "cpu")]
    assert stack.decoding_changes == []


def test_speech_is_found_window_by_window_in_pieces_within_the_turn_limit(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)

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


def test_parakeet_hears_one_turn_at_a_time_read_from_its_track(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)

    main([str(recording)])

    assert stack.reads == [
        ("call.opus", 4 * SECOND, 16 * SECOND, "float32"),
        ("mic.opus", 20 * SECOND, 24 * SECOND, "float32"),
        ("call.opus", 28 * SECOND, 40 * SECOND, "float32"),
        ("mic.opus", 60 * SECOND, 64 * SECOND, "float32"),
    ]
    assert [(run["batch_size"], run["verbose"]) for run in stack.transcriptions] == [(1, False)] * 4


def test_parakeet_gets_the_threads_torch_had_before_silero_took_them(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)

    main([str(recording)])

    assert [run["threads"] for run in stack.transcriptions] == [8] * 4


def test_the_models_run_without_recording_gradients(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)

    main([str(recording)])

    assert stack.inference_in_vad == {True}
    assert [run["inference"] for run in stack.transcriptions] == [True] * 4


def test_parakeet_runs_without_the_cache_that_grows_with_every_new_turn_length(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)

    main([str(recording)])

    assert [run["oneDNN"] for run in stack.transcriptions] == [False] * 4


def test_the_checkpoint_is_mapped_and_gone_before_the_first_turn(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)
    # Only the transcriber's own collection may free the checkpoint.
    gc.disable()

    try:
        main([str(recording)])
    finally:
        gc.enable()

    assert stack.checkpoint_mapped is True
    assert [run["checkpoint in memory"] for run in stack.transcriptions] == [False] * 4


def test_memory_follows_the_longest_turn_not_the_length_of_the_meeting(
    home: Path, stack: FakeSpeechStack, recording: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_model(home)
    # Ten minutes in which each side speaks four seconds out of every sixteen. The fake audio
    # is 38 MB of turns and 154 MB of VAD windows; one turn is half a megabyte.
    stack.seconds = 600
    stack.spoken = {
        "mic.opus": {(start, start + 4): "Dale." for start in range(0, 600, 16)},
        "call.opus": {(start, start + 4): "Sí." for start in range(8, 600, 16)},
    }
    tracemalloc.start()
    monkeypatch.setattr(stack, "transcriptions", [])
    before, _ = tracemalloc.get_traced_memory()

    try:
        assert main([str(recording)]) == 0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(stack.reads) == 75
    assert peak - before < 4_000_000


def test_main_boosts_the_words(home: Path, stack: FakeSpeechStack, recording: Path) -> None:
    install_model(home)
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
    install_model(home)
    words = home / "glossary.txt"
    words.write_text("\n", encoding="utf-8")

    assert main([str(recording), "--words", str(words)]) == 0

    assert stack.decoding_changes == []
    assert vars(stack.decoding.greedy) == {}


def test_a_recording_without_speech_gets_an_empty_transcript_and_loads_no_model(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)
    stack.spoken = {"mic.opus": {}, "call.opus": {}}

    assert main([str(recording)]) == 0

    assert (recording / "transcript.md").read_text(encoding="utf-8") == ""
    assert stack.restored == []


def test_a_track_that_is_missing_or_empty_is_left_out(
    home: Path, stack: FakeSpeechStack, recording: Path
) -> None:
    install_model(home)
    (recording / "call.opus").unlink()
    transcript = "[00:00:20] **Me:** CI is green since this morning.\n\n[00:01:00] **Me:** Dale.\n"

    assert main([str(recording)]) == 0
    assert (recording / "transcript.md").read_text(encoding="utf-8") == transcript

    (recording / "call.opus").write_bytes(b"")

    assert main([str(recording)]) == 0
    assert (recording / "transcript.md").read_text(encoding="utf-8") == transcript
    assert stack.opened == ["mic.opus", "closed"] * 2


def test_a_folder_without_tracks_gets_an_empty_transcript(
    home: Path, stack: FakeSpeechStack, tmp_path: Path
) -> None:
    install_model(home)

    assert main([str(tmp_path)]) == 0

    assert (tmp_path / "transcript.md").read_text(encoding="utf-8") == ""
    assert stack.opened == []


@pytest.mark.usefixtures("home")
def test_a_missing_model_fails_with_the_fix_and_writes_no_transcript(
    stack: FakeSpeechStack, recording: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([str(recording)]) == 1

    assert capsys.readouterr().err.splitlines()[-1] == MODEL_NOT_FOUND
    assert not (recording / "transcript.md").exists()
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
    assert result.stderr.splitlines()[-1] == MODEL_NOT_FOUND
    assert not (recording / "transcript.md").exists()
