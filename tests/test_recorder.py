import subprocess
from pathlib import Path

import pytest

from minutas import recorder


class FakeRun:
    """Stands in for subprocess.run: records each command and answers with a set result."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stderr = ""
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


CAPTURE = "audio/x-raw,rate=16000,channels=1"


def test_start_launches_one_pipeline_for_both_tracks_in_a_user_unit(
    fake_run: FakeRun, xdg_dirs: Path
) -> None:
    folder = Path("/data/recordings/2026-11-18T13-05-42")

    recorder.start(folder, on_exit=["/venv/bin/python", "-m", "minutas", "record", "stopped"])

    assert fake_run.calls[0][0] == [
        "systemd-run",
        "--user",
        "--unit=minutas-recorder.service",
        "--collect",
        "--quiet",
        "--property=KillSignal=SIGINT",
        "--property=TimeoutStopSec=5",
        "--property=ExecStopPost=/venv/bin/python -m minutas record stopped",
        f"--setenv=XDG_DATA_HOME={xdg_dirs / 'data'}",
        "--working-directory=/data/recordings/2026-11-18T13-05-42",
        "--",
        "gst-launch-1.0",
        "-e",
        "pulsesrc",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        CAPTURE,
        "!",
        "opusenc",
        "bitrate=32000",
        "!",
        "oggmux",
        "!",
        "filesink",
        "buffer-mode=unbuffered",
        "location=mic.opus",
        "pulsesrc",
        "device=@DEFAULT_MONITOR@",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        CAPTURE,
        "!",
        "opusenc",
        "bitrate=32000",
        "!",
        "oggmux",
        "!",
        "filesink",
        "buffer-mode=unbuffered",
        "location=call.opus",
    ]


def test_start_quotes_the_exit_command_for_systemd(fake_run: FakeRun) -> None:
    recorder.start(Path("/r"), on_exit=["/home/me/my venv/python", "-m", "minutas"])

    assert "--property=ExecStopPost='/home/me/my venv/python' -m minutas" in fake_run.calls[0][0]


def test_start_passes_no_data_home_when_unset(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME")

    recorder.start(Path("/r"), on_exit=["python"])

    assert [arg for arg in fake_run.calls[0][0] if arg.startswith("--setenv")] == []


def test_start_reports_why_systemd_run_failed(fake_run: FakeRun) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "Failed to start transient service unit: Unit already exists.\n"

    with pytest.raises(
        recorder.RecorderError, match=r"^Failed to start transient service unit: Unit already"
    ):
        recorder.start(Path("/r"), on_exit=["python"])


def test_failure_without_output_names_the_command(fake_run: FakeRun) -> None:
    fake_run.returncode = 3

    with pytest.raises(recorder.RecorderError, match=r"^systemctl exited with status 3$"):
        recorder.stop()


@pytest.mark.parametrize(("returncode", "running"), [(0, True), (3, False)])
def test_is_running_asks_systemd_for_the_unit(
    fake_run: FakeRun, returncode: int, *, running: bool
) -> None:
    fake_run.returncode = returncode

    assert recorder.is_running() is running
    assert fake_run.calls[0][0] == [
        "systemctl",
        "--user",
        "is-active",
        "--quiet",
        "minutas-recorder.service",
    ]


def test_stop_waits_for_the_unit_to_stop(fake_run: FakeRun) -> None:
    recorder.stop()

    assert fake_run.calls[0][0] == ["systemctl", "--user", "stop", "minutas-recorder.service"]
