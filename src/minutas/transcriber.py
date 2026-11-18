"""Writes a recording's transcript.json from its two tracks; runs in the speech environment.

    <speech-python> -P transcriber.py <recording-folder> [--words <file>]

Silero VAD finds the speech on each track. The mic's is mine; pyannote community-1 says whose
each stretch of the call's is. A speaker's consecutive stretches merge into turns of up to
MAX_TURN_SECONDS, and Parakeet transcribes each turn alone, because it detects the language per
input. pyannote runs in a process of its own, so all it took is given back before Parakeet,
the heavy one, loads.

transcript.json lists the turns that have words, in time order:

    [{"start": 4.0, "end": 16.0, "speaker": "Speaker 1", "text": "Arrancamos con el release."}]

start and end are seconds into the recording. speaker is a key, not a name: "Me" for the mic,
and "Speaker 1", "Speaker 2"… for the call's speakers in the order they first speak. The app
writes transcript.md from it, under the names the speakers have there. On failure the last line
of stderr is the reason.

The speech stack is imported inside the functions that use it: the app's Python and the tests
import this module without it.
"""

import argparse
import gc
import itertools
import json
import multiprocessing
import os
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Sequence, Sized
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

# What the recorder writes, and all the speech models take.
SAMPLE_RATE = 16_000
MAX_TURN_SECONDS = 30
MAX_TURN_SAMPLES = MAX_TURN_SECONDS * SAMPLE_RATE
# The window Silero's model takes at this rate.
VAD_WINDOW = 512
# The shortest sound Silero takes for speech.
SHORTEST_SPEECH_SAMPLES = SAMPLE_RATE // 4
MIC, CALL = "mic.opus", "call.opus"
ME = "Me"
# Whose the call's speech is when pyannote heard nobody on it.
UNHEARD = "unheard"
PARAKEET = "nvidia/parakeet-tdt-0.6b-v3"
PYANNOTE = "pyannote/speaker-diarization-community-1"
# NeMo's weight for the boosted phrases. Higher, it starts writing them where nobody said them.
BOOSTING_WEIGHT = 1.0


class TranscriberError(Exception):
    """Its message is the one line a person acts on."""


# The worker says the same of the transcriber when the limit kills it instead.
DIARIZATION_KILLED = "Speaker detection ran out of its memory limit"


@dataclass(frozen=True)
class Turn:
    """A stretch of one speaker's speech, in samples from the start of the recording."""

    speaker: str
    start: int
    end: int


class Segment(TypedDict):
    start: float
    end: float
    speaker: str
    text: str


def assign_speakers(spans: Iterable[tuple[int, int]], heard: Iterable[Turn]) -> list[Turn]:
    """Gives each stretch of the call's speech to whoever pyannote heard nearest in time.

    A stretch two people share is cut where the speaker changes: half-way through the pause
    between them. Where one speaker gives way to the next pyannote hears a moment of someone
    else: too short to be speech, it changes nothing. Cut there, the next turn starts with the
    end of somebody else's word, and Parakeet has answered that with nothing at all.
    """
    in_order = sorted(
        (turn for turn in heard if turn.end - turn.start >= SHORTEST_SPEECH_SAMPLES),
        key=lambda turn: turn.start,
    )
    # Where each speaker's part of the call ends; the last one's never does.
    changes = [
        ((earlier.end + later.start) // 2, earlier.speaker)
        for earlier, later in itertools.pairwise(in_order)
        if earlier.speaker != later.speaker
    ]
    last = in_order[-1].speaker if in_order else UNHEARD
    turns: list[Turn] = []
    for start, end in spans:
        cut = start
        for change, speaker in changes:
            if cut < change < end:
                turns.append(Turn(speaker, cut, change))
                cut = change
        speaker = next((speaker for change, speaker in changes if change >= end), last)
        turns.append(Turn(speaker, cut, end))
    return turns


def merge_turns(segments: Iterable[Turn]) -> list[Turn]:
    """Joins a speaker's segments into turns until someone else starts or the turn is full.

    Short words alone ("dale", "OK") flip Parakeet's language detection; inside a turn they don't.
    """
    turns: list[Turn] = []
    for segment in sorted(segments, key=lambda segment: segment.start):
        if (
            turns
            and turns[-1].speaker == segment.speaker
            and segment.end - turns[-1].start <= MAX_TURN_SAMPLES
        ):
            turns[-1] = Turn(segment.speaker, turns[-1].start, segment.end)
        else:
            turns.append(segment)
    return turns


def number_speakers(speakers: Iterable[str]) -> dict[str, str]:
    """The key of each speaker: the call's are numbered in the order they first speak."""
    keys = {ME: ME}
    for speaker in speakers:
        keys.setdefault(speaker, f"Speaker {len(keys)}")
    return keys


def segments(paragraphs: Sequence[tuple[Turn, str]]) -> list[Segment]:
    """One segment per turn that has words, the call's speakers numbered by their first turn.

    Numbered by their words instead, a turn that Run again's new words turn from nothing into
    "Eh." would move every later speaker's name onto someone else's lines.
    """
    keys = number_speakers(turn.speaker for turn, _ in paragraphs)
    spoken = [(turn, text.strip()) for turn, text in paragraphs if text.strip()]
    return [
        Segment(
            start=round(turn.start / SAMPLE_RATE, 3),
            end=round(turn.end / SAMPLE_RATE, 3),
            speaker=keys[turn.speaker],
            text=text,
        )
        for turn, text in spoken
    ]


def read_words(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def model_file(repository: str, file: str) -> Path:
    """A model's file at the revision install.sh pins, from the list it writes when done."""
    models = Path.home() / ".cache" / "minutas" / "models"
    revisions = models / "revisions"
    fetched = revisions.read_text(encoding="utf-8").splitlines() if revisions.is_file() else []
    for listed, revision, _ in (line.split() for line in fetched):
        if listed == repository:
            snapshot = models / f"models--{repository.replace('/', '--')}" / "snapshots" / revision
            for found in snapshot.glob(file):
                return found
    msg = f"{repository} not found in ~/.cache/minutas/models; run install.sh"
    raise TranscriberError(msg)


def unpacked(archive: Path) -> Path:
    """The archive's folder beside it, in its revision's snapshot, unpacked the first time.

    Given the archive itself, NeMo unpacks the whole model to disk again on every load.
    """
    folder = archive.with_suffix("")
    if not folder.is_dir():
        # Renamed once whole, so an unpack stopped half-way leaves no folder that looks done;
        # the next one writes every file again over what it left.
        partial = folder.with_name(f"{folder.name}.partial")
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(partial, filter="data")
        except BaseException:
            # On a disk that filled, what it left would keep it full for the next recording.
            shutil.rmtree(partial, ignore_errors=True)
            raise
        partial.rename(folder)
    return folder


def write_transcript(folder: Path, transcript: Sequence[Segment]) -> None:
    # A rename, so a transcriber stopped half-way leaves the previous transcript or none.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=".transcript.json.", suffix=".tmp", delete=False
    ) as tmp:
        json.dump(transcript, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    Path(tmp.name).replace(folder / "transcript.json")


def find_speech(track: Path) -> list[tuple[int, int]]:
    """Where Silero VAD hears speech on a track, reading it one window at a time."""
    if not track.is_file() or track.stat().st_size == 0:
        return []
    import soundfile
    import torch

    threads = torch.get_num_threads()
    # Importing silero_vad sets torch to one thread, and the other models would be left with it.
    import silero_vad

    # Only its first import does: the second track would step through on every thread, at the
    # same speed, for several times the CPU.
    torch.set_num_threads(1)
    vad = silero_vad.load_silero_vad()
    with soundfile.SoundFile(track) as audio, torch.inference_mode():
        probabilities = [
            vad(torch.from_numpy(window), SAMPLE_RATE).item()
            for window in audio.blocks(blocksize=VAD_WINDOW, dtype="float32", fill_value=0)
        ]
        # Silero cuts speech longer than a turn at a pause.
        spans = silero_vad.get_speech_timestamps_from_probs(
            probabilities,
            sampling_rate=SAMPLE_RATE,
            max_speech_duration_s=MAX_TURN_SECONDS,
            audio_length_samples=audio.frames,
        )
    torch.set_num_threads(threads)
    return [(span["start"], span["end"]) for span in spans]


class VoicedOnly:
    """pyannote's embedding model, kept from running where it has nobody to hear.

    pyannote asks it for three speakers in every ten seconds of the call, of whom one or two
    speak. For a silent one the model answers with no embedding, at the cost of a real one.
    """

    def __init__(self, embedding: Callable[..., object]) -> None:
        self.embedding = embedding

    def __getattr__(self, name: str) -> object:
        # The pipeline asks the model about itself too.
        return getattr(self.embedding, name)

    def __call__(self, waveforms: object, masks: Sized) -> object:
        import torch

        if torch.any(masks):
            return self.embedding(waveforms, masks=masks)
        return torch.full((len(masks), self.dimension), torch.nan).numpy()


def diarize(pipeline_file: Path, call: Path) -> list[Turn]:
    """Whom pyannote community-1 hears on the call, and when: one speaker at a time."""
    # Read when pyannote is imported: left alone, it reports every run to its makers.
    os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
    import soundfile
    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(pipeline_file)
    # On a CPU one embedding at a time is the fastest, and takes the least memory.
    pipeline.embedding_batch_size = 1
    # Its ten-second window moves one second at a time, so it embeds every second of the call ten
    # times over, most of its work. Moved three seconds, it tells the speakers apart as well; five,
    # and it starts to mix them up.
    pipeline._segmentation.step = 3.0  # noqa: SLF001 - only its constructor takes the step
    pipeline._embedding = VoicedOnly(pipeline._embedding)  # noqa: SLF001 - it has no setting for this
    # pyannote takes the call whole.
    audio, _ = soundfile.read(call, dtype="float32")
    heard = pipeline({"waveform": torch.from_numpy(audio)[None], "sample_rate": SAMPLE_RATE})
    return [
        Turn(speaker, round(segment.start * SAMPLE_RATE), round(segment.end * SAMPLE_RATE))
        for segment, _, speaker in heard.exclusive_speaker_diarization.itertracks(yield_label=True)
    ]


def recognize(
    weights: Path, folder: Path, turns: Iterable[Turn], words: Sequence[str]
) -> list[str]:
    """Each turn's text from Parakeet, with the words boosted."""
    import soundfile
    import torch.utils.serialization
    from nemo.collections.asr.models import ASRModel
    from nemo.core.connectors.save_restore_connector import SaveRestoreConnector
    from omegaconf import open_dict

    checkpoint = unpacked(weights)
    connector = SaveRestoreConnector()
    connector.model_extracted_dir = str(checkpoint)
    # Read into memory, the checkpoint would sit beside the model it fills: twice the model.
    torch.utils.serialization.config.load.mmap = True
    model = ASRModel.restore_from(
        str(checkpoint), map_location="cpu", save_restore_connector=connector
    )
    # NeMo leaves the checkpoint in a reference cycle.
    gc.collect()
    if words:
        decoding = model.cfg.decoding
        with open_dict(decoding):
            decoding.greedy.boosting_tree = {"key_phrases_list": list(words)}
            decoding.greedy.boosting_tree_alpha = BOOSTING_WEIGHT
        model.change_decoding_strategy(decoding)
    texts: list[str] = []
    # oneDNN keeps a compiled kernel per input length (up to 1024), so memory climbs with every
    # new turn length; with it off memory stays flat, at no measured cost in speed or text.
    with torch.inference_mode(), torch.backends.mkldnn.flags(enabled=False):
        for turn in turns:
            # Only this turn's audio is in memory, however long the meeting.
            clip, _ = soundfile.read(
                folder / (MIC if turn.speaker == ME else CALL),
                start=turn.start,
                stop=turn.end,
                dtype="float32",
            )
            texts.append(model.transcribe([clip], batch_size=1, verbose=False)[0].text)
    return texts


def transcribe(folder: Path, words: Sequence[str]) -> list[Segment]:
    weights = model_file(PARAKEET, "*.nemo")
    pipeline = model_file(PYANNOTE, "config.yaml")
    speech = [Turn(ME, start, end) for start, end in find_speech(folder / MIC)]
    on_the_call = find_speech(folder / CALL)
    if on_the_call:
        # In a process of its own: what pyannote took, its libraries included, goes back whole
        # when it ends. Started afresh, since this one already runs torch's threads.
        try:
            with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as apart:
                heard = apart.submit(diarize, pipeline, folder / CALL).result()
        except BrokenProcessPool:
            # At the memory limit the kernel kills the biggest process: pyannote's, not this one.
            raise TranscriberError(DIARIZATION_KILLED) from None
        speech += assign_speakers(on_the_call, heard)
    # A cut at a speaker change can leave a sliver of a stretch, too short to hold a word.
    turns = [
        turn for turn in merge_turns(speech) if turn.end - turn.start >= SHORTEST_SPEECH_SAMPLES
    ]
    if not turns:
        return []
    return segments(list(zip(turns, recognize(weights, folder, turns, words), strict=True)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -P transcriber.py",
        description="Writes transcript.json for a recording folder's mic.opus and call.opus.",
    )
    parser.add_argument("folder", type=Path, help="the recording's folder")
    parser.add_argument("--words", type=Path, help="phrases to boost, one per line")
    args = parser.parse_args(argv)
    try:
        transcript = transcribe(args.folder, read_words(args.words))
    except TranscriberError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    write_transcript(args.folder, transcript)
    return 0


if __name__ == "__main__":
    sys.exit(main())
