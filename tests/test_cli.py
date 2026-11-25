import fcntl
import io
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from keyring.errors import KeyringLocked

from minutas import (
    agaric,
    calendar,
    cli,
    github,
    gitlab,
    recorder,
    settings,
    store,
    summarizer,
    worker,
)
from minutas.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)


class FakeCalendar:
    """Stands in for the calendar lookup: no Evolution Data Server."""

    def __init__(self) -> None:
        self.event: calendar.Event | None = None
        self.asked: list[tuple[datetime, datetime]] = []

    def lookup(self, start: datetime, stop: datetime) -> calendar.Event | None:
        self.asked.append((start, stop))
        return self.event


@pytest.fixture(autouse=True)
def fake_calendar(monkeypatch: pytest.MonkeyPatch) -> FakeCalendar:
    fake = FakeCalendar()
    monkeypatch.setattr(calendar, "lookup", fake.lookup)
    return fake


class FakeRecorder:
    """Stands in for the recorder module: no systemd, microphone or GStreamer."""

    def __init__(self) -> None:
        self.running = False
        self.error: recorder.RecorderError | None = None
        self.on_failed_start: Callable[[Path], None] = lambda _: None
        self.started: list[tuple[Path, list[str]]] = []
        # The files each start recorded into.
        self.tracks: list[tuple[str, str]] = []
        # Whether each start was asked for the echo-cancelled mic, and whether one that is
        # gets it.
        self.echo_cancel: list[bool] = []
        self.canceller_starts = True
        self.stops = 0
        self.pauses = 0
        self.resumes = 0
        # What the unit runs once its recorder has stopped.
        self.on_stop: Callable[[], object] = lambda: None

    def start(
        self,
        folder: Path,
        *,
        on_exit: list[str],
        tracks: tuple[str, str] = store.TRACKS,
        echo_cancel: bool = False,
    ) -> bool:
        if self.error is not None:
            self.on_failed_start(folder)
            raise self.error
        self.started.append((folder, on_exit))
        self.tracks.append(tracks)
        self.echo_cancel.append(echo_cancel)
        return echo_cancel and self.canceller_starts

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        if self.error is not None:
            raise self.error
        self.stops += 1
        self.running = False
        self.on_stop()

    def pause(self) -> None:
        if self.error is not None:
            raise self.error
        self.pauses += 1

    def resume(self) -> None:
        if self.error is not None:
            raise self.error
        self.resumes += 1


@pytest.fixture
def fake_recorder(monkeypatch: pytest.MonkeyPatch) -> FakeRecorder:
    fake = FakeRecorder()
    monkeypatch.setattr(recorder, "start", fake.start)
    monkeypatch.setattr(recorder, "is_running", fake.is_running)
    monkeypatch.setattr(recorder, "stop", fake.stop)
    monkeypatch.setattr(recorder, "pause", fake.pause)
    monkeypatch.setattr(recorder, "resume", fake.resume)
    return fake


def read_status() -> dict[str, object]:
    return cast("dict[str, object]", json.loads((store.data_dir() / "status.json").read_text()))


def abandoned_recording() -> str:
    """A recording still open whose recorder is gone, its audio last written 30 s in."""
    recording = store.create_recording(START)
    track = store.recording_dir(recording["id"]) / "mic.opus"
    track.write_bytes(b"OggS")
    written = (START + timedelta(seconds=30)).timestamp()
    os.utime(track, (written, written))
    return recording["id"]


def test_version_prints_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"minutas {version('minutas')}\n"


def test_no_arguments_prints_help_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: minutas")


def test_unknown_option_exits_with_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--no-such-option"])

    assert exit_info.value.code == 2
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err


def test_module_entry_point_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "minutas", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == f"minutas {version('minutas')}\n"


def test_record_start_creates_the_recording_and_starts_the_recorder(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["record", "start"]) == 0

    printed = json.loads(capsys.readouterr().out)
    recording = store.load_recording(printed["id"])
    assert printed == {"id": recording["id"], "started_at": recording["started_at"]}
    assert fake_recorder.started == [
        (
            store.recording_dir(recording["id"]),
            [sys.executable, "-m", "minutas", "record", "stopped", recording["id"]],
        )
    ]
    assert read_status()["recording"] == printed


NO_ECHO_CANCELLATION = (
    "Echo cancellation didn't start, so the microphone is recorded as it is; "
    "journalctl --user -u minutas-echo-cancel.service says why.\n"
)


def test_record_start_with_echo_cancellation_off_records_the_plain_mic_and_notes_nothing(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.set_echo_cancel(on=False)

    assert main(["record", "start"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert fake_recorder.echo_cancel == [False]
    assert "echo_cancelled" not in store.load_recording(printed["id"])


def test_record_start_records_the_cancelled_mic_by_default_and_notes_it(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["record", "start"]) == 0

    printed, said = capsys.readouterr()
    assert said == ""
    assert fake_recorder.echo_cancel == [True]
    assert store.load_recording(json.loads(printed)["id"])["echo_cancelled"] is True


def test_record_start_whose_echo_canceller_fails_records_the_plain_mic_and_says_so(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.set_echo_cancel(on=True)
    fake_recorder.canceller_starts = False

    assert main(["record", "start"]) == 0

    printed, said = capsys.readouterr()
    assert said == NO_ECHO_CANCELLATION
    recording = store.load_recording(json.loads(printed)["id"])
    assert recording["echo_cancelled"] is False
    # It records all the same.
    assert read_status()["recording"] == json.loads(printed)


def test_record_start_while_recording_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_recorder.running = True

    assert main(["record", "start"]) == 1

    assert capsys.readouterr().err == "Already recording.\n"
    assert fake_recorder.started == []
    assert not store.recordings_dir().exists()


@pytest.mark.usefixtures("fake_recorder")
def test_record_start_closes_a_recording_whose_recorder_died(
    capsys: pytest.CaptureFixture[str],
) -> None:
    abandoned_id = abandoned_recording()

    assert main(["record", "start"]) == 0

    new_id = json.loads(capsys.readouterr().out)["id"]
    abandoned = store.load_recording(abandoned_id)
    assert abandoned["stopped_at"] == "2026-11-18T13:06:12+00:00"
    assert abandoned["incomplete"] is True
    status = read_status()
    assert status["recording"] == {
        "id": new_id,
        "started_at": store.load_recording(new_id)["started_at"],
    }
    recent = status["recent"]
    assert isinstance(recent, list)
    assert [entry["id"] for entry in recent] == [abandoned_id]


def test_record_start_failure_leaves_no_recording(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_recorder.error = recorder.RecorderError("Failed to connect to bus")

    assert main(["record", "start"]) == 1

    assert capsys.readouterr().err == "Could not start the recorder: Failed to connect to bus\n"
    assert list(store.recordings_dir().iterdir()) == []
    assert read_status() == {"version": 1, "recording": None, "recent": []}


def died_at_once(folder: Path) -> None:
    """What a recorder that dies on start leaves: empty tracks, closed by its exit command."""
    for name in store.TRACKS:
        (folder / name).touch()
    store.mark_stopped(folder.name, START, incomplete=True)
    store.write_status()


def test_record_start_removes_what_a_recorder_that_died_at_once_left(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_recorder.error = recorder.RecorderError("no audio reached its tracks")
    fake_recorder.on_failed_start = died_at_once

    assert main(["record", "start"]) == 1

    assert capsys.readouterr() == (
        "",
        "Could not start the recorder: no audio reached its tracks\n",
    )
    assert list(store.recordings_dir().iterdir()) == []
    assert read_status() == {"version": 1, "recording": None, "recent": []}


def test_record_start_keeps_audio_a_failed_start_captured(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def died_after_some_audio(folder: Path) -> None:
        died_at_once(folder)
        (folder / "mic.opus").write_bytes(b"OggS")

    fake_recorder.error = recorder.RecorderError("no audio reached its tracks")
    fake_recorder.on_failed_start = died_after_some_audio
    monkeypatch.setattr(cli, "now", lambda: START)

    assert main(["record", "start"]) == 1

    assert capsys.readouterr().err == "Could not start the recorder: no audio reached its tracks\n"
    assert [path.name for path in store.recordings_dir().iterdir()] == ["2026-11-18T13-05-42"]
    assert (store.recording_dir("2026-11-18T13-05-42") / "mic.opus").read_bytes() == b"OggS"
    status = read_status()
    assert status["recording"] is None
    recent = status["recent"]
    assert isinstance(recent, list)
    assert [(entry["id"], entry["incomplete"]) for entry in recent] == [
        ("2026-11-18T13-05-42", True)
    ]


def test_record_start_in_the_second_of_another_recording_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = store.create_recording(START)
    store.mark_stopped(recording["id"], START, incomplete=False)
    before = (store.recording_dir(recording["id"]) / "recording.json").read_text()
    monkeypatch.setattr(cli, "now", lambda: START)

    assert main(["record", "start"]) == 1

    assert capsys.readouterr() == ("", "A recording started this same second; try again.\n")
    assert fake_recorder.started == []
    assert (store.recording_dir(recording["id"]) / "recording.json").read_text() == before
    assert sorted(p.name for p in store.recordings_dir().iterdir()) == [recording["id"]]


def test_record_stop_stops_the_recorder(fake_recorder: FakeRecorder) -> None:
    fake_recorder.running = True

    assert main(["record", "stop"]) == 0

    assert fake_recorder.stops == 1


def test_record_stop_reports_a_failed_stop(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_recorder.running = True
    fake_recorder.error = recorder.RecorderError("Job failed")

    assert main(["record", "stop"]) == 1

    assert capsys.readouterr().err == "Could not stop the recorder: Job failed\n"


def test_record_stop_with_nothing_recording_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["record", "stop"]) == 1

    assert capsys.readouterr().err == "Nothing is recording.\n"
    assert fake_recorder.stops == 0


@pytest.mark.usefixtures("fake_recorder")
def test_record_stop_closes_a_recording_whose_recorder_died() -> None:
    abandoned_id = abandoned_recording()

    assert main(["record", "stop"]) == 1

    assert store.load_recording(abandoned_id)["incomplete"] is True
    assert read_status()["recording"] is None


@pytest.mark.parametrize(
    ("service_result", "incomplete"),
    [("success", False), ("signal", True), ("exit-code", True), ("oom-kill", True), (None, True)],
)
def test_record_stopped_marks_the_recording_by_how_the_recorder_ended(
    monkeypatch: pytest.MonkeyPatch, service_result: str | None, *, incomplete: bool
) -> None:
    recording = store.create_recording(START)
    if service_result is None:
        monkeypatch.delenv("SERVICE_RESULT", raising=False)
    else:
        monkeypatch.setenv("SERVICE_RESULT", service_result)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording["id"]]) == 0

    stopped = store.load_recording(recording["id"])
    assert stopped["incomplete"] is incomplete
    assert stopped["stopped_at"] == "2026-11-18T13:07:12+00:00"
    status = read_status()
    assert status["recording"] is None
    recent = status["recent"]
    assert isinstance(recent, list)
    assert recent[0]["incomplete"] is incomplete


class FakeWorker:
    """Stands in for starting the worker's unit and for the worker itself."""

    def __init__(self) -> None:
        self.error: worker.WorkerError | None = None
        self.starts = 0
        self.works = 0
        self.cancelled: list[tuple[str, int]] = []

    def ensure_worker(self) -> None:
        self.starts += 1
        if self.error is not None:
            raise self.error

    def work(self) -> None:
        self.works += 1

    def cancel(self, recording_id: str, index: int) -> None:
        self.cancelled.append((recording_id, index))


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> FakeWorker:
    fake = FakeWorker()
    monkeypatch.setattr(worker, "ensure_worker", fake.ensure_worker)
    monkeypatch.setattr(worker, "work", fake.work)
    monkeypatch.setattr(worker, "cancel", fake.cancel)
    return fake


def with_audio(recording_id: str, track: str = "mic.opus") -> str:
    (store.recording_dir(recording_id) / track).write_bytes(b"OggS")
    return recording_id


def queued(at: str) -> dict[str, object]:
    return {
        "action": "transcribe",
        "status": "queued",
        "queued_at": at,
        "error": None,
        "details": None,
        "output": None,
    }


def recent_transcribe() -> list[tuple[object, object]]:
    recent = read_status()["recent"]
    assert isinstance(recent, list)
    return [(entry["transcribe"], entry["failed"]) for entry in recent]


def recording_in_progress(fake_recorder: FakeRecorder) -> str:
    fake_recorder.running = True
    return store.create_recording(START)["id"]


def test_record_pause_pauses_the_recorder_and_the_status_says_since_when(
    fake_recorder: FakeRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording_in_progress(fake_recorder)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=60))

    assert main(["record", "pause"]) == 0

    assert fake_recorder.pauses == 1
    assert read_status()["recording"] == {
        "id": recording_id,
        "started_at": "2026-11-18T13:05:42+00:00",
        "paused_at": "2026-11-18T13:06:42+00:00",
    }


def test_record_pause_while_paused_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = recording_in_progress(fake_recorder)
    store.mark_paused(recording_id, START + timedelta(seconds=60))

    assert main(["record", "pause"]) == 1

    assert capsys.readouterr().err == "Already paused.\n"
    assert fake_recorder.pauses == 0
    assert store.load_recording(recording_id).get("paused_at") == "2026-11-18T13:06:42+00:00"


def test_record_pause_reports_a_recorder_that_took_no_signal(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = recording_in_progress(fake_recorder)
    fake_recorder.error = recorder.RecorderError("Failed to kill unit")

    assert main(["record", "pause"]) == 1

    assert capsys.readouterr().err == "Could not pause the recorder: Failed to kill unit\n"
    assert "paused_at" not in store.load_recording(recording_id)


def test_record_resume_resumes_the_recorder_and_the_status_says_how_long_it_was_paused(
    fake_recorder: FakeRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording_in_progress(fake_recorder)
    store.mark_paused(recording_id, START + timedelta(seconds=60))
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "resume"]) == 0

    assert fake_recorder.resumes == 1
    assert read_status()["recording"] == {
        "id": recording_id,
        "started_at": "2026-11-18T13:05:42+00:00",
        "paused_seconds": 30,
    }


def test_record_resume_while_recording_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = recording_in_progress(fake_recorder)

    assert main(["record", "resume"]) == 1

    assert capsys.readouterr().err == "Not paused.\n"
    assert fake_recorder.resumes == 0
    assert "paused_seconds" not in store.load_recording(recording_id)


def test_record_resume_reports_a_recorder_that_took_no_signal(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = recording_in_progress(fake_recorder)
    store.mark_paused(recording_id, START + timedelta(seconds=60))
    fake_recorder.error = recorder.RecorderError("Failed to kill unit")

    assert main(["record", "resume"]) == 1

    assert capsys.readouterr().err == "Could not resume the recorder: Failed to kill unit\n"
    assert store.load_recording(recording_id).get("paused_at") == "2026-11-18T13:06:42+00:00"


@pytest.mark.usefixtures("fake_recorder")
@pytest.mark.parametrize("command", ["pause", "resume", "discard"])
def test_with_nothing_recording_the_recording_controls_refuse_and_close_an_abandoned_one(
    capsys: pytest.CaptureFixture[str], command: str
) -> None:
    abandoned_id = abandoned_recording()

    assert main(["record", command]) == 1

    assert capsys.readouterr().err == "Nothing is recording.\n"
    assert store.load_recording(abandoned_id)["incomplete"] is True
    assert read_status()["recording"] is None


def store_lock_held() -> bool:
    with (store.data_dir() / "store.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


class FakeTrash:
    """Stands in for the desktop's Trash: GLib reads XDG_DATA_HOME once, before any test sets
    it, so a real trash would land in the real one."""

    def __init__(self, into: Path) -> None:
        self.into = into
        self.error: Exception | None = None
        # Each folder moved to the Trash, and whether the store lock was held meanwhile.
        self.trashed: list[tuple[str, bool]] = []

    def trash(self, folder: Path) -> bool:
        self.trashed.append((str(folder), store_lock_held()))
        if self.error is not None:
            raise self.error
        self.into.mkdir()
        folder.rename(self.into / folder.name)
        return True


@pytest.fixture
def fake_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTrash:
    # Ubuntu builds its PyGObject for its own Python alone.
    pytest.importorskip("gi", exc_type=ImportError)
    from gi.repository import Gio  # noqa: PLC0415

    fake = FakeTrash(tmp_path / "Trash")
    # A function, which the file it is called on binds to, as it does Gio's own.
    monkeypatch.setattr(
        Gio.File, "trash", lambda file, _cancellable: fake.trash(Path(file.get_path()))
    )
    return fake


def test_record_discard_stops_the_recorder_and_moves_the_recording_to_the_trash(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    fake_calendar: FakeCalendar,
    fake_trash: FakeTrash,
) -> None:
    recording_id = with_audio(recording_in_progress(fake_recorder))
    folder = store.recording_dir(recording_id)
    store.write_status()
    fake_recorder.on_stop = lambda: main(["record", "stopped", recording_id])

    assert main(["record", "discard"]) == 0

    assert fake_recorder.stops == 1
    assert fake_trash.trashed == [(str(folder), True)]
    assert (fake_trash.into / recording_id / "mic.opus").read_bytes() == b"OggS"
    # Nothing looked for its meeting, and nothing will transcribe it.
    assert fake_calendar.asked == []
    assert fake_worker.starts == 0
    assert json.loads((fake_trash.into / recording_id / "runs.json").read_text()) == []
    assert read_status() == {"version": 1, "recording": None, "recent": []}


def test_record_discard_keeps_a_recording_the_trash_refused_and_says_why(
    fake_recorder: FakeRecorder, fake_trash: FakeTrash, capsys: pytest.CaptureFixture[str]
) -> None:
    from gi.repository import GLib  # noqa: PLC0415 - the fixture skipped the test without it

    recording_id = with_audio(recording_in_progress(fake_recorder))
    fake_recorder.on_stop = lambda: main(["record", "stopped", recording_id])
    fake_trash.error = GLib.Error("Unable to find or create trash directory")

    assert main(["record", "discard"]) == 1

    assert capsys.readouterr().err == (
        "Stopped recording, but could not move it to the Trash: "
        "Unable to find or create trash directory\n"
    )
    assert (store.recording_dir(recording_id) / "mic.opus").read_bytes() == b"OggS"
    assert store.load_runs(recording_id) == []
    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert [(entry["id"], entry["transcribe"]) for entry in recent] == [(recording_id, None)]


def test_record_discard_reports_a_failed_stop_and_trashes_nothing(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    fake_calendar: FakeCalendar,
    fake_trash: FakeTrash,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recording_id = with_audio(recording_in_progress(fake_recorder))
    fake_recorder.error = recorder.RecorderError("Job failed")

    assert main(["record", "discard"]) == 1

    assert capsys.readouterr().err == "Could not stop the recorder: Job failed\n"
    assert fake_trash.trashed == []
    # The recorder goes on, and Finish ends it as any recording.
    fake_recorder.error = None
    fake_recorder.on_stop = lambda: main(["record", "stopped", recording_id])
    assert main(["record", "stop"]) == 0
    assert len(fake_calendar.asked) == 1
    assert [run["status"] for run in store.load_runs(recording_id)] == ["queued"]
    assert fake_worker.starts == 1


@pytest.mark.parametrize("service_result", ["success", "signal"])
def test_record_stopped_queues_a_transcription_of_what_was_captured(
    fake_worker: FakeWorker, monkeypatch: pytest.MonkeyPatch, service_result: str
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"], "call.opus")
    monkeypatch.setenv("SERVICE_RESULT", service_result)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording_id]) == 0

    assert store.load_runs(recording_id) == [queued("2026-11-18T13:07:12+00:00")]
    assert fake_worker.starts == 1
    assert recent_transcribe() == [("queued", [])]


def test_record_stopped_looks_up_and_queues_nothing_for_a_discarded_recording(
    fake_worker: FakeWorker, fake_calendar: FakeCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    store.mark_discarded(recording_id)
    monkeypatch.setenv("SERVICE_RESULT", "success")
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording_id]) == 0

    assert store.load_recording(recording_id)["stopped_at"] == "2026-11-18T13:07:12+00:00"
    assert store.load_runs(recording_id) == []
    assert fake_worker.starts == 0
    assert fake_calendar.asked == []


def test_record_stopped_queues_nothing_for_a_recording_without_audio(
    fake_worker: FakeWorker, fake_calendar: FakeCalendar
) -> None:
    recording = store.create_recording(START)
    for name in store.TRACKS:
        (store.recording_dir(recording["id"]) / name).touch()

    assert main(["record", "stopped", recording["id"]]) == 0

    assert store.load_runs(recording["id"]) == []
    assert fake_worker.starts == 0
    assert fake_calendar.asked == []


WEEKLY_SYNC = calendar.Event(
    name="Weekly sync", attendees=("Ana Díaz", "bob.ruiz"), start=1795006800, end=1795010400
)


@pytest.mark.usefixtures("fake_worker")
def test_record_stopped_names_the_recording_after_the_calendar_event(
    fake_calendar: FakeCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    fake_calendar.event = WEEKLY_SYNC
    monkeypatch.setenv("SERVICE_RESULT", "success")
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording_id]) == 0

    assert fake_calendar.asked == [(START, START + timedelta(seconds=90))]
    assert store.load_recording(recording_id) == {
        "id": "2026-11-18T13-05-42",
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": "2026-11-18T13:07:12+00:00",
        "incomplete": False,
        "name": "Weekly sync",
        "name_source": "calendar",
        "attendees": ["Ana Díaz", "bob.ruiz"],
    }
    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert [(entry["name"], entry["transcribe"]) for entry in recent] == [("Weekly sync", "queued")]


@pytest.mark.usefixtures("fake_worker")
def test_record_stopped_keeps_a_name_typed_while_recording(fake_calendar: FakeCalendar) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    store.set_typed_name(recording_id, "Planning")
    fake_calendar.event = WEEKLY_SYNC

    assert main(["record", "stopped", recording_id]) == 0

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Planning", "typed")
    assert recording.get("attendees") == ["Ana Díaz", "bob.ruiz"]


@pytest.mark.usefixtures("fake_worker")
def test_record_stopped_without_an_event_keeps_the_default_name() -> None:
    recording_id = with_audio(store.create_recording(START)["id"])

    assert main(["record", "stopped", recording_id]) == 0

    recording = store.load_recording(recording_id)
    assert (recording["name"], recording["name_source"]) == ("Meeting", "default")
    assert "attendees" not in recording


def test_record_stopped_reports_a_worker_that_will_not_start(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    fake_worker.error = worker.WorkerError("Failed to connect to bus")
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording_id]) == 1

    assert capsys.readouterr().err == "Could not start transcribing: Failed to connect to bus\n"
    assert store.load_runs(recording_id) == [
        {
            **queued("2026-11-18T13:07:12+00:00"),
            "status": "failed",
            "error": "Failed to connect to bus",
        }
    ]
    assert recent_transcribe() == [("failed", ["transcribe"])]


@pytest.mark.usefixtures("fake_worker")
def test_record_stopped_deletes_the_audio_past_the_age_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    month_ago = START - timedelta(days=30)
    transcribed = with_audio(store.create_recording(month_ago)["id"])
    store.mark_stopped(transcribed, month_ago + timedelta(hours=1), incomplete=False)
    runs = [{**queued(month_ago.isoformat()), "status": "succeeded"}]
    (store.recording_dir(transcribed) / "runs.json").write_text(json.dumps(runs))
    recording_id = with_audio(store.create_recording(START)["id"])
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(seconds=90))

    assert main(["record", "stopped", recording_id]) == 0

    # The defaults: 30 days. The one that just stopped waits for its transcription.
    assert (store.holds_audio(transcribed), store.holds_audio(recording_id)) == (False, True)


def test_transcribe_queues_a_run_for_a_recording(
    fake_worker: FakeWorker, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    store.mark_stopped(recording_id, START + timedelta(seconds=60), incomplete=False)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(hours=1))

    assert main(["transcribe", recording_id]) == 0

    assert store.load_runs(recording_id) == [queued("2026-11-18T14:05:42+00:00")]
    assert fake_worker.starts == 1
    assert recent_transcribe() == [("queued", [])]


@pytest.mark.parametrize("recording_id", ["2026-11-18T13-05-43", ".."])
def test_transcribe_refuses_an_unknown_recording(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str], recording_id: str
) -> None:
    with_audio(store.create_recording(START)["id"])

    assert main(["transcribe", recording_id]) == 1

    assert capsys.readouterr().err == f"No recording {recording_id}.\n"
    assert fake_worker.starts == 0


def test_transcribe_refuses_a_recording_whose_tracks_are_gone(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = store.create_recording(START)["id"]

    assert main(["transcribe", recording_id]) == 1

    assert capsys.readouterr().err == f"The tracks of {recording_id} are gone.\n"
    assert store.load_runs(recording_id) == []
    assert fake_worker.starts == 0


@pytest.mark.parametrize("status", ["queued", "running"])
def test_transcribe_cancel_cancels_the_latest_transcription_in_progress(
    fake_worker: FakeWorker, status: str
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    earlier = {**queued("2026-11-18T13:07:12+00:00"), "status": "failed"}
    latest = {**queued("2026-11-18T14:05:42+00:00"), "status": status}
    store.write_json(store.recording_dir(recording_id) / "runs.json", [earlier, latest])

    assert main(["transcribe", "--cancel", recording_id]) == 0

    assert fake_worker.cancelled == [(recording_id, 1)]
    assert fake_worker.starts == 0


@pytest.mark.parametrize("statuses", [[], ["succeeded"], ["queued", "cancelled"]])
def test_transcribe_cancel_refuses_when_the_latest_transcription_is_not_in_progress(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str], statuses: list[str]
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    runs = [{**queued("2026-11-18T13:07:12+00:00"), "status": status} for status in statuses]
    # Another action's run in progress is not a transcription.
    runs.append({**queued("2026-11-18T15:05:42+00:00"), "action": "summarize", "status": "running"})
    store.write_json(store.recording_dir(recording_id) / "runs.json", runs)

    assert main(["transcribe", "--cancel", recording_id]) == 1

    assert capsys.readouterr().err == (
        f"No transcription of {recording_id} is queued or running.\n"
    )
    assert fake_worker.cancelled == []


def test_transcribe_cancel_refuses_an_unknown_recording(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["transcribe", "--cancel", "2026-11-18T13-05-43"]) == 1

    assert capsys.readouterr().err == "No recording 2026-11-18T13-05-43.\n"
    assert fake_worker.cancelled == []


def test_transcribe_reports_a_worker_that_will_not_start(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    fake_worker.error = worker.WorkerError("Unit minutas-transcribe.service already exists.")

    assert main(["transcribe", recording_id]) == 1

    assert capsys.readouterr().err == (
        "Could not start transcribing: Unit minutas-transcribe.service already exists.\n"
    )
    assert [run["status"] for run in store.load_runs(recording_id)] == ["failed"]


@pytest.mark.usefixtures("fake_recorder")
def test_repair_closes_a_recording_whose_recorder_is_gone_and_starts_the_worker(
    fake_worker: FakeWorker,
) -> None:
    abandoned_id = abandoned_recording()
    store.write_status()

    assert main(["repair"]) == 0

    abandoned = store.load_recording(abandoned_id)
    assert (abandoned["stopped_at"], abandoned["incomplete"]) == ("2026-11-18T13:06:12+00:00", True)
    assert read_status()["recording"] is None
    assert fake_worker.starts == 1


def test_repair_leaves_a_recording_whose_recorder_is_running(
    fake_recorder: FakeRecorder, fake_worker: FakeWorker
) -> None:
    fake_recorder.running = True
    recording = store.create_recording(START)

    assert main(["repair"]) == 0

    assert store.load_recording(recording["id"])["stopped_at"] is None
    assert read_status()["recording"] == {"id": recording["id"], "started_at": START.isoformat()}
    assert fake_worker.starts == 1


@pytest.mark.usefixtures("fake_recorder")
def test_repair_reports_a_worker_that_will_not_start_and_keeps_the_queue(
    fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = with_audio(store.create_recording(START)["id"])
    store.mark_stopped(recording_id, START + timedelta(seconds=90), incomplete=False)
    store.write_json(
        store.recording_dir(recording_id) / "runs.json", [queued("2026-11-18T13:07:12+00:00")]
    )
    fake_worker.error = worker.WorkerError("Failed to connect to bus")

    assert main(["repair"]) == 1

    assert capsys.readouterr().err == "Could not start transcribing: Failed to connect to bus\n"
    assert store.load_runs(recording_id) == [queued("2026-11-18T13:07:12+00:00")]


def test_worker_runs_the_queue(fake_worker: FakeWorker) -> None:
    assert main(["worker"]) == 0

    assert fake_worker.works == 1


class FakeWindow:
    """Stands in for the window module, which needs GTK: it opens nothing."""

    def __init__(self) -> None:
        self.exit_status = 0
        self.opened: list[str | None] = []
        self.settings_opened = 0

    def run(self, recording_id: str | None, *, open_settings: bool = False) -> int:
        self.opened.append(recording_id)
        self.settings_opened += open_settings
        return self.exit_status


@pytest.fixture
def fake_window(monkeypatch: pytest.MonkeyPatch) -> FakeWindow:
    fake = FakeWindow()
    monkeypatch.setitem(sys.modules, "minutas.window", cast("ModuleType", fake))
    return fake


def test_history_opens_the_window(fake_window: FakeWindow) -> None:
    assert main(["history"]) == 0

    assert fake_window.opened == [None]


def test_history_opens_the_window_on_a_recording(fake_window: FakeWindow) -> None:
    store.create_recording(START)

    assert main(["history", "--recording", "2026-11-18T13-05-42"]) == 0

    assert fake_window.opened == ["2026-11-18T13-05-42"]


@pytest.mark.parametrize("recording_id", ["2026-11-18T13-05-43", "..", "."])
def test_history_refuses_an_unknown_recording(
    fake_window: FakeWindow, capsys: pytest.CaptureFixture[str], recording_id: str
) -> None:
    store.create_recording(START)

    assert main(["history", "--recording", recording_id]) == 1

    assert capsys.readouterr().err == f"No recording {recording_id}.\n"
    assert fake_window.opened == []


def test_history_refuses_a_folder_outside_the_recordings(
    fake_window: FakeWindow, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "recording.json").write_text("{}")

    assert main(["history", "--recording", str(elsewhere)]) == 1

    assert capsys.readouterr().err == f"No recording {elsewhere}.\n"
    assert fake_window.opened == []


def test_settings_alone_opens_the_settings(fake_window: FakeWindow) -> None:
    assert main(["settings"]) == 0

    assert (fake_window.opened, fake_window.settings_opened) == ([None], 1)


def test_history_opens_no_settings(fake_window: FakeWindow) -> None:
    assert main(["history"]) == 0

    assert fake_window.settings_opened == 0


def test_history_passes_on_the_windows_exit_status(fake_window: FakeWindow) -> None:
    fake_window.exit_status = 2

    assert main(["history"]) == 2


class FakePublisher:
    """Stands in for the agaric module: no agaric-mcp, GStreamer or keyring."""

    def __init__(self) -> None:
        self.succeeds = True
        # Each publish: the recording, when, the page, and the recording's name at that moment.
        self.published: list[tuple[str, datetime, str | None, str]] = []
        self.spaces: list[tuple[str, str]] = []
        self.error: agaric.AgaricError | None = None

    def publish(self, recording_id: str, published: datetime, *, page: str | None = None) -> bool:
        name = store.load_recording(recording_id)["name"]
        self.published.append((recording_id, published, page, name))
        return self.succeeds

    def list_spaces(self) -> list[tuple[str, str]]:
        if self.error is not None:
            raise self.error
        return self.spaces


@pytest.fixture
def fake_publisher(monkeypatch: pytest.MonkeyPatch) -> FakePublisher:
    fake = FakePublisher()
    monkeypatch.setattr(agaric, "publish", fake.publish)
    monkeypatch.setattr(agaric, "list_spaces", fake.list_spaces)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(hours=1))
    return fake


@pytest.mark.parametrize(("succeeds", "code"), [(True, 0), (False, 1)])
def test_publish_agaric_publishes_the_recording(
    fake_publisher: FakePublisher, *, succeeds: bool, code: int
) -> None:
    store.create_recording(START)
    fake_publisher.succeeds = succeeds

    assert main(["publish", "agaric", "2026-11-18T13-05-42"]) == code

    assert fake_publisher.published == [
        ("2026-11-18T13-05-42", START + timedelta(hours=1), None, "Meeting")
    ]


def test_publish_agaric_to_a_page(fake_publisher: FakePublisher) -> None:
    store.create_recording(START)

    assert main(["publish", "agaric", "2026-11-18T13-05-42", "--page", "01JPAGE"]) == 0

    assert fake_publisher.published[0][2] == "01JPAGE"


def test_publish_agaric_with_a_name_types_it_first(fake_publisher: FakePublisher) -> None:
    store.create_recording(START)

    assert main(["publish", "agaric", "2026-11-18T13-05-42", "--name", " Weekly sync "]) == 0

    assert fake_publisher.published[0][3] == "Weekly sync"
    assert store.load_recording("2026-11-18T13-05-42")["name"] == "Weekly sync"


def test_publish_agaric_refuses_a_blank_name(
    fake_publisher: FakePublisher, capsys: pytest.CaptureFixture[str]
) -> None:
    store.create_recording(START)

    assert main(["publish", "agaric", "2026-11-18T13-05-42", "--name", "  "]) == 1

    assert capsys.readouterr().err == "A name can't be blank.\n"
    assert fake_publisher.published == []
    assert store.load_recording("2026-11-18T13-05-42")["name"] == "Meeting"


def test_publish_agaric_refuses_an_unknown_recording(
    fake_publisher: FakePublisher, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "agaric", "2026-11-18T13-05-42"]) == 1

    assert capsys.readouterr().err == "No recording 2026-11-18T13-05-42.\n"
    assert fake_publisher.published == []


class FakeGitLab:
    """Stands in for the gitlab module's publish: no glab."""

    def __init__(self) -> None:
        self.succeeds = True
        self.published: list[tuple[str, datetime, gitlab.Target]] = []

    def publish(self, recording_id: str, published: datetime, target: gitlab.Target) -> bool:
        self.published.append((recording_id, published, target))
        return self.succeeds


@pytest.fixture
def fake_gitlab(monkeypatch: pytest.MonkeyPatch) -> FakeGitLab:
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab, "publish", fake.publish)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(hours=1))
    return fake


ISSUE_LINK = "https://gitlab.example.com/group/project/-/issues/7"


def summarized() -> str:
    recording_id = store.create_recording(START)["id"]
    (store.recording_dir(recording_id) / "summary.md").write_text("Ship on Friday.\n")
    return recording_id


@pytest.mark.parametrize(("succeeds", "code"), [(True, 0), (False, 1)])
def test_publish_gitlab_posts_the_summary_where_the_link_says(
    fake_gitlab: FakeGitLab, *, succeeds: bool, code: int
) -> None:
    recording_id = summarized()
    fake_gitlab.succeeds = succeeds

    assert main(["publish", "gitlab", recording_id, f"{ISSUE_LINK}#note_3"]) == code

    assert fake_gitlab.published == [
        (
            recording_id,
            START + timedelta(hours=1),
            gitlab.Target(ISSUE_LINK, "gitlab.example.com", "group/project", 7),
        )
    ]


@pytest.mark.parametrize(
    ("link", "error"),
    [
        ("https://gitlab.example.com/group", "Not a GitLab issue or project link: "),
        (ISSUE_LINK, "No recording 2026-11-18T13-05-42."),
        (ISSUE_LINK, "2026-11-18T13-05-42 has no summary yet: summarize it first."),
    ],
    ids=["not gitlab", "unknown recording", "no summary"],
)
def test_publish_gitlab_refuses_what_it_cannot_post(
    fake_gitlab: FakeGitLab, capsys: pytest.CaptureFixture[str], link: str, error: str
) -> None:
    if error.startswith("Not"):
        summarized()
        error += link
    elif error.endswith("summarize it first."):
        store.create_recording(START)

    assert main(["publish", "gitlab", "2026-11-18T13-05-42", link]) == 1

    assert capsys.readouterr().err == f"{error}\n"
    assert fake_gitlab.published == []


class FakeGitHub:
    """Stands in for the github module's publish: no gh."""

    def __init__(self) -> None:
        self.succeeds = True
        self.published: list[tuple[str, datetime, github.Target]] = []

    def publish(self, recording_id: str, published: datetime, target: github.Target) -> bool:
        self.published.append((recording_id, published, target))
        return self.succeeds


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "publish", fake.publish)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(hours=1))
    return fake


GITHUB_ISSUE = "https://github.com/owner/repo/issues/7"


@pytest.mark.parametrize(("succeeds", "code"), [(True, 0), (False, 1)])
def test_publish_github_posts_the_summary_where_the_link_says(
    fake_github: FakeGitHub, *, succeeds: bool, code: int
) -> None:
    recording_id = summarized()
    fake_github.succeeds = succeeds

    assert main(["publish", "github", recording_id, f"{GITHUB_ISSUE}#issuecomment-3"]) == code

    assert fake_github.published == [
        (
            recording_id,
            START + timedelta(hours=1),
            github.Target(GITHUB_ISSUE, "github.com", "owner/repo", 7),
        )
    ]


@pytest.mark.parametrize(
    ("link", "error"),
    [
        ("https://github.com/owner", "Not a GitHub issue or repository link: "),
        (GITHUB_ISSUE, "No recording 2026-11-18T13-05-42."),
        (GITHUB_ISSUE, "2026-11-18T13-05-42 has no summary yet: summarize it first."),
    ],
    ids=["not github", "unknown recording", "no summary"],
)
def test_publish_github_refuses_what_it_cannot_post(
    fake_github: FakeGitHub, capsys: pytest.CaptureFixture[str], link: str, error: str
) -> None:
    if error.startswith("Not"):
        summarized()
        error += link
    elif error.endswith("summarize it first."):
        store.create_recording(START)

    assert main(["publish", "github", "2026-11-18T13-05-42", link]) == 1

    assert capsys.readouterr().err == f"{error}\n"
    assert fake_github.published == []


class FakeSummarizer:
    """Stands in for the summarizer module: no Devin."""

    def __init__(self) -> None:
        self.succeeds = True
        self.summarized: list[tuple[str, datetime, str, Sequence[str]]] = []

    def summarize(
        self, recording_id: str, started: datetime, prompt: str, context: Sequence[str]
    ) -> bool:
        self.summarized.append((recording_id, started, prompt, context))
        return self.succeeds


@pytest.fixture
def fake_summarizer(monkeypatch: pytest.MonkeyPatch) -> FakeSummarizer:
    fake = FakeSummarizer()
    monkeypatch.setattr(summarizer, "summarize", fake.summarize)
    monkeypatch.setattr(cli, "now", lambda: START + timedelta(hours=1))
    return fake


def transcribed() -> str:
    recording_id = store.create_recording(START)["id"]
    (store.recording_dir(recording_id) / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")
    return recording_id


@pytest.mark.parametrize(("succeeds", "code"), [(True, 0), (False, 1)])
def test_summarize_summarizes_the_recording_with_the_prompt(
    fake_summarizer: FakeSummarizer, *, succeeds: bool, code: int
) -> None:
    recording_id = transcribed()
    fake_summarizer.succeeds = succeeds

    # With "=", as the window passes it: a prompt that starts with a dash is still the prompt.
    assert main(["summarize", recording_id, "--prompt=- Only the blockers. "]) == code

    assert fake_summarizer.summarized == [
        (recording_id, START + timedelta(hours=1), "- Only the blockers.", [])
    ]


def test_summarize_adds_the_context_presets_named_in_their_order(
    fake_summarizer: FakeSummarizer,
) -> None:
    recording_id = transcribed()

    arguments = ["--prompt=Summarize.", "--context=Team glossary", "--context=-Project"]
    assert main(["summarize", recording_id, *arguments]) == 0

    assert fake_summarizer.summarized == [
        (recording_id, START + timedelta(hours=1), "Summarize.", ["Team glossary", "-Project"])
    ]


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (["--prompt", " \n "], "A prompt can't be blank."),
        (["--prompt", "Summarize."], "No recording 2026-11-18T13-05-42."),
        (
            ["--prompt", "Summarize.", "--context=Team", "--context=../.ssh/id_ed25519"],
            "No context preset can be named ../.ssh/id_ed25519.",
        ),
    ],
    ids=["blank prompt", "unknown recording", "bad context name"],
)
def test_summarize_refuses_a_blank_prompt_or_an_unknown_recording(
    fake_summarizer: FakeSummarizer,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    error: str,
) -> None:
    if not error.startswith("No recording"):
        transcribed()

    assert main(["summarize", "2026-11-18T13-05-42", *arguments]) == 1

    assert capsys.readouterr().err == f"{error}\n"
    assert fake_summarizer.summarized == []


def test_summarize_refuses_a_recording_without_a_transcript(
    fake_summarizer: FakeSummarizer, capsys: pytest.CaptureFixture[str]
) -> None:
    store.create_recording(START)

    assert main(["summarize", "2026-11-18T13-05-42", "--prompt", "Summarize."]) == 1

    assert capsys.readouterr().err == (
        "2026-11-18T13-05-42 has no transcript yet: transcribe it first.\n"
    )
    assert fake_summarizer.summarized == []


def test_settings_agaric_sets_the_space() -> None:
    assert main(["settings", "agaric", "--space", "01HZWORK"]) == 0

    assert settings.agaric_space_id() == "01HZWORK"


TOKEN = "agt_55555555555555555555555555555555"  # noqa: S105 - a made-up agent token
AGARIC_CONFIG = json.dumps(
    {
        "mcpServers": {
            "agaric-minutas-vptx4p": {
                "command": "/home/me/.local/share/com.agaric.app/bin/agaric-mcp",
                "args": [
                    "--socket",
                    "/home/me/.local/share/com.agaric.app/mcp.sock",
                    "--token",
                    TOKEN,
                ],
            }
        }
    },
    indent=2,
)
WORK, PERSONAL = ("01HZWORK", "Work"), ("01HZPERSONAL", "Personal")
PUBLISHES_TO_WORK = "The stored agent token publishes to Work.\n"
SEVERAL = "The stored agent token may use several spaces: choose the one to publish to.\n"
NO_SPACE = (
    "The stored agent token may use no space: give its agent read-write access to the Work "
    "space in Agaric's Settings → Agent access.\n"
)


@pytest.mark.parametrize("pasted", [f" {TOKEN} \n", AGARIC_CONFIG])
def test_settings_agaric_takes_the_token_alone_or_in_agarics_whole_config(
    fake_publisher: FakePublisher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pasted: str,
) -> None:
    fake_publisher.spaces = [WORK]
    monkeypatch.setattr(sys, "stdin", io.StringIO(pasted))

    assert main(["settings", "agaric", "--token-from-stdin"]) == 0

    assert settings.agaric_token() == TOKEN
    assert settings.agaric_space_id() == "01HZWORK"
    assert capsys.readouterr() == ("01HZWORK\tWork\n", PUBLISHES_TO_WORK)


@pytest.mark.parametrize("pasted", ["\n", "agt_1\n", '{"mcpServers": {}}'])
def test_settings_agaric_refuses_what_holds_no_token(
    fake_publisher: FakePublisher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pasted: str,
) -> None:
    fake_publisher.spaces = [WORK]
    monkeypatch.setattr(sys, "stdin", io.StringIO(pasted))

    assert main(["settings", "agaric", "--token-from-stdin"]) == 1

    said = capsys.readouterr()
    assert said.out == ""
    assert said.err == (
        "No Agaric agent token in that: paste the config Agaric shows for the agent in its "
        "Settings → Agent access, or the token alone.\n"
    )
    assert settings.agaric_token() is None


def test_settings_agaric_says_why_the_keyring_refused_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def locked(_token: str) -> None:
        reason = "dismissed"
        raise KeyringLocked(reason)  # what dismissing the unlock prompt raises

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{TOKEN}\n"))
    monkeypatch.setattr(settings, "set_agaric_token", locked)

    assert main(["settings", "agaric", "--token-from-stdin"]) == 1

    assert capsys.readouterr().err == (
        "Could not store the Agaric agent token in the keyring: dismissed\n"
    )


def test_settings_agaric_keeps_the_token_when_agaric_cannot_list_the_spaces(
    fake_publisher: FakePublisher,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_publisher.error = agaric.AgaricError(agaric.NOT_RUNNING)
    monkeypatch.setattr(sys, "stdin", io.StringIO(AGARIC_CONFIG))

    assert main(["settings", "agaric", "--token-from-stdin"]) == 1

    assert settings.agaric_token() == TOKEN
    assert settings.agaric_space_id() is None
    assert capsys.readouterr().err == (
        f"Kept the agent token, but couldn't choose the space: {agaric.NOT_RUNNING}\n"
    )


@pytest.mark.parametrize(
    ("saved", "spaces", "outcome"),
    [
        (None, [WORK], ("01HZWORK", PUBLISHES_TO_WORK)),
        ("01HZGONE", [WORK], ("01HZWORK", PUBLISHES_TO_WORK)),
        (
            "01HZPERSONAL",
            [PERSONAL, WORK],
            ("01HZPERSONAL", "The stored agent token publishes to Personal.\n"),
        ),
        (None, [PERSONAL, WORK], (None, SEVERAL)),
        ("01HZGONE", [PERSONAL, WORK], ("01HZGONE", SEVERAL)),
        (None, [], (None, NO_SPACE)),
    ],
)
def test_settings_agaric_chooses_the_space_only_when_the_token_leaves_no_choice(
    fake_publisher: FakePublisher,
    capsys: pytest.CaptureFixture[str],
    saved: str | None,
    spaces: list[tuple[str, str]],
    outcome: tuple[str | None, str],
) -> None:
    """outcome: the space set afterwards, and what it said about it."""
    if saved is not None:
        settings.set_agaric_space_id(saved)
    fake_publisher.spaces = spaces

    assert main(["settings", "agaric", "--list-spaces"]) == 0

    assert (settings.agaric_space_id(), capsys.readouterr().err) == outcome


def test_settings_agaric_lists_the_spaces(
    fake_publisher: FakePublisher, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_publisher.spaces = [("01HZPERSONAL", "Personal"), ("01HZWORK", "Work")]

    assert main(["settings", "agaric", "--list-spaces"]) == 0

    assert capsys.readouterr().out == "01HZPERSONAL\tPersonal\n01HZWORK\tWork\n"


def test_settings_agaric_reports_why_spaces_cannot_be_listed(
    fake_publisher: FakePublisher, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_publisher.error = agaric.AgaricError(agaric.NOT_RUNNING)

    assert main(["settings", "agaric", "--list-spaces"]) == 1

    assert capsys.readouterr().err == f"{agaric.NOT_RUNNING}\n"


def test_settings_agaric_without_an_option_says_what_it_takes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["settings", "agaric"]) == 1

    assert capsys.readouterr().err == (
        "Nothing to do: pass --space, --token-from-stdin or --list-spaces.\n"
    )


class FakeGlabLogin:
    """Stands in for the gitlab module's glab login: no glab."""

    def __init__(self) -> None:
        self.problem = ""
        self.logged_in = True
        self.refused: str | None = None
        self.handed: list[tuple[str, str]] = []
        self.asked: list[str] = []

    def log_in(self, host: str, token: str) -> None:
        self.handed.append((host, token))
        if self.refused is not None:
            raise gitlab.GitLabError(self.refused)

    def login_status(self, host: str) -> str:
        self.asked.append(host)
        if not self.logged_in:
            reason = f"glab isn't logged in to {host}"
            raise gitlab.GitLabError(reason)
        return f"glab is logged in to {host}."


@pytest.fixture
def glab_login(monkeypatch: pytest.MonkeyPatch) -> FakeGlabLogin:
    fake = FakeGlabLogin()
    monkeypatch.setattr(gitlab, "glab_problem", lambda: fake.problem)
    monkeypatch.setattr(gitlab, "log_in", fake.log_in)
    monkeypatch.setattr(gitlab, "login_status", fake.login_status)
    return fake


GITLAB_TOKEN = "glpat-" + "a" * 20


@pytest.mark.parametrize(
    ("link", "host"), [(None, "gitlab.com"), (ISSUE_LINK, "gitlab.example.com")]
)
def test_settings_gitlab_says_whether_glab_is_logged_in_to_the_default_links_server(
    glab_login: FakeGlabLogin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    link: str | None,
    host: str,
) -> None:
    settings.set_gitlab_link(link)
    # Without --token-from-stdin, a token on stdin is left alone.
    monkeypatch.setattr(sys, "stdin", io.StringIO(GITLAB_TOKEN))

    assert main(["settings", "gitlab"]) == 0

    assert capsys.readouterr() == ("", f"glab is logged in to {host}.\n")
    assert (glab_login.asked, glab_login.handed) == ([host], [])


def test_settings_gitlab_fails_while_glab_is_not_logged_in(
    glab_login: FakeGlabLogin, capsys: pytest.CaptureFixture[str]
) -> None:
    glab_login.logged_in = False

    assert main(["settings", "gitlab"]) == 1

    assert capsys.readouterr().err == "glab isn't logged in to gitlab.com\n"


def test_settings_gitlab_hands_glab_the_token_then_says_whether_it_is_logged_in(
    glab_login: FakeGlabLogin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.set_gitlab_link(ISSUE_LINK)
    monkeypatch.setattr(sys, "stdin", io.StringIO(f" {GITLAB_TOKEN} \n"))

    assert main(["settings", "gitlab", "--token-from-stdin"]) == 0

    assert glab_login.handed == [("gitlab.example.com", GITLAB_TOKEN)]
    assert glab_login.asked == ["gitlab.example.com"]
    assert capsys.readouterr().err == "glab is logged in to gitlab.example.com.\n"


def test_settings_gitlab_refuses_a_blank_token(
    glab_login: FakeGlabLogin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(" \n"))

    assert main(["settings", "gitlab", "--token-from-stdin"]) == 1

    assert capsys.readouterr().err == (
        "No token in that: paste a GitLab personal access token with the api scope.\n"
    )
    assert (glab_login.handed, glab_login.asked) == ([], [])


def test_settings_gitlab_says_why_glab_did_not_keep_the_token(
    glab_login: FakeGlabLogin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused = "glab couldn't keep the token for gitlab.com: the keyring is locked"
    glab_login.refused = refused
    monkeypatch.setattr(sys, "stdin", io.StringIO(GITLAB_TOKEN))

    assert main(["settings", "gitlab", "--token-from-stdin"]) == 1

    assert capsys.readouterr().err == f"{refused}\n"
    assert glab_login.asked == []


@pytest.mark.parametrize("options", [[], ["--token-from-stdin"]])
def test_settings_gitlab_says_what_is_wrong_with_the_glab_installed(
    glab_login: FakeGlabLogin,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    glab_login.problem = "glab not found: install glab 1.91.0 or newer"
    monkeypatch.setattr(sys, "stdin", io.StringIO(GITLAB_TOKEN))

    assert main(["settings", "gitlab", *options]) == 1

    assert capsys.readouterr().err == "glab not found: install glab 1.91.0 or newer\n"
    assert (glab_login.handed, glab_login.asked) == ([], [])


class Releases:
    """A Minutas repository with release tags, and the clone install.sh ran from."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin"
        self.clone = root / "clone"
        self.link = root / "home" / ".local" / "share" / "minutas" / "app" / "repository"
        # Each release's install.sh adds a line here.
        self.installed = root / "installed.log"

    @staticmethod
    def git(folder: Path, *args: str) -> str:
        return subprocess.run(  # noqa: S603 - the test's own command
            ["git", "-C", str(folder), *args],  # noqa: S607 - git from the PATH
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def release(self, tag: str, install_sh: str | None = None) -> None:
        script = self.origin / "install.sh"
        script.write_text(install_sh or f'#!/bin/sh\necho {tag} >>"{self.installed}"\n')
        script.chmod(0o755)
        self.git(self.origin, "add", "install.sh")
        self.git(self.origin, "commit", "--quiet", "--message", tag)
        self.git(self.origin, "tag", "--annotate", "--message", tag, tag)

    def checked_out(self) -> str:
        return self.git(self.clone, "rev-parse", "HEAD")

    def commit(self, tag: str) -> str:
        return self.git(self.origin, "rev-parse", f"{tag}^{{commit}}")


@pytest.fixture
def releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Releases:
    """The clone is at v0.9.0; v0.10.0 came out after it was cloned, so only a fetch finds it.

    As text, v0.1.0 sorts first and v0.9.0 last; as versions, v0.10.0 is the newest.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # The pre-push hook runs the suite with GIT_DIR and the like naming this repository: left in,
    # they turn every git command here on it.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    # Nothing of this machine's git configuration: no signing, no hooks.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.org")
    releases = Releases(tmp_path)
    releases.origin.mkdir()
    releases.git(releases.origin, "init", "--quiet", "--initial-branch", "main")
    releases.release("v0.1.0")
    releases.release("v0.9.0")
    releases.git(tmp_path, "clone", "--quiet", str(releases.origin), str(releases.clone))
    releases.git(releases.clone, "checkout", "--quiet", "v0.9.0")
    releases.link.parent.mkdir(parents=True)
    releases.link.symlink_to(releases.clone)
    releases.release("v0.10.0")
    return releases


def test_update_moves_to_the_latest_release_and_runs_its_install_sh(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["update"]) == 0

    assert capsys.readouterr().out == "Updating to v0.10.0.\n"
    assert releases.checked_out() == releases.commit("v0.10.0")
    assert releases.installed.read_text() == "v0.10.0\n"


def test_update_on_the_latest_release_says_so_and_runs_install_sh_again(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["update"])
    capsys.readouterr()

    assert main(["update"]) == 0

    assert capsys.readouterr().out == "Already on the latest release, v0.10.0.\n"
    assert releases.checked_out() == releases.commit("v0.10.0")
    assert releases.installed.read_text() == "v0.10.0\nv0.10.0\n"


def test_update_without_install_sh_having_run_says_to_run_it(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    releases.link.unlink()

    assert main(["update"]) == 1

    assert capsys.readouterr().err == (
        f"No clone of Minutas at {releases.link}: run install.sh from your clone first.\n"
    )


def test_update_reports_why_the_fetch_failed(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    before = releases.checked_out()
    releases.origin.rename(releases.origin.with_name("moved"))

    assert main(["update"]) == 1

    error = capsys.readouterr().err
    assert error.startswith("Could not fetch the releases: fatal: ")
    assert str(releases.origin) in error
    assert releases.checked_out() == before
    assert not releases.installed.exists()


def test_update_without_a_release_says_so(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    releases.git(releases.origin, "tag", "--delete", "v0.1.0", "v0.9.0", "v0.10.0")
    releases.git(releases.clone, "tag", "--delete", "v0.1.0", "v0.9.0")

    assert main(["update"]) == 1

    assert capsys.readouterr().err == "No release to update to yet.\n"


def test_update_reports_why_the_checkout_failed(
    releases: Releases, capsys: pytest.CaptureFixture[str]
) -> None:
    (releases.clone / "install.sh").write_text("#!/bin/sh\n# a local change\n")

    assert main(["update"]) == 1

    assert capsys.readouterr().err.startswith(
        "Could not check out v0.10.0: error: Your local changes to the following files would be "
        "overwritten by checkout:"
    )
    assert releases.checked_out() == releases.commit("v0.9.0")
    assert not releases.installed.exists()


def test_update_after_a_failed_install_sh_runs_it_again(releases: Releases) -> None:
    failing = f'#!/bin/sh\necho v0.11.0 >>"{releases.installed}"\nexit 3\n'
    releases.release("v0.11.0", install_sh=failing)

    assert main(["update"]) == 3
    assert main(["update"]) == 3

    assert releases.installed.read_text() == "v0.11.0\nv0.11.0\n"


STOPPED = START + timedelta(seconds=60)
CONTINUED = START + timedelta(seconds=100)


def false_stop(*transcriptions: str) -> str:
    """A recording stopped a minute in, with both tracks and its transcriptions so far."""
    recording_id = store.create_recording(START)["id"]
    for name in store.TRACKS:
        with_audio(recording_id, name)
    store.mark_stopped(recording_id, STOPPED, incomplete=False)
    runs = [{**queued(STOPPED.isoformat()), "status": status} for status in transcriptions]
    (store.recording_dir(recording_id) / "runs.json").write_text(json.dumps(runs))
    return recording_id


@pytest.mark.parametrize("transcription", ["queued", "running"])
def test_record_continue_records_into_the_recording_again_and_cancels_its_transcription(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transcription: str,
) -> None:
    recording_id = false_stop("failed", transcription)
    monkeypatch.setattr(cli, "now", lambda: CONTINUED)

    assert main(["record", "continue", recording_id]) == 0

    assert capsys.readouterr() == ("", "")
    assert fake_recorder.started == [
        (
            store.recording_dir(recording_id),
            [sys.executable, "-m", "minutas", "record", "stopped", recording_id],
        )
    ]
    assert fake_recorder.tracks == [("mic.continued.opus", "call.continued.opus")]
    assert fake_worker.cancelled == [(recording_id, 1)]
    # The time since the stop is one more pause: the timer goes on from the minute it had.
    assert read_status() == {
        "version": 1,
        "recording": {
            "id": recording_id,
            "started_at": "2026-11-18T13:05:42+00:00",
            "paused_seconds": 40,
            "continued": True,
        },
        "recent": [],
    }


@pytest.mark.parametrize("transcriptions", [[], ["failed"], ["queued", "cancelled"]])
def test_record_continue_cancels_no_transcription_that_has_ended(
    fake_recorder: FakeRecorder, fake_worker: FakeWorker, transcriptions: list[str]
) -> None:
    recording_id = false_stop(*transcriptions)

    assert main(["record", "continue", recording_id]) == 0

    assert len(fake_recorder.started) == 1
    assert fake_worker.cancelled == []


def test_a_continued_recording_pauses_and_finishes_as_a_new_one_and_keeps_its_typed_name(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    fake_calendar: FakeCalendar,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_id = false_stop("cancelled")
    store.set_typed_name(recording_id, "Planning")
    fake_calendar.event = WEEKLY_SYNC
    fake_recorder.on_stop = lambda: main(["record", "stopped", recording_id])
    monkeypatch.setenv("SERVICE_RESULT", "success")
    # Continue, pause, resume, the stop, and the transcription it queues.
    clock = iter([CONTINUED + timedelta(seconds=s) for s in (0, 10, 15, 30, 31)])
    monkeypatch.setattr(cli, "now", lambda: next(clock))

    assert main(["record", "continue", recording_id]) == 0
    fake_recorder.running = True
    assert [main(["record", command]) for command in ("pause", "resume", "stop")] == [0, 0, 0]

    assert (fake_recorder.pauses, fake_recorder.resumes, fake_recorder.stops) == (1, 1, 1)
    assert fake_calendar.asked == [(START, CONTINUED + timedelta(seconds=30))]
    assert store.load_recording(recording_id) == {
        "id": recording_id,
        "started_at": "2026-11-18T13:05:42+00:00",
        "stopped_at": "2026-11-18T13:07:52+00:00",
        "incomplete": False,
        "name": "Planning",
        "name_source": "typed",
        "attendees": ["Ana Díaz", "bob.ruiz"],
        # The 40 s it was stopped for and the 5 s it was paused for.
        "paused_seconds": 45,
        "continued": True,
    }
    assert [run["status"] for run in store.load_runs(recording_id)] == ["cancelled", "queued"]
    assert fake_worker.starts == 1
    recent = read_status()["recent"]
    assert isinstance(recent, list)
    assert [(entry["name"], entry["duration_seconds"]) for entry in recent] == [("Planning", 85)]


def test_record_continue_takes_up_a_recording_whose_recorder_died(
    fake_recorder: FakeRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    abandoned_id = abandoned_recording()
    folder = store.recording_dir(abandoned_id)
    (folder / "call.opus").write_bytes(b"OggS")
    written = (folder / "mic.opus").stat().st_mtime
    os.utime(folder / "call.opus", (written, written))
    monkeypatch.setattr(cli, "now", lambda: CONTINUED)

    assert main(["record", "continue", abandoned_id]) == 0

    assert fake_recorder.tracks == [store.PARTS]
    # It stopped when its audio was last written, 30 s in.
    assert store.load_recording(abandoned_id).get("paused_seconds") == 70


def test_record_continue_while_recording_refuses(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = false_stop("queued")
    fake_recorder.running = True

    assert main(["record", "continue", recording_id]) == 1

    assert capsys.readouterr().err == "Already recording.\n"
    assert fake_recorder.started == []
    assert store.load_recording(recording_id)["stopped_at"] == "2026-11-18T13:06:42+00:00"


def test_record_continue_refuses_an_unknown_recording(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["record", "continue", "2026-11-18T13-05-43"]) == 1

    assert capsys.readouterr().err == "No recording 2026-11-18T13-05-43.\n"
    assert fake_recorder.started == []


def test_record_continue_refuses_a_recording_that_cannot_be_continued_and_says_why(
    fake_recorder: FakeRecorder, fake_worker: FakeWorker, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = false_stop("succeeded")
    before = (store.recording_dir(recording_id) / "recording.json").read_text()
    abandoned_id = store.create_recording(START + timedelta(hours=1))["id"]
    store.write_status()

    assert main(["record", "continue", recording_id]) == 1

    assert capsys.readouterr().err == f"Can't continue {recording_id}: it has been transcribed.\n"
    assert (fake_recorder.started, fake_worker.cancelled) == ([], [])
    assert (store.recording_dir(recording_id) / "recording.json").read_text() == before
    # The status says what closing the recording whose recorder had died changed.
    assert store.load_recording(abandoned_id)["incomplete"] is True
    assert read_status()["recording"] is None


def test_record_continue_whose_recorder_captures_nothing_leaves_the_recording_stopped_again(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recording_id = false_stop("running")
    fake_recorder.error = recorder.RecorderError("no audio reached its tracks")

    def exit_command_ran(_: Path) -> None:
        """Its unit ran, so its exit command did."""
        main(["record", "stopped", recording_id])

    fake_recorder.on_failed_start = exit_command_ran
    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    # Continue, the recorder's exit two seconds on, the transcription it queues, and after.
    clock = iter([CONTINUED + timedelta(seconds=s) for s in (0, 2, 3, 4)])
    monkeypatch.setattr(cli, "now", lambda: next(clock))

    assert main(["record", "continue", recording_id]) == 1

    assert capsys.readouterr().err == "Could not start the recorder: no audio reached its tracks\n"
    # As its exit command left it, the two seconds the recorder tried for included.
    recording = store.load_recording(recording_id)
    assert (recording["stopped_at"], recording["incomplete"]) == ("2026-11-18T13:07:24+00:00", True)
    assert store.duration_seconds(recording) == 62
    # The transcription it cancelled is queued again, as after any stop.
    assert fake_worker.cancelled == [(recording_id, 0)]
    assert [run["status"] for run in store.load_runs(recording_id)] == ["running", "queued"]
    assert read_status()["recording"] is None


def test_record_continue_whose_recorder_never_started_stops_the_recording_as_any_stop_does(
    fake_recorder: FakeRecorder,
    fake_worker: FakeWorker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    recording_id = false_stop("running")
    store.mark_stopped(recording_id, STOPPED, incomplete=True)
    # No unit, so no exit command either.
    fake_recorder.error = recorder.RecorderError("Failed to connect to bus")
    monkeypatch.setattr(cli, "now", lambda: CONTINUED)

    assert main(["record", "continue", recording_id]) == 1

    assert capsys.readouterr().err == "Could not start the recorder: Failed to connect to bus\n"
    recording = store.load_recording(recording_id)
    assert (recording["stopped_at"], recording["incomplete"]) == ("2026-11-18T13:07:22+00:00", True)
    assert store.duration_seconds(recording) == 60
    # The transcription it cancelled is queued again.
    assert fake_worker.cancelled == [(recording_id, 0)]
    assert [run["status"] for run in store.load_runs(recording_id)] == ["running", "queued"]
    status = read_status()
    assert status["recording"] is None
    recent = status["recent"]
    assert isinstance(recent, list)
    assert [(entry["id"], entry["continuable"]) for entry in recent] == [(recording_id, True)]


@pytest.mark.parametrize(
    ("noted", "setting", "asked"),
    [
        # Its mic track was the echo-cancelled one, so its part is too, whatever the setting now.
        ({"echo_cancelled": True}, False, True),
        # It was the plain one: because the setting was off, or because the canceller failed.
        ({}, True, False),
        ({"echo_cancelled": False}, True, False),
    ],
)
def test_record_continue_records_the_mic_the_recording_started_with_whatever_the_setting_is_now(
    fake_recorder: FakeRecorder,
    capsys: pytest.CaptureFixture[str],
    noted: dict[str, bool],
    *,
    setting: bool,
    asked: bool,
) -> None:
    recording_id = false_stop()
    for echo_cancelled in noted.values():
        store.mark_echo_cancelled(recording_id, echo_cancelled=echo_cancelled)
    settings.set_echo_cancel(on=setting)

    assert main(["record", "continue", recording_id]) == 0

    assert capsys.readouterr() == ("", "")
    assert fake_recorder.echo_cancel == [asked]
    recording = store.load_recording(recording_id)
    assert {key: recording[key] for key in recording if key == "echo_cancelled"} == noted


def test_record_continue_whose_echo_canceller_fails_records_the_plain_mic_and_says_so(
    fake_recorder: FakeRecorder, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = false_stop()
    store.mark_echo_cancelled(recording_id, echo_cancelled=True)
    fake_recorder.canceller_starts = False

    assert main(["record", "continue", recording_id]) == 0

    assert capsys.readouterr() == ("", NO_ECHO_CANCELLATION)
    assert fake_recorder.tracks == [store.PARTS]
    # Its mic track is no longer the echo-cancelled mic all through.
    recording = store.load_recording(recording_id)
    assert (recording["echo_cancelled"], recording["stopped_at"]) == (False, None)


def test_record_discard_refuses_a_continued_recording(
    fake_recorder: FakeRecorder, fake_trash: FakeTrash, capsys: pytest.CaptureFixture[str]
) -> None:
    recording_id = false_stop("cancelled")
    assert main(["record", "continue", recording_id]) == 0
    fake_recorder.running = True

    assert main(["record", "discard"]) == 1

    assert capsys.readouterr().err == (
        "A continued recording can't be discarded: Finish ends it.\n"
    )
    assert (fake_recorder.stops, fake_trash.trashed) == (0, [])
    assert "discarded" not in store.load_recording(recording_id)
