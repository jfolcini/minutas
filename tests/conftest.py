from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points every XDG base directory into tmp_path so no test touches the real data."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path
