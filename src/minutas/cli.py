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

from minutas import recorder, store


def now() -> datetime:
    return datetime.now(UTC).astimezone()


def fail(reason: str) -> int:
    sys.stderr.write(f"{reason}\n")
    return 1


def record_start(_: argparse.Namespace) -> int:
    if recorder.is_running():
        return fail("Already recording.")
    store.close_abandoned()
    recording = store.create_recording(now(), echo_cancel=False)
    on_exit = [sys.executable, "-m", "minutas", "record", "stopped", recording["id"]]
    try:
        recorder.start(store.recording_dir(recording["id"]), on_exit=on_exit)
    except recorder.RecorderError as error:
        store.discard_recording(recording["id"])
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


def record_stopped(args: argparse.Namespace) -> int:
    # Anything but a clean exit after Stop means the recorder died: what it wrote is kept.
    incomplete = os.environ.get("SERVICE_RESULT") != "success"
    store.mark_stopped(args.recording_id, now(), incomplete=incomplete)
    store.write_status()
    return 0


def history(args: argparse.Namespace) -> int:
    folder = store.recordings_dir()
    if args.recording is not None:
        folder = store.recording_dir(args.recording)
        if not (folder / "recording.json").is_file():
            return fail(f"No recording {args.recording}.")
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
