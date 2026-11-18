import json
from pathlib import Path

import keyring
import pytest

from minutas import settings


def test_settings_file_follows_xdg_config_home(xdg_dirs: Path) -> None:
    assert settings.settings_path() == xdg_dirs / "config" / "minutas" / "settings.json"


@pytest.mark.parametrize("value", [None, ""])
def test_settings_file_defaults_to_dot_config(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", value)

    assert settings.settings_path() == tmp_path / ".config" / "minutas" / "settings.json"


def test_space_is_unset_without_a_settings_file() -> None:
    assert settings.agaric_space_id() is None


def test_set_space_writes_it_and_keeps_the_other_settings() -> None:
    settings.settings_path().parent.mkdir(parents=True)
    settings.settings_path().write_text('{"glossary_hint": true}')

    settings.set_agaric_space_id("01HZWORK")

    assert settings.agaric_space_id() == "01HZWORK"
    assert json.loads(settings.settings_path().read_text()) == {
        "glossary_hint": True,
        "agaric_space_id": "01HZWORK",
    }


def test_token_is_unset_until_set() -> None:
    assert settings.agaric_token() is None


def test_set_token_keeps_it_in_the_keyring() -> None:
    settings.set_agaric_token("agt_1")

    assert keyring.get_password("minutas", "agaric") == "agt_1"
    assert settings.agaric_token() == "agt_1"
