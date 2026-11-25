import fcntl
import gc
import json
import os
import signal
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Self, TextIO, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

import minutas
from minutas import settings, store, transcriber, worker

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
TRANSCRIBER = str(Path(minutas.__file__).with_name("transcriber.py"))
KILL = ["systemctl", "--user", "kill", "--signal=SIGUSR1", "minutas-transcribe.service"]
JOINER = [sys.executable, "-m", "minutas.join"]


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
    """As the transcriber does: what was said, under the speakers' keys."""
    said = [{"start": 1.0, "end": 2.5, "speaker": "Speaker 1", "text": "Hola."}]
    (Path(args[3]) / "transcript.json").write_text(json.dumps(said))
    return 0


# A transcriber's stand-in: given its arguments and stderr file, it returns its exit status, or
# yields the lines it prints on stdout and then returns it.
Transcriber = Callable[[list[str], TextIO], int | Generator[str, None, int]]


def line(phase: str, done: int, total: int) -> str:
    """A line in which the transcriber says how far it has got."""
    return json.dumps({"phase": phase, "done": done, "total": total}) + "\n"


class FakeTranscriber:
    """Stands in for the transcriber's subprocess.Popen: it runs as its stdout is read."""

    def __init__(self, run: Transcriber, args: list[str], stderr: TextIO) -> None:
        self.returncode: int | None = None
        self.stdout = self.printed(run, args, stderr)

    def printed(self, run: Transcriber, args: list[str], stderr: TextIO) -> Iterator[str]:
        said = run(args, stderr)
        if isinstance(said, int):
            self.returncode = said
        else:
            self.returncode = yield from said

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def wait(self) -> int | None:
        return self.returncode


class FakeSystem:
    """Stands in for subprocess.run and subprocess.Popen: systemd-run answers from start_codes,
    one per call, then keeps giving the last; systemctl answers kill_code; the transcriber is
    the transcribe callable, and the joiner the join one, given its stderr file."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.start_codes = [0]
        self.start_stderr = ""
        self.kill_code = 0
        self.killed_inside_the_store_lock: list[bool] = []
        self.transcribe: Transcriber = write_transcript
        self.join: Callable[[TextIO], int] = lambda _stderr: 0

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args == JOINER:
            return subprocess.CompletedProcess(
                args, self.join(cast("TextIO", kwargs["stderr"])), "", ""
            )
        if args[0] == "systemctl":
            self.killed_inside_the_store_lock.append(store_lock_held())
            return subprocess.CompletedProcess(args, self.kill_code, "", "")
        assert args[0] == "systemd-run"
        code = self.start_codes.pop(0) if len(self.start_codes) > 1 else self.start_codes[0]
        return subprocess.CompletedProcess(args, code, "", self.start_stderr)

    def popen(self, args: list[str], **kwargs: object) -> FakeTranscriber:
        self.calls.append((args, kwargs))
        return FakeTranscriber(self.transcribe, args, cast("TextIO", kwargs["stderr"]))

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]

    def transcribed(self) -> list[str]:
        return [Path(args[3]).name for args in self.commands() if args[1:3] == ["-P", TRANSCRIBER]]

    def kills(self) -> list[list[str]]:
        return [args for args in self.commands() if args[0] == "systemctl"]


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
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    return fake


@pytest.fixture
def speech(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSpeechPython:
    fake = FakeSpeechPython(tmp_path / "home" / ".local/share/minutas/speech/bin/python")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fake.install()
    return fake


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeTime:
    fake = FakeTime()
    monkeypatch.setattr(worker, "time", fake)
    return fake


@pytest.fixture
def sigterm() -> Iterator[None]:
    """work() installs its own signal handlers; the test process gets its own back."""
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGUSR1)}
    yield
    for number, handler in handlers.items():
        signal.signal(number, handler)


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


def lock_held(name: str) -> bool:
    with (store.data_dir() / name).open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def worker_lock_held() -> bool:
    return lock_held("worker.lock")


def store_lock_held() -> bool:
    return lock_held("store.lock")


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
            "-P",
            TRANSCRIBER,
            str(folder),
        ]
    ]
    kwargs = system.calls[0][1]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert seen == [("running", [])]
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "succeeded", "details": "run-0.log", "output": "transcript.md"}
    ]
    assert (folder / "run-0.log").read_text() == "loading the model\n"
    assert store.load_recording(recording_id).get("speakers") == {"Speaker 1": "Speaker 1"}
    assert (folder / "transcript.md").read_text() == "[00:00:01] **Speaker 1:** Hola.\n"
    assert transcribe_status(recording_id) == ("succeeded", [])
    assert worker_lock_held() is False


# Stands in for the transcriber: an empty transcript, unless the speech Python reached a module
# installed beside it, which exits with the reason.
PROBE = """\
import calendar
import sys
from pathlib import Path

try:
    import app_only
except ModuleNotFoundError:
    Path(sys.argv[1], "transcript.json").write_text("[]")
"""


@pytest.mark.usefixtures("sigterm")
def test_the_speech_python_reaches_nothing_installed_beside_the_transcriber(
    speech: FakeSpeechPython, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Installed, minutas sits in the app's site-packages, among packages built for Ubuntu's Python.
    site_packages = tmp_path / "app/lib/python3.12/site-packages"
    package = site_packages / "minutas"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "transcriber.py").write_text(PROBE)
    (package / "calendar.py").write_text(
        "raise SystemExit(\"minutas' calendar took the standard library's place\")\n"
    )
    (site_packages / "app_only.py").write_text(
        'raise SystemExit("The speech Python imported the app\'s packages")\n'
    )
    monkeypatch.setattr(minutas, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(transcriber, "__file__", str(package / "transcriber.py"))
    speech.path.unlink()
    speech.path.symlink_to(sys.executable)
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])

    worker.work()

    runs = store.load_runs(recording_id)
    assert [(run["status"], run["error"]) for run in runs] == [("succeeded", None)]


@pytest.mark.usefixtures("speech", "sigterm", "system")
def test_work_writes_the_transcript_under_the_names_the_speakers_already_have() -> None:
    recording_id = recording()
    # An earlier transcription found the speaker, and a person named it.
    write_transcript(
        ["python", "-P", TRANSCRIBER, str(store.recording_dir(recording_id))], sys.stderr
    )
    store.write_transcript(recording_id)
    store.set_speaker_name(recording_id, "Speaker 1", "Ana Díaz")
    put_runs(recording_id, [queued_run(START)])

    worker.work()

    transcript = store.recording_dir(recording_id) / "transcript.md"
    assert transcript.read_text(encoding="utf-8") == "[00:00:01] **Ana Díaz:** Hola.\n"
    assert store.load_recording(recording_id).get("speakers") == {"Speaker 1": "Ana Díaz"}


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_the_glossary_as_the_word_list(system: FakeSystem, xdg_dirs: Path) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    glossary = xdg_dirs / "config" / "minutas" / "glossary.txt"
    glossary.parent.mkdir(parents=True)
    glossary.write_text("deploy\nPR\n")

    worker.work()

    words = store.recording_dir(recording_id) / "words.txt"
    assert system.commands()[0][4:] == ["--words", str(words)]
    assert words.read_text() == "deploy\nPR\n"


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_the_attendees_and_the_glossary_as_one_word_list(
    system: FakeSystem, xdg_dirs: Path
) -> None:
    recording_id = recording()
    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz", "bob.ruiz"])
    put_runs(recording_id, [queued_run(START)])
    glossary = xdg_dirs / "config" / "minutas" / "glossary.txt"
    glossary.parent.mkdir(parents=True)
    glossary.write_text("deploy\nPR\n")

    worker.work()

    words = store.recording_dir(recording_id) / "words.txt"
    assert system.commands()[0][4:] == ["--words", str(words)]
    assert words.read_text() == "Ana Díaz\nbob.ruiz\ndeploy\nPR\n"


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_the_attendees_alone_without_a_glossary(system: FakeSystem) -> None:
    recording_id = recording()
    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz"])
    put_runs(recording_id, [queued_run(START)])

    worker.work()

    words = store.recording_dir(recording_id) / "words.txt"
    assert system.commands()[0][4:] == ["--words", str(words)]
    assert words.read_text() == "Ana Díaz\n"


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

    words = store.recording_dir(recording_id) / "words.txt"
    assert system.commands()[0][4:] == ["--words", str(words)]
    assert words.read_text() == "deploy\n"


def write_vocabulary(name: str, words: str) -> None:
    worker.vocabulary_file(name).parent.mkdir(parents=True, exist_ok=True)
    worker.vocabulary_file(name).write_text(words)


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_the_words_of_every_vocabulary_used_each_once_in_a_stable_order(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz", "Atlas"])
    put_runs(recording_id, [queued_run(START)])
    write_vocabulary(settings.GLOSSARY, "deploy\nPR\n")
    write_vocabulary("Zeta", "Zeta\n")
    write_vocabulary("Atlas", "Atlas\n  PR \n\n")
    write_vocabulary("Off", "Off\n")
    # In the order switched on, with one deleted since.
    settings.set_vocabularies(["Zeta", "Gone", settings.GLOSSARY, "Atlas"])

    worker.work()

    words = store.recording_dir(recording_id) / "words.txt"
    assert system.commands()[0][4:] == ["--words", str(words)]
    assert words.read_text() == "Ana Díaz\nAtlas\ndeploy\nPR\nZeta\n"


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_fails_each_run_at_once_naming_a_vocabulary_it_cant_read_and_goes_on(
    system: FakeSystem,
) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START)])
    put_runs(second, [queued_run(START + timedelta(hours=1))])
    write_vocabulary("Atlas", "")
    worker.vocabulary_file("Atlas").write_bytes("Díaz\n".encode("latin-1"))
    settings.set_vocabularies([settings.GLOSSARY, "Atlas"])

    worker.work()

    file = worker.vocabulary_file("Atlas")
    said = f"Can't read the vocabulary {file}: fix it in Settings, or switch it off"
    assert system.commands() == []
    for recording_id in (first, second):
        runs = store.load_runs(recording_id)
        assert [(run["status"], run["error"]) for run in runs] == [("failed", said)]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_passes_no_word_list_with_every_vocabulary_switched_off(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    write_vocabulary(settings.GLOSSARY, "deploy\n")
    write_vocabulary("Atlas", "Atlas\n")
    settings.set_vocabularies([])

    worker.work()

    assert system.commands()[0][4:] == []
    assert not (store.recording_dir(recording_id) / "words.txt").exists()


def test_vocabularies_are_the_glossary_and_the_visible_text_files_of_the_vocabulary_folder(
    xdg_dirs: Path,
) -> None:
    folder = xdg_dirs / "config" / "minutas" / "vocabulary"
    assert worker.vocabulary_file(settings.GLOSSARY) == worker.glossary()
    assert worker.vocabulary_file("Atlas") == folder / "Atlas.txt"
    assert worker.vocabulary_names() == []

    folder.mkdir(parents=True)
    for name in ("Team words.txt", "atlas.txt", "Zeta.txt", "notes.md", ".hidden.txt"):
        (folder / name).write_text("")
    (folder / "folder.txt").mkdir()

    assert worker.vocabulary_names() == ["atlas", "Team words", "Zeta"]


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

    def failing(_: list[str], stderr: TextIO) -> int:
        nonlocal calls
        calls += 1
        if calls in traced_at:
            # Empties the interpreter's free lists, whose blocks tracemalloc counts as in use.
            gc.collect()
            traced_at[calls] = tracemalloc.get_traced_memory()[0]
        stderr.write("transcribing turn 1 of 40\n" * 2_000 + "No luck\n")
        return 1

    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda args, **kwargs: FakeTranscriber(failing, args, kwargs["stderr"]),
    )
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


@pytest.mark.usefixtures("speech", "sigterm")
def test_cancel_takes_a_queued_run_out_of_the_queue(system: FakeSystem) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START + timedelta(seconds=60))])
    put_runs(second, [queued_run(START + timedelta(hours=2))])
    store.write_status()

    worker.cancel(first, 0)

    assert store.load_runs(first) == [{**QUEUED, "status": "cancelled"}]
    assert transcribe_status(first) == ("cancelled", [])
    # Nothing is running for it: nothing is stopped.
    assert system.kills() == []

    worker.work()

    assert system.transcribed() == [second]
    assert store.load_runs(first) == [{**QUEUED, "status": "cancelled"}]


@pytest.mark.usefixtures("speech", "sigterm")
def test_cancel_stops_the_running_transcription_and_the_worker_goes_on(system: FakeSystem) -> None:
    first, second = recording(), recording(START + timedelta(hours=1))
    put_runs(first, [queued_run(START + timedelta(seconds=60))])
    put_runs(second, [queued_run(START + timedelta(hours=2))])

    def cancelled_midway(args: list[str], stderr: TextIO) -> int:
        if Path(args[3]).name == second:
            return write_transcript(args, stderr)
        stderr.write("transcribing turn 3 of 40\n")
        stderr.flush()
        worker.cancel(first, 0)
        # What the signal does to the transcriber.
        return -signal.SIGUSR1

    system.transcribe = cancelled_midway

    worker.work()

    assert system.kills() == [KILL]
    # Sent inside the store lock, where the worker cannot have moved on to another run.
    assert system.killed_inside_the_store_lock == [True]
    # Cancelled, with what the transcriber had written; not failed as killed, nor interrupted.
    assert store.load_runs(first) == [{**QUEUED, "status": "cancelled", "details": "run-0.log"}]
    assert transcribe_status(first) == ("cancelled", [])
    assert system.transcribed() == [first, second]
    assert [run["status"] for run in store.load_runs(second)] == ["succeeded"]


@pytest.mark.usefixtures("speech", "sigterm")
def test_the_worker_outlives_the_signal_that_stops_its_transcriber(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    handled: list[bool] = []

    def signalled(args: list[str], stderr: TextIO) -> int:
        # A handler, which the transcriber does not inherit: an ignored signal it would.
        handled.append(callable(signal.getsignal(signal.SIGUSR1)))
        if handled == [True]:
            os.kill(os.getpid(), signal.SIGUSR1)
        return write_transcript(args, stderr)

    system.transcribe = signalled

    worker.work()

    assert handled == [True]
    # No cancel was recorded, so the run's own outcome stands.
    assert [run["status"] for run in store.load_runs(recording_id)] == ["succeeded"]


@pytest.mark.usefixtures("speech", "sigterm")
def test_a_run_cancelled_after_its_worker_died_stays_cancelled(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [cast("store.Run", {**QUEUED, "status": "running"})])
    # No unit is left to signal.
    system.kill_code = 1

    worker.cancel(recording_id, 0)

    assert system.kills() == [KILL]
    assert store.load_runs(recording_id) == [{**QUEUED, "status": "cancelled"}]

    worker.work()

    # The next worker fails what was left running as interrupted; this was cancelled.
    assert store.load_runs(recording_id) == [{**QUEUED, "status": "cancelled"}]
    assert transcribe_status(recording_id) == ("cancelled", [])


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_cancel_leaves_a_run_that_has_ended(system: FakeSystem, status: str) -> None:
    recording_id = recording()
    ended = cast("store.Run", {**QUEUED, "status": status, "output": "transcript.md"})
    put_runs(recording_id, [ended])

    worker.cancel(recording_id, 0)

    assert store.load_runs(recording_id) == [ended]
    assert system.kills() == []


def transcribe_progress(recording_id: str) -> object:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return next(entry for entry in status["recent"] if entry["id"] == recording_id)["progress"]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_records_how_far_the_transcriber_has_got_as_it_says_it(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    saved: list[object] = []
    save_runs = store.save_runs

    def spy(saved_id: str, runs: list[store.Run]) -> None:
        saved.append(runs[0].get("progress"))
        save_runs(saved_id, runs)

    monkeypatch.setattr(store, "save_runs", spy)
    seen: list[object] = []
    mic = "finding speech on the microphone"

    def transcriber(args: list[str], stderr: TextIO) -> Generator[str, None, int]:
        # What a library prints is no progress.
        yield "Loading the model\n"
        yield line(mic, 0, 200)
        seen.append(transcribe_progress(recording_id))
        # Still 0%: nothing is written again.
        yield line(mic, 1, 200)
        yield line(mic, 2, 200)
        seen.append(transcribe_progress(recording_id))
        yield line("transcribing", 3, 3)
        seen.append(transcribe_progress(recording_id))
        return write_transcript(args, stderr)

    system.transcribe = transcriber

    worker.work()

    assert seen == [
        {"phase": mic, "percent": 0},
        {"phase": mic, "percent": 1},
        {"phase": "transcribing", "percent": 100},
    ]
    # Claimed, each new phase or percent, and ended without it.
    assert saved == [
        None,
        {"phase": mic, "percent": 0},
        {"phase": mic, "percent": 1},
        {"phase": "transcribing", "percent": 100},
        None,
    ]
    assert store.load_runs(recording_id) == [
        {
            **QUEUED,
            "queued_at": START.isoformat(),
            "status": "succeeded",
            "details": "run-0.log",
            "output": "transcript.md",
        }
    ]
    assert transcribe_progress(recording_id) is None


@pytest.mark.usefixtures("speech", "sigterm")
def test_progress_said_after_a_cancel_leaves_the_run_cancelled(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])

    def cancelled_midway(_: list[str], stderr: TextIO) -> Generator[str, None, int]:
        yield line("transcribing", 1, 4)
        stderr.write("transcribing turn 1 of 4\n")
        stderr.flush()
        worker.cancel(recording_id, 0)
        # Already on its way when the signal came.
        yield line("transcribing", 2, 4)
        return -signal.SIGUSR1

    system.transcribe = cancelled_midway

    worker.work()

    assert store.load_runs(recording_id) == [
        {**QUEUED, "queued_at": START.isoformat(), "status": "cancelled", "details": "run-0.log"}
    ]
    assert transcribe_progress(recording_id) is None


def test_progress_said_after_a_cancel_rewrites_nothing() -> None:
    recording_id = recording()
    cancelled = cast("store.Run", {**QUEUED, "status": "cancelled"})
    put_runs(recording_id, [cancelled])

    worker.set_progress(recording_id, 0, store.Progress(phase="transcribing", percent=50))

    assert store.load_runs(recording_id) == [cancelled]
    assert not (store.data_dir() / "status.json").exists()


@pytest.mark.usefixtures("speech", "sigterm")
def test_sigterm_while_the_transcriber_says_how_far_it_has_got_fails_the_run(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])

    def stopped_by_systemd(_: list[str], _stderr: TextIO) -> Generator[str, None, int]:
        yield line("identifying speakers", 1, 4)
        os.kill(os.getpid(), signal.SIGTERM)
        yield line("identifying speakers", 2, 4)
        return 0

    system.transcribe = stopped_by_systemd

    worker.work()

    assert store.load_runs(recording_id) == [
        {
            **QUEUED,
            "queued_at": START.isoformat(),
            "status": "failed",
            "error": "interrupted",
            "details": "run-0.log",
        }
    ]
    assert transcribe_progress(recording_id) is None


# Stands in for the transcriber: a library's output that is no UTF-8, its progress, and an empty
# transcript.
PRINTING = """\
import sys
from pathlib import Path

sys.stdout.buffer.write(b"\\xff\\xfe from a library\\n")
sys.stdout.buffer.write(b'{"phase": "transcribing", "done": 1, "total": 2}\\n')
sys.stdout.flush()
Path(sys.argv[1], "transcript.json").write_text("[]")
"""


@pytest.mark.usefixtures("sigterm")
def test_the_transcribers_progress_reaches_its_run_whatever_else_it_prints(
    speech: FakeSpeechPython, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "transcriber.py"
    script.write_text(PRINTING)
    monkeypatch.setattr(transcriber, "__file__", str(script))
    speech.path.unlink()
    speech.path.symlink_to(sys.executable)
    recording_id = recording()
    put_runs(recording_id, [queued_run(START)])
    saved: list[object] = []
    save_runs = store.save_runs

    def spy(saved_id: str, runs: list[store.Run]) -> None:
        saved.append(runs[0].get("progress"))
        save_runs(saved_id, runs)

    monkeypatch.setattr(store, "save_runs", spy)

    worker.work()

    assert saved == [None, {"phase": "transcribing", "percent": 50}, None]
    assert [run["status"] for run in store.load_runs(recording_id)] == ["succeeded"]


@pytest.mark.parametrize(
    "said",
    [
        "Loading the model\n",
        "\n",
        "[1, 2]\n",
        '{"phase": "dreaming", "done": 1, "total": 2}\n',
        '{"phase": "transcribing", "done": 3, "total": 2}\n',
        '{"phase": "transcribing", "done": -1, "total": 2}\n',
        '{"phase": "transcribing", "done": 0, "total": 0}\n',
        '{"phase": "transcribing", "done": 1.5, "total": 2}\n',
        '{"phase": "transcribing", "done": true, "total": 2}\n',
        '{"phase": "transcribing", "done": 1}\n',
        '{"phase": "transcribing", "done": 1, "total": ' + "9" * 5000 + "}\n",
    ],
)
def test_a_line_that_is_not_progress_says_none(said: str) -> None:
    assert worker.read_progress(said) is None


@given(
    st.sampled_from(transcriber.PHASES),
    st.integers(min_value=1, max_value=10**6).flatmap(
        lambda total: st.tuples(st.integers(min_value=0, max_value=total), st.just(total))
    ),
)
def test_progress_is_the_phase_and_the_whole_percent_done(
    phase: str, counts: tuple[int, int]
) -> None:
    done, total = counts

    progress = worker.read_progress(line(phase, done, total))

    assert progress == {"phase": phase, "percent": done * 100 // total}
    assert progress is not None
    assert 0 <= progress["percent"] <= 100
    assert (progress["percent"] == 100) is (done == total)


@given(st.text())
def test_any_line_reads_as_progress_or_nothing(said: str) -> None:
    progress = worker.read_progress(said)

    assert progress is None or progress["phase"] in transcriber.PHASES


def continued_recording() -> str:
    """A stopped recording whose tracks each have a part recorded after Continue to join."""
    recording_id = recording()
    for name in store.PARTS:
        (store.recording_dir(recording_id) / name).write_bytes(b"OggS")
    put_runs(recording_id, [queued_run(START + timedelta(seconds=60))])
    return recording_id


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_joins_what_was_recorded_after_continue_before_it_transcribes(
    system: FakeSystem,
) -> None:
    recording_id = continued_recording()
    folder = store.recording_dir(recording_id)
    heard: list[list[str]] = []

    def joiner(stderr: TextIO) -> int:
        stderr.write("joining\n")
        stderr.flush()
        for name in store.PARTS:
            (folder / name).unlink()
        return 0

    def transcriber(args: list[str], stderr: TextIO) -> int:
        heard.append(sorted(path.name for path in folder.glob("*.opus")))
        stderr.write("loading the model\n")
        return write_transcript(args, stderr)

    system.join, system.transcribe = joiner, transcriber

    worker.work()

    assert system.commands()[0] == JOINER
    assert system.transcribed() == [recording_id]
    kwargs = system.calls[0][1]
    assert (kwargs.get("cwd"), kwargs["stdin"]) == (folder, subprocess.DEVNULL)
    # The transcriber heard the joined tracks.
    assert heard == [["call.opus", "mic.opus"]]
    assert (folder / "run-0.log").read_text() == "joining\nloading the model\n"
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "succeeded", "details": "run-0.log", "output": "transcript.md"}
    ]


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        (
            "Could not demultiplex stream.\n\nCould not join call.opus. Both are kept.\n",
            "Could not join call.opus. Both are kept.",
        ),
        ("", "The joiner exited with status 1"),
    ],
)
@pytest.mark.usefixtures("speech", "sigterm")
def test_work_fails_the_run_when_the_join_fails_and_transcribes_nothing(
    system: FakeSystem, said: str, reason: str
) -> None:
    recording_id = continued_recording()

    def joiner(stderr: TextIO) -> int:
        stderr.write(said)
        stderr.flush()
        return 1

    system.join = joiner

    worker.work()

    assert system.commands() == [JOINER]
    assert store.load_runs(recording_id) == [
        {**QUEUED, "status": "failed", "error": reason, "details": "run-0.log"}
    ]
    assert transcribe_status(recording_id) == ("failed", ["transcribe"])
    assert store.audio_kept(recording_id) == [*store.TRACKS, *store.PARTS]


@pytest.mark.usefixtures("speech", "sigterm")
def test_work_runs_no_joiner_for_parts_that_hold_no_audio(system: FakeSystem) -> None:
    recording_id = recording()
    for name in store.PARTS:
        (store.recording_dir(recording_id) / name).touch()
    put_runs(recording_id, [queued_run(START + timedelta(seconds=60))])

    worker.work()

    assert [command[1:3] for command in system.commands()] == [["-P", TRANSCRIBER]]
