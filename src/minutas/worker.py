"""Runs queued Transcribe runs one at a time, oldest first, in a systemd user unit.

The worker holds DATA/worker.lock while it runs and gives it up, inside the store lock, only
once it finds no queued run. A run queued while the lock is held is sure to be seen; one queued
after has to start a new worker.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import IO, TypedDict, cast

from minutas import store, transcriber

UNIT = "minutas-transcribe.service"
ACTION = "transcribe"
WORDS = "words.txt"
INTERRUPTED = "interrupted"
# Sent to every process of the unit: it ends the transcriber, which takes the signal's default
# action, and leaves the worker, which handles it and goes on with the queue.
CANCEL_SIGNAL = signal.SIGUSR1
STOP_TRANSCRIBER = ["systemctl", "--user", "kill", f"--signal={CANCEL_SIGNAL.name}", UNIT]
SPEECH_MISSING = "Speech environment not found; run install.sh"
OUT_OF_MEMORY = "Transcription ran out of its memory limit"
# A runaway transcription is killed at its own limit instead of swapping the desktop to death,
# and its run fails with OUT_OF_MEMORY. No MemoryHigh: below MemoryMax it throttles the process
# so it never reaches the kill and the queue stalls behind it.
MEMORY_LIMITS = ("MemoryMax=6G", "MemorySwapMax=512M")
# What the worker's unit must see as its caller does: the data and the glossary.
FORWARDED_ENVIRONMENT = ("XDG_DATA_HOME", "XDG_CONFIG_HOME")
# A worker that found the queue empty is gone, and one just started holds its lock, within a
# fraction of a second.
START_TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.05


class WorkerError(Exception):
    pass


class Interrupted(Exception):  # noqa: N818 - what happened to the run, not a fault in the code
    pass


class Outcome(TypedDict):
    status: str
    error: str | None
    details: str | None
    output: str | None


def failed(error: str, details: str | None = None) -> Outcome:
    return Outcome(status="failed", error=error, details=details, output=None)


def ended(run: store.Run, outcome: Outcome) -> store.Run:
    return store.Run(action=run["action"], queued_at=run["queued_at"], **outcome)


def speech_python() -> Path:
    return Path.home() / ".local" / "share" / "minutas" / "speech" / "bin" / "python"


def glossary() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "minutas" / "glossary.txt"


def word_list(recording_id: str) -> list[str]:
    """The phrases the transcriber boosts: the meeting's attendees, then the glossary."""
    attendees = store.load_recording(recording_id).get("attendees", [])
    listed = glossary().read_text(encoding="utf-8").splitlines() if glossary().is_file() else []
    return [*attendees, *listed]


def open_worker_lock() -> IO[str]:
    store.data_dir().mkdir(parents=True, exist_ok=True)
    return (store.data_dir() / "worker.lock").open("a")


def worker_holds_lock() -> bool:
    with open_worker_lock() as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def start_command() -> list[str]:
    return [
        *("systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet"),
        # Idle CPU and disk priority: a call recorded meanwhile keeps the machine.
        *("--property=CPUSchedulingPolicy=idle", "--property=IOSchedulingClass=idle"),
        *(f"--property={limit}" for limit in MEMORY_LIMITS),
        # The OOM killer picks the transcriber; by default systemd would then stop the worker too,
        # failing the run as interrupted and leaving the runs queued behind it waiting.
        "--property=OOMPolicy=continue",
        *(
            f"--setenv={name}={os.environ[name]}"
            for name in FORWARDED_ENVIRONMENT
            if name in os.environ
        ),
        *("--", sys.executable, "-m", "minutas", "worker"),
    ]


def ensure_worker() -> None:
    """Returns once a worker that has yet to look at the queue again is running."""
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not worker_holds_lock():
        # The commands are this module's own argument lists, never a shell string.
        result = subprocess.run(start_command(), capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode == 0:
            return
        # The unit is still there: its worker found the queue empty and is exiting, or it has only
        # just started and has yet to take its lock.
        if time.monotonic() >= deadline:
            reason = result.stderr.strip() or f"systemd-run exited with status {result.returncode}"
            raise WorkerError(reason)
        time.sleep(POLL_SECONDS)


def set_outcome(recording_id: str, index: int, outcome: Outcome) -> None:
    with store.lock():
        runs = store.load_runs(recording_id)
        # Cancel got there first and stopped the transcriber: how that exited is not the outcome.
        if runs[index]["status"] != "cancelled":
            runs[index] = ended(runs[index], outcome)
            store.save_runs(recording_id, runs)
    store.write_status()


def queue(recording_id: str, queued: datetime) -> None:
    """Queues a Transcribe run and makes sure a worker takes it; fails the run if none starts."""
    with store.lock():
        runs = store.load_runs(recording_id)
        runs.append(
            store.Run(
                action=ACTION,
                status="queued",
                queued_at=queued.isoformat(timespec="seconds"),
                error=None,
                details=None,
                output=None,
            )
        )
        store.save_runs(recording_id, runs)
    store.write_status()
    try:
        ensure_worker()
    except WorkerError as error:
        set_outcome(recording_id, len(runs) - 1, failed(str(error)))
        raise


def runs_by_recording() -> dict[str, list[store.Run]]:
    # By runs.json, which a recording gets only after its recording.json.
    return {
        path.parent.name: store.load_runs(path.parent.name)
        for path in store.recordings_dir().glob("*/runs.json")
    }


def fail_interrupted() -> None:
    """Fails the transcriptions a worker that crashed, or was killed, left running."""
    with store.lock():
        for recording_id, runs in runs_by_recording().items():
            stale = [
                i
                for i, run in enumerate(runs)
                if run["action"] == ACTION and run["status"] == "running"
            ]
            for index in stale:
                runs[index] = ended(runs[index], interrupted(recording_id, index))
            if stale:
                store.save_runs(recording_id, runs)
    store.write_status()


def claim_next(worker_lock: IO[str]) -> tuple[str, int] | None:
    """Marks the oldest queued Transcribe run running; with none left, gives up the worker lock."""
    with store.lock():
        logs = runs_by_recording()
        queued = [
            (datetime.fromisoformat(run["queued_at"]), recording_id, index)
            for recording_id, runs in logs.items()
            for index, run in enumerate(runs)
            if run["action"] == ACTION and run["status"] == "queued"
        ]
        if not queued:
            fcntl.flock(worker_lock, fcntl.LOCK_UN)
            return None
        _, recording_id, index = min(queued)
        logs[recording_id][index]["status"] = "running"
        store.save_runs(recording_id, logs[recording_id])
    store.write_status()
    return recording_id, index


def interrupted(recording_id: str, index: int) -> Outcome:
    return failed(INTERRUPTED, store.written_log(recording_id, index))


def cancel(recording_id: str, index: int) -> None:
    """Cancels a Transcribe run that is queued or running; one that has ended keeps its outcome."""
    with store.lock():
        runs = store.load_runs(recording_id)
        status = runs[index]["status"]
        if status not in {"queued", "running"}:
            return
        cancelled = Outcome(
            status="cancelled",
            error=None,
            details=store.written_log(recording_id, index),
            output=None,
        )
        runs[index] = ended(runs[index], cancelled)
        store.save_runs(recording_id, runs)
        if status == "running":
            # Still inside the lock, where the worker cannot have moved on to another run. A unit
            # that is gone has nothing to stop. A transcriber in the moment between the claim and
            # its start is missed: it runs to the end, and set_outcome keeps the cancel.
            subprocess.run(STOP_TRANSCRIBER, capture_output=True, check=False)  # noqa: S603
    store.write_status()


def read_progress(line: str) -> store.Progress | None:
    """The phase and whole percent done a line of the transcriber's stdout says; None for any
    other line, such as one a library printed."""
    try:
        said = json.loads(line)
    except ValueError:
        return None
    if not isinstance(said, dict):
        return None
    phase, done, total = said.get("phase"), said.get("done"), said.get("total")
    # Not bool, which is an int too.
    counted = type(done) is int and type(total) is int and 0 <= done <= total and total > 0
    if phase not in transcriber.PHASES or not counted:
        return None
    return store.Progress(phase=phase, percent=done * 100 // total)


def set_progress(recording_id: str, index: int, progress: store.Progress) -> None:
    with store.lock():
        runs = store.load_runs(recording_id)
        # Cancel may have ended the run while the line was on its way.
        if runs[index]["status"] == "running":
            runs[index]["progress"] = progress
            store.save_runs(recording_id, runs)
    store.write_status()


def follow(recording_id: str, index: int, printed: Iterable[str]) -> None:
    """Records on the run how far the transcriber says it has got, as it says it: only a new
    phase or a new whole percent, so a long meeting rewrites the run no more than a short one."""
    shown = None
    for line in printed:
        progress = read_progress(line)
        if progress is not None and progress != shown:
            set_progress(recording_id, index, progress)
            shown = progress


def transcribe(recording_id: str, index: int) -> Outcome:
    python = speech_python()
    if not python.is_file():
        return failed(SPEECH_MISSING)
    folder = store.recording_dir(recording_id)
    # A file, not a module: the speech environment has no minutas, and the folder that would find
    # it is, once installed, the app's site-packages, whose packages would shadow the speech
    # environment's own with builds for Ubuntu's Python. -P keeps the file's own folder off the
    # path, where minutas.calendar would take the standard library's place.
    command = [str(python), "-P", transcriber.__file__, str(folder)]
    words = word_list(recording_id)
    if words:
        (folder / WORDS).write_text("".join(f"{word}\n" for word in words), encoding="utf-8")
        command += ["--words", str(folder / WORDS)]
    log = store.run_log(recording_id, index)
    with (
        log.open("w") as stderr,
        subprocess.Popen(  # noqa: S603 - this module's own argument list
            command,
            stdin=subprocess.DEVNULL,
            # Its progress; stderr is the log, whose last line is the reason it failed.
            stdout=subprocess.PIPE,
            stderr=stderr,
            # Whatever a library prints there is read without failing the run.
            encoding="utf-8",
            errors="replace",
        ) as process,
    ):
        follow(recording_id, index, cast("IO[str]", process.stdout))
        returncode = process.wait()
    if returncode == 0:
        store.write_transcript(recording_id)
        return Outcome(status="succeeded", error=None, details=log.name, output=store.TRANSCRIPT)
    # A killed transcriber gave no reason, its last line is only what it was logging; the OOM
    # kill at MemoryMax is a SIGKILL.
    if returncode == -signal.SIGKILL:
        return failed(OUT_OF_MEMORY, log.name)
    if returncode < 0:
        return failed(f"The transcriber was killed by {signal.Signals(-returncode).name}", log.name)
    reason = f"The transcriber exited with status {returncode}"
    # Line by line: a long log never sits in memory whole.
    with log.open(errors="replace") as lines:
        for line in lines:
            reason = line.strip() or reason
    return failed(reason, log.name)


def interrupt(_signal: int, _frame: FrameType | None) -> None:
    raise Interrupted


def outlive(_signal: int, _frame: FrameType | None) -> None:
    """Handled, not ignored: a signal the worker ignored its transcriber would ignore too."""


def work() -> None:
    """Transcribes queued runs until none is left.

    systemd stops the unit with SIGTERM, at logout or shutdown: the run it was on fails as
    interrupted, as one left running by a crash does at the next start.
    """
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(CANCEL_SIGNAL, outlive)
    with open_worker_lock() as worker_lock:
        fcntl.flock(worker_lock, fcntl.LOCK_EX)
        fail_interrupted()
        while (claimed := claim_next(worker_lock)) is not None:
            recording_id, index = claimed
            try:
                outcome = transcribe(recording_id, index)
            except Interrupted:
                set_outcome(recording_id, index, interrupted(recording_id, index))
                return
            set_outcome(recording_id, index, outcome)
