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

from minutas import settings, store

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
        lambda: store.mark_paused("2026-11-18T13-05-42", START),
        lambda: store.mark_resumed("2026-11-18T13-05-42", START),
        lambda: store.mark_discarded("2026-11-18T13-05-42"),
        lambda: store.mark_silent("2026-11-18T13-05-42", START),
        lambda: store.mark_heard("2026-11-18T13-05-42"),
        lambda: store.mark_echo_cancelled("2026-11-18T13-05-42", echo_cancelled=True),
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


def test_a_pause_lasts_until_the_recording_is_resumed() -> None:
    recording_id = store.create_recording(START)["id"]

    store.mark_paused(recording_id, START + timedelta(seconds=60))

    assert store.load_recording(recording_id).get("paused_at") == "2026-11-18T13:06:42+00:00"

    store.mark_resumed(recording_id, START + timedelta(seconds=90))
    store.mark_paused(recording_id, START + timedelta(seconds=120))
    store.mark_resumed(recording_id, START + timedelta(seconds=125))

    recording = store.load_recording(recording_id)
    assert recording.get("paused_seconds") == 35
    assert "paused_at" not in recording


def test_a_recording_stopped_while_paused_was_paused_until_it_stopped() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_paused(recording_id, START + timedelta(seconds=60))

    store.mark_stopped(recording_id, START + timedelta(seconds=100), incomplete=False)

    recording = store.load_recording(recording_id)
    assert recording.get("paused_seconds") == 40
    assert "paused_at" not in recording
    assert store.duration_seconds(recording) == 60


def test_a_recording_never_paused_keeps_no_pause() -> None:
    recording = store.load_recording(make_stopped(START, seconds=90))

    assert "paused_seconds" not in recording
    assert store.duration_seconds(recording) == 90


def test_a_recording_in_progress_has_no_duration_yet() -> None:
    assert store.duration_seconds(store.create_recording(START)) == 0


@given(
    spans=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=7200), st.integers(min_value=0, max_value=7200)
        ),
        max_size=6,
    ),
    last=st.integers(min_value=0, max_value=7200),
)
def test_a_recording_lasts_as_long_as_it_was_not_paused(
    spans: list[tuple[int, int]], last: int
) -> None:
    recording = default_recording()
    clock = START
    for recorded, paused in spans:
        clock += timedelta(seconds=recorded)
        recording["paused_at"] = clock.isoformat()
        clock += timedelta(seconds=paused)
        store.end_pause(recording, clock)
    recording["stopped_at"] = (clock + timedelta(seconds=last)).isoformat()

    assert store.duration_seconds(recording) == sum(recorded for recorded, _ in spans) + last
    assert recording.get("paused_seconds", 0) == sum(paused for _, paused in spans)


def test_mark_discarded_says_so_and_changes_nothing_else() -> None:
    created = store.create_recording(START)

    store.mark_discarded(created["id"])

    assert store.load_recording(created["id"]) == {**created, "discarded": True}


@pytest.mark.parametrize("echo_cancelled", [True, False])
def test_mark_echo_cancelled_says_which_mic_was_recorded_and_changes_nothing_else(
    *, echo_cancelled: bool
) -> None:
    created = store.create_recording(START)

    store.mark_echo_cancelled(created["id"], echo_cancelled=echo_cancelled)

    assert store.load_recording(created["id"]) == {**created, "echo_cancelled": echo_cancelled}


def test_close_abandoned_on_a_paused_recording_stops_at_the_last_audio_written() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_paused(recording_id, START + timedelta(seconds=20))
    store.mark_resumed(recording_id, START + timedelta(seconds=50))
    track = store.recording_dir(recording_id) / "mic.opus"
    track.write_bytes(b"OggS")
    written = (START + timedelta(seconds=118)).timestamp()
    os.utime(track, (written, written))
    store.mark_paused(recording_id, START + timedelta(seconds=120))

    store.close_abandoned()

    recording = store.load_recording(recording_id)
    assert (recording["stopped_at"], recording["incomplete"]) == ("2026-11-18T13:07:40+00:00", True)
    # The pause it was in began after its audio ends: only the first one counts.
    assert recording.get("paused_seconds") == 30
    assert store.duration_seconds(recording) == 88
    assert track.read_bytes() == b"OggS"


def test_a_recording_abandoned_with_no_audio_since_before_its_pauses_lasted_no_time() -> None:
    recording_id = store.create_recording(START)["id"]
    track = store.recording_dir(recording_id) / "mic.opus"
    track.write_bytes(b"OggS")
    written = (START + timedelta(seconds=9)).timestamp()
    os.utime(track, (written, written))
    store.mark_paused(recording_id, START + timedelta(seconds=10))
    store.mark_resumed(recording_id, START + timedelta(seconds=20))
    store.mark_paused(recording_id, START + timedelta(seconds=21))

    store.close_abandoned()

    recording = store.load_recording(recording_id)
    assert (recording["stopped_at"], recording.get("paused_seconds")) == (
        "2026-11-18T13:05:51+00:00",
        10,
    )
    assert store.duration_seconds(recording) == 0


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
        "progress": None,
        "failed": [],
        "continuable": False,
    }


@pytest.mark.parametrize("count", [1, 3])
def test_status_lists_as_many_stopped_as_the_settings_say(count: int) -> None:
    for hour in range(4):
        make_stopped(START + timedelta(hours=hour))
    settings.set_recent_in_menu(count)

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    newest = ["2026-11-18T16-05-42", "2026-11-18T15-05-42", "2026-11-18T14-05-42"]
    assert [entry["id"] for entry in recent] == newest[:count]


def test_status_says_the_active_recording_is_paused_and_for_how_long_it_was() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_paused(recording_id, START + timedelta(seconds=60))

    store.write_status()

    active = {"id": "2026-11-18T13-05-42", "started_at": "2026-11-18T13:05:42+00:00"}
    assert read_status()["recording"] == {**active, "paused_at": "2026-11-18T13:06:42+00:00"}

    store.mark_resumed(recording_id, START + timedelta(seconds=90))
    store.write_status()

    assert read_status()["recording"] == {**active, "paused_seconds": 30}


def test_a_silence_lasts_until_the_recording_is_heard_again() -> None:
    recording_id = store.create_recording(START)["id"]

    store.mark_silent(recording_id, START + timedelta(seconds=60))

    assert store.load_recording(recording_id).get("silent_since") == "2026-11-18T13:06:42+00:00"

    store.mark_heard(recording_id)

    assert "silent_since" not in store.load_recording(recording_id)


def test_hearing_a_recording_that_was_not_silent_changes_nothing() -> None:
    recording_id = store.create_recording(START)["id"]
    before = store.load_recording(recording_id)

    store.mark_heard(recording_id)

    assert store.load_recording(recording_id) == before


@pytest.mark.parametrize(
    "end",
    [
        lambda recording_id: store.mark_paused(recording_id, START + timedelta(seconds=90)),
        lambda recording_id: store.mark_stopped(
            recording_id, START + timedelta(seconds=90), incomplete=False
        ),
    ],
)
def test_a_pause_or_a_stop_ends_a_silence(end: Callable[[str], None]) -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_silent(recording_id, START + timedelta(seconds=60))

    end(recording_id)

    assert "silent_since" not in store.load_recording(recording_id)


def test_status_says_since_when_the_active_recording_has_been_silent() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_silent(recording_id, START + timedelta(seconds=60))

    store.write_status()

    assert read_status()["recording"] == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "silent_since": "2026-11-18T13:06:42+00:00",
    }


def test_status_leaves_the_pauses_out_of_a_recordings_duration() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_paused(recording_id, START + timedelta(seconds=60))
    store.mark_resumed(recording_id, START + timedelta(seconds=90))
    store.mark_stopped(recording_id, START + timedelta(seconds=100), incomplete=False)

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert recent[0]["duration_seconds"] == 70


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
            {"action": "publish_github", "status": "failed"},
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
    assert recent[0]["failed"] == ["publish_agaric", "publish_gitlab", "publish_github"]


def test_status_says_how_far_the_latest_transcription_has_got() -> None:
    recording_id = make_stopped(START)
    progress = {"phase": "identifying speakers", "percent": 40}
    write_runs(
        recording_id,
        [
            {"action": "transcribe", "status": "running", "progress": {"phase": "x", "percent": 9}},
            {"action": "transcribe", "status": "running", "progress": progress},
            {"action": "summarize", "status": "succeeded"},
        ],
    )

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert (recent[0]["transcribe"], recent[0]["progress"]) == ("running", progress)


def test_status_has_no_progress_before_the_transcriber_says_any() -> None:
    recording_id = make_stopped(START)
    write_runs(recording_id, [{"action": "transcribe", "status": "running"}])

    store.write_status()

    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert (recent[0]["transcribe"], recent[0]["progress"]) == ("running", None)


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


GB = 10**9
TRANSCRIBED = [a_run("transcribe", "succeeded", output="transcript.md")]


def kept(
    name: str,
    days_old: float,
    size: int = GB,
    runs: list[store.Run] = TRANSCRIBED,
    *,
    stopped: bool = True,
) -> tuple[store.Recording, list[store.Run], int]:
    """A recording as the limits see it: its runs, and the bytes its tracks take."""
    started = START - timedelta(days=days_old)
    recording = store.Recording(
        id=name,
        started_at=started.isoformat(),
        stopped_at=(started + timedelta(hours=1)).isoformat() if stopped else None,
        incomplete=False,
        name="Meeting",
        name_source="default",
    )
    return recording, runs, size


@pytest.mark.parametrize(
    ("recordings", "age_limit_days", "size_limit_gb", "gone"),
    [
        # Past the age limit; to the second at it, still kept.
        ([kept("old", 31), kept("young", 29), kept("at", 30)], 30, None, ["old"]),
        # Over the size limit the oldest go until the rest fit, however young: size wins.
        ([kept("c", 1, 2 * GB), kept("a", 3, 2 * GB), kept("b", 2, 2 * GB)], 30, 5, ["a"]),
        ([kept("a", 3, 2 * GB), kept("b", 2, 2 * GB), kept("c", 1, 2 * GB)], 30, 3, ["a", "b"]),
        # Right at the size limit, they fit.
        ([kept("a", 2, GB // 2), kept("b", 1, GB // 2)], None, 1, []),
        # Tracks that can't go count towards the size all the same, and may keep it over.
        ([kept("untranscribed", 2, 4 * GB, []), kept("b", 1, 2 * GB)], None, 5, ["b"]),
        ([kept("active", 0, 6 * GB, [], stopped=False), kept("b", 1)], None, 5, ["b"]),
        # Never during the recording, transcribed or not.
        ([kept("active", 40, runs=TRANSCRIBED, stopped=False)], 30, 1, []),
        # Never before the latest transcription succeeded.
        ([kept("retried", 40, runs=[*TRANSCRIBED, a_run("transcribe", "failed")])], 30, 1, []),
        ([kept("queued", 40, runs=[*TRANSCRIBED, a_run("transcribe", "queued")])], 30, 1, []),
        # Nothing left to delete.
        ([kept("deleted", 40, 0)], 30, 1, []),
        # Both limits off.
        ([kept("ancient", 400, 10 * GB)], None, None, []),
    ],
)
def test_audio_over_limits_examples(
    recordings: list[tuple[store.Recording, list[store.Run], int]],
    age_limit_days: int | None,
    size_limit_gb: int | None,
    gone: list[str],
) -> None:
    assert store.audio_over_limits(recordings, START, age_limit_days, size_limit_gb) == gone


STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
some_kept = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=24 * 90),
        # In half GB, so that the tracks often meet a limit exactly.
        st.integers(min_value=0, max_value=6).map(lambda halves: halves * GB // 2),
        # Often transcribed, the only audio that may go.
        st.just([("transcribe", "succeeded")])
        | st.lists(
            st.tuples(st.sampled_from(store.ACTIONS), st.sampled_from(STATUSES)), max_size=4
        ),
        st.booleans(),
    ),
    unique_by=lambda one: one[0],
    max_size=8,
).map(
    lambda many: [
        kept(f"{hours}h", hours / 24, size, [a_run(*run) for run in runs], stopped=stopped)
        for hours, size, runs, stopped in many
    ]
)


@given(
    recordings=some_kept,
    age_limit_days=st.none() | st.integers(min_value=1, max_value=60),
    size_limit_gb=st.none() | st.integers(min_value=1, max_value=10),
)
def test_audio_goes_only_once_transcribed_by_age_then_oldest_first_by_size(
    recordings: list[tuple[store.Recording, list[store.Run], int]],
    age_limit_days: int | None,
    size_limit_gb: int | None,
) -> None:
    gone = store.audio_over_limits(recordings, START, age_limit_days, size_limit_gb)

    sizes = {recording["id"]: size for recording, _, size in recordings}
    oldest_first = sorted(recordings, key=lambda one: datetime.fromisoformat(one[0]["started_at"]))
    may_go = [
        recording["id"]
        for recording, runs, size in oldest_first
        if size > 0
        and recording["stopped_at"] is not None
        and [run["status"] for run in runs if run["action"] == "transcribe"][-1:] == ["succeeded"]
    ]
    # Only a stopped recording whose latest transcription succeeded, oldest first, and once.
    assert gone == [recording_id for recording_id in may_go if recording_id in gone]
    # Every one of them past the age limit goes, first.
    too_old = [
        recording_id
        for recording, _, _ in oldest_first
        if (recording_id := recording["id"]) in may_go
        and age_limit_days is not None
        and START - datetime.fromisoformat(recording["started_at"]) > timedelta(days=age_limit_days)
    ]
    assert gone[: len(too_old)] == too_old
    # Then the oldest of the rest, just as many as the size limit needs: no more, and no fewer
    # unless none is left.
    for_size = gone[len(too_old) :]
    rest = [recording_id for recording_id in may_go if recording_id not in too_old]
    assert for_size == rest[: len(for_size)]
    left = sum(sizes.values()) - sum(sizes[recording_id] for recording_id in gone)
    if size_limit_gb is None:
        assert for_size == []
    else:
        assert left <= size_limit_gb * GB or for_size == rest
        assert for_size == [] or left + sizes[for_size[-1]] > size_limit_gb * GB


def with_tracks(started: datetime, size: int, runs: list[store.Run]) -> str:
    """A stopped recording, transcribed or not, whose tracks take size bytes, without the disk."""
    recording_id = make_stopped(started)
    folder = store.recording_dir(recording_id)
    for name in store.TRACKS:
        with (folder / name).open("wb") as track:
            track.truncate(size // 2)
    (folder / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")
    store.save_runs(recording_id, runs)
    return recording_id


def test_delete_audio_over_limits_takes_only_the_tracks_as_delete_now_does() -> None:
    old = with_tracks(START - timedelta(days=31), GB, TRANSCRIBED)
    young = with_tracks(START - timedelta(days=1), GB, TRANSCRIBED)
    untranscribed = with_tracks(START - timedelta(days=40), GB, [])

    store.delete_audio_over_limits(START, 30, 5)

    assert sorted(path.name for path in store.recording_dir(old).iterdir()) == [
        "recording.json",
        "runs.json",
        "transcript.md",
    ]
    assert store.load_runs(old) == TRANSCRIBED
    assert (store.audio_size(young), store.audio_size(untranscribed)) == (GB, GB)


def test_delete_audio_over_limits_takes_the_oldest_until_all_fit_and_never_the_active_one() -> None:
    oldest = with_tracks(START - timedelta(days=3), 2 * GB, TRANSCRIBED)
    older = with_tracks(START - timedelta(days=2), 2 * GB, TRANSCRIBED)
    newer = with_tracks(START - timedelta(days=1), 2 * GB, TRANSCRIBED)
    active = store.create_recording(START)["id"]
    with (store.recording_dir(active) / "mic.opus").open("wb") as track:
        track.truncate(GB)

    store.delete_audio_over_limits(START, None, 4)

    assert [store.audio_size(one) for one in (oldest, older, newer, active)] == [0, 0, 2 * GB, GB]


def test_delete_audio_over_limits_reads_the_runs_and_deletes_inside_the_store_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_tracks(START - timedelta(days=31), GB, TRANSCRIBED)
    held: list[tuple[str, bool]] = []
    load_runs, delete_tracks = store.load_runs, store.delete_tracks

    def reading(recording_id: str) -> list[store.Run]:
        held.append(("load_runs", store_lock_held()))
        return load_runs(recording_id)

    def deleting(recording_id: str) -> None:
        held.append(("delete_tracks", store_lock_held()))
        delete_tracks(recording_id)

    monkeypatch.setattr(store, "load_runs", reading)
    monkeypatch.setattr(store, "delete_tracks", deleting)

    store.delete_audio_over_limits(START, 30, None)

    assert held == [("load_runs", True), ("delete_tracks", True)]


def test_audio_size_is_what_both_tracks_take_and_nothing_once_deleted() -> None:
    recording_id = make_stopped(START)
    folder = store.recording_dir(recording_id)
    (folder / "mic.opus").write_bytes(b"OggS")
    (folder / "call.opus").write_bytes(b"OggS call")
    (folder / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")

    assert store.audio_size(recording_id) == 13

    store.delete_tracks(recording_id)

    assert store.audio_size(recording_id) == 0


def with_audio(recording_id: str, names: tuple[str, ...] = store.TRACKS) -> str:
    for name in names:
        (store.recording_dir(recording_id) / name).write_bytes(b"OggS")
    return recording_id


def stopped_recording() -> store.Recording:
    recording = default_recording()
    recording["stopped_at"] = "2026-11-18T13:06:42+00:00"
    return recording


@pytest.mark.parametrize(
    "transcriptions",
    [[], ["queued"], ["running"], ["failed"], ["cancelled"], ["succeeded", "failed"]],
)
def test_a_stopped_recording_not_transcribed_with_both_tracks_can_be_continued(
    transcriptions: list[str],
) -> None:
    runs = [a_run("summarize", "succeeded"), *(a_run("transcribe", s) for s in transcriptions)]

    assert store.continue_refusal(stopped_recording(), runs, store.TRACKS) is None


@pytest.mark.parametrize(
    ("stopped", "transcriptions", "audio", "refusal"),
    [
        (False, [], store.TRACKS, "it is still being recorded"),
        (True, ["failed", "succeeded"], store.TRACKS, "it has been transcribed"),
        (True, [], ("mic.opus",), "its audio is gone"),
        (True, [], (), "its audio is gone"),
        (
            True,
            ["cancelled"],
            (*store.TRACKS, "call.continued.opus"),
            "what it recorded after the last Continue is still to be joined: transcribe it first",
        ),
    ],
)
def test_a_recording_that_cannot_be_continued_says_why(
    transcriptions: list[str], audio: tuple[str, ...], refusal: str, *, stopped: bool
) -> None:
    recording = stopped_recording() if stopped else default_recording()
    runs = [a_run("transcribe", status) for status in transcriptions]

    assert store.continue_refusal(recording, runs, audio) == refusal


@given(
    stopped=st.booleans(),
    runs=st.lists(st.tuples(st.sampled_from(store.ACTIONS), st.sampled_from(STATUSES)), max_size=5),
    audio=st.sets(st.sampled_from([*store.TRACKS, *store.PARTS])),
)
def test_only_a_stopped_untranscribed_recording_with_its_two_tracks_and_no_part_can_be_continued(
    runs: list[tuple[str, str]], audio: set[str], *, stopped: bool
) -> None:
    recording = stopped_recording() if stopped else default_recording()

    refusal = store.continue_refusal(recording, [a_run(*run) for run in runs], audio)

    transcriptions = [status for action, status in runs if action == "transcribe"]
    assert (refusal is None) == (
        stopped and transcriptions[-1:] != ["succeeded"] and audio == set(store.TRACKS)
    )


def test_a_continued_recording_goes_on_from_where_it_stopped() -> None:
    recording_id = make_stopped(START, seconds=60, incomplete=True)
    store.set_typed_name(recording_id, "Planning")
    store.mark_discarded(recording_id)

    store.mark_continued(recording_id, START + timedelta(seconds=100))

    assert store.load_recording(recording_id) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": None,
        "incomplete": False,
        "name": "Planning",
        "name_source": "typed",
        # The time since it stopped, as one more pause.
        "paused_seconds": 40,
        "continued": True,
    }

    store.mark_stopped(recording_id, START + timedelta(seconds=130), incomplete=False)

    assert store.duration_seconds(store.load_recording(recording_id)) == 90


def test_a_recording_continued_after_a_pause_keeps_its_earlier_pauses() -> None:
    recording_id = store.create_recording(START)["id"]
    store.mark_paused(recording_id, START + timedelta(seconds=10))
    store.mark_stopped(recording_id, START + timedelta(seconds=30), incomplete=False)

    store.mark_continued(recording_id, START + timedelta(seconds=45))

    recording = store.load_recording(recording_id)
    assert recording.get("paused_seconds") == 35
    assert "paused_at" not in recording


@given(
    spans=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=7200),
            st.integers(min_value=0, max_value=7200),
            st.booleans(),
        ),
        max_size=6,
    ),
    last=st.integers(min_value=0, max_value=7200),
)
def test_a_recording_lasts_as_long_as_it_was_neither_paused_nor_stopped(
    spans: list[tuple[int, int, bool]], last: int
) -> None:
    recording = default_recording()
    clock = START
    for recorded, away, stopped in spans:
        clock += timedelta(seconds=recorded)
        if stopped:
            recording["stopped_at"] = clock.isoformat()
            clock += timedelta(seconds=away)
            store.reopen(recording, clock)
        else:
            recording["paused_at"] = clock.isoformat()
            clock += timedelta(seconds=away)
            store.end_pause(recording, clock)
    recording["stopped_at"] = (clock + timedelta(seconds=last)).isoformat()

    assert recording["started_at"] == "2026-11-18T13:05:42+00:00"
    assert store.duration_seconds(recording) == sum(recorded for recorded, _, _ in spans) + last
    assert recording.get("paused_seconds", 0) == sum(away for _, away, _ in spans)


def recent_continuable() -> list[tuple[object, object]]:
    recent = cast("list[dict[str, object]]", read_status()["recent"])
    return [(entry["id"], entry["continuable"]) for entry in recent]


def test_status_says_which_recent_recordings_can_be_continued() -> None:
    transcribed = with_audio(make_stopped(START))
    store.save_runs(transcribed, TRANSCRIBED)
    deleted = make_stopped(START + timedelta(hours=1))
    to_join = with_audio(make_stopped(START + timedelta(hours=2)), (*store.TRACKS, *store.PARTS))
    false_stop = with_audio(make_stopped(START + timedelta(hours=3)))
    store.save_runs(false_stop, [a_run("transcribe", "running")])

    store.write_status()

    assert recent_continuable() == [
        (false_stop, True),
        (to_join, False),
        (deleted, False),
        (transcribed, False),
    ]


def test_status_offers_no_recording_to_continue_while_one_is_in_progress() -> None:
    false_stop = with_audio(make_stopped(START))
    store.create_recording(START + timedelta(hours=1))

    store.write_status()

    assert recent_continuable() == [(false_stop, False)]


def test_status_says_the_active_recording_was_continued() -> None:
    recording_id = make_stopped(START, seconds=60)
    store.mark_continued(recording_id, START + timedelta(seconds=90))

    store.write_status()

    assert read_status()["recording"] == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "paused_seconds": 30,
        "continued": True,
    }


def test_audio_kept_is_the_tracks_and_parts_that_hold_audio() -> None:
    recording_id = with_audio(make_stopped(START), ("call.opus", "mic.continued.opus"))
    (store.recording_dir(recording_id) / "mic.opus").touch()
    (store.recording_dir(recording_id) / "call.continued.opus").touch()

    assert store.audio_kept(recording_id) == ["call.opus", "mic.continued.opus"]


def test_delete_tracks_removes_what_was_recorded_after_continue_too() -> None:
    # And what a join that was cancelled had written.
    names = (*store.TRACKS, "mic.continued.opus", "call.continued.opus", "mic.joined.opus")
    recording_id = with_audio(make_stopped(START), names)

    store.delete_tracks(recording_id)

    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "recording.json",
        "runs.json",
    ]


def test_the_sizes_the_limits_count_leave_out_what_is_still_to_be_joined() -> None:
    recording_id = with_audio(make_stopped(START), (*store.TRACKS, *store.PARTS))

    assert store.audio_size(recording_id) == 8


def test_close_abandoned_on_a_continued_recording_stops_at_the_last_audio_written() -> None:
    recording_id = with_audio(make_stopped(START, seconds=60))
    store.mark_continued(recording_id, START + timedelta(seconds=100))
    folder = store.recording_dir(recording_id)
    for name, seconds in [("mic.opus", 60), ("call.opus", 60), ("mic.continued.opus", 130)]:
        (folder / name).write_bytes(b"OggS")
        written = (START + timedelta(seconds=seconds)).timestamp()
        os.utime(folder / name, (written, written))

    store.close_abandoned()

    recording = store.load_recording(recording_id)
    assert (recording["stopped_at"], recording["incomplete"]) == ("2026-11-18T13:07:52+00:00", True)
    assert store.duration_seconds(recording) == 90
