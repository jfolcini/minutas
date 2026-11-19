"""Publishes a recording to Agaric with its agaric-mcp bridge: `agaric-mcp call <tool> <json>`.

The first publish makes the meeting page with add_meeting, with the summary if there is one.
Once one has succeeded, publishing again never makes a second page: it renames that page when the
recording's name changed, and adds a summary newer than the one it holds above the recording's
block, the one that holds its files.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import FrameType
from typing import NamedTuple, NotRequired, cast

from keyring.errors import KeyringError

from minutas import settings, store

ACTION = "publish_agaric"
SUMMARIZE = "summarize"
BRIDGE = "agaric-mcp"
GST_LAUNCH = "gst-launch-1.0"
TRANSCRIPT = "transcript.md"
DEFAULT_NAME = "Meeting"
PAGE_LINK = "agaric://page/"
# The tracks' own format; Opus at 32 kbps keeps about three hours under Agaric's 50 MB cap.
MIX_CAPS = "audio/x-raw,rate=16000,channels=1"

# How Agaric's Settings → Agent access makes an agent's token.
TOKEN = re.compile(r"\bagt_[0-9a-f]{32}\b")

KEEP_IT = "paste the config it shows under Agaric in Minutas' Settings"
NO_TOKEN = (
    "No Agaric agent token: add an agent with read-write access to the Work space in Agaric's "
    f"Settings → Agent access, then {KEEP_IT}"
)
NO_SPACE = "No Agaric space chosen: choose one under Agaric in Minutas' Settings"
BRIDGE_MISSING = "agaric-mcp not found: install Agaric 0.26 or newer and start it once"
UPDATE_AGARIC = (
    "Update Agaric to 0.26 or newer and start it once: the agaric-mcp Minutas found has no "
    "`call` command"
)
NOT_RUNNING = "Agaric isn't running: start it and turn on Settings → Agent access"
TOKEN_REFUSED = (
    f"The agent token was revoked: add an agent in Agaric's Settings → Agent access, then {KEEP_IT}"
)
TOO_BIG = "The meeting's audio is over Agaric's 50 MB attachment cap"
UNEXPECTED = "Agaric answered without a page: see Details"
GSTREAMER_MISSING = "gst-launch-1.0 not found: run install.sh"
AUDIO_LEFT_OUT = "Published without the audio: its tracks were deleted"

# Agaric's messages, on the bridge's stderr, for the failures that have a fix of their own.
KNOWN_FAILURES = (
    # A copy from before `call` refuses it; one from before agents refuses --token first.
    ("agaric-mcp: unknown argument: ", UPDATE_AGARIC),
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


class PublishRun(store.Run):
    # The page's title as the run left it, so publishing again knows when the name changed.
    title: NotRequired[str]
    # The Summarize run whose summary the page holds, so publishing again adds only a newer one.
    summary_run: NotRequired[int]
    # The block holding the recording's files, which a later summary goes above: on a page that
    # holds other recordings too, the page's first block holding a file may be another's.
    recording_block: NotRequired[str]


class Published(NamedTuple):
    """What a publish left in Agaric."""

    page_id: str
    title: str
    recording_block: str | None
    summary_run: int | None
    note: str | None = None


class AgaricError(Exception):
    def __init__(self, error: str, choice: store.Choice | None = None) -> None:
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


def bridge() -> str:
    """Agaric's own bridge, which an AppImage of Agaric keeps current in Agaric's data folder;
    else the one on the PATH, where a .deb puts it and where a stale copy may linger."""
    # Agaric's data folder sits beside Minutas' own, in XDG_DATA_HOME.
    own = store.data_dir().parent / "com.agaric.app" / "bin" / BRIDGE
    return str(own) if own.is_file() else BRIDGE


def token_in(pasted: str) -> str | None:
    """The agent token in what Agaric gives to paste: the token alone, or the config or command
    that holds it. None when there is none."""
    found = TOKEN.search(pasted)
    return None if found is None else found.group()


def call(token: str, tool: str, arguments: Mapping[str, object], log: Path) -> object:
    """Runs one tool and returns its result; the log gets the call, never the token."""
    request = json.dumps(arguments)
    with log.open("a") as out:
        out.write(f"$ {BRIDGE} call {tool} {request}\n")
        try:
            # No shell is involved. The bridge takes the token only as an argument, and only this
            # user can reach Agaric's socket.
            result = subprocess.run(  # noqa: S603
                [bridge(), "--token", token, "call", tool, request],
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


def id_in(answer: object, key: str) -> str:
    try:
        found = cast("dict[str, object]", answer)[key]
    except (KeyError, TypeError):
        found = None
    if not isinstance(found, str):
        raise AgaricError(UNEXPECTED)
    return found


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


def agent_token() -> str:
    try:
        token = settings.agaric_token()
    except KeyringError as error:
        # Raised when the person dismisses the keyring's unlock prompt, for one.
        reason = f"Could not read the Agaric agent token from the keyring: {error}"
        raise AgaricError(reason) from None
    if token is None:
        raise AgaricError(NO_TOKEN)
    return token


def latest_success(runs: list[store.Run], action: str) -> int | None:
    return next(
        (
            index
            for index, run in reversed(list(enumerate(runs)))
            if run["action"] == action and run["status"] == "succeeded"
        ),
        None,
    )


def send(recording_id: str, runs: list[store.Run], page: str | None, log: Path) -> Published:
    """Makes the page, adds the files to page, or brings the page made before up to date.

    runs is the log before this run.
    """
    token = agent_token()
    space_id = settings.agaric_space_id()
    if space_id is None:
        raise AgaricError(NO_SPACE)
    recording = store.load_recording(recording_id)
    day = datetime.fromisoformat(recording["started_at"]).date()
    title = page_title(recording["name"], day)
    taken = f"Agaric already has a page named {title}"
    summary_run = latest_success(runs, SUMMARIZE)
    summary: dict[str, str] = {}
    if summary_run is not None:
        summary_file = store.recording_dir(recording_id) / store.SUMMARY
        summary["summary_markdown"] = summary_file.read_text(encoding="utf-8")
    published = latest_success(runs, ACTION)
    if page is None and published is not None:
        previous = cast("PublishRun", runs[published])
        page_id = cast("str", previous["output"]).removeprefix(PAGE_LINK)
        if previous.get("title") != title:
            rename = {"space_id": space_id, "block_id": page_id, "content": title}
            try:
                call(token, "update_block_content", rename, log)
            except NameTakenError:
                raise AgaricError(taken, store.Choice(kind="rename_taken")) from None
        block = previous.get("recording_block")
        if summary_run is not None and summary_run != previous.get("summary_run"):
            # A run from before blocks were kept knows only the page.
            target = {"page": page_id} if block is None else {"block_id": block}
            call(token, "add_meeting", {"space_id": space_id, **target, **summary}, log)
        return Published(page_id, title, block, summary_run)
    target = (
        {"title": recording["name"], "date": day.isoformat()} if page is None else {"page": page}
    )
    # The cheapest call that proves the bridge, Agaric and the token, so their failures come
    # back at once rather than after mixing a long meeting.
    call(token, "list_spaces", {}, log)
    try:
        with meeting_files(store.recording_dir(recording_id), log) as files:
            answer = call(
                token, "add_meeting", {"space_id": space_id, **target, **files, **summary}, log
            )
    except NameTakenError:
        found = call(token, "resolve", {"space_id": space_id, "names": [title]}, log)
        holder = id_in(cast("list[object]", found)[0], "page_id")
        raise AgaricError(taken, store.Choice(kind="name_taken", page_id=holder)) from None
    note = None if "audio_path" in files else AUDIO_LEFT_OUT
    page_id, block = id_in(answer, "page_id"), id_in(answer, "recording_block_id")
    return Published(page_id, title, block, summary_run, note)


def interrupt(_signal: int, _frame: FrameType | None) -> None:
    raise AgaricError(store.INTERRUPTED)


def publish(recording_id: str, published: datetime, *, page: str | None = None) -> bool:
    """Publishes the recording, or adds its files to page, as a run in its log.

    Returns whether the run succeeded. SIGTERM, at logout or shutdown, fails the run as
    interrupted: the bridge or the mix is killed and the files made for Agaric are removed.
    """
    queued_at = published.isoformat(timespec="seconds")
    index, runs = store.start_run(
        recording_id,
        store.Run(
            action=ACTION,
            status="running",
            queued_at=queued_at,
            error=None,
            details=None,
            output=None,
        ),
    )
    run = PublishRun(
        action=ACTION,
        status="failed",
        queued_at=queued_at,
        error=None,
        details=None,
        output=None,
    )
    on_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        left = send(recording_id, runs[:index], page, store.run_log(recording_id, index))
    except AgaricError as error:
        run["error"] = str(error)
        if error.choice is not None:
            run["choice"] = error.choice
    else:
        run.update(status="succeeded", output=f"{PAGE_LINK}{left.page_id}", title=left.title)
        if left.note is not None:
            run["note"] = left.note
        if left.recording_block is not None:
            run["recording_block"] = left.recording_block
        if left.summary_run is not None:
            run["summary_run"] = left.summary_run
    finally:
        signal.signal(signal.SIGTERM, on_term)
    run["details"] = store.written_log(recording_id, index)
    store.end_run(recording_id, index, run)
    return run["status"] == "succeeded"


def list_spaces() -> list[tuple[str, str]]:
    """Every space the agent token may use, as its id and name."""
    spaces = cast("list[dict[str, str]]", call(agent_token(), "list_spaces", {}, Path(os.devnull)))
    return [(space["id"], space["name"]) for space in spaces]
