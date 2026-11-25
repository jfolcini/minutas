"""Publishes a recording's summary to GitLab with glab's API command, `glab api`.

An issue link gets a comment, a project link a new issue. The transcript and the audio are
uploaded to the project first, and the summary links them. A failed run keeps the audio it
uploaded, so Retry to the same project doesn't upload it again. Audio over the server's upload
limit is left out, with a note: only an admin can read the limit, so the smallest audio a server
refused is remembered, and a later audio as big is left out without uploading.

The Settings hand glab a token here, and ask whether it is logged in: glab holds the login.
"""

import json
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import NamedTuple, NotRequired, cast
from urllib.parse import quote, urlsplit, urlunsplit

from minutas import agaric, settings, store

ACTION = "publish_gitlab"
GLAB = "glab"
# The server glab logs in to without a default link, as glab itself does.
DEFAULT_HOST = "gitlab.com"
# glab api --form, the multipart body that POST /projects/:id/uploads takes, came in 1.91.0.
MINIMUM = (1, 91, 0)
# Not the snap: confined to the home folder's visible files, it can't read the recordings.
INSTALL = (
    f"install glab {'.'.join(map(str, MINIMUM))} or newer from the .deb on "
    "https://gitlab.com/gitlab-org/cli/-/releases"
)
OLDER = f"older than {'.'.join(map(str, MINIMUM))}: {INSTALL}"
GLAB_MISSING = f"glab not found: {INSTALL}, then log in with `glab auth login`"
UNEXPECTED = "GitLab answered without a link: see Details"

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
ISSUE = re.compile(r"(?:issues|work_items)/([0-9]+)")
# What glab says when GitLab refuses an upload as over the server's limit.
TOO_BIG = "HTTP 413"


class Target(NamedTuple):
    """Where a link says to post: a comment on an issue, or a new issue in a project."""

    # The link without its query or fragment.
    link: str
    host: str
    # The project's path: group/project, or group/subgroup/project.
    project: str
    # The issue's number in its project; None for a project link.
    issue: int | None

    @property
    def api(self) -> str:
        """The project's endpoint in GitLab's API."""
        return f"projects/{quote(self.project, safe='')}"

    @property
    def plan(self) -> str:
        """What publishing to the link does, as the window says it before posting."""
        if self.issue is None:
            return f"Opens a new issue in {self.project}"
        return f"Comments on issue #{self.issue} of {self.project}"


class GitLabRun(store.Run):
    # Each upload the run finished, by what it holds ("transcript", "audio"), as GitLab's
    # Markdown link to it.
    uploads: NotRequired[dict[str, str]]


class GitLabError(Exception):
    pass


class UploadLimitError(GitLabError):
    """GitLab answered an upload with HTTP 413."""


def parse_link(link: str) -> Target | None:
    """The issue or project the link points to; None for anything else."""
    try:
        parts = urlsplit(link.strip())
    except ValueError:
        # A typed "https://[", say: a host that opens an IPv6 address and never closes it.
        return None
    project, dash, rest = parts.path.strip("/").partition("/-/")
    issue = ISSUE.fullmatch(rest)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or "/" not in project
        # A group's page, such as an epic's work item: GitLab reserves "groups" as a group name.
        or project.startswith("groups/")
        or (dash and issue is None)
    ):
        return None
    link = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return Target(link, parts.netloc, project, None if issue is None else int(issue[1]))


def glab_problem() -> str:
    """Why the glab on PATH can't publish, and how to put one that can in its place; empty
    when it can."""
    path = shutil.which(GLAB)
    if path is None:
        return GLAB_MISSING
    # /snap/bin/glab links to snap, which runs glab confined to the home's visible files.
    if Path(path).resolve().name == "snap":
        return f"The glab at {path} is the snap, which can't read the recordings: {INSTALL}"
    # The path just found, never a shell string.
    said = subprocess.run(  # noqa: S603
        [path, "--version"], capture_output=True, text=True, check=False
    ).stdout
    version = VERSION.search(said)
    if version is None or tuple(map(int, version.groups())) < MINIMUM:
        return f"The glab at {path} is {OLDER}"
    return ""


def login_host() -> str:
    """The server whose glab login the Settings show and set: the default link's."""
    target = parse_link(settings.gitlab_link() or "")
    return DEFAULT_HOST if target is None else target.host


def log_in(host: str, token: str) -> None:
    """Hands glab the token for the host, which glab keeps in the keyring."""
    # On stdin, never in the arguments, which any process can read.
    result = subprocess.run(  # noqa: S603 - this module's own argument list
        [GLAB, "auth", "login", f"--hostname={host}", "--stdin", "--use-keyring"],
        input=token,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        said = agaric.first_line(result.stderr) or f"glab exited with status {result.returncode}"
        reason = f"glab couldn't keep the token for {host}: {said}"
        raise GitLabError(reason)


def login_status(host: str) -> str:
    """Says glab is logged in to the host; raises GitLabError with why when it isn't."""
    # glab signs in to the server to check: a token GitLab refuses is no login.
    result = subprocess.run(  # noqa: S603 - this module's own argument list
        [GLAB, "auth", "status", f"--hostname={host}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return f"glab is logged in to {host}."
    refused = re.search(r"API call failed: (.+)", result.stderr)
    # A 401 is no token for the host, as a fresh glab has for gitlab.com, or a stale one: either
    # way a new token is the fix.
    if refused is not None and ": 401 " not in refused[1]:
        reason = f"glab couldn't sign in to {host}: {refused[1].strip()}"
    else:
        reason = (
            f"glab isn't logged in to {host}: paste a personal access token for it, with the api "
            "scope"
        )
    raise GitLabError(reason)


def failure(target: Target, stderr: str, returncode: int) -> str:
    host = target.host
    # What glab says on stderr for the failures that have a fix of their own.
    known = (
        (
            "HTTP 401",
            f"glab isn't logged in to {host}: run `glab auth login --hostname {host}`, then Retry",
        ),
        (
            "HTTP 40[34]",
            (
                f"no access to {target.link}: check the link, and that your account on {host} can "
                "see it"
            ),
        ),
        (TOO_BIG, f"it is over {host}'s upload limit"),
        ("permission denied", f"a glab installed as a snap can't read the recordings: {INSTALL}"),
        ("dial tcp", f"{host} is out of reach: check the network, then Retry"),
        # A glab from before 1.91.
        ("unknown flag: --form", f"glab is {OLDER}"),
    )
    for marker, reason in known:
        if re.search(marker, stderr):
            return reason
    reason = agaric.first_line(stderr)
    if reason is None:
        return f"glab exited with status {returncode}"
    return reason.removeprefix("glab: ")


def post(target: Target, step: str, arguments: list[str], key: str, log: Path) -> str:
    """POSTs to GitLab's API with glab: arguments are the endpoint, then its fields.

    Returns that key of GitLab's answer. The log gets the call and everything glab said.
    """
    command = [GLAB, "api", f"--hostname={target.host}", "--method=POST", *arguments]
    with log.open("a") as out:
        out.write(f"$ {shlex.join(command)}\n")
        try:
            # This module's own argument list, never a shell string; glab is found on PATH.
            result = subprocess.run(  # noqa: S603
                command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            reason = f"Could not {step}: {GLAB_MISSING}"
            raise GitLabError(reason) from None
        out.write(f"{result.stdout}{result.stderr}exit status {result.returncode}\n")
    if result.returncode != 0:
        reason = f"Could not {step}: {failure(target, result.stderr, result.returncode)}"
        raise (UploadLimitError if TOO_BIG in result.stderr else GitLabError)(reason)
    try:
        found = cast("dict[str, object]", json.loads(result.stdout))[key]
    except (ValueError, KeyError, TypeError):
        found = None
    if not isinstance(found, str | int):
        reason = f"Could not {step}: {UNEXPECTED}"
        raise GitLabError(reason)
    return str(found)


def upload(target: Target, what: str, file: Path, log: Path) -> str:
    """Uploads the file to the target's project, under its own name; returns GitLab's
    Markdown link to it."""
    arguments = [f"{target.api}/uploads", f"--form=file=@{file}"]
    return post(target, f"upload the {what}", arguments, "markdown", log)


def upload_audio(target: Target, mixed: Path, uploads: dict[str, str], log: Path) -> str | None:
    """Uploads the audio, adding it to uploads; returns the note when the server's limit leaves
    it out."""
    size = mixed.stat().st_size
    left_out = f"The audio was left out: it is over {target.host}'s upload limit"
    refused = settings.refused_audio_size(target.host)
    if refused is not None and size >= refused:
        return left_out
    try:
        uploads["audio"] = upload(target, "audio", mixed, log)
    except UploadLimitError:
        settings.refuse_audio_size(target.host, size)
        return left_out
    return None


def send(
    recording_id: str, target: Target, uploads: dict[str, str], log: Path
) -> tuple[str, str | None]:
    """Uploads the transcript and, unless uploads has it, the audio, adding each as it is done,
    then posts the summary.

    Returns the link to the comment or issue, and a note on what was left out.
    """
    folder = store.recording_dir(recording_id)
    note = None
    with tempfile.TemporaryDirectory(dir=folder, prefix=".publish-") as work:
        copy = Path(work) / f"{recording_id}.transcript.md"
        shutil.copyfile(folder / store.TRANSCRIPT, copy)
        uploads["transcript"] = upload(target, "transcript", copy, log)
        if "audio" not in uploads and store.holds_audio(recording_id):
            mixed = agaric.mix(folder, Path(work), log)
            note = upload_audio(target, mixed, uploads, log)
        elif "audio" not in uploads:
            note = agaric.AUDIO_LEFT_OUT
    summary = (folder / store.SUMMARY).read_text(encoding="utf-8").strip()
    links = [uploads[what] for what in ("transcript", "audio") if what in uploads]
    body = "\n\n".join([summary, *links]) + "\n"
    # --raw-field, never --field: --field reads the file that a value starting with @ names.
    if target.issue is None:
        recording = store.load_recording(recording_id)
        day = datetime.fromisoformat(recording["started_at"]).date()
        arguments = [
            f"{target.api}/issues",
            f"--raw-field=title={agaric.page_title(recording['name'], day)}",
            f"--raw-field=description={body}",
        ]
        return post(target, "open the issue", arguments, "web_url", log), note
    arguments = [f"{target.api}/issues/{target.issue}/notes", f"--raw-field=body={body}"]
    comment = post(target, f"comment on issue #{target.issue}", arguments, "id", log)
    return f"{target.link}#note_{comment}", note


def carried(runs: list[store.Run], target: Target) -> dict[str, str]:
    """The audio upload of the last run, when it failed publishing to the target's project.

    An upload belongs to its project; publishing again, after a success, uploads what there is
    now. The transcript is always uploaded again: naming a speaker changes it.
    """
    last = next((run for run in reversed(runs) if run["action"] == ACTION), None)
    if last is None or last["status"] != "failed":
        return {}
    before = parse_link(last.get("link", ""))
    if before is None or (before.host, before.project) != (target.host, target.project):
        return {}
    audio = cast("GitLabRun", last).get("uploads", {}).get("audio")
    return {} if audio is None else {"audio": audio}


def interrupt(_signal: int, _frame: FrameType | None) -> None:
    raise GitLabError(store.INTERRUPTED)


def publish(recording_id: str, published: datetime, target: Target) -> bool:
    """Publishes the recording's summary to the target, as a run in its log.

    Returns whether the run succeeded. SIGTERM, at logout or shutdown, fails the run as
    interrupted: glab or the mix is killed, and the uploads already finished are kept.
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
            link=target.link,
        ),
    )
    run = GitLabRun(
        action=ACTION,
        status="failed",
        queued_at=queued_at,
        error=None,
        details=None,
        output=None,
        link=target.link,
    )
    uploads = carried(runs[:index], target)
    on_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        output, note = send(recording_id, target, uploads, store.run_log(recording_id, index))
    except (GitLabError, agaric.AgaricError) as error:
        run["error"] = str(error)
    else:
        run.update(status="succeeded", output=output)
        if note is not None:
            run["note"] = note
    finally:
        signal.signal(signal.SIGTERM, on_term)
    if uploads:
        run["uploads"] = uploads
    run["details"] = store.written_log(recording_id, index)
    store.end_run(recording_id, index, run)
    return run["status"] == "succeeded"
