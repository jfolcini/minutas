import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from keyring.errors import KeyringLocked

from minutas import agaric, cli, recorder, settings, store, worker
from minutas.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)


class FakeRecorder:
    """Stands in for the recorder module: no systemd, microphone or GStreamer."""

    def __init__(self) -> None:
        self.running = False
        self.error: recorder.RecorderError | None = None
        self.on_failed_start: Callable[[Path], None] = lambda _: None
        self.started: list[tuple[Path, list[str]]] = []
        self.stops = 0

    def start(self, folder: Path, *, on_exit: list[str]) -> None:
        if self.error is not None:
            self.on_failed_start(folder)
            raise self.error
        self.started.append((folder, on_exit))

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        if self.error is not None:
            raise self.error
        self.stops += 1


@pytest.fixture
def fake_recorder(monkeypatch: pytest.MonkeyPatch) -> FakeRecorder:
    fake = FakeRecorder()
    monkeypatch.setattr(recorder, "start", fake.start)
    monkeypatch.setattr(recorder, "is_running", fake.is_running)
    monkeypatch.setattr(recorder, "stop", fake.stop)
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
    [("success", False), ("signal", True), ("exit-code", True), (None, True)],
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

    def ensure_worker(self) -> None:
        self.starts += 1
        if self.error is not None:
            raise self.error

    def work(self) -> None:
        self.works += 1


@pytest.fixture
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> FakeWorker:
    fake = FakeWorker()
    monkeypatch.setattr(worker, "ensure_worker", fake.ensure_worker)
    monkeypatch.setattr(worker, "work", fake.work)
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


def test_record_stopped_queues_nothing_for_a_recording_without_audio(
    fake_worker: FakeWorker,
) -> None:
    recording = store.create_recording(START)
    for name in store.TRACKS:
        (store.recording_dir(recording["id"]) / name).touch()

    assert main(["record", "stopped", recording["id"]]) == 0

    assert store.load_runs(recording["id"]) == []
    assert fake_worker.starts == 0


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


def test_worker_runs_the_queue(fake_worker: FakeWorker) -> None:
    assert main(["worker"]) == 0

    assert fake_worker.works == 1


class FakeWindow:
    """Stands in for the window module, which needs GTK: it opens nothing."""

    def __init__(self) -> None:
        self.exit_status = 0
        self.opened: list[str | None] = []

    def run(self, recording_id: str | None) -> int:
        self.opened.append(recording_id)
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


def test_settings_agaric_sets_the_space() -> None:
    assert main(["settings", "agaric", "--space", "01HZWORK"]) == 0

    assert settings.agaric_space_id() == "01HZWORK"


def test_settings_agaric_reads_the_token_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(" agt_1 \n"))

    assert main(["settings", "agaric", "--token-from-stdin"]) == 0

    assert settings.agaric_token() == "agt_1"


def test_settings_agaric_says_why_the_keyring_refused_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def locked(_token: str) -> None:
        reason = "dismissed"
        raise KeyringLocked(reason)  # what dismissing the unlock prompt raises

    monkeypatch.setattr(sys, "stdin", io.StringIO("agt_1\n"))
    monkeypatch.setattr(settings, "set_agaric_token", locked)

    assert main(["settings", "agaric", "--token-from-stdin"]) == 1

    assert capsys.readouterr().err == (
        "Could not store the Agaric agent token in the keyring: dismissed\n"
    )


def test_settings_agaric_refuses_an_empty_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))

    assert main(["settings", "agaric", "--token-from-stdin"]) == 1

    assert capsys.readouterr().err == "No token on stdin.\n"
    assert settings.agaric_token() is None


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
