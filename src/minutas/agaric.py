"""Publishes a recording to Agaric with its agaric-mcp bridge: `agaric-mcp call <tool> <json>`.

The first publish makes the meeting page with add_meeting. Once one has succeeded, publishing
again only renames that page when the recording's name changed: it never makes a second page.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from minutas import settings, store

ACTION = "publish_agaric"
BRIDGE = "agaric-mcp"
GST_LAUNCH = "gst-launch-1.0"
TRANSCRIPT = "transcript.md"
DEFAULT_NAME = "Meeting"
PAGE_LINK = "agaric://page/"
INTERRUPTED = "interrupted"
# The tracks' own format; Opus at 32 kbps keeps about three hours under Agaric's 50 MB cap.
MIX_CAPS = "audio/x-raw,rate=16000,channels=1"

KEEP_IT = "run `minutas settings agaric --token-from-stdin` with its token"
NO_TOKEN = (
    "No Agaric agent token: add an agent with read-write access to the Work space in Agaric's "
    f"Settings → Agent access, then {KEEP_IT}"
)
NO_SPACE = (
    "No Agaric space set: list them with `minutas settings agaric --list-spaces`, then run "
    "`minutas settings agaric --space <id>`"
)
BRIDGE_MISSING = "agaric-mcp not found: install Agaric, or put its agaric-mcp on the PATH"
UPDATE_AGARIC = "Update Agaric: agaric-mcp has no `call` command"
NOT_RUNNING = "Agaric isn't running: start it, turn on Settings → Agent access and press Retry"
TOKEN_REFUSED = (
    f"The agent token was revoked: add an agent in Agaric's Settings → Agent access, then {KEEP_IT}"
)
TOO_BIG = "The meeting's audio is over Agaric's 50 MB attachment cap"
UNEXPECTED = "Agaric answered without a page: see Details"
GSTREAMER_MISSING = "gst-launch-1.0 not found: run install.sh"

# Agaric's messages, on the bridge's stderr, for the failures that have a fix of their own.
KNOWN_FAILURES = (
    ("unknown argument: call", UPDATE_AGARIC),
    ("Agaric MCP not running", NOT_RUNNING),
    ("no agent has this token", TOKEN_REFUSED),
    ("was revoked; add an agent", TOKEN_REFUSED),
    ("attachment cap", TOO_BIG),
)
# add_meeting's refusal of a new page's name, then update_block_content's, by title or alias.
NAME_TAKEN = re.compile(
    r"a page (named|titled) '.*' already exists in (the|this) space"
    r"|a page in this space has the alias '"
)


class Choice(TypedDict):
    kind: str
    page_id: str


class PublishRun(store.Run):
    # The page's title as the run left it, so publishing again knows when the name changed.
    title: NotRequired[str]
    # A failed run that waits for the person to choose how to go on.
    choice: NotRequired[Choice]


class AgaricError(Exception):
    def __init__(self, error: str, choice: Choice | None = None) -> None:
        super().__init__(error)
        self.choice = choice


class NameTakenError(AgaricError):
    pass


def page_title(name: str, day: date) -> str:
    """The title Agaric gives a meeting page: it trims the name, and an empty one is Meeting."""
    return f"{name.strip() or DEFAULT_NAME} {day.isoformat()}"


def first_line(output: str) -> str | None:
    return next((line.strip() for line in output.splitlines() if line.strip()), None)


def failure(stderr: str, returncode: int) -> AgaricError:
    if NAME_TAKEN.search(stderr):
        return NameTakenError(stderr)
    for marker, error in KNOWN_FAILURES:
        if marker in stderr:
            return AgaricError(error)
    reason = first_line(stderr)
    if reason is None:
        return AgaricError(f"{BRIDGE} exited with status {returncode}")
    return AgaricError(reason.removeprefix(f"{BRIDGE}: "))


def call(token: str, tool: str, arguments: Mapping[str, object], log: Path) -> object:
    """Runs one tool and returns its result; the log gets the call, never the token."""
    request = json.dumps(arguments)
    with log.open("a") as out:
        out.write(f"$ {BRIDGE} call {tool} {request}\n")
        try:
            # The bridge is found on PATH like any desktop command; no shell is involved.
            result = subprocess.run(  # noqa: S603
                [BRIDGE, "--token", token, "call", tool, request],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            raise AgaricError(BRIDGE_MISSING) from None
        out.write(f"{result.stdout}{result.stderr}exit status {result.returncode}\n")
    if result.returncode != 0:
        raise failure(result.stderr, result.returncode)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise AgaricError(UNEXPECTED) from None


def page_id_in(answer: object) -> str:
    try:
        page_id = cast("dict[str, object]", answer)["page_id"]
    except (KeyError, TypeError):
        page_id = None
    if not isinstance(page_id, str):
        raise AgaricError(UNEXPECTED)
    return page_id


def mix(folder: Path, work: Path, log: Path) -> Path:
    """Mixes the tracks that hold audio into one Ogg Opus file in work, a folder in folder."""
    mixed = work / f"{folder.name}.opus"
    command = [
        *(GST_LAUNCH, "-q", "audiomixer", "name=mix"),
        *("!", "audioconvert", "!", "audioresample", "!", MIX_CAPS),
        *("!", "opusenc", "bitrate=32000", "!", "oggmux", "!", "filesink"),
        # Relative locations: gst-launch re-splits its arguments on spaces.
        f"location={mixed.name}",
    ]
    for name in store.TRACKS:
        if store.has_audio(folder / name):
            command += ["filesrc", f"location=../{name}", "!", "decodebin"]
            command += ["!", "audioconvert", "!", "audioresample", "!", "mix."]
    with log.open("a") as out:
        out.write(f"$ {shlex.join(command)}\n")
        try:
            # This module's own argument list, never a shell string.
            result = subprocess.run(  # noqa: S603
                command, cwd=work, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            raise AgaricError(GSTREAMER_MISSING) from None
        out.write(f"{result.stdout}{result.stderr}exit status {result.returncode}\n")
    if result.returncode != 0:
        reason = first_line(result.stderr) or f"{GST_LAUNCH} exited with status {result.returncode}"
        error = f"Could not mix the tracks: {reason}"
        raise AgaricError(error)
    return mixed


@contextmanager
def meeting_files(folder: Path, log: Path) -> Generator[dict[str, str]]:
    """The mixed tracks and a copy of the transcript, removed afterwards.

    Both are named after the recording, so Agaric pairs them even on a page that already holds
    another recording.
    """
    with tempfile.TemporaryDirectory(dir=folder, prefix=".publish-") as work:
        files: dict[str, str] = {}
        if store.holds_audio(folder.name):
            files["audio_path"] = str(mix(folder, Path(work), log))
        if (folder / TRANSCRIPT).is_file():
            copy = Path(work) / f"{folder.name}.md"
            shutil.copyfile(folder / TRANSCRIPT, copy)
            files["transcript_path"] = str(copy)
        yield files


def send(
    recording_id: str, previous: PublishRun | None, page: str | None, log: Path
) -> tuple[str, str]:
    """Makes the page, adds the files to page, or renames the page made before.

    Returns the page's id and the title it now has.
    """
    token = settings.agaric_token()
    if token is None:
        raise AgaricError(NO_TOKEN)
    space_id = settings.agaric_space_id()
    if space_id is None:
        raise AgaricError(NO_SPACE)
    recording = store.load_recording(recording_id)
    day = datetime.fromisoformat(recording["started_at"]).date()
    title = page_title(recording["name"], day)
    try:
        if page is None and previous is not None:
            page_id = cast("str", previous["output"]).removeprefix(PAGE_LINK)
            if previous.get("title") != title:
                rename = {"space_id": space_id, "block_id": page_id, "content": title}
                call(token, "update_block_content", rename, log)
            return page_id, title
        target = (
            {"title": recording["name"], "date": day.isoformat()}
            if page is None
            else {"page": page}
        )
        with meeting_files(store.recording_dir(recording_id), log) as files:
            answer = call(token, "add_meeting", {"space_id": space_id, **target, **files}, log)
        return page_id_in(answer), title
    except NameTakenError:
        found = call(token, "resolve", {"space_id": space_id, "names": [title]}, log)
        holder = page_id_in(cast("list[object]", found)[0])
        error = f"Agaric already has a page named {title}"
        raise AgaricError(error, Choice(kind="name_taken", page_id=holder)) from None


def start_run(recording_id: str, queued_at: str) -> tuple[int, PublishRun | None]:
    """Appends a running run; returns its index and the latest publish that succeeded.

    A publish still running is one a crash left behind: it fails as interrupted.
    """
    with store.lock():
        runs = store.load_runs(recording_id)
        for index, run in enumerate(runs):
            if run["action"] == ACTION and run["status"] == "running":
                run["status"] = "failed"
                run["error"] = INTERRUPTED
                run["details"] = existing_log(recording_id, index)
        succeeded = [r for r in runs if r["action"] == ACTION and r["status"] == "succeeded"]
        runs.append(
            store.Run(
                action=ACTION,
                status="running",
                queued_at=queued_at,
                error=None,
                details=None,
                output=None,
            )
        )
        store.save_runs(recording_id, runs)
    store.write_status()
    return len(runs) - 1, cast("PublishRun", succeeded[-1]) if succeeded else None


def end_run(recording_id: str, index: int, run: PublishRun) -> None:
    with store.lock():
        runs = store.load_runs(recording_id)
        runs[index] = run
        store.save_runs(recording_id, runs)
    store.write_status()


def run_log(recording_id: str, index: int) -> Path:
    return store.recording_dir(recording_id) / f"run-{index}.log"


def existing_log(recording_id: str, index: int) -> str | None:
    log = run_log(recording_id, index)
    return log.name if log.is_file() else None


def publish(recording_id: str, published: datetime, *, page: str | None = None) -> bool:
    """Publishes the recording, or adds its files to page, as a run in its log.

    Returns whether the run succeeded.
    """
    queued_at = published.isoformat(timespec="seconds")
    index, previous = start_run(recording_id, queued_at)
    run = PublishRun(
        action=ACTION,
        status="failed",
        queued_at=queued_at,
        error=None,
        details=None,
        output=None,
    )
    try:
        page_id, title = send(recording_id, previous, page, run_log(recording_id, index))
    except AgaricError as error:
        run["error"] = str(error)
        if error.choice is not None:
            run["choice"] = error.choice
    else:
        run.update(status="succeeded", output=f"{PAGE_LINK}{page_id}", title=title)
    run["details"] = existing_log(recording_id, index)
    end_run(recording_id, index, run)
    return run["status"] == "succeeded"


def list_spaces() -> list[tuple[str, str]]:
    """Every space the agent token may use, as its id and name."""
    token = settings.agaric_token()
    if token is None:
        raise AgaricError(NO_TOKEN)
    spaces = cast("list[dict[str, str]]", call(token, "list_spaces", {}, Path(os.devnull)))
    return [(space["id"], space["name"]) for space in spaces]
