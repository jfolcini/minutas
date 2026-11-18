import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from minutas import cli, recorder, store
from minutas.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable

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


class FakeGio:
    def __init__(self) -> None:
        self.returncode = 0
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.returncode)


@pytest.fixture
def fake_gio(monkeypatch: pytest.MonkeyPatch) -> FakeGio:
    fake = FakeGio()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_history_opens_the_recordings_folder(fake_gio: FakeGio) -> None:
    assert main(["history"]) == 0

    assert fake_gio.calls == [["gio", "open", str(store.recordings_dir())]]
    assert store.recordings_dir().is_dir()


def test_history_opens_one_recordings_folder(fake_gio: FakeGio) -> None:
    store.create_recording(START)

    assert main(["history", "--recording", "2026-11-18T13-05-42"]) == 0

    assert fake_gio.calls == [["gio", "open", str(store.recording_dir("2026-11-18T13-05-42"))]]


@pytest.mark.parametrize("recording_id", ["2026-11-18T13-05-43", "..", "."])
def test_history_refuses_an_unknown_recording(
    fake_gio: FakeGio, capsys: pytest.CaptureFixture[str], recording_id: str
) -> None:
    store.create_recording(START)

    assert main(["history", "--recording", recording_id]) == 1

    assert capsys.readouterr().err == f"No recording {recording_id}.\n"
    assert fake_gio.calls == []


def test_history_refuses_a_folder_outside_the_recordings(
    fake_gio: FakeGio, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "recording.json").write_text("{}")

    assert main(["history", "--recording", str(elsewhere)]) == 1

    assert capsys.readouterr().err == f"No recording {elsewhere}.\n"
    assert fake_gio.calls == []


def test_history_passes_on_gio_failure(fake_gio: FakeGio) -> None:
    fake_gio.returncode = 2

    assert main(["history"]) == 2
