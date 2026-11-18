import subprocess
import sys
from pathlib import Path

import pytest

from minutas.scorer import Score, ScoringError, main, normalize, score_testset


def add_entry(testset: Path, hypotheses: Path, name: str, reference: str, hypothesis: str) -> None:
    (testset / name).mkdir(parents=True)
    (testset / name / "reference.md").write_text(reference, encoding="utf-8")
    hypotheses.mkdir(exist_ok=True)
    (hypotheses / f"{name}.transcript.md").write_text(hypothesis, encoding="utf-8")


def test_normalize_strips_speaker_prefixes_case_and_punctuation() -> None:
    transcript = (
        "[00:00:04] **Ana:** ¿Arrancamos con el Release?\n\n"
        "[01:02:11] **Speaker 2:** CI is green, it's fine; está.\n"
    )

    assert normalize(transcript) == "arrancamos con el release ci is green its fine está"


def test_normalize_composes_decomposed_accents() -> None:
    assert normalize("está") == normalize("está")


def test_score_counts_substitutions_and_deletions(tmp_path: Path) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(
        testset, hypotheses, "standup", "[00:00:01] **Me:** a b c d", "[00:00:01] **Me:** a x c"
    )

    entries, overall = score_testset(testset, hypotheses)

    assert entries == [Score("standup", 0.5, 4)]
    assert overall == Score("overall", 0.5, 4)


def test_score_counts_an_accent_difference_as_an_error(tmp_path: Path) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(testset, hypotheses, "sync", "Ya está listo.", "ya esta listo")

    entries, _ = score_testset(testset, hypotheses)

    assert entries == [Score("sync", 1 / 3, 3)]


def test_overall_is_weighted_by_reference_words(tmp_path: Path) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(testset, hypotheses, "a-short", "uno dos", "uno tres")
    add_entry(testset, hypotheses, "b-long", "a b c d e f g h", "a b c d e f g h")

    entries, overall = score_testset(testset, hypotheses)

    assert entries == [Score("a-short", 0.5, 2), Score("b-long", 0.0, 8)]
    assert overall == Score("overall", 0.1, 10)


def test_missing_hypothesis_names_the_entry(tmp_path: Path) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(testset, hypotheses, "standup", "hola", "hola")
    (testset / "retro").mkdir()
    (testset / "retro" / "reference.md").write_text("chau", encoding="utf-8")

    with pytest.raises(ScoringError, match=r"no hypothesis for retro: .*retro\.transcript\.md"):
        score_testset(testset, hypotheses)


def test_empty_testset_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScoringError, match=r"no test-set entries \(\*/reference\.md\) in "):
        score_testset(tmp_path, tmp_path)


def test_main_prints_each_entry_and_the_overall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(testset, hypotheses, "a-short", "uno dos", "uno tres")
    add_entry(testset, hypotheses, "b-long", "a b c d e f g h", "a b c d e f g h")

    assert main([str(hypotheses), "--testset", str(testset)]) == 0
    assert capsys.readouterr().out == (
        "a-short  50.00%  (2 words)\nb-long  0.00%  (8 words)\noverall  10.00%  (10 words)\n"
    )


def test_main_reports_a_scoring_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path), "--testset", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("scorer: no test-set entries")


def test_module_runs_as_a_script(tmp_path: Path) -> None:
    testset, hypotheses = tmp_path / "testset", tmp_path / "hypotheses"
    add_entry(testset, hypotheses, "standup", "hola mundo", "hola")

    result = subprocess.run(
        [sys.executable, "-m", "minutas.scorer", "hypotheses", "--testset", "testset"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == "standup  50.00%  (2 words)\noverall  50.00%  (2 words)\n"
