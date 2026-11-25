import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from minutas import recorder

IS_ACTIVE = ["systemctl", "--user", "is-active", "--quiet", "minutas-recorder.service"]
STOP = ["systemctl", "--user", "stop", "minutas-recorder.service"]


class FakeRun:
    """Stands in for subprocess.run: records each command and answers with a set result.

    is-active answers from is_active, one code per call, then keeps giving the last one.
    """

    def __init__(self) -> None:
        self.returncode = 0
        self.stderr = ""
        self.is_active = [0]
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        returncode = self.returncode
        if args == IS_ACTIVE:
            returncode = self.is_active.pop(0) if len(self.is_active) > 1 else self.is_active[0]
        return subprocess.CompletedProcess(args, returncode, "", self.stderr)

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]


class FakeClock:
    """A clock that only moves when the recorder waits; the tracks fill on a chosen wait."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.now = 0.0
        self.waits = 0
        self.fill_on_wait: int | None = None
        self.fill: tuple[str, ...] = ("mic.opus", "call.opus")

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.now += seconds
        self.waits += 1
        if self.waits == self.fill_on_wait:
            fill_tracks(self.folder, self.fill)


def fill_tracks(folder: Path, names: tuple[str, ...] = ("mic.opus", "call.opus")) -> None:
    for name in names:
        (folder / name).write_bytes(b"OggS")


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "2026-11-18T13-05-42"
    path.mkdir()
    return path


@pytest.fixture
def fake_clock(folder: Path) -> FakeClock:
    return FakeClock(folder)


def start(folder: Path, fake_clock: FakeClock, on_exit: list[str] | None = None) -> None:
    recorder.start(
        folder, on_exit=on_exit or ["python"], clock=fake_clock.clock, wait=fake_clock.wait
    )


CAPTURE = "audio/x-raw,rate=16000,channels=1"


def test_start_launches_one_pipeline_for_both_tracks_in_a_user_unit(
    fake_run: FakeRun, xdg_dirs: Path, folder: Path, fake_clock: FakeClock
) -> None:
    fill_tracks(folder)

    start(folder, fake_clock, ["/venv/bin/python", "-m", "minutas", "record", "stopped"])

    assert fake_run.commands() == [
        [
            "systemd-run",
            "--user",
            "--unit=minutas-recorder.service",
            "--collect",
            "--quiet",
            "--property=KillSignal=SIGINT",
            "--property=TimeoutStopSec=5",
            "--property=MemoryMax=256M",
            "--property=MemorySwapMax=0",
            "--property=ExecStopPost=/venv/bin/python -m minutas record stopped",
            f"--setenv=XDG_DATA_HOME={xdg_dirs / 'data'}",
            f"--setenv=XDG_CONFIG_HOME={xdg_dirs / 'config'}",
            f"--working-directory={folder}",
            "--",
            sys.executable,
            "-m",
            "minutas.capture",
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
    ]


def test_start_quotes_the_exit_command_for_systemd(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fill_tracks(folder)

    start(folder, fake_clock, ["/home/me/my venv/python", "-m", "minutas"])

    assert "--property=ExecStopPost='/home/me/my venv/python' -m minutas" in fake_run.calls[0][0]


def test_start_passes_neither_folder_when_unset(
    fake_run: FakeRun, monkeypatch: pytest.MonkeyPatch, folder: Path, fake_clock: FakeClock
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    fill_tracks(folder)

    start(folder, fake_clock)

    assert [arg for arg in fake_run.calls[0][0] if arg.startswith("--setenv")] == []


def test_start_returns_once_both_tracks_hold_audio(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fake_clock.fill_on_wait = 3

    start(folder, fake_clock)

    assert fake_clock.waits == 3
    assert fake_run.commands()[1:] == [IS_ACTIVE, IS_ACTIVE, IS_ACTIVE]


def test_start_fails_when_the_recorder_dies_before_capturing(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.is_active = [0, 3]
    fill_tracks(folder, ("mic.opus",))
    (folder / "call.opus").touch()

    with pytest.raises(
        recorder.RecorderError,
        match=r"^no audio reached its tracks; journalctl --user -u minutas-recorder\.service says",
    ):
        start(folder, fake_clock)

    assert fake_clock.waits == 1
    assert fake_run.commands()[1:] == [IS_ACTIVE, IS_ACTIVE, STOP]


def test_start_stops_a_recorder_that_captures_nothing_in_time(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fill_tracks(folder, ("call.opus",))

    with pytest.raises(recorder.RecorderError, match=r"^no audio reached its tracks"):
        start(folder, fake_clock)

    # Polled every 0.05 s for 2 s.
    assert fake_clock.waits == 40
    assert fake_run.commands()[-2:] == [IS_ACTIVE, STOP]


def test_start_reports_why_systemd_run_failed(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.returncode = 1
    fake_run.stderr = "Failed to start transient service unit: Unit already exists.\n"

    with pytest.raises(
        recorder.RecorderError, match=r"^Failed to start transient service unit: Unit already"
    ):
        start(folder, fake_clock)

    assert len(fake_run.calls) == 1


def test_failure_without_output_names_the_command(fake_run: FakeRun) -> None:
    fake_run.returncode = 3

    with pytest.raises(recorder.RecorderError, match=r"^systemctl exited with status 3$"):
        recorder.stop()


@pytest.mark.parametrize(("returncode", "running"), [(0, True), (3, False)])
def test_is_running_asks_systemd_for_the_unit(
    fake_run: FakeRun, returncode: int, *, running: bool
) -> None:
    fake_run.is_active = [returncode]

    assert recorder.is_running() is running
    assert fake_run.commands() == [IS_ACTIVE]


def test_stop_waits_for_the_unit_to_stop(fake_run: FakeRun) -> None:
    recorder.stop()

    assert fake_run.commands() == [STOP]


@pytest.mark.parametrize(
    ("ask", "sent"), [(recorder.pause, "SIGUSR1"), (recorder.resume, "SIGUSR2")]
)
def test_pause_and_resume_signal_the_units_program(
    fake_run: FakeRun, ask: Callable[[], None], sent: str
) -> None:
    ask()

    assert fake_run.commands() == [
        ["systemctl", "--user", "kill", f"--signal={sent}", "minutas-recorder.service"]
    ]


@pytest.mark.parametrize("ask", [recorder.pause, recorder.resume])
def test_a_signal_systemd_could_not_send_says_why(
    fake_run: FakeRun, ask: Callable[[], None]
) -> None:
    fake_run.returncode = 5
    fake_run.stderr = "Failed to kill unit minutas-recorder.service: Unit not loaded.\n"

    with pytest.raises(
        recorder.RecorderError,
        match=r"^Failed to kill unit minutas-recorder\.service: Unit not loaded\.$",
    ):
        ask()


def test_start_records_into_the_files_it_is_given_and_waits_for_those(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    # The tracks already hold what was recorded before Continue: they prove no capture.
    fill_tracks(folder)
    parts = ("mic.continued.opus", "call.continued.opus")
    fake_clock.fill = parts
    fake_clock.fill_on_wait = 2

    recorder.start(
        folder, on_exit=["python"], tracks=parts, clock=fake_clock.clock, wait=fake_clock.wait
    )

    assert [arg for arg in fake_run.calls[0][0] if arg.startswith("location=")] == [
        "location=mic.continued.opus",
        "location=call.continued.opus",
    ]
    assert fake_clock.waits == 2
