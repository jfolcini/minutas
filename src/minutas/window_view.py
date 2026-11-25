"""What a recording's row in the Recordings window shows, worked out from its metadata and runs.

No GTK and no files here: the window draws what these functions return.
"""

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from minutas import store

TRANSCRIBE, SUMMARIZE, PUBLISH = "transcribe", "summarize", "publish_agaric"
PUBLISH_GITLAB, PUBLISH_GITHUB = "publish_gitlab", "publish_github"
# The actions the window runs as commands, by the title it gives them.
COMMAND_TITLES = {
    SUMMARIZE: "Summarize",
    PUBLISH: "Publish to Agaric",
    PUBLISH_GITLAB: "Publish to GitLab",
    PUBLISH_GITHUB: "Publish to GitHub",
}
TRACK_TITLES = dict(zip(store.TRACKS, ("Microphone", "Call"), strict=True))
# The row that plays the tracks that hold audio mixed, as Publish to Agaric mixes them.
MIXED, MIXED_TITLE = "mixed", "Microphone and call"
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0)
# The multiplication sign, as GNOME's typography writes a speed.
SPEED_LABELS = tuple(f"{speed:g}×" for speed in SPEEDS)  # noqa: RUF001
# Each starts the Rename dialog's body, a sentence of its own.
NAME_SOURCES = {
    "default": "Default name",
    "summary": "Proposed by the summary",
    "calendar": "From the calendar event",
    "typed": "Typed name",
}
RESULTS = {
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Succeeded",
    "cancelled": "Cancelled",
}
IN_PROGRESS = {"queued": "Waiting to transcribe", "running": "Transcribing"}
# The questions a failed publish run can ask that Rename answers.
NAME_TAKEN = {"name_taken", "rename_taken"}
STILL_RECORDING = "Still recording"
CANCEL_TRANSCRIPTION = "Cancel the transcription first"
NO_AUDIO = "The audio was deleted"
NO_TRANSCRIPT = "Transcribe first"
NO_SUMMARY = "Summarize first"
AGARIC_SILENT = "Asking Agaric stopped without saying why"
# What NFKD splits off a Latin letter: "é" becomes "e" and one of these.
ACCENTS = re.compile("[\u0300-\u036f]")
# What install.sh says, run by an update too, when GNOME Shell has a new extension to load.
LOG_OUT = "Log out and back in to load the top-bar extension."
UPDATE_SILENT = "minutas update stopped without saying why"


@dataclass(frozen=True)
class TrackView:
    # A track's file, or MIXED.
    key: str
    title: str
    subtitle: str
    # It can be played and saved.
    available: bool


@dataclass(frozen=True)
class SpeakerView:
    # The transcriber's key for the speaker: "Speaker 1".
    key: str
    # The name a person gave it; empty until one does.
    name: str


@dataclass(frozen=True)
class ActionView:
    """An action's last result and its button, the next step."""

    result: str
    label: str
    failed: bool = False
    # Why the button cannot be pressed; empty when it can.
    disabled: str = ""
    # The run the button cancels; None when it starts one.
    cancels: int | None = None
    # The failed run's log, a file in the recording's folder.
    details: str | None = None
    # The newest output: a file in the recording's folder, or a link.
    output: str | None = None
    # The name is taken: Rename, and Use that page when the run names the page that holds it,
    # take the button's place.
    renames: bool = False
    taken_by: str | None = None
    # The label of the menu of presets that starts a run; None for an action without one.
    menu: str | None = None
    # The prompt of the newest run, and the context presets it added, which Retry sends again.
    prompt: str | None = None
    context: tuple[str, ...] = ()
    # The link of the newest run, which the dialog that asks for one starts from.
    link: str | None = None
    # How much of what the running run is doing is done, from 0 to 1; None when none is running.
    progress: float | None = None


@dataclass(frozen=True)
class RowView:
    recording_id: str
    name: str
    name_source: str
    subtitle: str
    warning: bool
    tracks: tuple[TrackView, ...]
    # Why Delete now cannot be pressed; empty when it can.
    delete_disabled: str
    transcribe: ActionView
    # The call's speakers, in the order they first speak, and the names offered for them.
    speakers: tuple[SpeakerView, ...]
    attendees: tuple[str, ...]
    summarize: ActionView
    publish: ActionView
    publish_gitlab: ActionView
    publish_github: ActionView
    # When the age limit deletes the tracks; empty when it never will.
    kept_until: str = ""
    # Why Move to Trash refuses; empty when the recording can go.
    trash_disabled: str = ""


@dataclass(frozen=True)
class AgaricView:
    """What the Agaric settings show once `minutas settings agaric` has answered."""

    said: str
    # The spaces the agent may use, as id and name: the one set shows, even when it is the only one.
    choices: tuple[tuple[str, str], ...]
    # The place among choices of the space set; None when it is not among them.
    chosen: int | None


@dataclass(frozen=True)
class UpdateView:
    """What the window says once `minutas update` has ended."""

    heading: str
    body: str


def limits_warning(over: int, size: str) -> str:
    """What saving the audio limits deletes at once: over recordings' audio, size of it."""
    if over == 0:
        return "No audio is over the new limits, so nothing is deleted now."
    recordings = "1 recording" if over == 1 else f"{over} recordings"
    return (
        f"The audio of {recordings}, {size}, is over the new limits and is deleted now, for "
        "good. The transcripts and everything published stay."
    )


def duration(seconds: int) -> str:
    minutes = seconds // 60
    if minutes == 0:
        return f"{seconds} s"
    if minutes < 60:  # noqa: PLR2004 - minutes in an hour
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def clock(seconds: float) -> str:
    """A time in a recording as its player shows it: 4:05, or 1:02:03 past the hour."""
    minutes, whole = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{whole:02}" if hours else f"{minutes}:{whole:02}"


def clean_name(typed: str) -> str | None:
    """The name on one line, or None when nothing was typed."""
    return " ".join(typed.split()) or None


def folded(text: str) -> str:
    """The text as search compares it, without case or accents: "jose" finds "José"."""
    return ACCENTS.sub("", unicodedata.normalize("NFKD", text).casefold())


def searched(view: RowView, transcript: str) -> str:
    """What a search looks in: the name, the subtitle, which starts with the date, and the
    transcript, which holds the speakers' names; folded."""
    return folded(f"{view.name}\n{view.subtitle}\n{transcript}")


def matches(typed: str, text: str) -> bool:
    """Whether every word typed is in the text, folded already, whatever its case and accents."""
    return all(word in text for word in folded(typed).split())


def latest(runs: Sequence[store.Run], action: str) -> tuple[int, store.Run] | None:
    """The newest run of the action, with its place in the log."""
    return next(
        ((index, run) for index, run in reversed(list(enumerate(runs))) if run["action"] == action),
        None,
    )


def unrecorded_failure(
    runs: Sequence[store.Run], action: str, stderr: str, *, exited_ok: bool, known: int
) -> str | None:
    """What to say when the action's command failed and left no failed run to say it.

    `known` is how many runs the recording had when the command started: an older failed run
    of the action says nothing about this one.
    """
    last = latest(runs, action)
    if exited_ok or (last is not None and last[0] >= known and last[1]["status"] == "failed"):
        return None
    said = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not said:
        return f"{COMMAND_TITLES[action]} stopped without saying why"
    return f"{COMMAND_TITLES[action]} stopped: {said[-1]}"


def last_result(
    runs: Sequence[store.Run], action: str, never_run: str, start: str, again: str
) -> ActionView:
    """The action's newest run, and the button that starts its next one."""
    last = latest(runs, action)
    outputs = [r["output"] for r in runs if r["action"] == action and r["status"] == "succeeded"]
    output = outputs[-1] if outputs else None
    if last is None:
        return ActionView(result=never_run, label=start, output=output)
    run = last[1]
    if run["status"] == "failed":
        return ActionView(
            result=f"Failed: {run['error']}",
            label="Retry",
            failed=True,
            details=run["details"],
            output=output,
        )
    label = {"succeeded": again, "cancelled": "Retry"}.get(run["status"], start)
    said = [RESULTS[run["status"]], run.get("note")]
    return ActionView(
        result=" · ".join(part for part in said if part is not None), label=label, output=output
    )


def trash_refusal(
    recording: store.Recording, runs: Sequence[store.Run], running: Collection[str]
) -> str:
    """Why the recording can't go to the Trash now; empty when it can.

    running: the actions whose command the window is running for it. The log's running runs are
    not asked: a command stopped with Ctrl-C or killed leaves its run running until the next run
    of its action, and would keep the recording from the Trash for good.
    """
    if recording["stopped_at"] is None:
        return STILL_RECORDING
    # Any, not only the newest: the worker takes every queued run from the folder.
    if any(run["action"] == TRANSCRIBE and run["status"] in IN_PROGRESS for run in runs):
        return CANCEL_TRANSCRIPTION
    busy = (title for action, title in COMMAND_TITLES.items() if action in running)
    return next((f"{title} is still running" for title in busy), "")


def subtitle(recording: store.Recording, transcribing: str | None) -> str:
    # In the recording's own time zone, whatever the window's is.
    started = datetime.fromisoformat(recording["started_at"])
    stopped = recording["stopped_at"] is not None
    said = [
        started.strftime("%Y-%m-%d %H:%M"),
        duration(store.duration_seconds(recording)) if stopped else "Recording",
        "Paused" if not stopped and "paused_at" in recording else None,
        # Asked for, and it did not start: the mic track holds the call too.
        "No echo cancellation" if recording.get("echo_cancelled") is False else None,
        "Incomplete" if recording["incomplete"] else None,
        transcribing,
    ]
    return " · ".join(part for part in said if part is not None)


def link_publisher(
    runs: Sequence[store.Run], action: str, *, running: bool, tool_problem: str, disabled: str
) -> tuple[ActionView, bool]:
    """Publish to GitLab or GitHub, whose every press asks for a link, starting from the newest
    run's; and whether that run failed.

    tool_problem: why its tool is missing or too old, empty when it is there. disabled: why the
    button can't be pressed, empty when it can.
    """
    last = last_result(runs, action, "Not published", "Publish", "Publish again")
    label = f"{last.label}…"
    if running:
        shown = ActionView(result="Running", label=label, disabled="Publishing", output=last.output)
        return shown, last.failed
    newest = latest(runs, action)
    shown = replace(
        last,
        label=label,
        result=" · ".join(filter(None, [last.result, tool_problem])),
        link=None if newest is None else newest[1].get("link"),
        disabled=disabled,
    )
    return shown, last.failed


def row(  # noqa: PLR0913 - each is a separate input to the row; all after tracks go by name
    recording: store.Recording,
    runs: Sequence[store.Run],
    tracks: Collection[str],
    *,
    running: Collection[str],
    missing_tools: Mapping[str, str],
    age_limit_days: int | None = None,
) -> RowView:
    """The row of a recording.

    tracks: those of its two that hold audio. running: the actions whose command the window is
    running, whatever the run log says so far. missing_tools: by action, why the tool it runs
    is missing or too old, and how to install one that will do; empty or left out when the
    window found one. age_limit_days: how long kept audio is kept; None when that is not
    limited.
    """
    summarizer_missing = missing_tools.get(SUMMARIZE, "")
    glab_problem = missing_tools.get(PUBLISH_GITLAB, "")
    gh_problem = missing_tools.get(PUBLISH_GITHUB, "")
    publishing, summarizing = PUBLISH in running, SUMMARIZE in running
    stopped = recording["stopped_at"] is not None
    still_recording = "" if stopped else STILL_RECORDING
    no_audio = still_recording or ("" if tracks else NO_AUDIO)

    never_transcribed = "Not run yet" if stopped else "Starts when the recording stops"
    transcribe = replace(
        last_result(runs, TRANSCRIBE, never_transcribed, "Transcribe", "Run again"),
        disabled=no_audio,
    )
    transcription = latest(runs, TRANSCRIBE)
    transcribing = None if transcription is None else IN_PROGRESS.get(transcription[1]["status"])
    if transcription is not None and transcribing is not None:
        transcribe = replace(transcribe, label="Cancel", disabled="", cancels=transcription[0])
    if transcription is not None and transcription[1]["status"] == "running":
        progress = transcription[1].get("progress")
        if progress is not None:
            # The collapsed row says it too, in place of "Transcribing".
            transcribing = f"{progress['phase'].capitalize()} · {progress['percent']}%"
            transcribe = replace(transcribe, result=transcribing)
        transcribe = replace(
            transcribe, progress=0.0 if progress is None else progress["percent"] / 100
        )

    # Started from its menu of presets; its only button is Retry, after a run that did not end well.
    last_summary = last_result(runs, SUMMARIZE, "Not summarized", "", "")
    menu = "Summarize" if last_summary.output is None else "Summarize again"
    summarization = latest(runs, SUMMARIZE)
    if summarizing:
        summarize = ActionView(
            result="Running",
            label="",
            menu=menu,
            disabled="Summarizing",
            output=last_summary.output,
        )
    else:
        summarize = replace(
            last_summary,
            result=" · ".join(filter(None, [last_summary.result, summarizer_missing])),
            menu=menu,
            prompt=None if summarization is None else summarization[1].get("prompt"),
            context=() if summarization is None else tuple(summarization[1].get("context", ())),
            disabled=still_recording
            or summarizer_missing
            or ("" if transcribe.output is not None else NO_TRANSCRIPT),
        )

    last_publish = last_result(runs, PUBLISH, "Not published", "Publish", "Publish again")
    publication = latest(runs, PUBLISH)
    choice = None if publication is None else publication[1].get("choice")
    if publishing:
        publish = ActionView(
            result="Running", label="Publish", disabled="Publishing", output=last_publish.output
        )
    elif stopped and choice is not None and choice["kind"] in NAME_TAKEN:
        publish = replace(last_publish, renames=True, taken_by=choice.get("page_id"))
    else:
        publish = replace(last_publish, disabled=still_recording)

    no_summary = "" if last_summary.output is not None else NO_SUMMARY
    publish_gitlab, gitlab_failed = link_publisher(
        runs,
        PUBLISH_GITLAB,
        running=PUBLISH_GITLAB in running,
        tool_problem=glab_problem,
        disabled=still_recording or glab_problem or no_summary,
    )
    publish_github, github_failed = link_publisher(
        runs,
        PUBLISH_GITHUB,
        running=PUBLISH_GITHUB in running,
        tool_problem=gh_problem,
        disabled=still_recording or gh_problem or no_summary,
    )

    kept_until = ""
    if age_limit_days is not None and tracks and store.may_lose_audio(recording, runs):
        until = store.audio_kept_until(recording, age_limit_days)
        kept_until = f"Kept until {until:%Y-%m-%d %H:%M}"

    return RowView(
        recording_id=recording["id"],
        name=recording["name"],
        name_source=NAME_SOURCES[recording["name_source"]],
        subtitle=subtitle(recording, transcribing),
        warning=transcribe.failed
        or last_summary.failed
        or last_publish.failed
        or gitlab_failed
        or github_failed,
        tracks=tuple(
            TrackView(
                key=key,
                title=title,
                subtitle="" if stopped and holds_audio else ("Deleted" if stopped else "Recording"),
                available=stopped and holds_audio,
            )
            for key, title, holds_audio in (
                *((name, title, name in tracks) for name, title in TRACK_TITLES.items()),
                (MIXED, MIXED_TITLE, bool(tracks)),
            )
        ),
        delete_disabled=no_audio or ("" if transcribing is None else CANCEL_TRANSCRIPTION),
        transcribe=transcribe,
        speakers=tuple(
            SpeakerView(key=key, name="" if name == key else name)
            for key, name in recording.get("speakers", {}).items()
        ),
        attendees=tuple(recording.get("attendees", [])),
        summarize=summarize,
        publish=publish,
        publish_gitlab=publish_gitlab,
        publish_github=publish_github,
        kept_until=kept_until,
        trash_disabled=trash_refusal(recording, runs, running),
    )


def agaric(listed: str, stderr: str, space_id: str | None) -> AgaricView:
    """What the Agaric settings show, from the answer of `minutas settings agaric`.

    listed: its stdout, a space's id and name a line, tab between. stderr: what it said, where
    it publishes or why it can't, last. space_id: the space set once it exited.
    """
    choices = tuple(
        (space, name) for space, _, name in (line.partition("\t") for line in listed.splitlines())
    )
    said = [line.strip() for line in stderr.splitlines() if line.strip()]
    return AgaricView(
        said=said[-1] if said else AGARIC_SILENT,
        choices=choices,
        chosen=next((place for place, (space, _) in enumerate(choices) if space == space_id), None),
    )


def update(said: str, *, exited_ok: bool) -> UpdateView:
    """What the window says of an update, from all that `minutas update` printed: first which
    release it moved to, or that it was already on the latest, then what install.sh did."""
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    if not exited_ok:
        return UpdateView("Update failed", lines[-1] if lines else UPDATE_SILENT)
    first = lines[0] if lines else ""
    if first.startswith("Updating to "):
        release = first.removeprefix("Updating to ").removesuffix(".")
        heading = "Updated"
        body = f"Updated to {release}. Close this window and open it again to run the new version."
    else:
        heading, body = "Up to date", first
    return UpdateView(heading, f"{body} {LOG_OUT}" if LOG_OUT in said else body)
