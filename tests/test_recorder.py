import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from minutas import recorder, store

IS_ACTIVE = ["systemctl", "--user", "is-active", "--quiet", "minutas-recorder.service"]
STOP = ["systemctl", "--user", "stop", "minutas-recorder.service"]
CANCELLER_IS_ACTIVE = ["systemctl", "--user", "is-active", "--quiet", "minutas-echo-cancel.service"]
STOP_CANCELLER = ["systemctl", "--user", "stop", "minutas-echo-cancel.service"]
OUTPUT_PORTS = ["pw-link", "--output"]
# What pw-link lists once the echo canceller has added its source.
WITH_CANCELLED_SOURCE = (
    "alsa_input.pci-0000_00_1f.3.analog-stereo:capture_FL\n"
    "echo-cancel-capture:monitor_FL\n"
    "minutas-echo-cancelled:capture_FL\n"
    "minutas-echo-cancelled:capture_FR\n"
)


def answer[Answer](answers: list[Answer]) -> Answer:
    return answers.pop(0) if len(answers) > 1 else answers[0]


class FakeRun:
    """Stands in for subprocess.run: records each command and answers with a set result.

    is-active answers from is_active, or canceller_is_active for the echo canceller's unit, and
    pw-link from output_ports: one answer per call, then the last one again. A command holding
    one of failing exits 1.
    """

    def __init__(self) -> None:
        self.returncode = 0
        self.stderr = ""
        self.is_active = [0]
        self.canceller_is_active = [0]
        self.output_ports = ["alsa_input.pci-0000_00_1f.3.analog-stereo:capture_FL\n"]
        self.failing: list[str] = []
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        returncode, stdout = self.returncode, ""
        if args == IS_ACTIVE:
            returncode = answer(self.is_active)
        elif args == CANCELLER_IS_ACTIVE:
            returncode = answer(self.canceller_is_active)
        elif args == OUTPUT_PORTS:
            stdout = answer(self.output_ports)
        elif any(part in args for part in self.failing):
            returncode = 1
        return subprocess.CompletedProcess(args, returncode, stdout, self.stderr)

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


def start_echo_cancelled(folder: Path, fake_clock: FakeClock) -> bool:
    """Starts a recording with echo cancellation on, its tracks filling at once."""
    fill_tracks(folder)
    # A recording's folder is in the data folder, which holds the canceller's conf.
    store.data_dir().mkdir(parents=True)
    return recorder.start(
        folder, on_exit=["python"], echo_cancel=True, clock=fake_clock.clock, wait=fake_clock.wait
    )


def mic_source(command: list[str]) -> list[str]:
    """The recorder's mic branch up to its first link."""
    return command[command.index("pulsesrc") : command.index("!")]


def exit_commands(command: list[str]) -> list[str]:
    return [arg for arg in command if arg.startswith("--property=ExecStopPost=")]


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
            "audioconvert",
            "!",
            "audiocheblimit",
            "mode=high-pass",
            "cutoff=20",
            "!",
            "audioconvert",
            "!",
            "level",
            "interval=1000000000",
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
            "audioconvert",
            "!",
            "audiocheblimit",
            "mode=high-pass",
            "cutoff=20",
            "!",
            "audioconvert",
            "!",
            "level",
            "interval=1000000000",
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


def test_start_without_echo_cancellation_says_the_mic_is_the_plain_one(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fill_tracks(folder)

    echo_cancelled = recorder.start(
        folder, on_exit=["python"], clock=fake_clock.clock, wait=fake_clock.wait
    )

    assert echo_cancelled is False
    assert len(fake_run.calls) == 1


def test_start_with_echo_cancellation_records_the_mic_from_the_cancellers_source_once_it_is_there(
    fake_run: FakeRun, xdg_dirs: Path, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.output_ports = ["", "echo-cancel-capture:monitor_FL\n", WITH_CANCELLED_SOURCE]

    echo_cancelled = start_echo_cancelled(folder, fake_clock)

    assert echo_cancelled is True
    conf = xdg_dirs / "data" / "minutas" / "echo-cancel.conf"
    *before, recording = fake_run.commands()
    assert before == [
        [
            "systemd-run",
            "--user",
            "--unit=minutas-echo-cancel.service",
            "--collect",
            "--quiet",
            "--",
            "pipewire",
            "-c",
            str(conf),
        ],
        OUTPUT_PORTS,
        CANCELLER_IS_ACTIVE,
        OUTPUT_PORTS,
        CANCELLER_IS_ACTIVE,
        OUTPUT_PORTS,
    ]
    assert fake_clock.waits == 2
    assert mic_source(recording) == ["pulsesrc", "device=minutas-echo-cancelled"]
    # The call is recorded as ever.
    assert recording.count("pulsesrc") == 2
    assert recording.count("device=@DEFAULT_MONITOR@") == 1
    # The canceller stops with the recorder, however that ends, and first: a failed exit command
    # would keep systemd from running what follows it, and a canceller that will not stop must
    # not keep the exit command from running.
    assert exit_commands(recording) == [
        "--property=ExecStopPost=-systemctl --user stop minutas-echo-cancel.service",
        "--property=ExecStopPost=python",
    ]


def test_the_cancellers_conf_loads_pipewires_webrtc_echo_canceller_against_the_speakers(
    fake_run: FakeRun, xdg_dirs: Path, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.output_ports = [WITH_CANCELLED_SOURCE]

    start_echo_cancelled(folder, fake_clock)

    conf = (xdg_dirs / "data" / "minutas" / "echo-cancel.conf").read_text().splitlines()
    module = conf.index("    { name = libpipewire-module-echo-cancel")
    assert [line.strip() for line in conf[module + 1 : module + 9]] == [
        "args = {",
        "library.name = aec/libspa-aec-webrtc",
        # The reference is what the default sink plays, and no sink of its own is added.
        "monitor.mode = true",
        "source.props = {",
        'node.name = "minutas-echo-cancelled"',
        'node.description = "Minutas echo-cancelled microphone"',
        "}",
        "}",
    ]


def test_start_records_the_plain_mic_when_the_cancellers_source_never_shows(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    echo_cancelled = start_echo_cancelled(folder, fake_clock)

    assert echo_cancelled is False
    # Polled every 0.05 s for 2 s.
    assert fake_clock.waits == 40
    *_, last_poll, stopped, recording = fake_run.commands()
    assert (last_poll, stopped) == (OUTPUT_PORTS, STOP_CANCELLER)
    assert mic_source(recording) == ["pulsesrc"]
    assert exit_commands(recording) == ["--property=ExecStopPost=python"]


def test_start_records_the_plain_mic_at_once_when_the_canceller_will_not_run(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.failing = ["--unit=minutas-echo-cancel.service"]
    fake_run.canceller_is_active = [3]

    echo_cancelled = start_echo_cancelled(folder, fake_clock)

    assert echo_cancelled is False
    assert fake_clock.waits == 0
    assert fake_run.commands()[1:4] == [OUTPUT_PORTS, CANCELLER_IS_ACTIVE, STOP_CANCELLER]
    assert mic_source(fake_run.commands()[4]) == ["pulsesrc"]
    assert len(fake_run.calls) == 5


def test_start_records_from_a_canceller_that_was_left_running(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    # systemd-run refuses a unit that exists; the recorder stops it when it ends, as its own.
    fake_run.failing = ["--unit=minutas-echo-cancel.service"]
    fake_run.output_ports = [WITH_CANCELLED_SOURCE]

    echo_cancelled = start_echo_cancelled(folder, fake_clock)

    assert echo_cancelled is True
    assert mic_source(fake_run.commands()[-1]) == ["pulsesrc", "device=minutas-echo-cancelled"]


def test_a_recorder_that_will_not_start_leaves_no_canceller_behind(
    fake_run: FakeRun, folder: Path, fake_clock: FakeClock
) -> None:
    fake_run.failing = ["--unit=minutas-recorder.service"]
    fake_run.stderr = "Failed to start transient service unit: Unit already exists.\n"
    fake_run.output_ports = [WITH_CANCELLED_SOURCE]

    with pytest.raises(
        recorder.RecorderError, match=r"^Failed to start transient service unit: Unit already"
    ):
        start_echo_cancelled(folder, fake_clock)

    assert fake_run.commands()[-1] == STOP_CANCELLER
    assert "--unit=minutas-recorder.service" in fake_run.commands()[-2]
