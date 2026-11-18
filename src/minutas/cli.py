"""The `minutas` command."""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

from minutas import recorder, store, worker


def now() -> datetime:
    return datetime.now(UTC).astimezone()


def fail(reason: str) -> int:
    sys.stderr.write(f"{reason}\n")
    return 1


def record_start(_: argparse.Namespace) -> int:
    if recorder.is_running():
        return fail("Already recording.")
    store.close_abandoned()
    try:
        recording = store.create_recording(now())
    except FileExistsError:
        # Recording ids are to the second; the existing recording stays as it is.
        return fail("A recording started this same second; try again.")
    on_exit = [sys.executable, "-m", "minutas", "record", "stopped", recording["id"]]
    try:
        recorder.start(store.recording_dir(recording["id"]), on_exit=on_exit)
    except recorder.RecorderError as error:
        store.discard_if_silent(recording["id"])
        store.write_status()
        return fail(f"Could not start the recorder: {error}")
    store.write_status()
    sys.stdout.write(
        json.dumps({"id": recording["id"], "started_at": recording["started_at"]}) + "\n"
    )
    return 0


def record_stop(_: argparse.Namespace) -> int:
    if not recorder.is_running():
        store.close_abandoned()
        store.write_status()
        return fail("Nothing is recording.")
    try:
        recorder.stop()
    except recorder.RecorderError as error:
        return fail(f"Could not stop the recorder: {error}")
    return 0


def queue_transcription(recording_id: str) -> int:
    try:
        worker.queue(recording_id, now())
    except worker.WorkerError as error:
        return fail(f"Could not start transcribing: {error}")
    return 0


def record_stopped(args: argparse.Namespace) -> int:
    # systemd reports success for a clean exit, which closes both files: after Stop, or when the
    # session ends. Anything else means the recorder died: what it wrote is kept.
    incomplete = os.environ.get("SERVICE_RESULT") != "success"
    store.mark_stopped(args.recording_id, now(), incomplete=incomplete)
    store.write_status()
    # A recorder that captured nothing leaves a recording that record start discards.
    if not store.holds_audio(args.recording_id):
        return 0
    return queue_transcription(args.recording_id)


def transcribe(args: argparse.Namespace) -> int:
    if not store.is_recording(args.recording_id):
        return fail(f"No recording {args.recording_id}.")
    if not store.holds_audio(args.recording_id):
        return fail(f"The tracks of {args.recording_id} are gone.")
    return queue_transcription(args.recording_id)


def work(_: argparse.Namespace) -> int:
    worker.work()
    return 0


def history(args: argparse.Namespace) -> int:
    folder = store.recordings_dir()
    if args.recording is not None:
        if not store.is_recording(args.recording):
            return fail(f"No recording {args.recording}.")
        folder = store.recording_dir(args.recording)
    folder.mkdir(parents=True, exist_ok=True)
    # gio is GLib's own tool, found on PATH like every desktop command; no shell is involved.
    return subprocess.run(["gio", "open", str(folder)], check=False).returncode  # noqa: S603, S607


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minutas",
        description="Record, transcribe and publish work meetings.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('minutas')}")
    parser.set_defaults(handler=None)
    commands = parser.add_subparsers(title="commands")

    record = commands.add_parser("record", help="start or stop recording a meeting")
    record_commands = record.add_subparsers(title="commands", required=True)
    record_commands.add_parser("start", help="start recording").set_defaults(handler=record_start)
    record_commands.add_parser("stop", help="stop recording").set_defaults(handler=record_stop)
    stopped = record_commands.add_parser(
        "stopped", help="mark a recording stopped (the recorder's unit runs this when it exits)"
    )
    stopped.add_argument("recording_id")
    stopped.set_defaults(handler=record_stopped)

    transcribe_command = commands.add_parser("transcribe", help="transcribe a recording again")
    transcribe_command.add_argument("recording_id")
    transcribe_command.set_defaults(handler=transcribe)
    commands.add_parser(
        "worker", help="transcribe the queued recordings (the transcription unit runs this)"
    ).set_defaults(handler=work)

    history_command = commands.add_parser("history", help="open the recordings")
    history_command.add_argument("--recording", metavar="ID", help="open this recording")
    history_command.set_defaults(handler=history)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int] | None", args.handler)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
