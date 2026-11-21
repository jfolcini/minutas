import json
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import gitlab, settings, store

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
PUBLISHED = datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC)
HOST = "gitlab.example.com"
PROJECT_LINK = f"https://{HOST}/group/sub/project"
ISSUE_LINK = f"{PROJECT_LINK}/-/issues/7"
PROJECTS = "projects/group%2Fsub%2Fproject"
UPLOAD_FOLDER = "66dbcd21ec5d24ed6ea225176098d52b"
MIX = b"OggS mixed"
TRANSCRIPT = "[00:00:01] **Me:** Hola.\n"
SUMMARY = "## Decisions\n\n- Ship on Friday.\n"
NEW_ISSUE = f"{PROJECT_LINK}/-/issues/8"
GLAB_API = ["glab", "api", f"--hostname={HOST}", "--method=POST"]
INSTALL = (
    "install glab 1.91.0 or newer from the .deb on https://gitlab.com/gitlab-org/cli/-/releases"
)


def uploaded(name: str) -> str:
    """GitLab's Markdown link to a file uploaded to the project."""
    return f"[{name}](/uploads/{UPLOAD_FOLDER}/{name})"


TRANSCRIPT_LINK = uploaded(f"{FIRST}.transcript.md")
AUDIO_LINK = uploaded(f"{FIRST}.opus")


@dataclass
class Answer:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Call:
    """A glab api call: what it posts to, and each file it uploads, read while it ran."""

    kind: str
    argv: list[str]
    files: dict[str, bytes] = field(default_factory=dict)


class FakeSystem:
    """Stands in for subprocess.run: glab answers each kind of call from its queue of answers,
    then as GitLab does; gst-launch-1.0 writes the mixed file it was asked for."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.calls: list[Call] = []
        self.answers: dict[str, list[Answer]] = {}
        self.missing: set[str] = set()
        self.gst = Answer()
        self.version = "glab 1.91.0 (e60b4a84)\n"
        self.during_call = lambda: None

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if Path(args[0]).name in self.missing:
            raise FileNotFoundError(args[0])
        if args[1:] == ["--version"]:
            return subprocess.CompletedProcess(args, 0, self.version, "")
        if args[0] == "gst-launch-1.0":
            if self.gst.returncode == 0:
                location = args[args.index("filesink") + 1].removeprefix("location=")
                (cast("Path", kwargs["cwd"]) / location).write_bytes(MIX)
            return subprocess.CompletedProcess(
                args, self.gst.returncode, self.gst.stdout, self.gst.stderr
            )
        # Nothing glab could wait on: an endpoint or a field can't make it read standard input.
        assert kwargs.get("stdin") == subprocess.DEVNULL
        files = {
            Path(path).name: Path(path).read_bytes()
            for argument in args
            if (path := argument.removeprefix("--form=file=@")) != argument
        }
        kind = kind_of(args[4], files)
        self.calls.append(Call(kind, args, files))
        self.during_call()
        queue = self.answers.get(kind, [])
        answer = queue.pop(0) if queue else Answer(stdout=json.dumps(usual(kind, files)))
        return subprocess.CompletedProcess(args, answer.returncode, answer.stdout, answer.stderr)


def kind_of(endpoint: str, files: dict[str, bytes]) -> str:
    if endpoint.endswith("/uploads"):
        [name] = files
        return "transcript" if name.endswith(".transcript.md") else "audio"
    return "notes" if endpoint.endswith("/notes") else "issues"


def usual(kind: str, files: dict[str, bytes]) -> dict[str, object]:
    """What GitLab answers each kind of call with."""
    if kind in {"transcript", "audio"}:
        [name] = files
        return {
            "id": 5,
            "alt": name,
            "url": f"/uploads/{UPLOAD_FOLDER}/{name}",
            "full_path": f"/-/project/42/uploads/{UPLOAD_FOLDER}/{name}",
            "markdown": uploaded(name),
        }
    if kind == "notes":
        return {"id": 302, "type": None, "body": "…", "system": False}
    return {"id": 1001, "iid": 8, "title": "…", "web_url": NEW_ISSUE}


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def recording(tracks: tuple[str, ...] = store.TRACKS) -> str:
    recording_id = store.create_recording(START)["id"]
    store.mark_stopped(recording_id, START + timedelta(seconds=60), incomplete=False)
    folder = store.recording_dir(recording_id)
    for name in tracks:
        (folder / name).write_bytes(b"OggS")
    (folder / "transcript.md").write_text(TRANSCRIPT)
    (folder / "summary.md").write_text(SUMMARY)
    return recording_id


def target(link: str = ISSUE_LINK) -> gitlab.Target:
    found = gitlab.parse_link(link)
    assert found is not None
    return found


def gitlab_run(status: str, link: str = ISSUE_LINK, **changes: object) -> dict[str, object]:
    return {
        "action": "publish_gitlab",
        "status": status,
        "queued_at": "2026-11-18T13:30:00+00:00",
        "error": None if status == "succeeded" else "boom",
        "details": "run-0.log",
        "output": f"{link}#note_1" if status == "succeeded" else None,
        "link": link,
        **changes,
    }


def put_runs(recording_id: str, runs: list[dict[str, object]]) -> None:
    (store.recording_dir(recording_id) / "runs.json").write_text(json.dumps(runs))


def runs(recording_id: str) -> list[dict[str, object]]:
    return cast(
        "list[dict[str, object]]",
        json.loads((store.recording_dir(recording_id) / "runs.json").read_text()),
    )


def failed_in_status(recording_id: str) -> object:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return next(entry for entry in status["recent"] if entry["id"] == recording_id)["failed"]


def log_text(recording_id: str, name: str = "run-0.log") -> str:
    return (store.recording_dir(recording_id) / name).read_text()


def body(*links: str) -> str:
    return "\n\n".join([SUMMARY.strip(), *links]) + "\n"


def test_an_issue_link_gets_the_summary_as_a_comment_linking_the_uploads(
    system: FakeSystem,
) -> None:
    recording_id = recording()

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    [transcript, audio, comment] = system.calls
    copy = Path(transcript.argv[-1].removeprefix("--form=file=@"))
    assert transcript.argv == [*GLAB_API, f"{PROJECTS}/uploads", f"--form=file=@{copy}"]
    # Named after the recording, in a folder of its own inside the recording's.
    assert (copy.name, copy.parent.parent) == (
        f"{FIRST}.transcript.md",
        store.recording_dir(recording_id),
    )
    assert transcript.files == {f"{FIRST}.transcript.md": TRANSCRIPT.encode()}
    assert audio.argv[:5] == [*GLAB_API, f"{PROJECTS}/uploads"]
    assert audio.files == {f"{FIRST}.opus": MIX}
    assert comment.argv == [
        *GLAB_API,
        f"{PROJECTS}/issues/7/notes",
        f"--raw-field=body={body(TRANSCRIPT_LINK, AUDIO_LINK)}",
    ]
    assert system.commands[1][0] == "gst-launch-1.0"
    # The copies made for GitLab are gone.
    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "call.opus",
        "mic.opus",
        "recording.json",
        "run-0.log",
        "runs.json",
        "summary.md",
        "transcript.md",
    ]
    assert runs(recording_id) == [
        {
            "action": "publish_gitlab",
            "status": "succeeded",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": None,
            "details": "run-0.log",
            "output": f"{ISSUE_LINK}#note_302",
            "link": ISSUE_LINK,
            "uploads": {"transcript": TRANSCRIPT_LINK, "audio": AUDIO_LINK},
        }
    ]
    assert failed_in_status(recording_id) == []


def test_a_project_link_gets_a_new_issue_titled_with_the_name_and_date(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    store.set_typed_name(recording_id, " Weekly sync ")

    assert gitlab.publish(recording_id, PUBLISHED, target(PROJECT_LINK)) is True

    assert system.calls[-1].argv == [
        *GLAB_API,
        f"{PROJECTS}/issues",
        "--raw-field=title=Weekly sync 2026-11-18",
        f"--raw-field=description={body(TRANSCRIPT_LINK, AUDIO_LINK)}",
    ]
    run = runs(recording_id)[0]
    assert (run["output"], run["link"]) == (NEW_ISSUE, PROJECT_LINK)


def test_without_tracks_the_summary_links_the_transcript_alone_and_says_so(
    system: FakeSystem,
) -> None:
    recording_id = recording(tracks=())

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "notes"]
    assert system.calls[-1].argv[-1] == f"--raw-field=body={body(TRANSCRIPT_LINK)}"
    run = runs(recording_id)[0]
    assert (run["status"], run.get("note")) == (
        "succeeded",
        "Published without the audio: its tracks were deleted",
    )


LEFT_OUT = f"The audio was left out: it is over {HOST}'s upload limit"
TOO_BIG = Answer(1, "<html>413 Request Entity Too Large</html>", "glab: HTTP 413\n")


def test_audio_over_the_servers_limit_is_left_out_and_the_run_succeeds_with_a_note(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    system.answers["audio"] = [TOO_BIG]

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "audio", "notes"]
    assert system.calls[-1].argv[-1] == f"--raw-field=body={body(TRANSCRIPT_LINK)}"
    run = runs(recording_id)[0]
    assert (run["status"], run.get("note"), run.get("uploads")) == (
        "succeeded",
        LEFT_OUT,
        {"transcript": TRANSCRIPT_LINK},
    )
    assert settings.refused_audio_size(HOST) == len(MIX)


def test_audio_as_big_as_one_the_host_refused_is_left_out_without_uploading(
    system: FakeSystem,
) -> None:
    settings.refuse_audio_size(HOST, len(MIX))
    recording_id = recording()

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "notes"]
    assert runs(recording_id)[0].get("note") == LEFT_OUT


def test_audio_smaller_than_the_smallest_the_host_refused_is_uploaded(system: FakeSystem) -> None:
    settings.refuse_audio_size(HOST, len(MIX) + 1)
    recording_id = recording()

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "audio", "notes"]
    assert runs(recording_id)[0].get("note") is None


def test_another_hosts_refusal_does_not_leave_the_audio_out(system: FakeSystem) -> None:
    settings.refuse_audio_size("other.example.com", 1)
    recording_id = recording()

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "audio", "notes"]


@pytest.mark.parametrize(
    ("uploads", "calls"),
    [
        ({"transcript": "[t](/uploads/1/t)"}, ["transcript", "audio", "notes"]),
        (
            {"transcript": "[t](/uploads/1/t)", "audio": "[a](/uploads/1/a)"},
            ["transcript", "notes"],
        ),
    ],
)
def test_retry_to_the_same_project_carries_the_audio_over_and_uploads_the_transcript_again(
    system: FakeSystem, uploads: dict[str, str], calls: list[str]
) -> None:
    recording_id = recording()
    put_runs(recording_id, [gitlab_run("failed", uploads=uploads)])

    # Another issue of the same project: the uploads are the project's.
    assert gitlab.publish(recording_id, PUBLISHED, target(f"{PROJECT_LINK}/-/issues/9")) is True

    assert [call.kind for call in system.calls] == calls
    # The transcript may have changed since, a speaker named, say.
    links = {"transcript": TRANSCRIPT_LINK, "audio": uploads.get("audio", AUDIO_LINK)}
    assert system.calls[-1].argv[-1] == (
        f"--raw-field=body={body(links['transcript'], links['audio'])}"
    )
    run = runs(recording_id)[1]
    assert (run["uploads"], run.get("note")) == (links, None)
    assert ("gst-launch-1.0" in [command[0] for command in system.commands]) is ("audio" in calls)


@pytest.mark.parametrize(
    "before",
    [
        # Another project's uploads are no use to this one.
        [gitlab_run("failed", f"https://{HOST}/group/other/-/issues/7", uploads={"audio": "x"})],
        [
            gitlab_run(
                "failed", "https://other.example.com/group/sub/project", uploads={"audio": "x"}
            )
        ],
        # Only a failed run is carried on: publishing again posts what is there now.
        [gitlab_run("succeeded", uploads={"audio": "x"})],
        [gitlab_run("failed", uploads={"audio": "x"}), gitlab_run("succeeded")],
        [{**gitlab_run("failed", uploads={"audio": "x"}), "action": "publish_agaric"}],
    ],
    ids=["other project", "other server", "after a success", "after a later success", "agaric"],
)
def test_everything_is_uploaded_again_unless_it_retries_a_failure_to_the_project(
    system: FakeSystem, before: list[dict[str, object]]
) -> None:
    recording_id = recording()
    put_runs(recording_id, before)

    assert gitlab.publish(recording_id, PUBLISHED, target()) is True

    assert [call.kind for call in system.calls] == ["transcript", "audio", "notes"]


NOT_LOGGED_IN = (
    f"glab isn't logged in to {HOST}: run `glab auth login --hostname {HOST}`, then Retry"
)
NO_ACCESS = f"no access to {ISSUE_LINK}: check the link, and that your account on {HOST} can see it"
# What glab says, to a pipe, when it can't reach the server.
UNREACHABLE = (
    f'Post "https://{HOST}/api/v4/projects/group%2Fsub%2Fproject/uploads": dial tcp: lookup '
    f"{HOST}: no such host\n"
)


@pytest.mark.parametrize(
    ("kind", "answer", "error", "kept"),
    [
        (
            "transcript",
            Answer(1, '{"message":"401 Unauthorized"}', "glab: 401 Unauthorized (HTTP 401)\n"),
            f"Could not upload the transcript: {NOT_LOGGED_IN}",
            {},
        ),
        (
            "transcript",
            Answer(1, stderr="glab: 404 Project Not Found (HTTP 404)\n"),
            f"Could not upload the transcript: {NO_ACCESS}",
            {},
        ),
        (
            "notes",
            Answer(1, stderr="glab: 403 Forbidden (HTTP 403)\n"),
            f"Could not comment on issue #7: {NO_ACCESS}",
            {"transcript": TRANSCRIPT_LINK, "audio": AUDIO_LINK},
        ),
        (
            "transcript",
            Answer(1, "<html>413 Request Entity Too Large</html>", "glab: HTTP 413\n"),
            f"Could not upload the transcript: it is over {HOST}'s upload limit",
            {},
        ),
        (
            "audio",
            Answer(1, stderr="glab: 500 Internal Server Error (HTTP 500)\n"),
            "Could not upload the audio: 500 Internal Server Error (HTTP 500)",
            {"transcript": TRANSCRIPT_LINK},
        ),
        (
            "transcript",
            Answer(1, stderr=UNREACHABLE),
            (
                f"Could not upload the transcript: {HOST} is out of reach: check the network, then "
                "Retry"
            ),
            {},
        ),
        (
            "transcript",
            Answer(1, stderr="unknown flag: --form\n\nUsage:  glab api <endpoint> [flags]\n"),
            f"Could not upload the transcript: glab is older than 1.91.0: {INSTALL}",
            {},
        ),
        (
            "notes",
            Answer(1, stderr='glab: Note {:note=>["is too long"]} (HTTP 400)\n'),
            'Could not comment on issue #7: Note {:note=>["is too long"]} (HTTP 400)',
            {"transcript": TRANSCRIPT_LINK, "audio": AUDIO_LINK},
        ),
        (
            "transcript",
            # A snap's glab is confined to the home folder's visible files.
            Answer(
                1,
                stderr=f'Post "https://{HOST}/api/v4/projects/group%2Fsub%2Fproject/uploads": '
                f"open /home/u/.local/share/minutas/recordings/{FIRST}/.publish-x/"
                f"{FIRST}.transcript.md: permission denied\n",
            ),
            (
                "Could not upload the transcript: a glab installed as a snap can't read the "
                f"recordings: {INSTALL}"
            ),
            {},
        ),
        (
            "transcript",
            Answer(1, stderr="\nerror parsing --hostname: invalid hostname\n"),
            "Could not upload the transcript: error parsing --hostname: invalid hostname",
            {},
        ),
        ("transcript", Answer(2), "Could not upload the transcript: glab exited with status 2", {}),
    ],
    ids=[
        "logged out",
        "no project",
        "forbidden",
        "transcript too big",
        "audio server error",
        "offline",
        "old glab",
        "refused",
        "snap",
        "other",
        "silent",
    ],
)
def test_a_glab_failure_fails_the_run_at_its_step_with_the_fix(
    system: FakeSystem, kind: str, answer: Answer, error: str, kept: dict[str, str]
) -> None:
    recording_id = recording()
    system.answers[kind] = [answer]

    assert gitlab.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"], run["output"]) == (
        "failed",
        error,
        "run-0.log",
        None,
    )
    # What it finished is kept, so Retry carries on from the step that broke.
    assert run.get("uploads", {}) == kept
    assert system.calls[-1].kind == kind
    assert answer.stderr in log_text(recording_id)
    assert failed_in_status(recording_id) == ["publish_gitlab"]


def test_a_new_issue_failure_names_its_step(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["issues"] = [Answer(1, stderr="glab: 403 Forbidden (HTTP 403)\n")]

    gitlab.publish(recording_id, PUBLISHED, target(PROJECT_LINK))

    assert runs(recording_id)[0]["error"] == (
        f"Could not open the issue: no access to {PROJECT_LINK}: check the link, and that your "
        f"account on {HOST} can see it"
    )


@pytest.mark.parametrize(
    ("kind", "stdout"),
    [
        ("transcript", "not json"),
        ("transcript", '{"url": "/uploads/1/t"}'),
        ("audio", "[]"),
        ("notes", '{"body": "…"}'),
        ("notes", '{"id": null}'),
    ],
)
def test_an_answer_without_its_link_fails_the_run(
    system: FakeSystem, kind: str, stdout: str
) -> None:
    recording_id = recording()
    system.answers[kind] = [Answer(stdout=stdout)]

    assert gitlab.publish(recording_id, PUBLISHED, target()) is False

    assert runs(recording_id)[0]["error"] == (
        f"Could not {'comment on issue #7' if kind == 'notes' else f'upload the {kind}'}: "
        "GitLab answered without a link: see Details"
    )


def test_a_new_issue_without_its_link_fails_the_run(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["issues"] = [Answer(stdout='{"iid": 8}')]

    assert gitlab.publish(recording_id, PUBLISHED, target(PROJECT_LINK)) is False

    assert runs(recording_id)[0]["error"] == (
        "Could not open the issue: GitLab answered without a link: see Details"
    )


def test_a_missing_glab_fails_the_run(system: FakeSystem) -> None:
    recording_id = recording()
    system.missing = {"glab"}

    assert gitlab.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["error"], run["details"]) == (
        (
            f"Could not upload the transcript: glab not found: {INSTALL}, then log in with "
            "`glab auth login`"
        ),
        "run-0.log",
    )


def test_a_failed_mix_fails_the_run_and_keeps_the_transcript_upload(system: FakeSystem) -> None:
    recording_id = recording()
    system.gst = Answer(1, stderr="ERROR: Could not decode.\n")

    assert gitlab.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["error"], run["uploads"]) == (
        "Could not mix the tracks: ERROR: Could not decode.",
        {"transcript": TRANSCRIPT_LINK},
    )
    assert [call.kind for call in system.calls] == ["transcript"]


def test_sigterm_fails_the_run_as_interrupted_and_keeps_the_finished_uploads(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    before = signal.getsignal(signal.SIGTERM)

    def logout() -> None:
        if system.calls[-1].kind == "audio":
            signal.raise_signal(signal.SIGTERM)

    system.during_call = logout

    assert gitlab.publish(recording_id, PUBLISHED, target()) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["uploads"]) == (
        "failed",
        "interrupted",
        {"transcript": TRANSCRIPT_LINK},
    )
    assert not [path for path in store.recording_dir(recording_id).iterdir() if path.is_dir()]
    assert signal.getsignal(signal.SIGTERM) == before


def test_the_log_holds_each_call_and_what_glab_said(system: FakeSystem) -> None:
    recording_id = recording(tracks=())
    system.answers["notes"] = [Answer(1, "partial", "glab: 403 Forbidden (HTTP 403)\n")]

    gitlab.publish(recording_id, PUBLISHED, target())

    upload = system.calls[0].argv
    answer = json.dumps(usual("transcript", {f"{FIRST}.transcript.md": b""}))
    assert log_text(recording_id) == (
        f"$ glab api --hostname={HOST} --method=POST {PROJECTS}/uploads {upload[-1]}\n"
        f"{answer}exit status 0\n"
        f"$ glab api --hostname={HOST} --method=POST {PROJECTS}/issues/7/notes "
        f"'--raw-field=body={body(TRANSCRIPT_LINK)}'\n"
        "partialglab: 403 Forbidden (HTTP 403)\nexit status 1\n"
    )


@pytest.mark.parametrize(
    ("link", "found"),
    [
        (ISSUE_LINK, (ISSUE_LINK, HOST, "group/sub/project", 7)),
        (PROJECT_LINK, (PROJECT_LINK, HOST, "group/sub/project", None)),
        # As a browser shows it, and as GitLab now links an issue.
        (f" {ISSUE_LINK}#note_12 ", (ISSUE_LINK, HOST, "group/sub/project", 7)),
        (
            "http://gitlab.example.com/a/b/-/work_items/42/?type=ISSUE",
            ("http://gitlab.example.com/a/b/-/work_items/42", HOST, "a/b", 42),
        ),
        ("https://gitlab.com/a/b/", ("https://gitlab.com/a/b", "gitlab.com", "a/b", None)),
    ],
)
def test_parse_link_finds_the_issue_or_project(
    link: str, found: tuple[str, str, str, int | None]
) -> None:
    assert gitlab.parse_link(link) == found


@pytest.mark.parametrize(
    "link",
    [
        "",
        "gitlab.example.com/a/b",
        "ftp://gitlab.example.com/a/b",
        "https:///a/b",
        # A group, or a page of a project that is neither an issue nor the project.
        "https://gitlab.example.com/group",
        "https://gitlab.example.com/a/b/-/merge_requests/3",
        "https://gitlab.example.com/a/b/-/issues",
        "https://gitlab.example.com/a/b/-/issues/new",
        # A group's epic, which GitLab shows as a work item too.
        "https://gitlab.example.com/groups/g/-/work_items/5",
        "https://[gitlab.example.com/a/b",
    ],
)
def test_parse_link_refuses_anything_else(link: str) -> None:
    assert gitlab.parse_link(link) is None


SEGMENT = st.from_regex(r"[a-z0-9][a-z0-9_.]{0,8}", fullmatch=True)


@given(
    project=st.lists(SEGMENT, min_size=2, max_size=4).map("/".join),
    issue=st.none() | st.integers(min_value=1, max_value=10**6),
)
def test_a_link_parses_to_its_project_and_issue(project: str, issue: int | None) -> None:
    link = f"https://{HOST}/{project}" + ("" if issue is None else f"/-/issues/{issue}")

    found = gitlab.parse_link(link)

    assert found is not None
    assert found == (link, HOST, project, issue)
    assert found.plan == (
        f"Opens a new issue in {project}"
        if issue is None
        else f"Comments on issue #{issue} of {project}"
    )


@pytest.fixture
def glab_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "glab").write_text("#!/bin/sh\nexit 1\n")
    (folder / "glab").chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return folder / "glab"


@pytest.mark.usefixtures("glab_on_path")
@pytest.mark.parametrize("said", ["glab 1.91.0 (e60b4a84)\n", "glab version 1.131.0\n"])
def test_a_glab_new_enough_can_publish(system: FakeSystem, said: str) -> None:
    system.version = said

    assert gitlab.glab_problem() == ""


@pytest.mark.parametrize("said", ["glab version 1.36.0\n", "glab 1.90.9 (abc)\n", ""])
def test_an_older_glab_says_to_install_a_newer_one(
    system: FakeSystem, glab_on_path: Path, said: str
) -> None:
    system.version = said

    assert gitlab.glab_problem() == (f"The glab at {glab_on_path} is older than 1.91.0: {INSTALL}")
    assert system.commands == [[str(glab_on_path), "--version"]]


def test_a_snap_glab_says_to_install_the_deb_instead(
    system: FakeSystem, glab_on_path: Path
) -> None:
    # What /snap/bin/glab is: a link to snap, which runs glab confined to the visible home.
    snap = glab_on_path.with_name("snap")
    glab_on_path.rename(snap)
    glab_on_path.symlink_to(snap)
    system.version = "glab 1.91.0 (e60b4a84)\n"

    assert gitlab.glab_problem() == (
        f"The glab at {glab_on_path} is the snap, which can't read the recordings: {INSTALL}"
    )
    assert system.commands == []


def test_without_glab_it_says_how_to_install_it(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    assert gitlab.glab_problem() == (
        f"glab not found: {INSTALL}, then log in with `glab auth login`"
    )
    assert system.commands == []
