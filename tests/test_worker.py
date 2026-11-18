import fcntl
import gc
import json
import os
import signal
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TextIO, cast

import pytest

import minutas
from minutas import store, worker

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
SRC = str(Path(minutas.__file__).parent.parent)


def start_command(data_home: Path, config_home: Path) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--unit=minutas-transcribe.service",
        "--collect",
        "--quiet",
        "--property=CPUSchedulingPolicy=idle",
        "--property=IOSchedulingClass=idle",
        "--property=MemoryMax=6G",
        "--property=MemorySwapMax=512M",
        "--property=OOMPolicy=continue",
        f"--setenv=XDG_DATA_HOME={data_home}",
        f"--setenv=XDG_CONFIG_HOME={config_home}",
        "--",
        sys.executable,
        "-m",
        "minutas",
        "worker",
    ]


def write_transcript(args: list[str], _: TextIO) -> int:
    (Path(args[3]) / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")
    return 0


class FakeSystem:
    """Stands in for subprocess.run: systemd-run answers from start_codes, one per call, then
    keeps giving the last; the transcriber is the transcribe callable, given its stderr file."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.start_codes = [0]
        self.start_stderr = ""
        self.transcribe: Callable[[list[str], TextIO], int] = write_transcript

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args[0] == "systemd-run":
            code = self.start_codes.pop(0) if len(self.start_codes) > 1 else self.start_codes[0]
            return subprocess.CompletedProcess(args, code, "", self.start_stderr)
        return subprocess.CompletedProcess(
            args, self.transcribe(args, cast("TextIO", kwargs["stderr"]))
        )

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]

    def transcribed(self) -> list[str]:
        return [Path(args[3]).name for args in self.commands() if args[0] != "systemd-run"]


class FakeSpeechPython:
    def __init__(self, path: Path) -> None:
        self.path = path

    def install(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()


class FakeTime:
    """Stands in for the worker's time module: the clock only moves when the worker waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits = 0
        self.on_wait: Callable[[], None] = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.waits += 1
        self.on_wait()


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def speech(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSpeechPython:
    fake = FakeSpeechPython(tmp_path / "home" / ".local/share/minutas/speech/bin/python")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    fake.install()
    return fake


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(worker, "time", fake)
    return fake


@pytest.fixture
def sigterm() -> Iterator[None]:
    """work() installs its own SIGTERM handler; the test process gets its own back."""
    handler = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, handler)


@pytest.fixture
def traced() -> Iterator[None]:
    tracemalloc.start()
    yield
    tracemalloc.stop()


@pytest.fixture
def worker_running() -> Iterator[None]:
    """Holds the worker's lock the way a running worker does, from another open file."""
    store.data_dir().mkdir(parents=True, exist_ok=True)
    with (store.data_dir() / "worker.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def recording(started: datetime = START) -> str:
    """A stopped recording with audio in both tracks."""
    recording_id = store.create_recording(started)["id"]
    store.mark_stopped(recording_id, started + timedelta(seconds=60), incomplete=False)
    for name in store.TRACKS:
        (store.recording_dir(recording_id) / name).write_bytes(b"OggS")
    return recording_id


def queued_run(queued: datetime) -> store.Run:
    return store.Run(
        action="transcribe",
        status="queued",
        queued_at=queued.isoformat(),
        error=None,
        details=None,
        output=None,
    )


def put_runs(recording_id: str, runs: list[store.Run]) -> None:
    store.save_runs(recording_id, runs)


def transcribe_status(recording_id: str) -> tuple[object, object]:
    status = json.loads((store.data_dir() / "status.json").read_text())
    entry = next(entry for entry in status["recent"] if entry["id"] == recording_id)
    return entry["transcribe"], entry["failed"]


def worker_lock_held() -> bool:
    with (store.data_dir() / "worker.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


QUEUED = {
    "action": "transcribe",
    "status": "queued",
    "queued_at": "2026-11-18T13:06:42+00:00",
    "error": None,
    "details": None,
    "output": None,
}


def test_queue_adds_a_queued_run_and_starts_the_worker(system: FakeSystem, xdg_dirs: Path) -> None:
    recording_id = recording()

    worker.queue(recording_id, START + timedelta(seconds=60))

    assert store.load_runs(recording_id) == [QUEUED]
    assert system.commands() == [start_command(xdg_dirs / "data", xdg_dirs / "config")]
    assert transcribe_status(recording_id) == ("queued", [])


def test_queue_appends_after_earlier_runs(system: FakeSystem) -> None:
    recording_id = recording()
    earlier = {**QUEUED, "status": "failed", "error": "interrupted"}
    put_runs(recording_id, [cast("store.Run", earlier)])

    worker.queue(recording_id, START + timedelta(seconds=60))

    assert store.load_runs(recording_id) == [earlier, QUEUED]
    assert len(system.commands()) == 1


def test_start_forwards_only_the_environment_that_is_set(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, xdg_dirs: Path
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME")

    worker.ensure_worker()

    assert [arg for arg in system.commands()[0] if arg.startswith("--setenv")] == [
        f"--setenv=XDG_CONFIG_HOME={xdg_dirs / 'config'}"
    ]


@pytest.mark.usefixtures("worker_running")
def test_queue_starts_nothing_while_a_worker_holds_its_lock(system: FakeSystem) -> None:
    recording_id = recording()

    worker.queue(recording_id, START)

    assert system.commands() == []
    assert store.load_runs(recording_id)[0]["status"] == "queued"


def test_start_retries_while_a_worker_that_found_the_queue_empty_is_exiting(
    system: FakeSystem, fake_clock: FakeTime
) -> None:
    system.start_codes = [1, 1, 0]

    worker.ensure_worker()

    assert [args[0] for args in system.commands()] == ["systemd-run"] * 3
    assert fake_clock.waits == 2


def test_start_stops_retrying_once_a_starting_worker_takes_its_lock(
    system: FakeSystem, fake_clock: FakeTime
) -> None:
    system.start_codes = [1, 0]
    store.data_dir().mkdir(parents=True)
    with (store.data_dir() / "worker.lock").open("a") as lock:
        fake_clock.on_wait = lambda: fcntl.flock(lock, fcntl.LOCK_EX)

        worker.ensure_worker()

    assert [args[0] for args in system.commands()] == ["systemd-run"]
    assert fake_clock.waits == 1


def test_queue_fails_the_run_when_no_worker_starts(
    system: FakeSystem, fake_clock: FakeTime
) -> None:
    recording_id = recording()
    system.start_codes = [1]
    system.start_stderr = "Failed to connect to bus: No medium found\n"

    with pytest.raises(worker.WorkerError, match=r"^Failed to connect to bus: No medium found$"):
        worker.queue(recording_id, START + timedelta(seconds=60))

    # Polled every 0.05 s for 2 s.
    assert fake_clock.waits == 40
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "failed", "error": "Failed to connect to bus: No medium found"}
    ]
    assert transcribe_status(recording_id) == ("failed", ["transcribe"])


@pytest.mark.usefixtures("fake_clock")
def test_start_failure_without_output_names_the_command(system: FakeSystem) -> None:
    system.start_codes = [4]

    with pytest.raises(worker.WorkerError, match=r"^systemd-run exited with status 4$"):
        worker.ensure_worker()


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_transcribes_and_records_success(system: FakeSystem, tmp_path: Path) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START + timedelta(seconds=60))])
    seen: list[tuple[object, object]] = []

    def transcriber(args: list[str], stderr: TextIO) -> int:
        seen.append(transcribe_status(recording_id))
        stderr.write("loading the model\n")
        return write_transcript(args, stderr)

    system.transcribe = transcriber

    worker.work()

    folder = store.recording_dir(recording_id)
    assert system.commands() == [
        [
            str(tmp_path / "home/.local/share/minutas/speech/bin/python"),
            "-m",
            "minutas.transcriber",
            str(folder),
        ]
    ]
    kwargs = system.calls[0][1]
    assert cast("dict[str, str]", kwargs["env"]).get("PYTHONPATH") == SRC
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert seen == [("running", [])]
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "succeeded", "details": "run-0.log", "output": "transcript.md"}
    ]
    assert (folder / "run-0.log").read_text() == "loading the model\n"
    assert (folder / "transcript.md").read_text() == "[00:00:01] **Me:** Hola.\n"
    assert transcribe_status(recording_id) == ("succeeded", [])
    assert worker_lock_held() is False


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_puts_the_package_first_on_an_existing_python_path(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    monkeypatch.setenv("PYTHONPATH", "/opt/lib")

    worker.work()

    env = cast("dict[str, str]", system.calls[0][1]["env"])
    assert env["PYTHONPATH"] == f"{SRC}:/opt/lib"


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_the_glossary_as_the_word_list(system: FakeSystem, xdg_dirs: Path) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    glossary = xdg_dirs / "config" / "minutas" / "glossary.txt"
    glossary.parent.mkdir(parents=True)
    glossary.write_text("deploy\nPR\n")

    worker.work()

    assert system.commands()[0][4:] == ["--words", str(glossary)]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_finds_the_glossary_in_dot_config_by_default(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    monkeypatch.delenv("XDG_CONFIG_HOME")
    glossary = tmp_path / "home" / ".config" / "minutas" / "glossary.txt"
    glossary.parent.mkdir(parents=True)
    glossary.write_text("deploy\n")

    worker.work()

    assert system.commands()[0][4:] == ["--words", str(glossary)]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_takes_queued_runs_oldest_first(system: FakeSystem) -> None:
    first, second, third = (recording(START + timedelta(hours=hour)) for hour in range(3))
    put_runs(first, [queued_run(datetime(2026, 11, 18, 18, 0, tzinfo=UTC))])
    put_runs(second, [queued_run(datetime(2026, 11, 18, 16, 0, tzinfo=UTC))])
    # 15:00 UTC, the earliest, though its text sorts between the other two.
    plus_two = timezone(timedelta(hours=2))
    put_runs(third, [queued_run(datetime(2026, 11, 18, 17, 0, tzinfo=plus_two))])

    worker.work()

    assert system.transcribed() == [third, second, first]
    for recording_id in (first, second, third):
        assert [run["status"] for run in store.load_runs(recording_id)] == ["succeeded"]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_records_the_last_stderr_line_as_the_error(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START + timedelta(seconds=60))])
    reason = "Parakeet model not found in ~/.cache/minutas/models; run install.sh"

    def transcriber(_: list[str], stderr: TextIO) -> int:
        stderr.write(f"loading the model\n{reason}\n\n")
        return 1

    system.transcribe = transcriber

    worker.work()

    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "failed", "error": reason, "details": "run-0.log"}
    ]
    log = store.recording_dir(recording_id) / "run-0.log"
    assert log.read_text() == f"loading the model\n{reason}\n\n"
    assert transcribe_status(recording_id) == ("failed", ["transcribe"])


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_names_the_exit_status_when_the_transcriber_says_nothing(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    system.transcribe = lambda _args, _stderr: 3

    worker.work()

    assert store.load_runs(recording_id)[0]["error"] == "The transcriber exited with status 3"


@pytest.mark.parametrize(
    ("killer", "reason"),
    [
        (signal.SIGKILL, "Transcription ran out of its memory limit"),
        (signal.SIGSEGV, "The transcriber was killed by SIGSEGV"),
    ],
)
@pytest.mark.usefixtures("speech", "sigterm")
def test_work_says_why_a_killed_transcriber_stopped_and_goes_on(
    system: FakeSystem, killer: signal.Signals, reason: str
) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START + timedelta(seconds=60))])
    put_runs(second, [queued_run(START + timedelta(hours=2))])

    def killed(args: list[str], stderr: TextIO) -> int:
        if Path(args[3]).name == second:
            return write_transcript(args, stderr)
        stderr.write("transcribing turn 3 of 40\n")
        return -killer

    system.transcribe = killed

    worker.work()

    assert store.load_runs(first) == [
        {**QUEUED, "status": "failed", "error": reason, "details": "run-0.log"}
    ]
    assert [run["status"] for run in store.load_runs(second)] == ["succeeded"]


@pytest.mark.usefixtures("speech", "sigterm", "traced")
def test_work_finds_the_reason_in_a_long_log_without_holding_it(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    progress = "transcribing turn 1 of 40\n" * 1000

    def chatty(_: list[str], stderr: TextIO) -> int:
        stderr.writelines(progress for _chunk in range(100))
        stderr.write("Out of memory loading the model\n")
        return 1

    system.transcribe = chatty

    worker.work()

    assert store.load_runs(recording_id)[0]["error"] == "Out of memory loading the model"
    # Read line by line, the long log never sits in memory whole.
    assert tracemalloc.get_traced_memory()[1] < 1_000_000


@pytest.mark.usefixtures("speech", "sigterm", "traced")
def test_the_worker_keeps_nothing_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    recording_ids = [recording(START + timedelta(hours=hour)) for hour in range(4)]
    for recording_id in recording_ids:
        put_runs(recording_id, [queued_run(START) for _ in range(25)])
    # Two windows after a warm-up: a leak grows in both, a one-off resize of one of the
    # interpreter's tables in one at most. Each run's log is large, so a worker that holds on
    # to what a run produced grows by megabytes, far above what the interpreter and the test
    # tools allocate on any runner (CI's Python 3.12 measured 14 KB with nothing kept).
    traced_at = {20: 0, 60: 0, 100: 0}
    calls = 0

    def failing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls in traced_at:
            # Empties the interpreter's free lists, whose blocks tracemalloc counts as in use.
            gc.collect()
            traced_at[calls] = tracemalloc.get_traced_memory()[0]
        cast("TextIO", kwargs["stderr"]).write("transcribing turn 1 of 40\n" * 2_000 + "No luck\n")
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(subprocess, "run", failing)
    # Only for speed: the store's own tests cover its writes reaching the disk.
    monkeypatch.setattr(os, "fsync", lambda _fd: None)

    worker.work()

    assert calls == 100
    for recording_id in recording_ids:
        assert [run["error"] for run in store.load_runs(recording_id)] == ["No luck"] * 25
    windows = (traced_at[60] - traced_at[20], traced_at[100] - traced_at[60])
    assert min(windows) < 200_000


@pytest.mark.usefixtures("sigterm")
def test_work_fails_the_run_without_the_speech_environment(
    system: FakeSystem, speech: FakeSpeechPython
) -> None:
    speech.path.unlink()
    recording_id = recording()
    put_runs(recording_id, [queued_run(START + timedelta(seconds=60))])

    worker.work()

    assert system.commands() == []
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "failed", "error": "Speech environment not found; run install.sh"}
    ]
    assert transcribe_status(recording_id) == ("failed", ["transcribe"])


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_fails_transcriptions_a_previous_worker_left_running(system: FakeSystem) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    summarizing = {**QUEUED, "action": "summarize", "status": "running"}
    put_runs(
        first,
        [
            cast("store.Run", {**QUEUED, "status": "succeeded"}),
            cast("store.Run", {**QUEUED, "status": "running"}),
            cast("store.Run", summarizing),
        ],
    )
    put_runs(
        second,
        [
            cast("store.Run", {**QUEUED, "status": "succeeded"}),
            cast("store.Run", {**QUEUED, "status": "running"}),
        ],
    )
    (store.recording_dir(second) / "run-1.log").write_text("transcribing turn 3 of 40\n")
    store.write_status()

    worker.work()

    assert store.load_runs(first) == [
        {**QUEUED, "status": "succeeded"},
        {**QUEUED, "status": "failed", "error": "interrupted"},
        summarizing,
    ]
    # What the killed transcriber wrote stays as the details.
    assert store.load_runs(second) == [
        {**QUEUED, "status": "succeeded"},
        {**QUEUED, "status": "failed", "error": "interrupted", "details": "run-1.log"},
    ]
    assert system.commands() == []
    assert transcribe_status(first) == ("failed", ["transcribe"])


@pytest.mark.usefixtures("speech", "sigterm")
def test_sigterm_fails_the_running_transcription_and_stops_the_worker(system: FakeSystem) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START)])
    put_runs(second, [queued_run(START + timedelta(hours=2))])

    def stopped_by_systemd(_: list[str], stderr: TextIO) -> int:
        stderr.write("transcribing turn 3 of 40\n")
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    system.transcribe = stopped_by_systemd

    worker.work()

    assert store.load_runs(first) == [
        {
            **QUEUED,
            "queued_at": START.isoformat(),
            "status": "failed",
            "error": "interrupted",
            "details": "run-0.log",
        }
    ]
    assert (store.recording_dir(first) / "run-0.log").read_text() == "transcribing turn 3 of 40\n"
    assert store.load_runs(second)[0]["status"] == "queued"
    assert system.transcribed() == [first]
    assert transcribe_status(first) == ("failed", ["transcribe"])
    assert worker_lock_held() is False


@pytest.mark.usefixtures("speech", "sigterm")
def test_a_run_queued_while_the_worker_transcribes_is_taken_by_it(system: FakeSystem) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START)])

    def queue_another(args: list[str], stderr: TextIO) -> int:
        if Path(args[3]).name == first:
            worker.queue(second, START + timedelta(hours=2))
        return write_transcript(args, stderr)

    system.transcribe = queue_another

    worker.work()

    assert system.transcribed() == [first, second]
    assert [run["status"] for run in store.load_runs(second)] == ["succeeded"]


@pytest.mark.usefixtures("speech", "sigterm")
def test_a_run_queued_after_the_worker_found_the_queue_empty_starts_another(
    system: FakeSystem, xdg_dirs: Path
) -> None:
    worker.work()
    recording_id = recording()

    worker.queue(recording_id, START)

    assert system.commands() == [start_command(xdg_dirs / "data", xdg_dirs / "config")]


@pytest.mark.usefixtures("speech", "sigterm")
def test_the_worker_gives_up_its_lock_inside_the_store_lock(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    store_held_at_release: list[bool] = []
    flock = fcntl.flock

    def spy(file: TextIO, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            with (store.data_dir() / "store.lock").open("a") as other:
                try:
                    flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    store_held_at_release.append(True)
                else:
                    store_held_at_release.append(False)
        flock(file, operation)

    monkeypatch.setattr(fcntl, "flock", spy)

    worker.work()

    assert system.transcribed() == [recording_id]
    assert store_held_at_release == [True]
