from pathlib import Path
from typing import override

import keyring
import pytest
from keyring.backend import KeyringBackend


@pytest.fixture(autouse=True)
def xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points every XDG base directory into tmp_path so no test touches the real data."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


class MemoryKeyring(KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    @override
    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    @override
    def delete_password(self, service: str, username: str) -> None:
        del self.passwords[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    """Stands in for the GNOME keyring so no test reads or writes the real one."""
    fake = MemoryKeyring()
    keyring.set_keyring(fake)
    return fake
