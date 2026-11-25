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


def test_no_preset_is_saved_without_a_settings_file() -> None:
    assert settings.saved_presets() == {}


def test_save_preset_keeps_it_with_the_other_settings_and_replaces_one_of_its_name() -> None:
    settings.set_agaric_space_id("01HZWORK")

    settings.save_preset("Retro", "Summarize this retrospective.")
    settings.save_preset("Planning", "List the plans.")
    settings.save_preset("Retro", "Only what went wrong.")

    assert settings.saved_presets() == {
        "Retro": "Only what went wrong.",
        "Planning": "List the plans.",
    }
    assert list(settings.saved_presets()) == ["Retro", "Planning"]
    assert settings.agaric_space_id() == "01HZWORK"


def test_delete_preset_removes_only_that_one() -> None:
    settings.save_preset("Retro", "Summarize this retrospective.")
    settings.save_preset("Planning", "List the plans.")

    settings.delete_preset("Retro")

    assert settings.saved_presets() == {"Planning": "List the plans."}


def test_deleting_a_preset_that_is_gone_changes_nothing() -> None:
    settings.save_preset("Planning", "List the plans.")

    settings.delete_preset("Retro")

    assert settings.saved_presets() == {"Planning": "List the plans."}


def test_kept_audio_is_bounded_to_30_days_and_5_gb_until_changed() -> None:
    assert settings.audio_limits() == (30, 5)


# Only a hand edit writes these; 0 taken at its word would delete every transcribed track.
@pytest.mark.parametrize("value", [0, -1, "5", 2.5, True, [5]])
def test_a_hand_edited_limit_that_is_no_whole_number_of_at_least_one_is_the_default(
    value: object,
) -> None:
    settings.save({"audio_age_limit_days": value, "audio_size_limit_gb": value})

    assert settings.audio_limits() == (30, 5)


@pytest.mark.parametrize("limits", [(7, None), (None, 2), (None, None), (90, 20)])
def test_set_audio_limits_keeps_them_with_the_other_settings_and_none_turns_one_off(
    limits: tuple[int | None, int | None],
) -> None:
    settings.set_agaric_space_id("01HZWORK")

    settings.set_audio_limits(*limits)

    assert settings.audio_limits() == limits
    assert settings.agaric_space_id() == "01HZWORK"


def test_no_summary_model_is_set_until_one_is() -> None:
    assert settings.summary_model() is None


def test_set_summary_model_keeps_it_with_the_other_settings_and_none_clears_it() -> None:
    settings.set_agaric_space_id("01HZWORK")

    settings.set_summary_model("swe-2-high")

    assert settings.summary_model() == "swe-2-high"

    settings.set_summary_model(None)

    assert settings.summary_model() is None
    assert settings.agaric_space_id() == "01HZWORK"


def test_no_summarizer_is_chosen_until_one_is() -> None:
    assert settings.summarizer() is None


def test_set_summarizer_keeps_it_with_the_other_settings() -> None:
    settings.set_summary_model("swe-2-high")

    settings.set_summarizer("claude")

    assert settings.summarizer() == "claude"
    assert settings.summary_model() == "swe-2-high"


def test_claude_has_no_model_or_effort_until_they_are_set() -> None:
    assert (settings.claude_model(), settings.claude_effort()) == (None, None)


def test_claude_keeps_a_model_and_an_effort_of_its_own_and_none_clears_each() -> None:
    settings.set_summary_model("swe-2-high")

    settings.set_claude_model("opus")
    settings.set_claude_effort("xhigh")

    assert (settings.claude_model(), settings.claude_effort()) == ("opus", "xhigh")
    assert settings.summary_model() == "swe-2-high"

    settings.set_claude_model(None)

    assert (settings.claude_model(), settings.claude_effort()) == (None, "xhigh")

    settings.set_claude_effort(None)

    assert (settings.claude_model(), settings.claude_effort()) == (None, None)
    assert settings.summary_model() == "swe-2-high"


def test_no_summary_skill_is_set_until_one_is() -> None:
    assert settings.summary_skill() is None


def test_set_summary_skill_keeps_it_with_the_other_settings_and_none_clears_it() -> None:
    settings.set_summary_model("swe-2-high")

    settings.set_summary_skill("/home/me/skills/standup/SKILL.md")

    assert settings.summary_skill() == "/home/me/skills/standup/SKILL.md"

    settings.set_summary_skill(None)

    assert settings.summary_skill() is None
    assert settings.summary_model() == "swe-2-high"


def test_no_audio_is_refused_until_a_server_refuses_one() -> None:
    assert settings.refused_audio_size("gitlab.example.com") is None


def test_a_refused_size_is_kept_per_host_with_the_other_settings_and_only_shrinks() -> None:
    settings.set_agaric_space_id("01HZWORK")

    settings.refuse_audio_size("a.example.com", 900)
    settings.refuse_audio_size("a.example.com", 1200)
    settings.refuse_audio_size("b.example.com", 5000)
    settings.refuse_audio_size("a.example.com", 700)

    assert settings.refused_audio_size("a.example.com") == 700
    assert settings.refused_audio_size("b.example.com") == 5000
    assert settings.agaric_space_id() == "01HZWORK"


def test_no_gitlab_link_is_set_until_one_is() -> None:
    assert settings.gitlab_link() is None


def test_set_gitlab_link_keeps_it_with_the_other_settings_and_none_clears_it() -> None:
    settings.set_summary_model("swe-2-high")
    link = "https://gitlab.example.com/group/project"

    settings.set_gitlab_link(link)

    assert (settings.gitlab_link(), settings.summary_model()) == (link, "swe-2-high")

    settings.set_gitlab_link(None)

    assert settings.gitlab_link() is None


def test_no_default_github_link_is_set_until_one_is() -> None:
    assert settings.github_link() is None


def test_set_github_link_keeps_it_with_the_other_settings_and_none_clears_it() -> None:
    settings.set_summary_model("swe-2-high")

    settings.set_github_link("https://github.com/owner/repo")

    assert settings.github_link() == "https://github.com/owner/repo"

    settings.set_github_link(None)

    assert settings.github_link() is None
    assert settings.summary_model() == "swe-2-high"


def test_the_menu_lists_five_recent_recordings_until_changed() -> None:
    assert settings.recent_in_menu() == 5


@pytest.mark.parametrize("count", [1, 12, 20])
def test_set_recent_in_menu_keeps_it_with_the_other_settings(count: int) -> None:
    settings.set_agaric_space_id("01HZWORK")

    settings.set_recent_in_menu(count)

    assert settings.recent_in_menu() == count
    assert settings.agaric_space_id() == "01HZWORK"


# Only a hand edit writes these; with none listed, the menu would announce nothing.
@pytest.mark.parametrize("value", [0, -1, 21, None, "5", 2.5, True, [5]])
def test_a_hand_edited_count_of_recent_recordings_out_of_range_is_the_default(
    value: object,
) -> None:
    settings.save({"recent_in_menu": value})

    assert settings.recent_in_menu() == 5


def test_no_context_is_ticked_until_some_is() -> None:
    assert settings.summary_context() == []


def test_set_summary_context_keeps_it_with_the_other_settings_in_its_order() -> None:
    settings.set_summary_model("swe-2-high")

    settings.set_summary_context(["Team glossary", "Project"])

    assert settings.summary_context() == ["Team glossary", "Project"]
    assert settings.summary_model() == "swe-2-high"
