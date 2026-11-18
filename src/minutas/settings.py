"""The settings file, and the Agaric agent token, which lives in the keyring instead."""

import json
import os
from pathlib import Path
from typing import cast

import keyring

from minutas import store

KEYRING_SERVICE = "minutas"
AGARIC_KEYRING_USERNAME = "agaric"


def settings_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "minutas" / "settings.json"


def load() -> dict[str, object]:
    path = settings_path()
    return cast("dict[str, object]", json.loads(path.read_text())) if path.is_file() else {}


def save(changes: dict[str, object]) -> None:
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    store.write_json(settings_path(), {**load(), **changes})


def agaric_space_id() -> str | None:
    return cast("str | None", load().get("agaric_space_id"))


def set_agaric_space_id(space_id: str) -> None:
    save({"agaric_space_id": space_id})


def saved_presets() -> dict[str, str]:
    """The summary prompts saved from Custom, by name, oldest first."""
    return cast("dict[str, str]", load().get("presets", {}))


def save_preset(name: str, prompt: str) -> None:
    save({"presets": {**saved_presets(), name: prompt}})


def delete_preset(name: str) -> None:
    presets = saved_presets()
    presets.pop(name, None)
    save({"presets": presets})


def agaric_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME)


def set_agaric_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME, token)
