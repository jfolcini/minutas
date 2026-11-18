"""The `minutas` command."""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

from keyring.errors import KeyringError

from minutas import agaric, calendar, recorder, settings, store, summarizer, worker


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


def repair(_: argparse.Namespace) -> int:
    """Puts right what a crash or a logout left: a recording still open, transcriptions queued."""
    if not recorder.is_running():
        store.close_abandoned()
    store.write_status()
    try:
        # A worker first fails a run a crash left running, then takes the queued ones.
        worker.ensure_worker()
    except worker.WorkerError as error:
        return fail(f"Could not start transcribing: {error}")
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
    stopped = now()
    store.mark_stopped(args.recording_id, stopped, incomplete=incomplete)
    store.write_status()
    # A recorder that captured nothing leaves a recording that record start discards.
    if not store.holds_audio(args.recording_id):
        return 0
    started = datetime.fromisoformat(store.load_recording(args.recording_id)["started_at"])
    event = calendar.lookup(started, stopped)
    if event is not None:
        store.set_calendar_event(args.recording_id, event.name, list(event.attendees))
    # Queuing rewrites status.json, with the event's name.
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
    if args.recording is not None and not store.is_recording(args.recording):
        return fail(f"No recording {args.recording}.")
    # Here, not at the top: only the window needs GTK, which the other commands never load.
    from minutas.window import run  # noqa: PLC0415

    return run(args.recording)


def publish_agaric(args: argparse.Namespace) -> int:
    if not store.is_recording(args.recording_id):
        return fail(f"No recording {args.recording_id}.")
    if args.name is not None:
        name = args.name.strip()
        if not name:
            return fail("A name can't be blank.")
        store.set_typed_name(args.recording_id, name)
    return 0 if agaric.publish(args.recording_id, now(), page=args.page) else 1


def summarize(args: argparse.Namespace) -> int:
    recording_id = args.recording_id
    if not store.is_recording(recording_id):
        return fail(f"No recording {recording_id}.")
    prompt = args.prompt.strip()
    if not prompt:
        return fail("A prompt can't be blank.")
    if not (store.recording_dir(recording_id) / summarizer.TRANSCRIPT).is_file():
        return fail(f"{recording_id} has no transcript yet: transcribe it first.")
    return 0 if summarizer.summarize(recording_id, now(), prompt) else 1


def settings_agaric(args: argparse.Namespace) -> int:
    if args.space is None and not args.token_from_stdin and not args.list_spaces:
        return fail("Nothing to do: pass --space, --token-from-stdin or --list-spaces.")
    if args.token_from_stdin:
        # From stdin, never from the arguments, which the shell's history keeps.
        token = sys.stdin.readline().strip()
        if not token:
            return fail("No token on stdin.")
        try:
            settings.set_agaric_token(token)
        except KeyringError as error:
            # Dismissing the keyring's unlock prompt raises KeyringLocked, for one.
            return fail(f"Could not store the Agaric agent token in the keyring: {error}")
    if args.space is not None:
        settings.set_agaric_space_id(args.space)
    if args.list_spaces:
        try:
            spaces = agaric.list_spaces()
        except agaric.AgaricError as error:
            return fail(str(error))
        sys.stdout.writelines(f"{space_id}\t{name}\n" for space_id, name in spaces)
    return 0


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

    commands.add_parser(
        "repair",
        help="close a recording whose recorder is gone and resume the queued transcriptions"
        " (the extension runs this at login)",
    ).set_defaults(handler=repair)

    transcribe_command = commands.add_parser("transcribe", help="transcribe a recording again")
    transcribe_command.add_argument("recording_id")
    transcribe_command.set_defaults(handler=transcribe)
    commands.add_parser(
        "worker", help="transcribe the queued recordings (the transcription unit runs this)"
    ).set_defaults(handler=work)

    history_command = commands.add_parser("history", help="open the history window")
    history_command.add_argument("--recording", metavar="ID", help="open this recording")
    history_command.set_defaults(handler=history)

    summarize_command = commands.add_parser("summarize", help="summarize a recording with Devin")
    summarize_command.add_argument("recording_id")
    summarize_command.add_argument(
        "--prompt", required=True, help="what the summary should cover, as a preset says it"
    )
    summarize_command.set_defaults(handler=summarize)

    publish = commands.add_parser("publish", help="publish a recording")
    publish_commands = publish.add_subparsers(title="commands", required=True)
    agaric_command = publish_commands.add_parser("agaric", help="publish a recording to Agaric")
    agaric_command.add_argument("recording_id")
    agaric_command.add_argument("--page", metavar="ID", help="add the files to this page instead")
    agaric_command.add_argument("--name", help="name the recording first; the name is kept")
    agaric_command.set_defaults(handler=publish_agaric)

    settings_command = commands.add_parser("settings", help="change the settings")
    settings_commands = settings_command.add_subparsers(title="commands", required=True)
    agaric_settings = settings_commands.add_parser("agaric", help="the Agaric space and token")
    agaric_settings.add_argument("--space", metavar="ID", help="publish to this space")
    agaric_settings.add_argument(
        "--token-from-stdin",
        action="store_true",
        help="read the agent token from stdin and keep it in the keyring",
    )
    agaric_settings.add_argument(
        "--list-spaces", action="store_true", help="list the spaces the token may use"
    )
    agaric_settings.set_defaults(handler=settings_agaric)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int] | None", args.handler)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
