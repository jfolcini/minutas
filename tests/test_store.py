import fcntl
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import store

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)


def make_stopped(started: datetime, *, seconds: int = 60, incomplete: bool = False) -> str:
    recording = store.create_recording(started)
    store.mark_stopped(recording["id"], started + timedelta(seconds=seconds), incomplete=incomplete)
    return recording["id"]


def write_runs(recording_id: str, runs: list[dict[str, object]]) -> None:
    (store.recording_dir(recording_id) / "runs.json").write_text(json.dumps(runs))


def read_status() -> dict[str, object]:
    return cast("dict[str, object]", json.loads((store.data_dir() / "status.json").read_text()))


def said(start: float, speaker: str, text: str) -> store.Segment:
    return store.Segment(start=start, end=start + 3.0, speaker=speaker, text=text)


SAID = [
    said(4.0, "Speaker 1", "Arrancamos con el release."),
    said(11.0, "Me", "CI is green since this morning."),
    said(3725.9, "Speaker 2", "Año nuevo, <deploy> & PR."),
    said(3730.0, "Speaker 1", "Dale."),
]


def write_segments(recording_id: str, segments: list[store.Segment]) -> None:
    """As the transcriber leaves them."""
    path = store.recording_dir(recording_id) / "transcript.json"
    path.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")


def read_transcript(recording_id: str) -> str:
    return (store.recording_dir(recording_id) / "transcript.md").read_text(encoding="utf-8")


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
    recording = store.create_recording(START)

    folder = store.data_dir() / "recordings" / "2026-11-18T13-05-42"
    assert recording["id"] == "2026-11-18T13-05-42"
    assert json.loads((folder / "recording.json").read_text()) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": None,
        "incomplete": False,
        "name": "Meeting",
        "name_source": "default",
    }
    assert json.loads((folder / "runs.json").read_text()) == []


def test_a_recording_made_before_names_had_a_source_holds_the_default_name() -> None:
    store.create_recording(START)
    path = store.recording_dir("2026-11-18T13-05-42") / "recording.json"
    before = json.loads(path.read_text())
    del before["name_source"]
    path.write_text(json.dumps(before))

    assert store.load_recording("2026-11-18T13-05-42") == {**before, "name_source": "default"}


def test_set_typed_name_keeps_the_name_and_says_it_was_typed() -> None:
    recording_id = make_stopped(START, seconds=90)

    store.set_typed_name(recording_id, "Weekly sync")

    assert store.load_recording(recording_id) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": "2026-11-18T13:07:12+00:00",
        "incomplete": False,
        "name": "Weekly sync",
        "name_source": "typed",
    }


def test_set_calendar_event_names_the_recording_and_keeps_the_attendees() -> None:
    recording_id = make_stopped(START, seconds=90)

    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz", "bob.ruiz"])

    assert json.loads((store.recording_dir(recording_id) / "recording.json").read_text()) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": "2026-11-18T13:07:12+00:00",
        "incomplete": False,
        "name": "Weekly sync",
        "name_source": "calendar",
        "attendees": ["Ana Díaz", "bob.ruiz"],
    }


def test_set_calendar_event_keeps_a_typed_name() -> None:
    recording_id = make_stopped(START)
    store.set_typed_name(recording_id, "Planning")

    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz"])

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Planning", "typed")
    assert recording.get("attendees") == ["Ana Díaz"]


def test_an_event_without_a_name_keeps_the_default_name() -> None:
    recording_id = make_stopped(START)

    store.set_calendar_event(recording_id, " ", ["Ana Díaz"])

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Meeting", "default")


def test_set_summary_name_names_a_recording_that_has_only_the_default_name() -> None:
    recording_id = make_stopped(START)

    store.set_summary_name(recording_id, " Release plan ")

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Release plan", "summary")


@pytest.mark.parametrize("source", ["calendar", "typed"])
def test_set_summary_name_keeps_a_calendar_or_typed_name(source: str) -> None:
    recording_id = make_stopped(START)
    if source == "typed":
        store.set_typed_name(recording_id, "Weekly sync")
    else:
        store.set_calendar_event(recording_id, "Weekly sync", [])

    store.set_summary_name(recording_id, "Release plan")

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Weekly sync", source)


def default_recording() -> store.Recording:
    return store.Recording(
        id="2026-11-18T13-05-42",
        started_at="2026-11-18T13:05:42+00:00",
        stopped_at=None,
        incomplete=False,
        name="Meeting",
        name_source="default",
    )


@pytest.mark.parametrize(
    ("current", "offered", "kept"),
    [
        (("Meeting", "default"), ("Weekly sync", "calendar"), ("Weekly sync", "calendar")),
        (("Meeting", "default"), ("Release plan", "summary"), ("Release plan", "summary")),
        (("Weekly sync", "calendar"), ("Release plan", "summary"), ("Weekly sync", "calendar")),
        (("Release plan", "summary"), ("Weekly sync", "calendar"), ("Weekly sync", "calendar")),
        (("Weekly sync", "calendar"), ("Standup", "calendar"), ("Standup", "calendar")),
        (("Planning", "typed"), ("Weekly sync", "calendar"), ("Planning", "typed")),
        (("Weekly sync", "calendar"), ("Planning", "typed"), ("Planning", "typed")),
        (("Meeting", "default"), (" Weekly sync ", "calendar"), ("Weekly sync", "calendar")),
        (("Meeting", "default"), ("  ", "calendar"), ("Meeting", "default")),
    ],
)
def test_page_name_order_examples(
    current: tuple[str, str], offered: tuple[str, str], kept: tuple[str, str]
) -> None:
    recording = default_recording()
    recording["name"], recording["name_source"] = current

    assert store.chosen_name(recording, *offered) == kept


offers = st.lists(
    st.tuples(
        st.sampled_from(["", " ", "Weekly sync", " Standup", "Planning "]),
        st.sampled_from(["summary", "calendar", "typed"]),
    ),
    max_size=6,
)


@given(offers=offers)
def test_the_name_kept_is_the_latest_from_the_highest_source_that_gave_one(
    offers: list[tuple[str, str]],
) -> None:
    recording = default_recording()

    for name, source in offers:
        recording["name"], recording["name_source"] = store.chosen_name(recording, name, source)

    given_names = [(name.strip(), source) for name, source in offers if name.strip()]
    rank = ["summary", "calendar", "typed"].index
    if not given_names:
        assert (recording["name"], recording["name_source"]) == ("Meeting", "default")
    else:
        highest = max(rank(source) for _, source in given_names)
        latest = [named for named in given_names if rank(named[1]) == highest][-1]
        assert (recording["name"], recording["name_source"]) == latest


def test_set_typed_name_fails_for_a_recording_that_is_gone() -> None:
    with pytest.raises(FileNotFoundError):
        store.set_typed_name("2026-11-18T13-05-42", "Weekly sync")

    assert not store.recordings_dir().exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda: store.set_typed_name("2026-11-18T13-05-42", "Weekly sync"),
        lambda: store.mark_stopped("2026-11-18T13-05-42", START, incomplete=False),
        lambda: store.set_calendar_event("2026-11-18T13-05-42", "Weekly sync", ["Ana Díaz"]),
        lambda: store.write_transcript("2026-11-18T13-05-42"),
        lambda: store.set_speaker_name("2026-11-18T13-05-42", "Speaker 1", "Ana Díaz"),
        lambda: store.set_summary_name("2026-11-18T13-05-42", "Release plan"),
    ],
)
def test_a_recording_is_read_and_rewritten_inside_the_store_lock(
    change: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create_recording(START)
    write_segments("2026-11-18T13-05-42", SAID)
    held: list[bool] = []
    load_recording, write_json = store.load_recording, store.write_json

    def spy_load(recording_id: str) -> store.Recording:
        held.append(store_lock_held())
        return load_recording(recording_id)

    def spy_write(path: Path, value: object) -> None:
        held.append(store_lock_held())
        write_json(path, value)

    monkeypatch.setattr(store, "load_recording", spy_load)
    monkeypatch.setattr(store, "write_json", spy_write)

    change()

    assert held == [True, True]


def test_delete_tracks_removes_the_audio_and_keeps_everything_else() -> None:
    recording_id = make_stopped(START)
    folder = store.recording_dir(recording_id)
    for name in [*store.TRACKS, "transcript.md", "run-0.log"]:
        (folder / name).write_bytes(b"OggS")

    store.delete_tracks(recording_id)

    assert sorted(path.name for path in folder.iterdir()) == [
        "recording.json",
        "run-0.log",
        "runs.json",
        "transcript.md",
    ]
    assert store.holds_audio(recording_id) is False


def test_delete_tracks_removes_the_one_track_that_is_left() -> None:
    recording_id = make_stopped(START)
    (store.recording_dir(recording_id) / "call.opus").write_bytes(b"OggS")

    store.delete_tracks(recording_id)

    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "recording.json",
        "runs.json",
    ]


def test_create_recording_never_overwrites_an_existing_one() -> None:
    store.create_recording(START)
    store.mark_stopped("2026-11-18T13-05-42", START + timedelta(seconds=5), incomplete=False)

    with pytest.raises(FileExistsError):
        store.create_recording(START)

    assert store.load_recording("2026-11-18T13-05-42")["stopped_at"] == "2026-11-18T13:05:47+00:00"


@pytest.mark.parametrize("empty_tracks", [(), ("mic.opus",), ("mic.opus", "call.opus")])
def test_discard_if_silent_removes_a_recording_without_audio(empty_tracks: tuple[str, ...]) -> None:
    store.create_recording(START)
    for name in empty_tracks:
        (store.recording_dir("2026-11-18T13-05-42") / name).touch()

    store.discard_if_silent("2026-11-18T13-05-42")

    assert list(store.recordings_dir().iterdir()) == []


@pytest.mark.parametrize("track", ["mic.opus", "call.opus"])
def test_discard_if_silent_keeps_a_recording_with_audio(track: str) -> None:
    store.create_recording(START)
    folder = store.recording_dir("2026-11-18T13-05-42")
    for name in store.TRACKS:
        (folder / name).touch()
    (folder / track).write_bytes(b"OggS")

    store.discard_if_silent("2026-11-18T13-05-42")

    assert [path.name for path in store.recordings_dir().iterdir()] == ["2026-11-18T13-05-42"]
    assert sorted(path.name for path in folder.iterdir()) == [
        "call.opus",
        "mic.opus",
        "recording.json",
        "runs.json",
    ]
    assert (folder / track).read_bytes() == b"OggS"


def test_mark_stopped_records_stop_time_and_completeness() -> None:
    recording_id = make_stopped(START, seconds=90, incomplete=True)

    recording = store.load_recording(recording_id)
    assert recording["stopped_at"] == "2026-11-18T13:07:12+00:00"
    assert recording["incomplete"] is True


def test_open_recording_is_the_one_not_stopped() -> None:
    make_stopped(START)
    store.create_recording(START + timedelta(hours=1))

    open_recording = store.open_recording()

    assert open_recording is not None
    assert open_recording["id"] == "2026-11-18T14-05-42"


def test_open_recording_is_none_when_all_stopped() -> None:
    make_stopped(START)

    assert store.open_recording() is None


def test_close_abandoned_stops_at_the_last_audio_written() -> None:
    store.create_recording(START)
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
    store.create_recording(START)

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
    store.create_recording(START + timedelta(hours=7))

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
        "store.lock",
    ]


def store_lock_held() -> bool:
    with (store.data_dir() / "store.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def test_lock_shuts_out_every_other_holder_until_released() -> None:
    with store.lock():
        assert store_lock_held() is True

    assert store_lock_held() is False


def test_status_is_written_inside_the_store_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    store.data_dir().mkdir(parents=True)
    held: list[bool] = []
    write_json = store.write_json

    def spy(path: Path, value: object) -> None:
        held.append(store_lock_held())
        write_json(path, value)

    monkeypatch.setattr(store, "write_json", spy)

    store.write_status()

    assert held == [True]


def test_discard_if_silent_removes_the_folder_inside_the_store_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.create_recording(START)
    held: list[bool] = []
    rmdir = Path.rmdir

    def spy(path: Path) -> None:
        held.append(store_lock_held())
        rmdir(path)

    monkeypatch.setattr(Path, "rmdir", spy)

    store.discard_if_silent("2026-11-18T13-05-42")

    assert held == [True]


@pytest.mark.parametrize(
    ("tracks", "holds"),
    [({}, False), ({"mic.opus": b""}, False), ({"call.opus": b"OggS"}, True)],
)
def test_holds_audio_when_a_track_has_bytes(tracks: dict[str, bytes], *, holds: bool) -> None:
    store.create_recording(START)
    for name, content in tracks.items():
        (store.recording_dir("2026-11-18T13-05-42") / name).write_bytes(content)

    assert store.holds_audio("2026-11-18T13-05-42") is holds


@pytest.mark.parametrize(
    ("recording_id", "exists"),
    [("2026-11-18T13-05-42", True), ("2026-11-18T13-05-43", False), ("..", False), (".", False)],
)
def test_is_recording_only_for_a_recording_folder(recording_id: str, *, exists: bool) -> None:
    store.create_recording(START)

    assert store.is_recording(recording_id) is exists


def test_is_recording_refuses_a_folder_outside_the_recordings(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "recording.json").write_text("{}")

    assert store.is_recording(str(elsewhere)) is False


def test_save_runs_replaces_the_run_log() -> None:
    store.create_recording(START)
    runs = [
        store.Run(
            action="transcribe",
            status="queued",
            queued_at="2026-11-18T13:06:42+00:00",
            error=None,
            details=None,
            output=None,
        )
    ]

    store.save_runs("2026-11-18T13-05-42", runs)

    assert store.load_runs("2026-11-18T13-05-42") == runs


def a_run(action: str, status: str, **changes: object) -> store.Run:
    run = {
        "action": action,
        "status": status,
        "queued_at": "2026-11-18T13:30:00+00:00",
        "error": None,
        "details": None,
        "output": None,
    }
    return cast("store.Run", {**run, **changes})


def failed_in_status(recording_id: str) -> object:
    recent = cast("list[dict[str, object]]", read_status()["recent"])
    return next(entry for entry in recent if entry["id"] == recording_id)["failed"]


def test_start_run_appends_the_run_and_fails_one_of_its_action_a_crash_left_running() -> None:
    recording_id = make_stopped(START)
    (store.recording_dir(recording_id) / "run-0.log").write_text("$ devin -p …\n")
    transcribing = a_run("transcribe", "running")
    store.save_runs(recording_id, [a_run("summarize", "running"), transcribing])
    started = a_run("summarize", "running", queued_at="2026-11-18T14:00:00+00:00")

    index, runs = store.start_run(recording_id, started)

    expected = [
        a_run("summarize", "failed", error="interrupted", details="run-0.log"),
        transcribing,
        started,
    ]
    assert (index, runs) == (2, expected)
    assert store.load_runs(recording_id) == expected
    # Rewritten with the new run's status, which hides the failure before it.
    assert failed_in_status(recording_id) == []


def test_end_run_puts_the_ended_run_in_its_place() -> None:
    recording_id = make_stopped(START)
    store.save_runs(recording_id, [a_run("transcribe", "succeeded"), a_run("summarize", "running")])

    store.end_run(recording_id, 1, a_run("summarize", "failed", error="boom"))

    assert store.load_runs(recording_id) == [
        a_run("transcribe", "succeeded"),
        a_run("summarize", "failed", error="boom"),
    ]
    assert failed_in_status(recording_id) == ["summarize"]


def test_fail_left_running_fails_the_run_of_the_action_a_dead_command_left_running() -> None:
    recording_id = make_stopped(START)
    (store.recording_dir(recording_id) / "run-1.log").write_text("$ agaric-mcp call …\n")
    published = a_run("publish_agaric", "succeeded", output="agaric://page/01JPAGE")
    transcribing = a_run("transcribe", "running")
    store.save_runs(recording_id, [published, a_run("publish_agaric", "running"), transcribing])

    store.fail_left_running(
        recording_id, "publish_agaric", "Publish to Agaric stopped: RuntimeError: boom"
    )

    assert store.load_runs(recording_id) == [
        published,
        a_run(
            "publish_agaric",
            "failed",
            error="Publish to Agaric stopped: RuntimeError: boom",
            details="run-1.log",
        ),
        transcribing,
    ]
    assert failed_in_status(recording_id) == ["publish_agaric"]


def test_fail_left_running_leaves_a_log_with_no_run_of_the_action_running() -> None:
    recording_id = make_stopped(START)
    runs = [a_run("publish_agaric", "succeeded"), a_run("transcribe", "running")]
    store.save_runs(recording_id, runs)

    store.fail_left_running(recording_id, "summarize", "Summarize stopped without saying why")

    assert store.load_runs(recording_id) == runs
    assert failed_in_status(recording_id) == []


def test_set_typed_name_keeps_the_name_for_good() -> None:
    recording_id = make_stopped(START)

    store.set_typed_name(recording_id, "Weekly sync")

    assert json.loads((store.recording_dir(recording_id) / "recording.json").read_text()) == {
        "id": recording_id,
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": "2026-11-18T13:06:42+00:00",
        "incomplete": False,
        "name": "Weekly sync",
        "name_source": "typed",
    }


@pytest.mark.parametrize(
    ("seconds", "stamp"),
    [
        (0.0, "00:00:00"),
        (4.0, "00:00:04"),
        (4.999, "00:00:04"),
        (59.0, "00:00:59"),
        (60.0, "00:01:00"),
        (3599.5, "00:59:59"),
        (3661.0, "01:01:01"),
        (360000.0, "100:00:00"),
    ],
)
def test_timestamp_is_whole_seconds_from_the_recording_start(seconds: float, stamp: str) -> None:
    assert store.format_timestamp(seconds) == stamp


def test_transcript_is_one_paragraph_per_segment_under_the_speakers_names() -> None:
    names = {"Speaker 1": "Ana Díaz", "Speaker 2": "Speaker 2"}

    assert store.format_transcript(SAID, names) == (
        "[00:00:04] **Ana Díaz:** Arrancamos con el release.\n"
        "\n"
        "[00:00:11] **Me:** CI is green since this morning.\n"
        "\n"
        "[01:02:05] **Speaker 2:** Año nuevo, <deploy> & PR.\n"
        "\n"
        "[01:02:10] **Ana Díaz:** Dale.\n"
    )


def test_a_speaker_nobody_listed_goes_by_its_key() -> None:
    assert store.format_transcript(SAID[:2], {}) == (
        "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
        "\n"
        "[00:00:11] **Me:** CI is green since this morning.\n"
    )


speaker_keys = st.sampled_from(["Me", "Speaker 1", "Speaker 2", "Speaker 3"])
given_names = st.text(st.characters(codec="utf-8", exclude_categories=["C"]), min_size=1)
segments_said = st.lists(
    st.builds(
        store.Segment,
        start=st.floats(0, 36000),
        end=st.floats(0, 36000),
        speaker=speaker_keys,
        text=st.text(st.characters(codec="utf-8", exclude_categories=["C", "Z"]), min_size=1),
    )
)


@given(
    segments=segments_said,
    names=st.dictionaries(speaker_keys, given_names),
    renamed=speaker_keys,
    name=given_names,
)
def test_renaming_a_speaker_rewrites_that_speakers_paragraphs_and_no_others(
    segments: list[store.Segment], names: dict[str, str], renamed: str, name: str
) -> None:
    before = store.format_transcript(segments, names).split("\n\n")
    after = store.format_transcript(segments, {**names, renamed: name}).split("\n\n")

    assert len(after) == len(before) == max(len(segments), 1)
    for segment, was, now in zip(segments, before, after, strict=False):
        stamp = store.format_timestamp(segment["start"])
        if segment["speaker"] == renamed:
            assert now.rstrip("\n") == f"[{stamp}] **{name}:** {segment['text']}"
        else:
            assert now == was


def test_write_transcript_lists_the_calls_speakers_under_their_keys() -> None:
    recording_id = make_stopped(START)
    write_segments(recording_id, SAID)

    store.write_transcript(recording_id)

    assert store.load_recording(recording_id).get("speakers") == {
        "Speaker 1": "Speaker 1",
        "Speaker 2": "Speaker 2",
    }
    assert read_transcript(recording_id) == (
        "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
        "\n"
        "[00:00:11] **Me:** CI is green since this morning.\n"
        "\n"
        "[01:02:05] **Speaker 2:** Año nuevo, <deploy> & PR.\n"
        "\n"
        "[01:02:10] **Speaker 1:** Dale.\n"
    )


def test_write_transcript_replaces_the_old_one_and_leaves_nothing_else() -> None:
    recording_id = make_stopped(START)
    write_segments(recording_id, SAID[:1])
    (store.recording_dir(recording_id) / "transcript.md").write_text("old\n")

    store.write_transcript(recording_id)

    assert read_transcript(recording_id) == "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "recording.json",
        "runs.json",
        "transcript.json",
        "transcript.md",
    ]


def test_write_transcript_fails_without_the_transcribers_segments() -> None:
    recording_id = make_stopped(START)

    with pytest.raises(FileNotFoundError):
        store.write_transcript(recording_id)

    assert "speakers" not in store.load_recording(recording_id)
    assert not (store.recording_dir(recording_id) / "transcript.md").exists()


def test_set_speaker_name_writes_the_transcript_again_with_the_name() -> None:
    recording_id = make_stopped(START)
    write_segments(recording_id, SAID)
    store.write_transcript(recording_id)

    store.set_speaker_name(recording_id, "Speaker 2", "Ana Díaz")

    saved = json.loads((store.recording_dir(recording_id) / "recording.json").read_text())
    assert saved["speakers"] == {"Speaker 1": "Speaker 1", "Speaker 2": "Ana Díaz"}
    assert read_transcript(recording_id) == (
        "[00:00:04] **Speaker 1:** Arrancamos con el release.\n"
        "\n"
        "[00:00:11] **Me:** CI is green since this morning.\n"
        "\n"
        "[01:02:05] **Ana Díaz:** Año nuevo, <deploy> & PR.\n"
        "\n"
        "[01:02:10] **Speaker 1:** Dale.\n"
    )


def test_set_speaker_name_fails_for_a_recording_that_is_gone() -> None:
    with pytest.raises(FileNotFoundError):
        store.set_speaker_name("2026-11-18T13-05-42", "Speaker 1", "Ana Díaz")

    assert not store.recordings_dir().exists()


def test_transcribing_again_keeps_the_names_of_the_speakers_still_heard() -> None:
    recording_id = make_stopped(START)
    write_segments(recording_id, SAID)
    store.write_transcript(recording_id)
    store.set_speaker_name(recording_id, "Speaker 1", "Ana Díaz")
    store.set_speaker_name(recording_id, "Speaker 2", "Bob Ruiz")
    again = [said(2.0, "Speaker 1", "Hola."), said(9.0, "Speaker 3", "Buenas.")]
    write_segments(recording_id, again)

    store.write_transcript(recording_id)

    assert store.load_recording(recording_id).get("speakers") == {
        "Speaker 1": "Ana Díaz",
        "Speaker 3": "Speaker 3",
    }
    assert read_transcript(recording_id) == (
        "[00:00:02] **Ana Díaz:** Hola.\n\n[00:00:09] **Speaker 3:** Buenas.\n"
    )


def test_a_recording_with_only_my_voice_lists_no_speakers() -> None:
    recording_id = make_stopped(START)
    write_segments(recording_id, [said(1.0, "Me", "Hola.")])

    store.write_transcript(recording_id)

    assert store.load_recording(recording_id).get("speakers") == {}
    assert read_transcript(recording_id) == "[00:00:01] **Me:** Hola.\n"
