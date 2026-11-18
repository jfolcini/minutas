"""The settings file, and the Agaric agent token, which lives in the keyring instead."""

import json
import os
from pathlib import Path
from typing import cast

import keyring

from minutas import store

KEYRING_SERVICE = "minutas"
AGARIC_KEYRING_USERNAME = "agaric"
# Kept audio is bounded until a limit is turned off, which saves it as None.
AUDIO_AGE_LIMIT_DAYS = 30
AUDIO_SIZE_LIMIT_GB = 5


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


def audio_limit(saved: dict[str, object], key: str, default: int) -> int | None:
    # Only a hand edit saves anything but a whole number of at least one, and 0 taken at its
    # word would delete every transcribed track.
    value = saved.get(key, default)
    if value is None or (type(value) is int and value >= 1):
        return value
    return default


def audio_limits() -> tuple[int | None, int | None]:
    """The age limit in days and the size limit in GB of kept audio; None when turned off."""
    saved = load()
    return (
        audio_limit(saved, "audio_age_limit_days", AUDIO_AGE_LIMIT_DAYS),
        audio_limit(saved, "audio_size_limit_gb", AUDIO_SIZE_LIMIT_GB),
    )


def set_audio_limits(age_limit_days: int | None, size_limit_gb: int | None) -> None:
    save({"audio_age_limit_days": age_limit_days, "audio_size_limit_gb": size_limit_gb})


def summary_model() -> str | None:
    """The model Summarize asks Devin for; None for Devin's own default."""
    return cast("str | None", load().get("summary_model"))


def set_summary_model(model: str | None) -> None:
    save({"summary_model": model})


def agaric_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME)


def set_agaric_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME, token)
