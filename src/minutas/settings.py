"""The settings file, and the Agaric agent token, which lives in the keyring instead."""

import json
import os
from pathlib import Path
from typing import cast

import keyring

from minutas.atomic import write_json

KEYRING_SERVICE = "minutas"
AGARIC_KEYRING_USERNAME = "agaric"
# Kept audio is bounded until a limit is turned off, which saves it as None.
AUDIO_AGE_LIMIT_DAYS = 30
AUDIO_SIZE_LIMIT_GB = 5
# How many recordings the top-bar menu lists, newest first. It announces a ready transcript or a
# failed run only for those, so it lists one at least.
RECENT_IN_MENU = 5
MOST_RECENT_IN_MENU = 20
# How long both tracks are quiet before the top bar reminds that it is still recording, in
# minutes, until turned off, which saves it as None.
SILENCE_REMINDER_MINUTES = 5
LONGEST_SILENCE_REMINDER = 120
# The glossary's name among the vocabularies, which no other vocabulary's can be.
GLOSSARY = ""


def settings_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "minutas" / "settings.json"


def load() -> dict[str, object]:
    path = settings_path()
    return cast("dict[str, object]", json.loads(path.read_text())) if path.is_file() else {}


def save(changes: dict[str, object]) -> None:
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    write_json(settings_path(), {**load(), **changes})


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


def recent_in_menu() -> int:
    value = load().get("recent_in_menu", RECENT_IN_MENU)
    if type(value) is int and 1 <= value <= MOST_RECENT_IN_MENU:
        return value
    return RECENT_IN_MENU


def set_recent_in_menu(count: int) -> None:
    save({"recent_in_menu": count})


def silence_reminder_minutes() -> int | None:
    value = load().get("silence_reminder_minutes", SILENCE_REMINDER_MINUTES)
    if value is None or (type(value) is int and 1 <= value <= LONGEST_SILENCE_REMINDER):
        return value
    return SILENCE_REMINDER_MINUTES


def set_silence_reminder_minutes(minutes: int | None) -> None:
    save({"silence_reminder_minutes": minutes})


def echo_cancel() -> bool:
    """Whether the next recording takes its mic track from PipeWire's echo-cancelled mic."""
    # Only a hand edit saves anything but a switch's state, and only off turns it off.
    return load().get("echo_cancel") is not False


def set_echo_cancel(*, on: bool) -> None:
    save({"echo_cancel": on})


def summary_model() -> str | None:
    """The model, with its effort, Summarize asks Devin for; None for Devin's own default."""
    return cast("str | None", load().get("summary_model"))


def set_summary_model(model: str | None) -> None:
    save({"summary_model": model})


def summarizer() -> str | None:
    """The command of the summarizer Summarize runs; None until one is chosen."""
    return cast("str | None", load().get("summarizer"))


def set_summarizer(command: str) -> None:
    save({"summarizer": command})


def claude_model() -> str | None:
    """The model Summarize asks Claude Code for; None for Claude Code's own default."""
    return cast("str | None", load().get("claude_model"))


def set_claude_model(model: str | None) -> None:
    save({"claude_model": model})


def claude_effort() -> str | None:
    """The effort Summarize asks Claude Code for; None for Claude Code's own default."""
    return cast("str | None", load().get("claude_effort"))


def set_claude_effort(effort: str | None) -> None:
    save({"claude_effort": effort})


def summary_skill() -> str | None:
    """The path of the skill file Summarize follows; None for the built-in one."""
    return cast("str | None", load().get("summary_skill"))


def set_summary_skill(path: str | None) -> None:
    save({"summary_skill": path})


def summary_context() -> list[str]:
    """The names of the context presets ticked in the Summarize menu, in the order ticked."""
    return cast("list[str]", load().get("summary_context", []))


def set_summary_context(names: list[str]) -> None:
    save({"summary_context": names})


def vocabularies() -> list[str]:
    """The names of the vocabularies every transcription boosts, GLOSSARY for the glossary."""
    return cast("list[str]", load().get("vocabularies", [GLOSSARY]))


def set_vocabularies(names: list[str]) -> None:
    save({"vocabularies": names})


def bad_preset_name(name: str) -> bool:
    """Whether the name can't be a context preset's or a vocabulary's, which names its file."""
    # A slash would put the file in another folder, and a dot first hide it in this one.
    return not name or "/" in name or name.startswith(".")


def preset_names(folder: Path, suffix: str) -> list[str]:
    """The names of the folder's context presets or vocabularies, in alphabetical order."""
    files = folder.glob(f"*{suffix}")
    names = (file.stem for file in files if file.is_file())
    # pathlib's glob, unlike the shell's, finds hidden files too.
    return sorted((name for name in names if not bad_preset_name(name)), key=str.casefold)


def gitlab_link() -> str | None:
    """The project or issue link Publish to GitLab starts from; None until one is set."""
    return cast("str | None", load().get("gitlab_link"))


def set_gitlab_link(link: str | None) -> None:
    save({"gitlab_link": link})


def github_link() -> str | None:
    """The issue or repository link Publish to GitHub starts from; None for none."""
    return cast("str | None", load().get("github_link"))


def set_github_link(link: str | None) -> None:
    save({"github_link": link})


def agaric_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME)


def set_agaric_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, AGARIC_KEYRING_USERNAME, token)
