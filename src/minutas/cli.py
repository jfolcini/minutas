"""The `minutas` command."""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import cast

from keyring.errors import KeyringError

from minutas import (
    agaric,
    calendar,
    github,
    gitlab,
    recorder,
    settings,
    store,
    summarizer,
    worker,
)


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


def git(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this module's own argument list
        ["git", "-C", str(clone), *args],  # noqa: S607 - git from the PATH, like any command
        capture_output=True,
        text=True,
        check=False,
    )


def update(_: argparse.Namespace) -> int:
    # install.sh links the clone it ran from here.
    clone = Path.home() / ".local" / "share" / "minutas" / "app" / "repository"
    if not (clone / "install.sh").is_file():
        return fail(f"No clone of Minutas at {clone}: run install.sh from your clone first.")
    fetched = git(clone, "fetch", "--quiet", "--tags")
    if fetched.returncode != 0:
        return fail(f"Could not fetch the releases: {fetched.stderr.strip()}")
    # Release tags are v<version>; sorted as versions, v0.10.0 comes after v0.9.0.
    releases = git(clone, "tag", "--list", "--sort=-version:refname", "v*").stdout.split()
    if not releases:
        return fail("No release to update to yet.")
    latest = releases[0]
    on_latest = (
        git(clone, "rev-parse", "HEAD").stdout == git(clone, "rev-parse", f"{latest}^{{}}").stdout
    )
    checked_out = git(clone, "checkout", "--quiet", latest)
    if checked_out.returncode != 0:
        return fail(f"Could not check out {latest}: {checked_out.stderr.strip()}")
    sys.stdout.write(
        f"Already on the latest release, {latest}.\n" if on_latest else f"Updating to {latest}.\n"
    )
    # Before install.sh writes its own lines. It runs on the latest release too: a failed run
    # carries on from where it stopped, and an install that is whole prints ok for every step.
    sys.stdout.flush()
    return subprocess.run([clone / "install.sh"], check=False).returncode  # noqa: S603


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
    # Kept audio is bounded where that costs nothing, and before status.json tells the window.
    store.delete_audio_over_limits(stopped, *settings.audio_limits())
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


def open_settings(_: argparse.Namespace) -> int:
    from minutas.window import run  # noqa: PLC0415 - only the window needs GTK

    return run(None, open_settings=True)


def publish_agaric(args: argparse.Namespace) -> int:
    if not store.is_recording(args.recording_id):
        return fail(f"No recording {args.recording_id}.")
    if args.name is not None:
        name = args.name.strip()
        if not name:
            return fail("A name can't be blank.")
        store.set_typed_name(args.recording_id, name)
    return 0 if agaric.publish(args.recording_id, now(), page=args.page) else 1


def publish_gitlab(args: argparse.Namespace) -> int:
    recording_id = args.recording_id
    if not store.is_recording(recording_id):
        return fail(f"No recording {recording_id}.")
    target = gitlab.parse_link(args.link)
    if target is None:
        return fail(f"Not a GitLab issue or project link: {args.link}")
    if not (store.recording_dir(recording_id) / store.SUMMARY).is_file():
        return fail(f"{recording_id} has no summary yet: summarize it first.")
    return 0 if gitlab.publish(recording_id, now(), target) else 1


def publish_github(args: argparse.Namespace) -> int:
    recording_id = args.recording_id
    if not store.is_recording(recording_id):
        return fail(f"No recording {recording_id}.")
    target = github.parse_link(args.link)
    if target is None:
        return fail(f"Not a GitHub issue or repository link: {args.link}")
    if not (store.recording_dir(recording_id) / store.SUMMARY).is_file():
        return fail(f"{recording_id} has no summary yet: summarize it first.")
    return 0 if github.publish(recording_id, now(), target) else 1


def summarize(args: argparse.Namespace) -> int:
    recording_id = args.recording_id
    if not store.is_recording(recording_id):
        return fail(f"No recording {recording_id}.")
    prompt = args.prompt.strip()
    if not prompt:
        return fail("A prompt can't be blank.")
    bad = next((name for name in args.context if summarizer.bad_context_name(name)), None)
    if bad is not None:
        return fail(f"No context preset can be named {bad}.")
    if not (store.recording_dir(recording_id) / summarizer.TRANSCRIPT).is_file():
        return fail(f"{recording_id} has no transcript yet: transcribe it first.")
    return 0 if summarizer.summarize(recording_id, now(), prompt, args.context) else 1


def choose_space(*, token_kept: bool) -> int:
    """Lists the spaces the token may use, and sets the one to publish to when that leaves no
    choice: the space set, while the token may use it, else the token's only space.

    The list goes to stdout, for the window to offer; where it publishes goes to stderr.
    """
    try:
        names = dict(agaric.list_spaces())
    except agaric.AgaricError as error:
        if token_kept:
            return fail(f"Kept the agent token, but couldn't choose the space: {error}")
        return fail(str(error))
    sys.stdout.writelines(f"{space_id}\t{name}\n" for space_id, name in names.items())
    chosen = settings.agaric_space_id()
    if chosen is None or chosen not in names:
        if not names:
            sys.stderr.write(
                "The stored agent token may use no space: give its agent read-write access to the "
                "Work space in Agaric's Settings → Agent access.\n"
            )
            return 0
        if len(names) != 1:
            sys.stderr.write(
                "The stored agent token may use several spaces: choose the one to publish to.\n"
            )
            return 0
        [chosen] = names
        settings.set_agaric_space_id(chosen)
    sys.stderr.write(f"The stored agent token publishes to {names[chosen]}.\n")
    return 0


def settings_agaric(args: argparse.Namespace) -> int:
    if args.space is None and not args.token_from_stdin and not args.list_spaces:
        return fail("Nothing to do: pass --space, --token-from-stdin or --list-spaces.")
    if args.token_from_stdin:
        # From stdin, never from the arguments, which the shell's history keeps; all of it, since
        # Agaric's config spans lines.
        token = agaric.token_in(sys.stdin.read())
        if token is None:
            return fail(
                "No Agaric agent token in that: paste the config Agaric shows for the agent in its "
                "Settings → Agent access, or the token alone."
            )
        try:
            settings.set_agaric_token(token)
        except KeyringError as error:
            # Dismissing the keyring's unlock prompt raises KeyringLocked, for one.
            return fail(f"Could not store the Agaric agent token in the keyring: {error}")
    if args.space is not None:
        settings.set_agaric_space_id(args.space)
    if args.list_spaces or args.token_from_stdin:
        return choose_space(token_kept=args.token_from_stdin)
    return 0


def settings_gitlab(args: argparse.Namespace) -> int:
    problem = gitlab.glab_problem()
    if problem:
        return fail(problem)
    host = gitlab.login_host()
    try:
        if args.token_from_stdin:
            # From stdin, never from the arguments, which the shell's history keeps.
            token = sys.stdin.read().strip()
            if not token:
                return fail(
                    "No token in that: paste a GitLab personal access token with the api scope."
                )
            gitlab.log_in(host, token)
        said = gitlab.login_status(host)
    except gitlab.GitLabError as error:
        return fail(str(error))
    sys.stderr.write(f"{said}\n")
    return 0


def add_summarize_arguments(summarize_command: argparse.ArgumentParser) -> None:
    summarize_command.add_argument("recording_id")
    summarize_command.add_argument(
        "--prompt", required=True, help="what the summary should cover, as a preset says it"
    )
    summarize_command.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="NAME",
        help="a context preset to add, by name; repeat for each",
    )
    summarize_command.set_defaults(handler=summarize)


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
    commands.add_parser(
        "update", help="move to the latest release and run its install.sh"
    ).set_defaults(handler=update)

    transcribe_command = commands.add_parser("transcribe", help="transcribe a recording again")
    transcribe_command.add_argument("recording_id")
    transcribe_command.set_defaults(handler=transcribe)
    commands.add_parser(
        "worker", help="transcribe the queued recordings (the transcription unit runs this)"
    ).set_defaults(handler=work)

    history_command = commands.add_parser("history", help="open the Recordings window")
    history_command.add_argument("--recording", metavar="ID", help="open this recording")
    history_command.set_defaults(handler=history)

    add_summarize_arguments(
        commands.add_parser(
            "summarize", help="summarize a recording with the summarizer chosen in Settings"
        )
    )

    publish = commands.add_parser("publish", help="publish a recording")
    publish_commands = publish.add_subparsers(title="commands", required=True)
    agaric_command = publish_commands.add_parser("agaric", help="publish a recording to Agaric")
    agaric_command.add_argument("recording_id")
    agaric_command.add_argument("--page", metavar="ID", help="add the files to this page instead")
    agaric_command.add_argument("--name", help="name the recording first; the name is kept")
    agaric_command.set_defaults(handler=publish_agaric)
    gitlab_command = publish_commands.add_parser(
        "gitlab", help="post a recording's summary to GitLab"
    )
    gitlab_command.add_argument("recording_id")
    gitlab_command.add_argument(
        "link", help="an issue to comment on, or a project to open an issue in"
    )
    gitlab_command.set_defaults(handler=publish_gitlab)
    github_command = publish_commands.add_parser(
        "github", help="post a recording's summary to GitHub, its transcript in a secret gist"
    )
    github_command.add_argument("recording_id")
    github_command.add_argument(
        "link", help="an issue to comment on, or a repository to open an issue in"
    )
    github_command.set_defaults(handler=publish_github)

    settings_command = commands.add_parser(
        "settings", help="open the Settings, or change one with a command"
    )
    settings_command.set_defaults(handler=open_settings)
    settings_commands = settings_command.add_subparsers(title="commands")
    agaric_settings = settings_commands.add_parser("agaric", help="the Agaric space and token")
    agaric_settings.add_argument("--space", metavar="ID", help="publish to this space")
    agaric_settings.add_argument(
        "--token-from-stdin",
        action="store_true",
        help="read the agent token, or the whole config Agaric shows for the agent, from stdin;"
        " keep the token in the keyring, then do as --list-spaces does",
    )
    agaric_settings.add_argument(
        "--list-spaces",
        action="store_true",
        help="list the spaces the token may use, and choose the one to publish to when the"
        " token may use only one",
    )
    agaric_settings.set_defaults(handler=settings_agaric)
    gitlab_settings = settings_commands.add_parser(
        "gitlab",
        help="say whether glab is logged in to the default GitLab link's server, gitlab.com"
        " without one",
    )
    gitlab_settings.add_argument(
        "--token-from-stdin",
        action="store_true",
        help="read an access token from stdin and hand it to glab, which keeps it in the keyring,"
        " first",
    )
    gitlab_settings.set_defaults(handler=settings_gitlab)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int] | None", args.handler)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
