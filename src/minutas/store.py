"""One folder per recording under DATA/recordings, and DATA/status.json for the extension.

A recording's transcript.md is written here, from the segments the transcriber leaves in
transcript.json and the names the call's speakers have in recording.json, every time either
changes: whatever reads it, publishing included, reads the current names.
"""

import fcntl
import json
import os
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from minutas import settings
from minutas.atomic import write_json, write_text

RECORDING_ID_FORMAT = "%Y-%m-%dT%H-%M-%S"
TRACKS = ("mic.opus", "call.opus")
# What each track records after Continue, until the transcription worker joins it onto the track:
# never a track itself, so nothing plays, sizes or publishes it.
PARTS = ("mic.continued.opus", "call.continued.opus")
# Each track with its part joined on, while the joiner writes and checks it.
JOINED = ("mic.joined.opus", "call.joined.opus")
SEGMENTS = "transcript.json"
TRANSCRIPT = "transcript.md"
# The transcriber's key for the mic track's speaker, who has no other name.
ME = "Me"
SUMMARY = "summary.md"
ACTIONS = ("transcribe", "summarize", "publish_agaric", "publish_gitlab", "publish_github")
# What status.json says of the recording in progress; the pause keys only once it has been
# paused, continued only once it has been continued, silent_since only while it is silent.
ACTIVE_IN_STATUS = ("id", "started_at", "paused_at", "paused_seconds", "continued", "silent_since")
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
    # When the pause it is in began; there only while it is paused.
    paused_at: NotRequired[str]
    # How long its ended pauses lasted, all together.
    paused_seconds: NotRequired[int]
    # Discard stopped it: its stop looks up no calendar event and queues no transcription.
    discarded: NotRequired[bool]
    # It was stopped and then recorded into again: Discard would take what it held before.
    continued: NotRequired[bool]
    # When neither track was heard any more, once that has lasted as long as the settings'
    # silence reminder; there only until either is heard again, it pauses or it stops.
    silent_since: NotRequired[str]
    # There when echo cancellation was on as it started: whether its mic track is PipeWire's
    # echo-cancelled mic, or the plain one because that did not start.
    echo_cancelled: NotRequired[bool]


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


class Progress(TypedDict):
    """How far a running transcription has got."""

    # What it is doing: one of the transcriber's phases.
    phase: str
    # The whole percent of the phase done.
    percent: int


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
    # What a Summarize run asked the summarizer, and the context presets it added by name, so
    # Retry asks the same.
    prompt: NotRequired[str]
    context: NotRequired[list[str]]
    # The link a Publish to GitLab or GitHub run posted to, which the next one offers.
    link: NotRequired[str]
    # How far a Transcribe run has got, while it runs.
    progress: NotRequired[Progress]


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


def end_pause(recording: Recording, ended: datetime) -> None:
    """Adds the pause it is in, if any, to its ended ones."""
    if "paused_at" not in recording:
        return
    lasted = int((ended - datetime.fromisoformat(recording["paused_at"])).total_seconds())
    # A recorder that died while paused stopped when its audio was last written, before the pause.
    recording["paused_seconds"] = recording.get("paused_seconds", 0) + max(0, lasted)
    del recording["paused_at"]


def duration_seconds(recording: Recording) -> int:
    """How long it recorded for, its pauses left out; nothing until it stops."""
    started = datetime.fromisoformat(recording["started_at"])
    stopped = datetime.fromisoformat(recording["stopped_at"] or recording["started_at"])
    # A recorder that died while paused may have last written its audio before earlier pauses.
    return max(0, int((stopped - started).total_seconds()) - recording.get("paused_seconds", 0))


def mark_paused(recording_id: str, paused: datetime) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording["paused_at"] = paused.isoformat(timespec="seconds")
        # Nothing is heard while paused, which is no silence: it is counted again from the resume.
        recording.pop("silent_since", None)
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_resumed(recording_id: str, resumed: datetime) -> None:
    with lock():
        recording = load_recording(recording_id)
        end_pause(recording, resumed)
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_silent(recording_id: str, since: datetime) -> None:
    with lock():
        recording = load_recording(recording_id)
        # The recorder can say it of a second from before a pause or a stop, which ended it.
        if "paused_at" in recording or recording.get("stopped_at") is not None:
            return
        recording["silent_since"] = since.isoformat(timespec="seconds")
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_heard(recording_id: str) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording.pop("silent_since", None)
        write_json(recording_dir(recording_id) / "recording.json", recording)


def reopen(recording: Recording, continued: datetime) -> None:
    """Opens a stopped recording again. The time since it stopped is one more pause: it has
    lasted as long as it had, and goes on from there."""
    recording["paused_at"] = cast("str", recording["stopped_at"])
    end_pause(recording, continued)
    recording["stopped_at"] = None
    recording["incomplete"] = False
    recording["continued"] = True
    # A discard the Trash refused is over: this stop transcribes it.
    recording.pop("discarded", None)


def mark_continued(recording_id: str, continued: datetime) -> None:
    with lock():
        recording = load_recording(recording_id)
        reopen(recording, continued)
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_discarded(recording_id: str, *, discarded: bool = True) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording["discarded"] = discarded
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_echo_cancelled(recording_id: str, *, echo_cancelled: bool) -> None:
    with lock():
        recording = load_recording(recording_id)
        recording["echo_cancelled"] = echo_cancelled
        write_json(recording_dir(recording_id) / "recording.json", recording)


def mark_stopped(recording_id: str, stopped: datetime, *, incomplete: bool) -> None:
    # Under the lock, so a name typed at the same moment is not written over, nor writes over this.
    with lock():
        recording = load_recording(recording_id)
        end_pause(recording, stopped)
        # A Continue counts silence afresh.
        recording.pop("silent_since", None)
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
    # A join that was cancelled leaves what it had written.
    for name in (*TRACKS, *PARTS, *JOINED):
        (recording_dir(recording_id) / name).unlink(missing_ok=True)


def audio_size(recording_id: str) -> int:
    """The bytes its tracks take."""
    folder = recording_dir(recording_id)
    return sum((folder / name).stat().st_size for name in TRACKS if (folder / name).is_file())


def audio_kept(recording_id: str) -> list[str]:
    """Its tracks and parts that hold audio."""
    folder = recording_dir(recording_id)
    return [name for name in (*TRACKS, *PARTS) if has_audio(folder / name)]


def continue_refusal(recording: Recording, runs: Sequence[Run], audio: Iterable[str]) -> str | None:
    """Why it can't be recorded into again, from its runs and audio_kept; None when it can."""
    transcriptions = [run["status"] for run in runs if run["action"] == "transcribe"]
    kept = set(audio)
    if recording["stopped_at"] is None:
        return "it is still being recorded"
    if transcriptions[-1:] == ["succeeded"]:
        return "it has been transcribed"
    if not kept.issuperset(TRACKS):
        return "its audio is gone"
    # A second part would write over the first.
    if kept.intersection(PARTS):
        return "what it recorded after the last Continue is still to be joined: transcribe it first"
    return None


def may_lose_audio(recording: Recording, runs: Sequence[Run]) -> bool:
    """Whether a limit may delete its tracks: a recording is never lost before it has been
    transcribed, so only a stopped one whose latest transcription succeeded."""
    transcriptions = [run["status"] for run in runs if run["action"] == "transcribe"]
    return recording["stopped_at"] is not None and transcriptions[-1:] == ["succeeded"]


def audio_kept_until(recording: Recording, age_limit_days: int) -> datetime:
    return datetime.fromisoformat(recording["started_at"]) + timedelta(days=age_limit_days)


def audio_over_limits(
    recordings: Sequence[tuple[Recording, Sequence[Run], int]],
    now: datetime,
    age_limit_days: int | None,
    size_limit_gb: int | None,
) -> list[str]:
    """The recordings whose tracks go, oldest first, from each one's runs and tracks' bytes.

    Those past the age limit go; then, while all the tracks together pass the size limit, the
    oldest of the rest go too, however young: size wins. None turns a limit off.
    """
    total = sum(size for _, _, size in recordings)
    gone: list[str] = []
    for recording, runs, size in sorted(
        recordings, key=lambda kept: datetime.fromisoformat(kept[0]["started_at"])
    ):
        if size == 0 or not may_lose_audio(recording, runs):
            continue
        # The age limit takes the oldest first too, so its recordings come before any other.
        too_old = age_limit_days is not None and now > audio_kept_until(recording, age_limit_days)
        too_big = size_limit_gb is not None and total > size_limit_gb * 10**9
        if too_old or too_big:
            gone.append(recording["id"])
            total -= size
    return gone


def recordings_over_limits(
    now: datetime, age_limit_days: int | None, size_limit_gb: int | None
) -> list[str]:
    """The recordings whose tracks the limits would delete now."""
    # By runs.json, which a recording gets only after its recording.json.
    ids = [path.parent.name for path in recordings_dir().glob("*/runs.json")]
    recordings = [(load_recording(i), load_runs(i), audio_size(i)) for i in ids]
    return audio_over_limits(recordings, now, age_limit_days, size_limit_gb)


def delete_audio_over_limits(
    now: datetime, age_limit_days: int | None, size_limit_gb: int | None
) -> None:
    """Deletes the tracks of the recordings over the limits, as Delete now does."""
    # Inside the lock, where no transcription can be queued between the choice and the delete.
    with lock():
        for recording_id in recordings_over_limits(now, age_limit_days, size_limit_gb):
            delete_tracks(recording_id)


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
    for name in (*TRACKS, *PARTS):
        if (folder / name).exists():
            written = datetime.fromtimestamp((folder / name).stat().st_mtime, tz=stopped.tzinfo)
            stopped = max(stopped, written)
    mark_stopped(recording["id"], stopped, incomplete=True)


def recent_entry(recording: Recording, *, idle: bool) -> dict[str, object]:
    """What status.json says of a stopped recording; idle: nothing is being recorded."""
    runs = load_runs(recording["id"])
    latest = {run["action"]: run["status"] for run in runs}
    transcriptions = [run for run in runs if run["action"] == "transcribe"]
    return {
        "id": recording["id"],
        "name": recording["name"],
        "started_at": recording["started_at"],
        "duration_seconds": duration_seconds(recording),
        "incomplete": recording["incomplete"],
        "transcribe": latest.get("transcribe"),
        "progress": transcriptions[-1].get("progress") if transcriptions else None,
        "failed": [action for action in ACTIONS if latest.get(action) == "failed"],
        "continuable": idle
        and continue_refusal(recording, runs, audio_kept(recording["id"])) is None,
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
            else {key: active[key] for key in ACTIVE_IN_STATUS if key in active},
            "recent": [
                recent_entry(r, idle=active is None) for r in stopped[: settings.recent_in_menu()]
            ],
        }
        write_json(data_dir() / "status.json", status)
