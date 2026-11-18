"""One folder per recording under DATA/recordings, and DATA/status.json for the extension.

A recording's transcript.md is written here, from the segments the transcriber leaves in
transcript.json and the names the call's speakers have in recording.json, every time either
changes: whatever reads it, publishing included, reads the current names.
"""

import fcntl
import json
import os
import tempfile
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

RECORDING_ID_FORMAT = "%Y-%m-%dT%H-%M-%S"
TRACKS = ("mic.opus", "call.opus")
SEGMENTS = "transcript.json"
TRANSCRIPT = "transcript.md"
# The transcriber's key for the mic track's speaker, who has no other name.
ME = "Me"
SUMMARY = "summary.md"
ACTIONS = ("transcribe", "summarize", "publish_agaric", "publish_gitlab")
RECENT_IN_STATUS = 5
INTERRUPTED = "interrupted"
# Where a name can come from, lowest first: a name gives way only to one from as high a source.
NAME_ORDER = ("default", "summary", "calendar", "typed")


class Recording(TypedDict):
    id: str
    started_at: str
    stopped_at: str | None
    incomplete: bool
    name: str
    # Where the name came from: one of NAME_ORDER.
    name_source: str
    # The calendar event's, for naming the speakers and boosting their names.
    attendees: NotRequired[list[str]]
    # The name of each speaker on the call, by the transcriber's key for it ("Speaker 1"),
    # which is also its name until a person gives it one.
    speakers: NotRequired[dict[str, str]]


class Segment(TypedDict):
    """What one speaker said in one turn, as the transcriber leaves it."""

    # Seconds into the recording.
    start: float
    end: float
    # A key: ME, or one of the call's speakers.
    speaker: str
    text: str


class Choice(TypedDict):
    """What a failed run asks before it can go on.

    "name_taken": another page holds the name, and page_id says which. "rename_taken": the typed
    name could not be applied to this recording's page, because another holds it.
    """

    kind: str
    page_id: NotRequired[str]


class Run(TypedDict):
    action: str
    status: str
    queued_at: str
    error: str | None
    # A file in the recording's folder: the full output of the tool that ran.
    details: str | None
    # A file in the recording's folder, or a link.
    output: str | None
    choice: NotRequired[Choice | None]
    # Something to know about a run that succeeded, such as what it left out.
    note: NotRequired[str | None]
    # What a Summarize run asked Devin, so Retry asks the same.
    prompt: NotRequired[str]


def data_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data_home) / "minutas"


def recordings_dir() -> Path:
    return data_dir() / "recordings"


def recording_dir(recording_id: str) -> Path:
    return recordings_dir() / recording_id


@contextmanager
def lock() -> Generator[None]:
    """Held across processes while a run log changes and while status.json is rewritten."""
    data_dir().mkdir(parents=True, exist_ok=True)
    with (data_dir() / "store.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def is_recording(recording_id: str) -> bool:
    folder = recording_dir(recording_id)
    return folder.parent == recordings_dir() and (folder / "recording.json").is_file()


def write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        # A rename that reaches the disk before the data leaves an empty file after a power cut.
        os.fsync(tmp.fileno())
    Path(tmp.name).replace(path)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2) + "\n")


def create_recording(started: datetime) -> Recording:
    recording = Recording(
        id=started.strftime(RECORDING_ID_FORMAT),
        started_at=started.isoformat(timespec="seconds"),
        stopped_at=None,
        incomplete=False,
        name="Meeting",
        name_source="default",
    )
    folder = recording_dir(recording["id"])
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    write_json(folder / "recording.json", recording)
    write_json(folder / "runs.json", [])
    return recording


def load_recording(recording_id: str) -> Recording:
    saved = json.loads((recording_dir(recording_id) / "recording.json").read_text())
    # A recording made before names had a source holds the default name.
    return {"name_source": "default", **saved}


def load_runs(recording_id: str) -> list[Run]:
    return cast("list[Run]", json.loads((recording_dir(recording_id) / "runs.json").read_text()))


def save_runs(recording_id: str, runs: list[Run]) -> None:
    write_json(recording_dir(recording_id) / "runs.json", runs)


def load_recordings() -> list[Recording]:
    """Newest first."""
    folders = sorted(recordings_dir().glob("*/recording.json"), reverse=True)
    return [load_recording(path.parent.name) for path in folders]


def has_audio(track: Path) -> bool:
    return track.is_file() and track.stat().st_size > 0


def holds_audio(recording_id: str) -> bool:
    return any(has_audio(recording_dir(recording_id) / name) for name in TRACKS)


def discard_if_silent(recording_id: str) -> None:
    """Removes a recording whose tracks hold no audio; one that captured anything is kept."""
    if holds_audio(recording_id):
        return
    folder = recording_dir(recording_id)
    # A status rewrite in another process must not find the folder half gone.
    with lock():
        for name in [*TRACKS, "recording.json", "runs.json"]:
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()


def mark_stopped(recording_id: str, stopped: datetime, *, incomplete: bool) -> None:
    # Under the lock, so a name typed at the same moment is not written over, nor writes over this.
    with lock():
        recording = load_recording(recording_id)
        recording["stopped_at"] = stopped.isoformat(timespec="seconds")
        recording["incomplete"] = incomplete
        write_json(recording_dir(recording_id) / "recording.json", recording)


def run_log(recording_id: str, index: int) -> Path:
    return recording_dir(recording_id) / f"run-{index}.log"


def written_log(recording_id: str, index: int) -> str | None:
    """The run's log file name, when its tool got far enough to write one."""
    log = run_log(recording_id, index)
    return log.name if log.is_file() else None


def fail_running(recording_id: str, runs: list[Run], action: str, error: str) -> None:
    for index, run in enumerate(runs):
        if run["action"] == action and run["status"] == "running":
            run["status"] = "failed"
            run["error"] = error
            run["details"] = written_log(recording_id, index)


def start_run(recording_id: str, run: Run) -> tuple[int, list[Run]]:
    """Appends a running run of a command; returns its index and the log with it.

    A run of the same action still running is one a crash left behind: it fails as interrupted.
    """
    with lock():
        runs = load_runs(recording_id)
        fail_running(recording_id, runs, run["action"], INTERRUPTED)
        runs.append(run)
        save_runs(recording_id, runs)
    write_status()
    return len(runs) - 1, runs


def end_run(recording_id: str, index: int, run: Run) -> None:
    with lock():
        runs = load_runs(recording_id)
        runs[index] = run
        save_runs(recording_id, runs)
    write_status()


def fail_left_running(recording_id: str, action: str, error: str) -> None:
    """Fails the run of a command that died before it could end it."""
    with lock():
        runs = load_runs(recording_id)
        fail_running(recording_id, runs, action, error)
        save_runs(recording_id, runs)
    write_status()


def set_typed_name(recording_id: str, name: str) -> None:
    """A typed name is kept for good: nothing automatic replaces it."""
    with lock():
        recording = load_recording(recording_id)
        recording["name"] = name
        recording["name_source"] = "typed"
        write_json(recording_dir(recording_id) / "recording.json", recording)


def chosen_name(recording: Recording, name: str, source: str) -> tuple[str, str]:
    """The name and source a recording keeps when offered another name (design § Page name)."""
    name = name.strip()
    if name and NAME_ORDER.index(source) >= NAME_ORDER.index(recording["name_source"]):
        return name, source
    return recording["name"], recording["name_source"]


def set_calendar_event(recording_id: str, name: str, attendees: list[str]) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording["name"], recording["name_source"] = chosen_name(recording, name, "calendar")
        recording["attendees"] = attendees
        write_json(recording_dir(recording_id) / "recording.json", recording)


def format_timestamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02}:{whole // 60 % 60:02}:{whole % 60:02}"


def format_transcript(segments: Iterable[Segment], names: Mapping[str, str]) -> str:
    """One paragraph per segment, under its speaker's name (design § Agaric contract)."""
    return "\n".join(
        f"[{format_timestamp(segment['start'])}] "
        f"**{names.get(segment['speaker'], segment['speaker'])}:** {segment['text']}\n"
        for segment in segments
    )


def load_segments(recording_id: str) -> list[Segment]:
    segments = (recording_dir(recording_id) / SEGMENTS).read_text(encoding="utf-8")
    return cast("list[Segment]", json.loads(segments))


def save_speakers(recording: Recording, segments: list[Segment], speakers: dict[str, str]) -> None:
    """Keeps the speakers' names and writes the transcript under them; inside the lock."""
    recording["speakers"] = speakers
    folder = recording_dir(recording["id"])
    write_json(folder / "recording.json", recording)
    write_text(folder / TRANSCRIPT, format_transcript(segments, speakers))


def write_transcript(recording_id: str) -> None:
    """Writes transcript.md for the segments the transcriber left.

    The call's speakers in them are the recording's from now on: one heard before keeps the name
    it had, so transcribing again loses no name.
    """
    with lock():
        recording = load_recording(recording_id)
        segments = load_segments(recording_id)
        named = recording.get("speakers", {})
        heard = dict.fromkeys(
            segment["speaker"] for segment in segments if segment["speaker"] != ME
        )
        save_speakers(recording, segments, {key: named.get(key, key) for key in heard})


def set_speaker_name(recording_id: str, speaker: str, name: str) -> None:
    with lock():
        recording = load_recording(recording_id)
        speakers = {**recording.get("speakers", {}), speaker: name}
        save_speakers(recording, load_segments(recording_id), speakers)


def set_summary_name(recording_id: str, name: str) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording["name"], recording["name_source"] = chosen_name(recording, name, "summary")
        write_json(recording_dir(recording_id) / "recording.json", recording)


def delete_tracks(recording_id: str) -> None:
    """Removes the audio; the recording, its transcript and its run log stay."""
    for name in TRACKS:
        (recording_dir(recording_id) / name).unlink(missing_ok=True)


def open_recording() -> Recording | None:
    return next((r for r in load_recordings() if r["stopped_at"] is None), None)


def close_abandoned() -> None:
    """Marks a recording whose recorder is gone without a stop as incomplete.

    It stops when its audio was last written.
    """
    recording = open_recording()
    if recording is None:
        return
    folder = recording_dir(recording["id"])
    stopped = datetime.fromisoformat(recording["started_at"])
    for name in TRACKS:
        if (folder / name).exists():
            written = datetime.fromtimestamp((folder / name).stat().st_mtime, tz=stopped.tzinfo)
            stopped = max(stopped, written)
    mark_stopped(recording["id"], stopped, incomplete=True)


def recent_entry(recording: Recording) -> dict[str, object]:
    started = datetime.fromisoformat(recording["started_at"])
    stopped = datetime.fromisoformat(recording["stopped_at"] or recording["started_at"])
    latest = {run["action"]: run["status"] for run in load_runs(recording["id"])}
    return {
        "id": recording["id"],
        "name": recording["name"],
        "started_at": recording["started_at"],
        "duration_seconds": int((stopped - started).total_seconds()),
        "incomplete": recording["incomplete"],
        "transcribe": latest.get("transcribe"),
        "failed": [action for action in ACTIONS if latest.get(action) == "failed"],
    }


def write_status() -> None:
    # Read and written under one lock, so the last process to write wrote the newest state.
    with lock():
        recordings = load_recordings()
        active = next((r for r in recordings if r["stopped_at"] is None), None)
        stopped = [r for r in recordings if r["stopped_at"] is not None]
        status = {
            "version": 1,
            "recording": None
            if active is None
            else {"id": active["id"], "started_at": active["started_at"]},
            "recent": [recent_entry(r) for r in stopped[:RECENT_IN_STATUS]],
        }
        write_json(data_dir() / "status.json", status)
