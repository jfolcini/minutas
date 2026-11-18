import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from minutas import store

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)


def make_stopped(started: datetime, *, seconds: int = 60, incomplete: bool = False) -> str:
    recording = store.create_recording(started, echo_cancel=False)
    store.mark_stopped(recording["id"], started + timedelta(seconds=seconds), incomplete=incomplete)
    return recording["id"]


def write_runs(recording_id: str, runs: list[dict[str, object]]) -> None:
    (store.recording_dir(recording_id) / "runs.json").write_text(json.dumps(runs))


def read_status() -> dict[str, object]:
    return cast("dict[str, object]", json.loads((store.data_dir() / "status.json").read_text()))


def test_data_dir_follows_xdg_data_home(xdg_dirs: Path) -> None:
    assert store.data_dir() == xdg_dirs / "data" / "minutas"


@pytest.mark.parametrize("value", [None, ""])
def test_data_dir_defaults_to_local_share(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    if value is None:
        monkeypatch.delenv("XDG_DATA_HOME")
    else:
        monkeypatch.setenv("XDG_DATA_HOME", value)

    assert store.data_dir() == tmp_path / ".local" / "share" / "minutas"


def test_create_recording_writes_metadata_and_empty_run_log() -> None:
    recording = store.create_recording(START, echo_cancel=False)

    folder = store.data_dir() / "recordings" / "2026-11-18T13-05-42"
    assert recording["id"] == "2026-11-18T13-05-42"
    assert json.loads((folder / "recording.json").read_text()) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": None,
        "incomplete": False,
        "name": "Meeting",
        "name_source": "default",
        "echo_cancel": False,
    }
    assert json.loads((folder / "runs.json").read_text()) == []


def test_create_recording_never_overwrites_an_existing_one() -> None:
    store.create_recording(START, echo_cancel=False)
    store.mark_stopped("2026-11-18T13-05-42", START + timedelta(seconds=5), incomplete=False)

    with pytest.raises(FileExistsError):
        store.create_recording(START, echo_cancel=False)

    assert store.load_recording("2026-11-18T13-05-42")["stopped_at"] == "2026-11-18T13:05:47+00:00"


def test_discard_recording_removes_a_recording_without_audio() -> None:
    store.create_recording(START, echo_cancel=False)

    store.discard_recording("2026-11-18T13-05-42")

    assert not store.recording_dir("2026-11-18T13-05-42").exists()


def test_discard_recording_keeps_captured_audio() -> None:
    store.create_recording(START, echo_cancel=False)
    track = store.recording_dir("2026-11-18T13-05-42") / "mic.opus"
    track.write_bytes(b"OggS")

    with pytest.raises(OSError, match="not empty"):
        store.discard_recording("2026-11-18T13-05-42")

    assert track.read_bytes() == b"OggS"


def test_mark_stopped_records_stop_time_and_completeness() -> None:
    recording_id = make_stopped(START, seconds=90, incomplete=True)

    recording = store.load_recording(recording_id)
    assert recording["stopped_at"] == "2026-11-18T13:07:12+00:00"
    assert recording["incomplete"] is True


def test_open_recording_is_the_one_not_stopped() -> None:
    make_stopped(START)
    store.create_recording(START + timedelta(hours=1), echo_cancel=False)

    open_recording = store.open_recording()

    assert open_recording is not None
    assert open_recording["id"] == "2026-11-18T14-05-42"


def test_open_recording_is_none_when_all_stopped() -> None:
    make_stopped(START)

    assert store.open_recording() is None


def test_close_abandoned_stops_at_the_last_audio_written() -> None:
    store.create_recording(START, echo_cancel=False)
    folder = store.recording_dir("2026-11-18T13-05-42")
    for name, seconds in [("mic.opus", 120), ("call.opus", 125)]:
        (folder / name).write_bytes(b"OggS")
        mtime = (START + timedelta(seconds=seconds)).timestamp()
        os.utime(folder / name, (mtime, mtime))

    store.close_abandoned()

    recording = store.load_recording("2026-11-18T13-05-42")
    assert recording["stopped_at"] == "2026-11-18T13:07:47+00:00"
    assert recording["incomplete"] is True


def test_close_abandoned_without_audio_stops_at_the_start() -> None:
    store.create_recording(START, echo_cancel=False)

    store.close_abandoned()

    recording = store.load_recording("2026-11-18T13-05-42")
    assert recording["stopped_at"] == "2026-11-18T13:05:42+00:00"
    assert recording["incomplete"] is True


def test_close_abandoned_leaves_stopped_recordings_alone() -> None:
    recording_id = make_stopped(START, seconds=60)

    store.close_abandoned()

    recording = store.load_recording(recording_id)
    assert recording["stopped_at"] == "2026-11-18T13:06:42+00:00"
    assert recording["incomplete"] is False


def test_status_with_nothing_recorded() -> None:
    store.write_status()

    assert read_status() == {"version": 1, "recording": None, "recent": []}


def test_status_lists_the_active_recording_and_five_newest_stopped() -> None:
    for hour in range(6):
        make_stopped(START + timedelta(hours=hour), seconds=1834 + hour)
    store.create_recording(START + timedelta(hours=7), echo_cancel=False)

    store.write_status()

    status = read_status()
    assert status["recording"] == {
        "id": "2026-11-18T20-05-42",
        "started_at": "2026-11-18T20:05:42+00:00",
    }
    recent = status["recent"]
    assert isinstance(recent, list)
    assert [entry["id"] for entry in recent] == [
        "2026-11-18T18-05-42",
        "2026-11-18T17-05-42",
        "2026-11-18T16-05-42",
        "2026-11-18T15-05-42",
        "2026-11-18T14-05-42",
    ]
    assert recent[0] == {
        "id": "2026-11-18T18-05-42",
        "name": "Meeting",
        "started_at": "2026-11-18T18:05:42+00:00",
        "duration_seconds": 1839,
        "incomplete": False,
        "transcribe": None,
        "failed": [],
    }


def test_status_shows_an_incomplete_recording() -> None:
    make_stopped(START, seconds=7, incomplete=True)

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert recent[0]["incomplete"] is True
    assert recent[0]["duration_seconds"] == 7


def test_status_takes_transcribe_and_failed_from_the_latest_runs() -> None:
    recording_id = make_stopped(START)
    write_runs(
        recording_id,
        [
            {"action": "publish_gitlab", "status": "failed"},
            {"action": "transcribe", "status": "failed"},
            {"action": "summarize", "status": "failed"},
            {"action": "transcribe", "status": "running"},
            {"action": "publish_agaric", "status": "failed"},
            {"action": "summarize", "status": "succeeded"},
        ],
    )

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert recent[0]["transcribe"] == "running"
    assert recent[0]["failed"] == ["publish_agaric", "publish_gitlab"]


def test_status_replaces_the_file_without_leaving_temporary_files() -> None:
    store.write_status()
    make_stopped(START)

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert len(recent) == 1
    assert sorted(path.name for path in store.data_dir().iterdir()) == [
        "recordings",
        "status.json",
    ]
