"""What a recording's row in the history window shows, worked out from its metadata and runs.

No GTK and no files here: the window draws what these functions return.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from minutas import store

TRANSCRIBE, PUBLISH = "transcribe", "publish_agaric"
TRACK_TITLES = dict(zip(store.TRACKS, ("Microphone", "Call"), strict=True))
NAME_SOURCES = {"default": "Default name", "typed": "Typed name"}
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
NO_AUDIO = "The audio was deleted"


@dataclass(frozen=True)
class TrackView:
    file: str
    title: str
    subtitle: str
    # It can be played and saved.
    available: bool


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
    publish: ActionView


def duration(seconds: int) -> str:
    minutes = seconds // 60
    if minutes == 0:
        return f"{seconds} s"
    if minutes < 60:  # noqa: PLR2004 - minutes in an hour
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def clean_name(typed: str) -> str | None:
    """The name on one line, or None when nothing was typed."""
    return " ".join(typed.split()) or None


def latest(runs: Sequence[store.Run], action: str) -> tuple[int, store.Run] | None:
    """The newest run of the action, with its place in the log."""
    return next(
        ((index, run) for index, run in reversed(list(enumerate(runs))) if run["action"] == action),
        None,
    )


def unrecorded_failure(runs: Sequence[store.Run], stderr: str, *, exited_ok: bool) -> str | None:
    """What to say when the publish command failed and left no failed run to say it."""
    last = latest(runs, PUBLISH)
    if exited_ok or (last is not None and last[1]["status"] == "failed"):
        return None
    said = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not said:
        return "Publish to Agaric stopped without saying why"
    return f"Publish to Agaric stopped: {said[-1]}"


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


def subtitle(recording: store.Recording, transcribing: str | None) -> str:
    # In the recording's own time zone, whatever the window's is.
    started = datetime.fromisoformat(recording["started_at"])
    stopped_at = recording["stopped_at"]
    said = [
        started.strftime("%Y-%m-%d %H:%M"),
        "Recording"
        if stopped_at is None
        else duration(int((datetime.fromisoformat(stopped_at) - started).total_seconds())),
        "Incomplete" if recording["incomplete"] else None,
        transcribing,
    ]
    return " · ".join(part for part in said if part is not None)


def row(
    recording: store.Recording,
    runs: Sequence[store.Run],
    tracks: Collection[str],
    *,
    publishing: bool,
) -> RowView:
    """The row of a recording.

    tracks: those of its two that hold audio. publishing: the window is running its publish
    command, whatever the run log says so far.
    """
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

    return RowView(
        recording_id=recording["id"],
        name=recording["name"],
        name_source=NAME_SOURCES[recording["name_source"]],
        subtitle=subtitle(recording, transcribing),
        warning=transcribe.failed or last_publish.failed,
        tracks=tuple(
            TrackView(
                file=name,
                title=title,
                subtitle=""
                if stopped and name in tracks
                else ("Deleted" if stopped else "Recording"),
                available=stopped and name in tracks,
            )
            for name, title in TRACK_TITLES.items()
        ),
        delete_disabled=no_audio
        or ("" if transcribing is None else "Cancel the transcription first"),
        transcribe=transcribe,
        publish=publish,
    )
