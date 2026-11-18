"""Writes a recording's transcript.md from its two tracks; runs in the speech environment.

    <speech-python> -m minutas.transcriber <recording-folder> [--words <file>]

Silero VAD finds the speech on each track, a speaker's consecutive segments merge into turns of
up to MAX_TURN_SECONDS, and Parakeet transcribes each turn alone, because it detects the language
per input. On failure the last line of stderr is the reason.

The speech stack is imported inside the functions that use it: the app's Python and the tests
import this module without it.
"""

import argparse
import gc
import os
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# What the recorder writes, and all the speech models take.
SAMPLE_RATE = 16_000
MAX_TURN_SECONDS = 30
MAX_TURN_SAMPLES = MAX_TURN_SECONDS * SAMPLE_RATE
# The window Silero's model takes at this rate.
VAD_WINDOW = 512
TRACKS = {"Me": "mic.opus", "Speaker 1": "call.opus"}
PARAKEET = "nvidia/parakeet-tdt-0.6b-v3"
# NeMo's weight for the boosted phrases. Higher, it starts writing them where nobody said them.
BOOSTING_WEIGHT = 1.0


class TranscriberError(Exception):
    """Its message is the one line a person acts on."""


@dataclass(frozen=True)
class Turn:
    """A stretch of one speaker's speech, in samples from the start of the recording."""

    speaker: str
    start: int
    end: int


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


def format_timestamp(samples: int) -> str:
    seconds = samples // SAMPLE_RATE
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def format_transcript(paragraphs: Iterable[tuple[Turn, str]]) -> str:
    """One paragraph per turn that has words (design § Agaric contract)."""
    return "\n".join(
        f"[{format_timestamp(turn.start)}] **{turn.speaker}:** {text.strip()}\n"
        for turn, text in paragraphs
        if text.strip()
    )


def read_words(path: Path | None) -> list[str]:
    if path is None:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def model_file() -> Path:
    """Parakeet's weights at the revision install.sh pins, from the list it writes when done."""
    models = Path.home() / ".cache" / "minutas" / "models"
    revisions = models / "revisions"
    fetched = revisions.read_text(encoding="utf-8").splitlines() if revisions.is_file() else []
    for repository, revision, pattern in (line.split() for line in fetched):
        if repository == PARAKEET:
            snapshot = models / f"models--{repository.replace('/', '--')}" / "snapshots" / revision
            for weights in snapshot.glob(pattern):
                return weights
    msg = "Parakeet model not found in ~/.cache/minutas/models; run install.sh"
    raise TranscriberError(msg)


def write_transcript(folder: Path, text: str) -> None:
    # A rename, so a transcriber stopped half-way leaves the previous transcript or none.
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=folder, prefix=".transcript.md.", suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
    Path(tmp.name).replace(folder / "transcript.md")


def find_speech(tracks: Mapping[str, Path]) -> list[Turn]:
    """Where Silero VAD hears each speaker, reading a track one window at a time."""
    import soundfile
    import torch

    threads = torch.get_num_threads()
    # Importing silero_vad sets torch to one thread, and Parakeet would be left with it.
    import silero_vad

    segments: list[Turn] = []
    for speaker, track in tracks.items():
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
        segments += [Turn(speaker, span["start"], span["end"]) for span in spans]
    torch.set_num_threads(threads)
    return segments


def recognize(
    weights: Path, tracks: Mapping[str, Path], turns: Iterable[Turn], words: Sequence[str]
) -> list[str]:
    """Each turn's text from Parakeet, with the words boosted."""
    import soundfile
    import torch.utils.serialization
    from nemo.collections.asr.models import ASRModel
    from omegaconf import open_dict

    # Read into memory, the checkpoint would sit beside the model it fills: twice the model.
    torch.utils.serialization.config.load.mmap = True
    model = ASRModel.restore_from(str(weights), map_location="cpu")
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
                tracks[turn.speaker], start=turn.start, stop=turn.end, dtype="float32"
            )
            texts.append(model.transcribe([clip], batch_size=1, verbose=False)[0].text)
    return texts


def transcribe(folder: Path, words: Sequence[str]) -> str:
    weights = model_file()
    tracks = {
        speaker: folder / name
        for speaker, name in TRACKS.items()
        if (folder / name).is_file() and (folder / name).stat().st_size > 0
    }
    turns = merge_turns(find_speech(tracks))
    if not turns:
        return ""
    return format_transcript(zip(turns, recognize(weights, tracks, turns, words), strict=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m minutas.transcriber",
        description="Writes transcript.md for a recording folder's mic.opus and call.opus.",
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
