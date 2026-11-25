from dataclasses import replace
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import store
from minutas.window_view import (
    ActionView,
    AgaricView,
    RowView,
    SpeakerView,
    TrackView,
    UpdateView,
    agaric,
    clean_name,
    duration,
    limits_warning,
    row,
    trash_refusal,
    unrecorded_failure,
    update,
)

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


def recording(**changes: object) -> store.Recording:
    stopped = {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+01:00",
        "stopped_at": "2026-11-18T13:36:16+01:00",
        "incomplete": False,
        "name": "Meeting",
        "name_source": "default",
    }
    return cast("store.Recording", {**stopped, **changes})


def run(action: str, status: str, **changes: object) -> store.Run:
    ended = {
        "action": action,
        "status": status,
        "queued_at": "2026-11-18T13:36:16+01:00",
        "error": None,
        "details": None,
        "output": None,
    }
    return cast("store.Run", {**ended, **changes})


def action(result: str, label: str, **changes: object) -> ActionView:
    return replace(ActionView(result=result, label=label), **changes)


def shown(
    runs: list[store.Run],
    changes: dict[str, object] | None = None,
    *,
    tracks: tuple[str, ...] = store.TRACKS,
    running: tuple[str, ...] = (),
    missing_tools: dict[str, str] | None = None,
) -> RowView:
    return row(
        recording(**(changes or {})),
        runs,
        tracks,
        running=running,
        missing_tools=missing_tools or {},
    )


def test_a_stopped_recording_shows_its_name_its_tracks_and_what_can_run() -> None:
    assert shown([]) == RowView(
        recording_id="2026-11-18T13-05-42",
        name="Meeting",
        name_source="Default name",
        subtitle="2026-11-18 13:05 · 30 min",
        warning=False,
        tracks=(
            TrackView(file="mic.opus", title="Microphone", subtitle="", available=True),
            TrackView(file="call.opus", title="Call", subtitle="", available=True),
        ),
        delete_disabled="",
        transcribe=action("Not run yet", "Transcribe"),
        speakers=(),
        attendees=(),
        summarize=action("Not summarized", "", menu="Summarize", disabled="Transcribe first"),
        publish=action("Not published", "Publish"),
        publish_gitlab=action("Not published", "Publish…", disabled="Summarize first"),
        publish_github=action("Not published", "Publish…", disabled="Summarize first"),
    )


def test_a_transcribed_recording_lists_the_calls_speakers_and_who_they_may_be() -> None:
    view = shown(
        [run("transcribe", "succeeded", output="transcript.md")],
        {
            "attendees": ["Ana Díaz", "bob.ruiz"],
            "speakers": {"Speaker 1": "Speaker 1", "Speaker 2": "Ana Díaz", "Speaker 3": "Caro"},
        },
    )

    # In the order they first speak; one nobody named yet has no name to show.
    assert view.speakers == (
        SpeakerView(key="Speaker 1", name=""),
        SpeakerView(key="Speaker 2", name="Ana Díaz"),
        SpeakerView(key="Speaker 3", name="Caro"),
    )
    assert view.attendees == ("Ana Díaz", "bob.ruiz")


def test_a_meeting_without_a_calendar_event_has_speakers_and_no_attendees() -> None:
    view = shown([], {"speakers": {"Speaker 1": "Speaker 1"}})

    assert view.speakers == (SpeakerView(key="Speaker 1", name=""),)
    assert view.attendees == ()


def test_a_typed_name_says_so() -> None:
    view = shown([], {"name": "Weekly sync", "name_source": "typed"})

    assert (view.name, view.name_source) == ("Weekly sync", "Typed name")


def test_a_name_from_the_calendar_says_so() -> None:
    view = shown([], {"name": "Weekly sync", "name_source": "calendar"})

    assert (view.name, view.name_source) == ("Weekly sync", "From the calendar event")


def test_a_name_from_the_summary_says_so() -> None:
    view = shown([], {"name": "Release plan", "name_source": "summary"})

    assert (view.name, view.name_source) == ("Release plan", "Proposed by the summary")


def test_a_recording_in_progress_offers_nothing_but_its_name() -> None:
    view = shown([], {"stopped_at": None})

    assert view.subtitle == "2026-11-18 13:05 · Recording"
    assert view.tracks == (
        TrackView(file="mic.opus", title="Microphone", subtitle="Recording", available=False),
        TrackView(file="call.opus", title="Call", subtitle="Recording", available=False),
    )
    assert view.delete_disabled == "Still recording"
    assert view.transcribe == action(
        "Starts when the recording stops", "Transcribe", disabled="Still recording"
    )
    assert view.summarize == action(
        "Not summarized", "", menu="Summarize", disabled="Still recording"
    )
    assert view.publish == action("Not published", "Publish", disabled="Still recording")
    assert view.publish_gitlab == action("Not published", "Publish…", disabled="Still recording")
    assert view.publish_github == action("Not published", "Publish…", disabled="Still recording")


@pytest.mark.parametrize(
    ("changes", "runs", "subtitle"),
    [
        ({"incomplete": True}, [], "2026-11-18 13:05 · 30 min · Incomplete"),
        ({}, [run("transcribe", "queued")], "2026-11-18 13:05 · 30 min · Waiting to transcribe"),
        ({}, [run("transcribe", "running")], "2026-11-18 13:05 · 30 min · Transcribing"),
        ({}, [run("transcribe", "succeeded")], "2026-11-18 13:05 · 30 min"),
        (
            {"incomplete": True},
            [run("transcribe", "running")],
            "2026-11-18 13:05 · 30 min · Incomplete · Transcribing",
        ),
        # The time is the recording's own, whatever zone the window runs in.
        (
            {"started_at": "2026-11-18T23:59:00-03:00", "stopped_at": "2026-11-19T00:00:05-03:00"},
            [],
            "2026-11-18 23:59 · 1 min",
        ),
    ],
)
def test_the_subtitle_says_when_how_long_and_what_is_going_on(
    changes: dict[str, object], runs: list[store.Run], subtitle: str
) -> None:
    assert shown(runs, changes).subtitle == subtitle


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0 s"),
        (59, "59 s"),
        (60, "1 min"),
        (1834, "30 min"),
        (3599, "59 min"),
        (3600, "1 h 00 min"),
        (3900, "1 h 05 min"),
        (36000, "10 h 00 min"),
    ],
)
def test_duration_reads_in_seconds_minutes_or_hours(seconds: int, text: str) -> None:
    assert duration(seconds) == text


@given(st.integers(min_value=0, max_value=100 * 3600))
def test_duration_never_overstates_and_is_exact_to_its_unit(seconds: int) -> None:
    number = {"s": 1, "min": 60, "h": 3600}
    words = duration(seconds).split()
    said = sum(
        int(count) * number[unit] for count, unit in zip(words[::2], words[1::2], strict=True)
    )

    assert said <= seconds < said + (1 if seconds < 60 else 60)


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (
            [run("transcribe", "queued")],
            action("Queued", "Cancel", cancels=0),
        ),
        (
            [run("transcribe", "failed", error="interrupted"), run("transcribe", "running")],
            action("Running", "Cancel", cancels=1),
        ),
        (
            [run("transcribe", "succeeded", details="run-0.log", output="transcript.md")],
            action("Succeeded", "Run again", output="transcript.md"),
        ),
        (
            [run("transcribe", "failed", error="Out of memory", details="run-0.log")],
            action("Failed: Out of memory", "Retry", failed=True, details="run-0.log"),
        ),
        (
            [run("transcribe", "failed", error="Speech environment not found; run install.sh")],
            action("Failed: Speech environment not found; run install.sh", "Retry", failed=True),
        ),
        (
            [run("transcribe", "cancelled", details="run-0.log")],
            action("Cancelled", "Retry"),
        ),
        # The transcript of an earlier success is still there after a run that did not make one.
        (
            [
                run("transcribe", "succeeded", output="transcript.md"),
                run("publish_agaric", "succeeded", output="agaric://page/01HX"),
                run("transcribe", "failed", error="interrupted", details="run-2.log"),
            ],
            action(
                "Failed: interrupted",
                "Retry",
                failed=True,
                details="run-2.log",
                output="transcript.md",
            ),
        ),
    ],
)
def test_transcribe_shows_its_last_result_and_the_next_step(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs).transcribe == expected


@pytest.mark.parametrize(
    ("status", "label"),
    [(None, "Transcribe"), ("succeeded", "Run again"), ("failed", "Retry"), ("cancelled", "Retry")],
)
def test_transcribing_needs_the_audio(status: str | None, label: str) -> None:
    runs = [] if status is None else [run("transcribe", status, error="Out of memory")]

    view = shown(runs, tracks=())

    assert (view.transcribe.label, view.transcribe.disabled) == (label, "The audio was deleted")
    assert view.tracks == (
        TrackView(file="mic.opus", title="Microphone", subtitle="Deleted", available=False),
        TrackView(file="call.opus", title="Call", subtitle="Deleted", available=False),
    )
    assert view.delete_disabled == "The audio was deleted"


def test_one_track_is_enough_to_transcribe_and_to_delete() -> None:
    view = shown([], tracks=("call.opus",))

    assert view.transcribe.disabled == ""
    assert [track.available for track in view.tracks] == [False, True]
    assert view.delete_disabled == ""


@pytest.mark.parametrize("status", ["queued", "running"])
def test_the_audio_stays_while_it_is_being_transcribed(status: str) -> None:
    view = shown([run("transcribe", status)])

    assert view.delete_disabled == "Cancel the transcription first"
    assert view.transcribe.disabled == ""


@pytest.mark.parametrize(
    ("changes", "runs", "running", "refused"),
    [
        ({}, [], (), ""),
        ({}, [run("transcribe", "failed"), run("summarize", "succeeded")], (), ""),
        ({"stopped_at": None}, [], (), "Still recording"),
        ({}, [run("transcribe", "queued")], (), "Cancel the transcription first"),
        # An older run still waiting counts too: the worker takes it from the folder.
        (
            {},
            [run("transcribe", "running"), run("transcribe", "failed")],
            (),
            "Cancel the transcription first",
        ),
        ({}, [], ("summarize",), "Summarize is still running"),
        ({}, [], ("publish_agaric",), "Publish to Agaric is still running"),
        ({}, [], ("publish_gitlab",), "Publish to GitLab is still running"),
    ],
)
def test_a_recording_goes_to_the_trash_only_once_nothing_works_on_it(
    changes: dict[str, object], runs: list[store.Run], running: tuple[str, ...], refused: str
) -> None:
    assert trash_refusal(recording(**changes), runs, running) == refused
    assert shown(runs, changes, running=running).trash_disabled == refused


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([run("publish_agaric", "running")], action("Running", "Publish")),
        (
            [
                run(
                    "publish_agaric",
                    "succeeded",
                    output="agaric://page/01HX",
                    note="Published without the audio: its tracks were deleted",
                )
            ],
            action(
                "Succeeded · Published without the audio: its tracks were deleted",
                "Publish again",
                output="agaric://page/01HX",
            ),
        ),
        (
            [run("publish_agaric", "succeeded", output="agaric://page/01HX", note=None)],
            action("Succeeded", "Publish again", output="agaric://page/01HX"),
        ),
        (
            [run("publish_agaric", "succeeded", output="agaric://page/01HX")],
            action("Succeeded", "Publish again", output="agaric://page/01HX"),
        ),
        (
            [
                run(
                    "publish_agaric",
                    "failed",
                    error="Agaric isn't running: start it and press Retry",
                    details="run-0.log",
                )
            ],
            action(
                "Failed: Agaric isn't running: start it and press Retry",
                "Retry",
                failed=True,
                details="run-0.log",
            ),
        ),
        ([run("publish_agaric", "cancelled")], action("Cancelled", "Retry")),
        # The link is to the page of the newest success.
        (
            [
                run("publish_agaric", "succeeded", output="agaric://page/01HX"),
                run("publish_agaric", "succeeded", output="agaric://page/01HY"),
            ],
            action("Succeeded", "Publish again", output="agaric://page/01HY"),
        ),
        (
            [
                run("publish_agaric", "succeeded", output="agaric://page/01HX"),
                run("publish_agaric", "failed", error="The agent token was revoked"),
            ],
            action(
                "Failed: The agent token was revoked",
                "Retry",
                failed=True,
                output="agaric://page/01HX",
            ),
        ),
    ],
)
def test_publish_shows_its_last_result_and_the_next_step(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs).publish == expected


TRANSCRIBED = run("transcribe", "succeeded", output="transcript.md")
SUMMARIZED = run("summarize", "succeeded", output="summary.md", prompt="Summarize this meeting.")


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([TRANSCRIBED], action("Not summarized", "", menu="Summarize")),
        (
            [TRANSCRIBED, SUMMARIZED],
            action(
                "Succeeded",
                "",
                menu="Summarize again",
                output="summary.md",
                prompt="Summarize this meeting.",
            ),
        ),
        # Retry sends the failed run's prompt again; the menu offers another.
        (
            [
                TRANSCRIBED,
                run(
                    "summarize",
                    "failed",
                    error="Error: not logged in",
                    details="run-1.log",
                    prompt="Only the blockers.",
                ),
            ],
            action(
                "Failed: Error: not logged in",
                "Retry",
                failed=True,
                details="run-1.log",
                menu="Summarize",
                prompt="Only the blockers.",
            ),
        ),
        (
            [
                TRANSCRIBED,
                SUMMARIZED,
                run("summarize", "failed", error="interrupted", prompt="Only the blockers."),
            ],
            action(
                "Failed: interrupted",
                "Retry",
                failed=True,
                menu="Summarize again",
                output="summary.md",
                prompt="Only the blockers.",
            ),
        ),
        # A transcription that failed after one that succeeded leaves its transcript.
        (
            [TRANSCRIBED, run("transcribe", "failed", error="Out of memory")],
            action("Not summarized", "", menu="Summarize"),
        ),
    ],
)
def test_summarize_shows_its_last_result_the_presets_menu_and_retry(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs).summarize == expected


MISSING = "devin not found: install the Devin CLI"


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        (
            [TRANSCRIBED],
            action(f"Not summarized · {MISSING}", "", menu="Summarize", disabled=MISSING),
        ),
        (
            [TRANSCRIBED, SUMMARIZED, run("summarize", "failed", error="boom", prompt="P")],
            action(
                f"Failed: boom · {MISSING}",
                "Retry",
                failed=True,
                menu="Summarize again",
                output="summary.md",
                prompt="P",
                disabled=MISSING,
            ),
        ),
        # A missing summarizer is the reason, whether or not there is a transcript yet.
        ([], action(f"Not summarized · {MISSING}", "", menu="Summarize", disabled=MISSING)),
    ],
)
def test_without_its_summarizer_summarize_says_how_to_install_it(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs, missing_tools={"summarize": MISSING}).summarize == expected


def test_still_recording_outranks_a_missing_summarizer() -> None:
    view = shown([], {"stopped_at": None}, missing_tools={"summarize": MISSING})

    assert view.summarize.disabled == "Still recording"


def test_summarizing_needs_a_transcript() -> None:
    runs = [run("transcribe", "failed", error="Out of memory")]

    assert shown(runs).summarize.disabled == "Transcribe first"


def test_while_the_window_summarizes_nothing_else_can_be_pressed() -> None:
    assert shown([TRANSCRIBED, SUMMARIZED], running=("summarize",)).summarize == action(
        "Running", "", menu="Summarize again", disabled="Summarizing", output="summary.md"
    )


def test_publishing_needs_no_audio() -> None:
    assert shown([], tracks=()).publish == action("Not published", "Publish")


TAKEN = run(
    "publish_agaric",
    "failed",
    error="Agaric already has a page named Meeting 2026-11-18",
    choice={"kind": "name_taken", "page_id": "01HXPAGE"},
)


def test_a_taken_name_asks_which_page() -> None:
    assert shown([TAKEN]).publish == action(
        "Failed: Agaric already has a page named Meeting 2026-11-18",
        "Retry",
        failed=True,
        taken_by="01HXPAGE",
        renames=True,
    )


def test_a_typed_name_agaric_refused_asks_only_for_another() -> None:
    refused = run(
        "publish_agaric",
        "failed",
        error="Agaric already has a page named Weekly sync 2026-11-18",
        choice={"kind": "rename_taken"},
    )

    assert shown([refused]).publish == action(
        "Failed: Agaric already has a page named Weekly sync 2026-11-18",
        "Retry",
        failed=True,
        renames=True,
    )


def test_a_question_the_window_does_not_know_leaves_retry() -> None:
    asked = run("publish_agaric", "failed", error="No", choice={"kind": "pick_space"})

    assert shown([asked]).publish == action("Failed: No", "Retry", failed=True)


@pytest.mark.parametrize(
    "later",
    [
        run("publish_agaric", "running"),
        run("publish_agaric", "succeeded", output="agaric://page/01HXPAGE"),
        run("publish_agaric", "failed", error="Agaric isn't running", choice=None),
    ],
)
def test_the_question_goes_with_the_run_that_asked_it(later: store.Run) -> None:
    publish = shown([TAKEN, later]).publish

    assert (publish.taken_by, publish.renames) == (None, False)


def test_while_the_window_publishes_nothing_else_can_be_pressed() -> None:
    assert shown([TAKEN], running=("publish_agaric",)).publish == action(
        "Running", "Publish", disabled="Publishing"
    )
    assert shown([run("publish_agaric", "running")], running=("publish_agaric",)).publish == action(
        "Running", "Publish", disabled="Publishing"
    )
    # The page published before can still be opened meanwhile.
    published = run("publish_agaric", "succeeded", output="agaric://page/01HX")
    assert shown([published], running=("publish_agaric",)).publish == action(
        "Running", "Publish", disabled="Publishing", output="agaric://page/01HX"
    )


ISSUE = "https://gitlab.example.com/group/project/-/issues/7"
GLAB_MISSING = "glab not found: install glab 1.91.0 or newer"


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([TRANSCRIBED, SUMMARIZED], action("Not published", "Publish…")),
        # A summary that failed after one that succeeded leaves its summary.
        (
            [TRANSCRIBED, SUMMARIZED, run("summarize", "failed", error="boom")],
            action("Not published", "Publish…"),
        ),
        (
            [
                TRANSCRIBED,
                SUMMARIZED,
                run("publish_gitlab", "succeeded", output=f"{ISSUE}#note_302", link=ISSUE),
            ],
            action("Succeeded", "Publish again…", output=f"{ISSUE}#note_302", link=ISSUE),
        ),
        # Retry asks for the link again, starting from the failed run's.
        (
            [
                TRANSCRIBED,
                SUMMARIZED,
                run(
                    "publish_gitlab",
                    "failed",
                    error="Could not upload the audio: it is over gitlab.example.com's limit",
                    details="run-2.log",
                    link=ISSUE,
                ),
            ],
            action(
                "Failed: Could not upload the audio: it is over gitlab.example.com's limit",
                "Retry…",
                failed=True,
                details="run-2.log",
                link=ISSUE,
            ),
        ),
        ([TRANSCRIBED], action("Not published", "Publish…", disabled="Summarize first")),
    ],
)
def test_publish_to_gitlab_waits_for_a_summary_and_asks_for_a_link(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs).publish_gitlab == expected


def test_without_glab_publish_to_gitlab_says_how_to_install_it() -> None:
    view = shown([TRANSCRIBED, SUMMARIZED], missing_tools={"publish_gitlab": GLAB_MISSING})

    assert view.publish_gitlab == action(
        f"Not published · {GLAB_MISSING}", "Publish…", disabled=GLAB_MISSING
    )
    # Still recording, or without a summary, it says how to install glab all the same.
    assert shown(
        [], {"stopped_at": None}, missing_tools={"publish_gitlab": GLAB_MISSING}
    ).publish_gitlab == action(
        f"Not published · {GLAB_MISSING}", "Publish…", disabled="Still recording"
    )
    assert (
        shown([], missing_tools={"publish_gitlab": GLAB_MISSING}).publish_gitlab.disabled
        == GLAB_MISSING
    )


def test_while_the_window_publishes_to_gitlab_nothing_else_can_be_pressed() -> None:
    published = run("publish_gitlab", "succeeded", output=f"{ISSUE}#note_1", link=ISSUE)

    assert shown(
        [TRANSCRIBED, SUMMARIZED, published], running=("publish_gitlab",)
    ).publish_gitlab == action(
        "Running", "Publish again…", disabled="Publishing", output=f"{ISSUE}#note_1"
    )


GITHUB_ISSUE = "https://github.com/owner/repo/issues/7"
COMMENT = f"{GITHUB_ISSUE}#issuecomment-302"
GH_MISSING = "gh not found: install gh 2.45.0 or newer"


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([TRANSCRIBED, SUMMARIZED], action("Not published", "Publish…")),
        (
            [
                TRANSCRIBED,
                SUMMARIZED,
                run(
                    "publish_github",
                    "succeeded",
                    output=COMMENT,
                    link=GITHUB_ISSUE,
                    note="Published without the audio: GitHub takes no audio files",
                ),
            ],
            action(
                "Succeeded · Published without the audio: GitHub takes no audio files",
                "Publish again…",
                output=COMMENT,
                link=GITHUB_ISSUE,
            ),
        ),
        # Retry asks for the link again, starting from the failed run's.
        (
            [
                TRANSCRIBED,
                SUMMARIZED,
                run(
                    "publish_github",
                    "failed",
                    error="Could not comment on issue #7: boom",
                    details="run-2.log",
                    link=GITHUB_ISSUE,
                ),
            ],
            action(
                "Failed: Could not comment on issue #7: boom",
                "Retry…",
                failed=True,
                details="run-2.log",
                link=GITHUB_ISSUE,
            ),
        ),
        # Another publisher's link is not GitHub's to start from.
        (
            [TRANSCRIBED, SUMMARIZED, run("publish_gitlab", "failed", link=ISSUE)],
            action("Not published", "Publish…"),
        ),
        ([TRANSCRIBED], action("Not published", "Publish…", disabled="Summarize first")),
    ],
)
def test_publish_to_github_waits_for_a_summary_and_asks_for_a_link(
    runs: list[store.Run], expected: ActionView
) -> None:
    assert shown(runs).publish_github == expected


def test_without_gh_publish_to_github_says_how_to_install_it() -> None:
    view = shown([TRANSCRIBED, SUMMARIZED], missing_tools={"publish_github": GH_MISSING})

    assert view.publish_github == action(
        f"Not published · {GH_MISSING}", "Publish…", disabled=GH_MISSING
    )
    # Glab's absence is GitLab's alone.
    assert shown(
        [TRANSCRIBED, SUMMARIZED], missing_tools={"publish_gitlab": GLAB_MISSING}
    ).publish_github == action("Not published", "Publish…")


def test_while_the_window_publishes_to_github_nothing_else_can_be_pressed() -> None:
    published = run("publish_github", "succeeded", output=COMMENT, link=GITHUB_ISSUE)

    assert shown(
        [TRANSCRIBED, SUMMARIZED, published], running=("publish_github",)
    ).publish_github == action("Running", "Publish again…", disabled="Publishing", output=COMMENT)


def test_a_publish_to_github_command_that_died_says_so() -> None:
    assert unrecorded_failure(
        [run("publish_github", "running")],
        "publish_github",
        "Traceback:\nRuntimeError: boom\n",
        exited_ok=False,
        known=0,
    ) == ("Publish to GitHub stopped: RuntimeError: boom")


@pytest.mark.parametrize(
    ("runs", "warning"),
    [
        ([], False),
        ([run("transcribe", "failed")], True),
        ([run("publish_agaric", "failed")], True),
        ([run("transcribe", "failed"), run("transcribe", "queued")], False),
        ([run("transcribe", "cancelled"), run("publish_agaric", "succeeded")], False),
        ([run("transcribe", "succeeded"), run("publish_agaric", "failed")], True),
        ([run("summarize", "failed")], True),
        ([run("summarize", "failed"), run("summarize", "succeeded")], False),
        ([run("publish_gitlab", "failed")], True),
        ([run("publish_github", "failed")], True),
    ],
)
def test_a_warning_marks_a_recording_whose_last_run_of_an_action_failed(
    runs: list[store.Run], *, warning: bool
) -> None:
    assert shown(runs).warning is warning


@pytest.mark.parametrize(
    ("typed", "name"),
    [
        ("Weekly sync", "Weekly sync"),
        ("  Weekly sync \n", "Weekly sync"),
        ("Weekly\tsync  with\nAna", "Weekly sync with Ana"),
        ("", None),
        (" \t\n", None),
    ],
)
def test_clean_name_is_one_line_or_nothing(typed: str, name: str | None) -> None:
    assert clean_name(typed) == name


@given(st.text())
def test_a_clean_name_has_no_stray_space_and_cleans_to_itself(typed: str) -> None:
    name = clean_name(typed)

    if name is None:
        assert typed.split() == []
    else:
        assert name == " ".join(typed.split())
        assert clean_name(name) == name


@pytest.mark.parametrize(
    ("exited_ok", "runs", "stderr", "message"),
    [
        (True, [run("publish_agaric", "succeeded")], "", None),
        (False, [run("publish_agaric", "failed", error="Agaric isn't running")], "", None),
        (
            False,
            [],
            "usage: minutas [-h]\nminutas: error: invalid choice: 'publish'\n",
            "Publish to Agaric stopped: minutas: error: invalid choice: 'publish'",
        ),
        (
            False,
            [run("publish_agaric", "running")],
            "Traceback (most recent call last):\nKeyError: 'page'\n\n",
            "Publish to Agaric stopped: KeyError: 'page'",
        ),
        (
            False,
            [run("transcribe", "failed"), run("publish_agaric", "succeeded")],
            "",
            "Publish to Agaric stopped without saying why",
        ),
    ],
)
def test_a_publish_command_that_died_without_a_failed_run_is_reported(
    *, exited_ok: bool, runs: list[store.Run], stderr: str, message: str | None
) -> None:
    assert unrecorded_failure(runs, "publish_agaric", stderr, exited_ok=exited_ok, known=0) == (
        message
    )


def test_a_publish_to_gitlab_command_that_died_says_so() -> None:
    assert unrecorded_failure(
        [run("publish_gitlab", "running")],
        "publish_gitlab",
        "Traceback:\nRuntimeError: boom\n",
        exited_ok=False,
        known=0,
    ) == ("Publish to GitLab stopped: RuntimeError: boom")


def test_a_summarize_command_that_died_says_so_and_ignores_publish_runs() -> None:
    runs = [run("publish_agaric", "failed"), run("summarize", "running")]

    assert unrecorded_failure(
        runs, "summarize", "Traceback:\nRuntimeError: boom\n", exited_ok=False, known=0
    ) == ("Summarize stopped: RuntimeError: boom")
    assert (
        unrecorded_failure(
            [*runs, run("summarize", "failed")], "summarize", "", exited_ok=False, known=0
        )
        is None
    )


def test_an_earlier_failed_publish_does_not_hide_a_retry_that_died() -> None:
    # Retry on a failed publish whose command dies before it records a run: the newest publish
    # run is still the earlier failure, which says nothing about this press.
    earlier = [run("publish_agaric", "failed", error="Agaric isn't running")]

    assert unrecorded_failure(
        earlier,
        "publish_agaric",
        "ModuleNotFoundError: No module named 'keyring'\n",
        exited_ok=False,
        known=1,
    ) == ("Publish to Agaric stopped: ModuleNotFoundError: No module named 'keyring'")


def a_run() -> st.SearchStrategy[store.Run]:
    return st.builds(
        lambda action, status, error, details, output, choice, prompt, link: run(
            action,
            status,
            error=error if status == "failed" else None,
            details=details,
            output=output if status == "succeeded" else None,
            choice=choice if status == "failed" else None,
            prompt=prompt,
            link=link,
        ),
        st.sampled_from(store.ACTIONS),
        st.sampled_from(STATUSES),
        st.text(min_size=1, max_size=20),
        st.none() | st.just("run.log"),
        st.sampled_from(
            ["transcript.md", "agaric://page/01HX", "agaric://page/01HY", f"{ISSUE}#note_1"]
        ),
        st.sampled_from(
            [
                None,
                {"kind": "name_taken", "page_id": "01HX"},
                {"kind": "name_taken", "page_id": "01HY"},
                {"kind": "rename_taken"},
            ]
        ),
        st.sampled_from(["Summarize this meeting.", "Only the blockers."]),
        st.sampled_from(
            [ISSUE, "https://gitlab.example.com/group/project", "https://github.com/owner/repo"]
        ),
    )


Row = tuple[store.Recording, list[store.Run], tuple[str, ...], tuple[str, ...], dict[str, str]]


def a_row() -> st.SearchStrategy[Row]:
    return st.tuples(
        st.builds(
            lambda stopped, incomplete: recording(
                stopped_at="2026-11-18T13:36:16+01:00" if stopped else None, incomplete=incomplete
            ),
            st.booleans(),
            st.booleans(),
        ),
        st.lists(a_run(), max_size=8),
        st.sets(st.sampled_from(store.TRACKS)).map(lambda names: tuple(sorted(names))),
        st.sets(
            st.sampled_from(["publish_agaric", "publish_gitlab", "publish_github", "summarize"])
        ).map(tuple),
        st.fixed_dictionaries(
            {
                "summarize": st.sampled_from(["", MISSING]),
                "publish_gitlab": st.sampled_from(["", GLAB_MISSING]),
                "publish_github": st.sampled_from(["", GH_MISSING]),
            }
        ),
    )


def latest(runs: list[store.Run], action: str) -> tuple[int, store.Run] | None:
    of_action = [(index, run) for index, run in enumerate(runs) if run["action"] == action]
    return of_action[-1] if of_action else None


@given(a_row())
def test_nothing_goes_to_the_trash_while_it_is_recorded_transcribed_or_worked_on(
    case: Row,
) -> None:
    a_recording, runs, tracks, running, missing_tools = case

    view = row(a_recording, runs, tracks, running=running, missing_tools=missing_tools)

    stopped = a_recording["stopped_at"] is not None
    in_progress = any(
        r["action"] == "transcribe" and r["status"] in {"queued", "running"} for r in runs
    )
    assert (view.trash_disabled == "") is (stopped and not in_progress and not running)


@given(a_row())
def test_every_row_keeps_the_rules(case: Row) -> None:
    a_recording, runs, tracks, running, missing_tools = case
    missing, no_glab = missing_tools["summarize"], missing_tools["publish_gitlab"]
    no_gh = missing_tools["publish_github"]
    publishing, summarizing = "publish_agaric" in running, "summarize" in running
    publishing_gitlab = "publish_gitlab" in running
    publishing_github = "publish_github" in running
    stopped = a_recording["stopped_at"] is not None
    transcription, publication = latest(runs, "transcribe"), latest(runs, "publish_agaric")
    summary, gitlab_publication = latest(runs, "summarize"), latest(runs, "publish_gitlab")
    github_publication = latest(runs, "publish_github")

    view = row(
        a_recording,
        runs,
        tracks,
        running=running,
        missing_tools=missing_tools,
    )

    # Cancel is for the latest Transcribe run, while it is queued or running, and only then.
    transcribing = transcription is not None and transcription[1]["status"] in {"queued", "running"}
    assert (view.transcribe.label == "Cancel") is transcribing
    assert view.transcribe.cancels == (
        transcription[0] if transcribing and transcription is not None else None
    )
    assert view.publish.cancels is None
    assert view.publish_gitlab.cancels is None
    assert view.publish_github.cancels is None
    # Nothing starts a transcription without audio, or during the recording; nothing deletes
    # audio that is missing, being recorded, or being transcribed.
    # Cancel can always be pressed.
    assert (view.transcribe.disabled == "") is (transcribing or (stopped and tracks != ()))
    assert (view.delete_disabled == "") is (stopped and tracks != () and not transcribing)
    assert [track.file for track in view.tracks if track.available] == (
        [name for name in store.TRACKS if name in tracks] if stopped else []
    )
    # A failure is shown, with its reason, exactly when the latest run of the action failed.
    for shown_action, last, running, missing_tool in (
        (view.transcribe, transcription, False, ""),
        (view.summarize, summary, summarizing, missing),
        (view.publish, publication, publishing, ""),
        (view.publish_gitlab, gitlab_publication, publishing_gitlab, no_glab),
        (view.publish_github, github_publication, publishing_github, no_gh),
    ):
        failed = last is not None and last[1]["status"] == "failed"
        if not running:
            assert shown_action.failed is failed
            if failed and last is not None:
                assert shown_action.result.removesuffix(f" · {missing_tool}") == (
                    f"Failed: {last[1]['error']}"
                )
                assert shown_action.details == last[1]["details"]
            else:
                assert shown_action.details is None
            # Without its tool, the row says how to install it, whatever it shows besides.
            assert shown_action.result.endswith(f" · {missing_tool}") is bool(missing_tool)
    assert view.warning is any(
        last is not None and last[1]["status"] == "failed"
        for last in (transcription, summary, publication, gitlab_publication, github_publication)
    )
    # The link is the newest output the action ever produced.
    for shown_action, name in (
        (view.transcribe, "transcribe"),
        (view.summarize, "summarize"),
        (view.publish, "publish_agaric"),
        (view.publish_gitlab, "publish_gitlab"),
        (view.publish_github, "publish_github"),
    ):
        outputs = [r["output"] for r in runs if r["action"] == name and r["status"] == "succeeded"]
        assert shown_action.output == (outputs[-1] if outputs else None)
    # Rename, and Use that page when the run names one, answer the latest publish run, and
    # never while one is running.
    choice = None if publication is None else publication[1].get("choice")
    asked = None if publishing or not stopped else choice
    assert view.publish.renames is (asked is not None)
    assert view.publish.taken_by == (None if asked is None else asked.get("page_id"))
    assert (view.publish.disabled == "") is (stopped and not publishing)
    # Summarize needs a transcript, from a transcription that succeeded, and is pressed once at
    # a time; Retry, with the same prompt, follows a run that failed or was cancelled.
    transcribed = any(r["action"] == "transcribe" and r["status"] == "succeeded" for r in runs)
    assert (view.summarize.disabled == "") is (
        stopped and transcribed and not summarizing and not missing
    )
    retries = summary is not None and summary[1]["status"] in {"failed", "cancelled"}
    assert view.summarize.label == ("Retry" if retries and not summarizing else "")
    if retries and not summarizing and summary is not None:
        assert view.summarize.prompt == summary[1].get("prompt")
    assert view.summarize.menu == (
        "Summarize again" if view.summarize.output is not None else "Summarize"
    )
    # Publish to GitLab needs a summary and glab, and is pressed once at a time; every press
    # asks for a link, starting from the latest run's.
    summarized = view.summarize.output is not None
    assert (view.publish_gitlab.disabled == "") is (
        stopped and summarized and not publishing_gitlab and not no_glab
    )
    assert view.publish_gitlab.label.endswith("…")
    if not publishing_gitlab:
        assert view.publish_gitlab.link == (
            None if gitlab_publication is None else gitlab_publication[1].get("link")
        )
    assert (
        view.transcribe.menu,
        view.publish.menu,
        view.publish_gitlab.menu,
        view.publish_github.menu,
    ) == (None,) * 4


@given(a_row())
def test_publish_to_github_keeps_gitlabs_rules_with_gh(case: Row) -> None:
    a_recording, runs, tracks, running, missing_tools = case
    publishing = "publish_github" in running
    publication = latest(runs, "publish_github")

    view = row(a_recording, runs, tracks, running=running, missing_tools=missing_tools)

    summarized = view.summarize.output is not None
    assert (view.publish_github.disabled == "") is (
        a_recording["stopped_at"] is not None
        and summarized
        and not publishing
        and not missing_tools["publish_github"]
    )
    assert view.publish_github.label.endswith("…")
    if not publishing:
        assert view.publish_github.link == (
            None if publication is None else publication[1].get("link")
        )


def test_the_audio_files_say_until_when_the_age_limit_keeps_them() -> None:
    view = row(
        recording(),
        [run("transcribe", "succeeded", output="transcript.md")],
        store.TRACKS,
        running=(),
        missing_tools={},
        age_limit_days=30,
    )

    # In the recording's own time zone, as its start is.
    assert view.kept_until == "Kept until 2026-12-18 13:05"


@given(
    case=a_row(),
    age_limit_days=st.none() | st.integers(min_value=1, max_value=400),
    transcribed_last=st.booleans(),
)
def test_a_row_says_when_the_age_limit_deletes_its_audio_only_when_it_may(
    case: Row, age_limit_days: int | None, *, transcribed_last: bool
) -> None:
    a_recording, runs, tracks, running, missing_tools = case
    if transcribed_last:
        runs = [*runs, run("transcribe", "succeeded", output="transcript.md")]
    transcription = latest(runs, "transcribe")

    view = row(
        a_recording,
        runs,
        tracks,
        running=running,
        missing_tools=missing_tools,
        age_limit_days=age_limit_days,
    )

    # Audio is never deleted before it has been transcribed, nor while it is being recorded.
    may_go = (
        age_limit_days is not None
        and tracks != ()
        and a_recording["stopped_at"] is not None
        and transcription is not None
        and transcription[1]["status"] == "succeeded"
    )
    assert (view.kept_until != "") is may_go


BOTH_SPACES = "01HZPERSONAL\tPersonal\n01HZWORK\tWork\n"


def test_agaric_with_one_space_says_where_it_publishes_and_shows_it_chosen() -> None:
    assert agaric(
        "01HZWORK\tWork\n", "The stored agent token publishes to Work.\n", "01HZWORK"
    ) == AgaricView(
        said="The stored agent token publishes to Work.",
        choices=(("01HZWORK", "Work"),),
        chosen=0,
    )


@pytest.mark.parametrize(
    ("space_id", "chosen"), [("01HZWORK", 1), ("01HZPERSONAL", 0), (None, None), ("01HZGONE", None)]
)
def test_agaric_offers_several_spaces_by_name_with_the_one_set_chosen(
    space_id: str | None, chosen: int | None
) -> None:
    assert agaric(BOTH_SPACES, "said\n", space_id) == AgaricView(
        said="said",
        choices=(("01HZPERSONAL", "Personal"), ("01HZWORK", "Work")),
        chosen=chosen,
    )


def test_agaric_out_of_reach_says_the_fix_it_was_given_last() -> None:
    stderr = "Traceback, or a warning first\n  Agaric isn't running: start it  \n\n"

    assert agaric("", stderr, "01HZWORK") == AgaricView(
        said="Agaric isn't running: start it", choices=(), chosen=None
    )


def test_agaric_that_said_nothing_says_so() -> None:
    assert agaric("", "", None).said == "Asking Agaric stopped without saying why"


GOES = (
    "is over the new limits and is deleted now, for good. The transcripts and everything "
    "published stay."
)


@pytest.mark.parametrize(
    ("over", "said"),
    [
        (0, "No audio is over the new limits, so nothing is deleted now."),
        (1, f"The audio of 1 recording, 1.2 GB, {GOES}"),
        (3, f"The audio of 3 recordings, 1.2 GB, {GOES}"),
    ],
)
def test_the_limits_warning_says_how_much_audio_goes_now(over: int, said: str) -> None:
    assert limits_warning(over, "1.2 GB") == said


INSTALLED = "uv: ok\nApp: installed /home/ana/.local/share/minutas/app and the minutas command\n"
REOPEN = "Close this window and open it again to run the new version."
LOGGED_OUT = "\nLeft to do:\n- Log out and back in to load the top-bar extension.\n"


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        (
            f"Updating to v0.4.0.\n{INSTALLED}",
            UpdateView("Updated", f"Updated to v0.4.0. {REOPEN}"),
        ),
        (
            f"Updating to v0.4.0.\n{INSTALLED}{LOGGED_OUT}",
            UpdateView(
                "Updated",
                f"Updated to v0.4.0. {REOPEN} Log out and back in to load the top-bar extension.",
            ),
        ),
        (
            "Already on the latest release, v0.4.0.\nuv: ok\nApp: ok\n",
            UpdateView("Up to date", "Already on the latest release, v0.4.0."),
        ),
    ],
)
def test_an_update_that_worked_says_what_to_do_next(said: str, shown: UpdateView) -> None:
    assert update(said, exited_ok=True) == shown


def test_a_failed_update_says_the_fix_it_was_given_last() -> None:
    said = (
        "Updating to v0.4.0.\nSystem packages: ok\n"
        "Hugging Face token: failed: none in the GNOME keyring  \n\n"
    )

    assert update(said, exited_ok=False) == UpdateView(
        "Update failed", "Hugging Face token: failed: none in the GNOME keyring", failed=True
    )


def test_a_failed_update_that_said_nothing_says_so() -> None:
    assert update("", exited_ok=False).body == "minutas update stopped without saying why"
