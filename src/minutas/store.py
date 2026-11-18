"""One folder per recording under DATA/recordings, and DATA/status.json for the extension."""

import fcntl
import json
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

RECORDING_ID_FORMAT = "%Y-%m-%dT%H-%M-%S"
TRACKS = ("mic.opus", "call.opus")
ACTIONS = ("transcribe", "summarize", "publish_agaric", "publish_gitlab")
RECENT_IN_STATUS = 5


class Recording(TypedDict):
    id: str
    started_at: str
    stopped_at: str | None
    incomplete: bool
    name: str


class Run(TypedDict):
    action: str
    status: str
    queued_at: str
    error: str | None
    # A file in the recording's folder: the full output of the tool that ran.
    details: str | None
    # A file in the recording's folder, or a link.
    output: str | None


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


def write_json(path: Path, value: object) -> None:
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        json.dump(value, tmp, indent=2)
        tmp.write("\n")
        tmp.flush()
        # A rename that reaches the disk before the data leaves an empty file after a power cut.
        os.fsync(tmp.fileno())
    Path(tmp.name).replace(path)


def create_recording(started: datetime) -> Recording:
    recording = Recording(
        id=started.strftime(RECORDING_ID_FORMAT),
        started_at=started.isoformat(timespec="seconds"),
        stopped_at=None,
        incomplete=False,
        name="Meeting",
    )
    folder = recording_dir(recording["id"])
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    write_json(folder / "recording.json", recording)
    write_json(folder / "runs.json", [])
    return recording


def load_recording(recording_id: str) -> Recording:
    return cast(
        "Recording", json.loads((recording_dir(recording_id) / "recording.json").read_text())
    )


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
    recording = load_recording(recording_id)
    recording["stopped_at"] = stopped.isoformat(timespec="seconds")
    recording["incomplete"] = incomplete
    write_json(recording_dir(recording_id) / "recording.json", recording)


def set_typed_name(recording_id: str, name: str) -> None:
    """A typed name is kept for good: nothing automatic replaces it."""
    path = recording_dir(recording_id) / "recording.json"
    recording = cast("dict[str, object]", json.loads(path.read_text()))
    write_json(path, {**recording, "name": name, "name_source": "typed"})


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
