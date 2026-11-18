"""Word error rate of hypothesis transcripts against the hand-corrected test set.

A test-set entry is a folder `<testset>/<name>/` holding its audio as production records it
(`mic.opus` and `call.opus`, or one Opus file) and `reference.md`, the corrected transcript.
The hypothesis for that entry is `<hypotheses>/<name>.transcript.md`.
"""

import argparse
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import jiwer

DEFAULT_TESTSET = Path.home() / ".local" / "share" / "minutas" / "testset"

_SPEAKER_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] \*\*[^*\n]+:\*\*", re.MULTILINE)


class ScoringError(Exception):
    pass


@dataclass(frozen=True)
class Score:
    name: str
    wer: float
    reference_words: int


def normalize(transcript: str) -> str:
    # NFC so a reference saved with decomposed accents still matches: "está" and "esta" differ.
    text = _SPEAKER_PREFIX.sub(" ", unicodedata.normalize("NFC", transcript)).lower()
    text = "".join(char for char in text if not unicodedata.category(char).startswith("P"))
    return " ".join(text.split())


def score_testset(testset: Path, hypotheses: Path) -> tuple[list[Score], Score]:
    entries = sorted(reference.parent for reference in testset.glob("*/reference.md"))
    if not entries:
        msg = f"no test-set entries (*/reference.md) in {testset}"
        raise ScoringError(msg)
    references, transcripts = [], []
    for entry in entries:
        hypothesis = hypotheses / f"{entry.name}.transcript.md"
        if not hypothesis.is_file():
            msg = f"no hypothesis for {entry.name}: {hypothesis}"
            raise ScoringError(msg)
        references.append(normalize((entry / "reference.md").read_text(encoding="utf-8")))
        transcripts.append(normalize(hypothesis.read_text(encoding="utf-8")))
    scores = [
        Score(entry.name, jiwer.process_words(reference, transcript).wer, len(reference.split()))
        for entry, reference, transcript in zip(entries, references, transcripts, strict=True)
    ]
    # One alignment over every entry: total errors over total reference words.
    overall = Score(
        "overall",
        jiwer.process_words(references, transcripts).wer,
        sum(score.reference_words for score in scores),
    )
    return scores, overall


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m minutas.scorer",
        description="Word error rate of transcripts against the test set's references.",
    )
    parser.add_argument("hypotheses", type=Path, help="folder of <entry>.transcript.md files")
    parser.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    args = parser.parse_args(argv)
    try:
        entries, overall = score_testset(args.testset, args.hypotheses)
    except ScoringError as error:
        sys.stderr.write(f"scorer: {error}\n")
        return 1
    for score in [*entries, overall]:
        sys.stdout.write(f"{score.name}  {score.wer:.2%}  ({score.reference_words} words)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
