import os
from pathlib import Path

import pytest

from minutas import atomic


def test_write_json_replaces_the_file_and_leaves_nothing_else(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("old")

    atomic.write_json(path, {"name": "Ana Díaz"})

    assert path.read_text(encoding="utf-8") == '{\n  "name": "Ana D\\u00edaz"\n}\n'
    assert [child.name for child in tmp_path.iterdir()] == ["settings.json"]


def test_a_write_that_fails_before_reaching_the_disk_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "transcript.md"
    path.write_text("old")

    def full_disk(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)

    with pytest.raises(OSError, match="No space left on device"):
        atomic.write_text(path, "new")

    assert path.read_text() == "old"
