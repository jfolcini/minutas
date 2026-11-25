"""Publishes a recording's summary to GitHub with GitHub's CLI, gh.

An issue link gets a comment, a repository link a new issue. GitHub takes no audio file, and gh
attaches none but images, so the transcript goes to a secret gist, which anyone with its link
can read, and the summary links it; the audio is left out. Every run makes its own gist, Retry
too: naming a speaker changes the transcript. The transcript and the summary go to gh on its
stdin, never as an argument.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

from minutas import agaric, store

ACTION = "publish_github"
GH = "gh"
# --body-file for gh issue comment and gh issue create, and --filename for gh gist create, are
# all in 2.45.0, Ubuntu 24.04's own gh.
MINIMUM = (2, 45, 0)
INSTALL = (
    f"install gh {'.'.join(map(str, MINIMUM))} or newer: `sudo apt install gh`, or see "
    "https://cli.github.com"
)
OLDER = f"older than {'.'.join(map(str, MINIMUM))}: {INSTALL}"
GH_MISSING = f"gh not found: {INSTALL}, then log in with `gh auth login`"
UNEXPECTED = "GitHub answered without a link: see Details"
AUDIO_LEFT_OUT = "Published without the audio: GitHub takes no audio files"

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
PATH = re.compile(r"/([^/]+/[^/]+)(?:/issues/([0-9]+))?/?")
LINK = re.compile(r"https?://\S+")


class Target(NamedTuple):
    """Where a link says to post: a comment on an issue, or a new issue in a repository."""

    # The link without its query or fragment.
    link: str
    host: str
    # owner/repo
    repository: str
    # The issue's number in its repository; None for a repository link.
    issue: int | None

    @property
    def plan(self) -> str:
        """What publishing to the link does, as the window says it before posting."""
        if self.issue is None:
            return f"Opens a new issue in {self.repository}"
        return f"Comments on issue #{self.issue} of {self.repository}"


class GitHubError(Exception):
    pass


def parse_link(link: str) -> Target | None:
    """The issue or repository the link points to; None for anything else."""
    try:
        parts = urlsplit(link.strip())
    except ValueError:
        # A typed "https://[", say: a host that opens an IPv6 address and never closes it.
        return None
    path = PATH.fullmatch(parts.path)
    if parts.scheme not in {"http", "https"} or not parts.netloc or path is None:
        return None
    link = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    issue = path[2]
    return Target(link, parts.netloc, path[1], None if issue is None else int(issue))


def gh_problem() -> str:
    """Why the gh on PATH can't publish, and how to put one that can in its place; empty when
    it can."""
    path = shutil.which(GH)
    if path is None:
        return GH_MISSING
    # The path just found, never a shell string.
    said = subprocess.run(  # noqa: S603
        [path, "--version"], capture_output=True, text=True, check=False
    ).stdout
    version = VERSION.search(said)
    if version is None or tuple(map(int, version.groups())) < MINIMUM:
        return f"The gh at {path} is {OLDER}"
    return ""


def failure(target: Target, stderr: str, returncode: int) -> str:
    host = target.host
    # What gh says on stderr for the failures that have a fix of their own.
    known = (
        (
            "gh auth login",
            f"gh isn't logged in to {host}: run `gh auth login --hostname {host}`, then Retry",
        ),
        (
            "Could not resolve to",
            (
                f"no access to {target.link}: check the link, and that your account on {host} can "
                "see it"
            ),
        ),
        ("error connecting to", f"{host} is out of reach: check the network, then Retry"),
        ("unknown flag: ", f"gh is {OLDER}"),
    )
    for marker, reason in known:
        if marker in stderr:
            return reason
    said = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not said:
        return f"gh exited with status {returncode}"
    # gh gist create says what it is doing first, and why it failed last.
    return said[-1].removeprefix("X Failed to create gist: ")


def gh(target: Target, step: str, arguments: list[str], text: str, log: Path) -> str:
    """Runs gh with text on its stdin; returns the link it printed. The log gets the call and
    everything gh said."""
    command = [GH, *arguments]
    with log.open("a") as out:
        out.write(f"$ {shlex.join(command)}\n")
        try:
            # This module's own argument list, never a shell string; gh is found on PATH. A gist
            # goes to the host GH_HOST names; an issue's link or --repo names its own.
            result = subprocess.run(  # noqa: S603
                command,
                input=text,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "GH_HOST": target.host},
            )
        except FileNotFoundError:
            reason = f"Could not {step}: {GH_MISSING}"
            raise GitHubError(reason) from None
        out.write(f"{result.stdout}{result.stderr}exit status {result.returncode}\n")
    if result.returncode != 0:
        reason = f"Could not {step}: {failure(target, result.stderr, result.returncode)}"
        raise GitHubError(reason)
    printed = result.stdout.strip()
    if LINK.fullmatch(printed) is None:
        reason = f"Could not {step}: {UNEXPECTED}"
        raise GitHubError(reason)
    return printed


def make_gist(recording_id: str, target: Target, log: Path) -> str:
    """Puts the transcript in a secret gist, gh's default; returns its link."""
    transcript = (store.recording_dir(recording_id) / store.TRANSCRIPT).read_text(encoding="utf-8")
    arguments = ["gist", "create", f"--filename={recording_id}.transcript.md", "-"]
    return gh(target, "make the transcript's gist", arguments, transcript, log)


def post(recording_id: str, target: Target, gist: str, log: Path) -> str:
    """Posts the summary, linking the gist; returns the link to the comment or issue."""
    folder = store.recording_dir(recording_id)
    summary = (folder / store.SUMMARY).read_text(encoding="utf-8").strip()
    body = f"{summary}\n\n[{recording_id}.transcript.md]({gist})\n"
    if target.issue is None:
        recording = store.load_recording(recording_id)
        day = datetime.fromisoformat(recording["started_at"]).date()
        arguments = [
            "issue",
            "create",
            f"--repo={target.host}/{target.repository}",
            f"--title={agaric.page_title(recording['name'], day)}",
            "--body-file=-",
        ]
        return gh(target, "open the issue", arguments, body, log)
    arguments = ["issue", "comment", target.link, "--body-file=-"]
    return gh(target, f"comment on issue #{target.issue}", arguments, body, log)


def interrupt(_signal: int, _frame: FrameType | None) -> None:
    raise GitHubError(store.INTERRUPTED)


def publish(recording_id: str, published: datetime, target: Target) -> bool:
    """Publishes the recording's summary to the target, as a run in its log.

    Returns whether the run succeeded. SIGTERM, at logout or shutdown, fails the run as
    interrupted: gh is killed.
    """
    queued_at = published.isoformat(timespec="seconds")
    index, _ = store.start_run(
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
    run = store.Run(
        action=ACTION,
        status="failed",
        queued_at=queued_at,
        error=None,
        details=None,
        output=None,
        link=target.link,
    )
    log = store.run_log(recording_id, index)
    on_term = signal.signal(signal.SIGTERM, interrupt)
    try:
        output = post(recording_id, target, make_gist(recording_id, target, log), log)
    except GitHubError as error:
        run["error"] = str(error)
    else:
        run.update(status="succeeded", output=output, note=AUDIO_LEFT_OUT)
    finally:
        signal.signal(signal.SIGTERM, on_term)
    run["details"] = store.written_log(recording_id, index)
    store.end_run(recording_id, index, run)
    return run["status"] == "succeeded"
