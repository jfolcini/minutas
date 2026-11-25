import json
import signal
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import github, store

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
PUBLISHED = datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC)
HOST = "github.example.com"
REPOSITORY_LINK = f"https://{HOST}/owner/repo"
ISSUE_LINK = f"{REPOSITORY_LINK}/issues/7"
TRANSCRIPT = "[00:00:01] **Me:** Hola.\n"
SUMMARY = "## Decisions\n\n- Ship on Friday.\n"
GIST = f"https://{HOST}/gist/ana/0123456789abcdef"
COMMENT = f"{ISSUE_LINK}#issuecomment-302"
NEW_ISSUE = f"{REPOSITORY_LINK}/issues/8"
GIST_CREATE = ["gh", "gist", "create", f"--filename={FIRST}.transcript.md", "-"]
COMMENT_ON = ["gh", "issue", "comment", ISSUE_LINK, "--body-file=-"]
INSTALL = "install gh 2.45.0 or newer: `sudo apt install gh`, or see https://cli.github.com"
NO_AUDIO = "Published without the audio: GitHub takes no audio files"


@dataclass
class Answer:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Call:
    """A gh call: what it was asked, and what it read on its stdin."""

    kind: str
    argv: list[str]
    stdin: str


class FakeSystem:
    """Stands in for subprocess.run: gh answers each kind of call from its queue of answers,
    then as GitHub does."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.calls: list[Call] = []
        self.answers: dict[str, list[Answer]] = {}
        self.missing = False
        self.version = "gh version 2.45.0 (2024-03-04)\nhttps://github.com/cli/cli/releases\n"
        self.during_call = lambda: None

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if self.missing:
            raise FileNotFoundError(args[0])
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, self.version, "")
        # A gist goes to the host gh is told, which is the link's.
        assert cast("dict[str, str]", kwargs["env"])["GH_HOST"] == HOST
        kind = args[1] if args[1] == "gist" else args[2]
        self.calls.append(Call(kind, args, cast("str", kwargs["input"])))
        self.during_call()
        queue = self.answers.get(kind, [])
        answer = queue.pop(0) if queue else Answer(stdout=f"{USUAL[kind]}\n")
        return subprocess.CompletedProcess(args, answer.returncode, answer.stdout, answer.stderr)


# What gh prints on stdout for each kind of call.
USUAL = {"gist": GIST, "comment": COMMENT, "create": NEW_ISSUE}


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def recording() -> str:
    recording_id = store.create_recording(START)["id"]
    store.mark_stopped(recording_id, START + timedelta(seconds=60), incomplete=False)
    folder = store.recording_dir(recording_id)
    for name in store.TRACKS:
        (folder / name).write_bytes(b"OggS")
    (folder / "transcript.md").write_text(TRANSCRIPT)
    (folder / "summary.md").write_text(SUMMARY)
    return recording_id


def target(link: str = ISSUE_LINK) -> github.Target:
    found = github.parse_link(link)
    assert found is not None
    return found


def runs(recording_id: str) -> list[dict[str, object]]:
    return cast(
        "list[dict[str, object]]",
        json.loads((store.recording_dir(recording_id) / "runs.json").read_text()),
    )


def failed_in_status(recording_id: str) -> object:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return next(entry for entry in status["recent"] if entry["id"] == recording_id)["failed"]


def log_text(recording_id: str) -> str:
    return (store.recording_dir(recording_id) / "run-0.log").read_text()


def body() -> str:
    return f"{SUMMARY.strip()}\n\n[{FIRST}.transcript.md]({GIST})\n"


def test_an_issue_link_gets_the_summary_as_a_comment_linking_a_secret_gist(
    system: FakeSystem,
) -> None:
    recording_id = recording()

    assert github.publish(recording_id, PUBLISHED, target()) is True

    [gist, comment] = system.calls
    # Without --public, gh makes the gist secret; the transcript and summary go on stdin, never
    # as an argument.
    assert (gist.argv, gist.stdin) == (GIST_CREATE, TRANSCRIPT)
    assert (comment.argv, comment.stdin) == (COMMENT_ON, body())
    assert runs(recording_id) == [
        {
            "action": "publish_github",
            "status": "succeeded",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": None,
            "details": "run-0.log",
            "output": COMMENT,
            "link": ISSUE_LINK,
            "note": NO_AUDIO,
        }
    ]
    assert failed_in_status(recording_id) == []


def test_a_repository_link_gets_a_new_issue_titled_with_the_name_and_date(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    store.set_typed_name(recording_id, " Weekly sync ")

    assert github.publish(recording_id, PUBLISHED, target(REPOSITORY_LINK)) is True

    [_, issue] = system.calls
    assert (issue.argv, issue.stdin) == (
        [
            "gh",
            "issue",
            "create",
            f"--repo={HOST}/owner/repo",
            "--title=Weekly sync 2026-11-18",
            "--body-file=-",
        ],
        body(),
    )
    run = runs(recording_id)[0]
    assert (run["output"], run["link"]) == (NEW_ISSUE, REPOSITORY_LINK)


def test_retry_makes_a_new_gist_of_the_transcript_there_is_now(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["comment"] = [Answer(1, stderr="HTTP 502: Bad Gateway\n")]
    assert github.publish(recording_id, PUBLISHED, target()) is False
    # A speaker named before Retry.
    renamed = "[00:00:01] **Ana:** Hola.\n"
    (store.recording_dir(recording_id) / "transcript.md").write_text(renamed)

    assert github.publish(recording_id, PUBLISHED, target()) is True

    assert [(call.kind, call.stdin) for call in system.calls] == [
        ("gist", TRANSCRIPT),
        ("comment", body()),
        ("gist", renamed),
        ("comment", body()),
    ]


NOT_LOGGED_IN = f"gh isn't logged in to {HOST}: run `gh auth login --hostname {HOST}`, then Retry"
NO_ACCESS = f"no access to {ISSUE_LINK}: check the link, and that your account on {HOST} can see it"


@pytest.mark.parametrize(
    ("kind", "answer", "error"),
    [
        (
            "gist",
            Answer(
                4,
                stderr="To get started with GitHub CLI, please run:  gh auth login\n"
                "Alternatively, populate the GH_TOKEN environment variable with a GitHub API "
                "authentication token.\n",
            ),
            f"Could not make the transcript's gist: {NOT_LOGGED_IN}",
        ),
        (
            "comment",
            Answer(
                1,
                stderr=f"HTTP 401: Bad credentials (https://{HOST}/api/graphql)\n"
                "Try authenticating with:  gh auth login\n",
            ),
            f"Could not comment on issue #7: {NOT_LOGGED_IN}",
        ),
        (
            "comment",
            Answer(
                1,
                stderr="GraphQL: Could not resolve to an issue or pull request with the number "
                "of 7. (repository.issue)\n",
            ),
            f"Could not comment on issue #7: {NO_ACCESS}",
        ),
        (
            "gist",
            Answer(
                1,
                stderr=f"- Creating gist {FIRST}.transcript.md\nerror connecting to {HOST}\n"
                "check your internet connection or https://githubstatus.com\n",
            ),
            (
                f"Could not make the transcript's gist: {HOST} is out of reach: check the "
                "network, then Retry"
            ),
        ),
        (
            "gist",
            Answer(
                1,
                stderr=f"- Creating gist {FIRST}.transcript.md\n"
                "X Failed to create gist: HTTP 500: Server Error (https://api.github.com/gists)\n",
            ),
            (
                "Could not make the transcript's gist: HTTP 500: Server Error "
                "(https://api.github.com/gists)"
            ),
        ),
        (
            "comment",
            Answer(1, stderr="unknown flag: --body-file\n\nUsage:  gh issue comment [flags]\n"),
            f"Could not comment on issue #7: gh is older than 2.45.0: {INSTALL}",
        ),
        ("comment", Answer(2), "Could not comment on issue #7: gh exited with status 2"),
    ],
    ids=["logged out", "unknown to host", "no issue", "offline", "server error", "old", "quiet"],
)
def test_a_gh_failure_fails_the_run_at_its_step_with_the_fix(
    system: FakeSystem, kind: str, answer: Answer, error: str
) -> None:
    recording_id = recording()
    system.answers[kind] = [answer]

    assert github.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"], run["output"]) == (
        "failed",
        error,
        "run-0.log",
        None,
    )
    assert system.calls[-1].kind == kind
    assert answer.stderr in log_text(recording_id)
    assert failed_in_status(recording_id) == ["publish_github"]


def test_a_new_issue_failure_names_its_step(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["create"] = [
        Answer(1, stderr="GraphQL: Could not resolve to a Repository with the name 'owner/repo'.\n")
    ]

    github.publish(recording_id, PUBLISHED, target(REPOSITORY_LINK))

    assert runs(recording_id)[0]["error"] == (
        f"Could not open the issue: no access to {REPOSITORY_LINK}: check the link, and that your "
        f"account on {HOST} can see it"
    )


@pytest.mark.parametrize(
    ("kind", "stdout", "step"),
    [
        ("gist", "", "make the transcript's gist"),
        ("gist", "Opening gist in your browser.\n", "make the transcript's gist"),
        ("comment", "\n", "comment on issue #7"),
    ],
)
def test_an_answer_without_a_link_fails_the_run(
    system: FakeSystem, kind: str, stdout: str, step: str
) -> None:
    recording_id = recording()
    system.answers[kind] = [Answer(stdout=stdout)]

    assert github.publish(recording_id, PUBLISHED, target()) is False

    assert runs(recording_id)[0]["error"] == (
        f"Could not {step}: GitHub answered without a link: see Details"
    )


def test_a_missing_gh_fails_the_run(system: FakeSystem) -> None:
    recording_id = recording()
    system.missing = True

    assert github.publish(recording_id, PUBLISHED, target()) is False

    assert runs(recording_id)[0]["error"] == (
        f"Could not make the transcript's gist: gh not found: {INSTALL}, then log in with "
        "`gh auth login`"
    )


def test_sigterm_fails_the_run_as_interrupted(system: FakeSystem) -> None:
    recording_id = recording()
    before = signal.getsignal(signal.SIGTERM)

    def logout() -> None:
        if system.calls[-1].kind == "comment":
            signal.raise_signal(signal.SIGTERM)

    system.during_call = logout

    assert github.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"]) == ("failed", "interrupted")
    assert signal.getsignal(signal.SIGTERM) == before


def test_the_log_holds_each_call_and_what_gh_said(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["comment"] = [Answer(1, "partial", "HTTP 422: Validation Failed\n")]

    github.publish(recording_id, PUBLISHED, target())

    assert log_text(recording_id) == (
        f"$ gh gist create --filename={FIRST}.transcript.md -\n"
        f"{GIST}\nexit status 0\n"
        f"$ gh issue comment {ISSUE_LINK} --body-file=-\n"
        "partialHTTP 422: Validation Failed\nexit status 1\n"
    )


@pytest.mark.parametrize(
    ("link", "found"),
    [
        (ISSUE_LINK, (ISSUE_LINK, HOST, "owner/repo", 7)),
        (REPOSITORY_LINK, (REPOSITORY_LINK, HOST, "owner/repo", None)),
        # As a browser shows a comment.
        (f" {ISSUE_LINK}#issuecomment-12 ", (ISSUE_LINK, HOST, "owner/repo", 7)),
        (
            "http://github.com/a/b/issues/42/?q=1",
            ("http://github.com/a/b/issues/42", "github.com", "a/b", 42),
        ),
        ("https://github.com/a/b/", ("https://github.com/a/b", "github.com", "a/b", None)),
    ],
)
def test_parse_link_finds_the_issue_or_repository(
    link: str, found: tuple[str, str, str, int | None]
) -> None:
    assert github.parse_link(link) == found


@pytest.mark.parametrize(
    "link",
    [
        "",
        "github.com/a/b",
        "ftp://github.com/a/b",
        "https:///a/b",
        # An account, or a page of a repository that is neither an issue nor the repository.
        "https://github.com/owner",
        "https://github.com/a/b/pull/3",
        "https://github.com/a/b/issues",
        "https://github.com/a/b/issues/new",
        "https://github.com/a/b/tree/main",
        "https://[github.com/a/b",
    ],
)
def test_parse_link_refuses_anything_else(link: str) -> None:
    assert github.parse_link(link) is None


SEGMENT = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,8}", fullmatch=True)


@given(
    owner=SEGMENT,
    repo=SEGMENT,
    issue=st.none() | st.integers(min_value=1, max_value=10**6),
)
def test_a_link_parses_to_its_repository_and_issue(
    owner: str, repo: str, issue: int | None
) -> None:
    link = f"https://{HOST}/{owner}/{repo}" + ("" if issue is None else f"/issues/{issue}")

    found = github.parse_link(link)

    assert found is not None
    assert found == (link, HOST, f"{owner}/{repo}", issue)
    assert found.plan == (
        f"Opens a new issue in {owner}/{repo}"
        if issue is None
        else f"Comments on issue #{issue} of {owner}/{repo}"
    )


@pytest.fixture
def gh_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "gh").write_text("#!/bin/sh\nexit 1\n")
    (folder / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return folder / "gh"


@pytest.mark.usefixtures("gh_on_path")
@pytest.mark.parametrize(
    "said", ["gh version 2.45.0 (2024-03-04)\n", "gh version 2.89.0 (2026-03-26)\nhttps://x\n"]
)
def test_a_gh_new_enough_can_publish(system: FakeSystem, said: str) -> None:
    system.version = said

    assert github.gh_problem() == ""


@pytest.mark.parametrize("said", ["gh version 2.44.9 (2024-02-01)\n", "gh version 1.14.0\n", ""])
def test_an_older_gh_says_to_install_a_newer_one(
    system: FakeSystem, gh_on_path: Path, said: str
) -> None:
    system.version = said

    assert github.gh_problem() == f"The gh at {gh_on_path} is older than 2.45.0: {INSTALL}"
    assert system.commands == [[str(gh_on_path), "--version"]]


def test_without_gh_it_says_how_to_install_it(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    assert github.gh_problem() == f"gh not found: {INSTALL}, then log in with `gh auth login`"
    assert system.commands == []
