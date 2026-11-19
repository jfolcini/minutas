import json
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import keyring
import pytest
from hypothesis import given
from hypothesis import strategies as st
from keyring.errors import KeyringLocked

from minutas import agaric, settings, store

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
PUBLISHED = datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC)
TOKEN = "agt_fake"  # noqa: S105 - a made-up agent token
SPACE = "01HZWORKSPACE0000000000000"
PAGE = "01JPAGE0000000000000000000"
OTHER_PAGE = "01JOTHER000000000000000000"
MIX = b"OggS mixed"
TAKEN_NEW = (
    "agaric-mcp: Validation error: a page named 'Meeting 2026-11-18' already exists in the "
    "space; add the files to it instead\n"
)
TAKEN_RENAME = (
    "agaric-mcp: Validation error: a page titled 'Weekly sync 2026-11-18' already exists in "
    "this space\n"
)
GST_ERROR = "ERROR: from element /GstPipeline:pipeline0/GstDecodeBin:decodebin0: Could not decode."
GST = [
    "gst-launch-1.0",
    "-q",
    "audiomixer",
    "name=mix",
    "!",
    "audioconvert",
    "!",
    "audioresample",
    "!",
    "audio/x-raw,rate=16000,channels=1",
    "!",
    "opusenc",
    "bitrate=32000",
    "!",
    "oggmux",
    "!",
    "filesink",
    f"location={FIRST}.opus",
]


def track_branch(name: str) -> list[str]:
    return [
        "filesrc",
        f"location=../{name}",
        "!",
        "decodebin",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "mix.",
    ]


@dataclass
class Answer:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Call:
    tool: str
    arguments: dict[str, object]
    # The files the call named, read while it ran.
    files: dict[str, bytes] = field(default_factory=dict)


class FakeSystem:
    """Stands in for subprocess.run: agaric-mcp answers each tool from its queue of answers, then
    with the tool's usual result; gst-launch-1.0 writes the mixed file it was asked for."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.commands: list[list[str]] = []
        self.answers: dict[str, list[Answer]] = {}
        self.missing: set[str] = set()
        self.gst = Answer()
        self.during_call = lambda: None

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        if args[0] in self.missing:
            raise FileNotFoundError(args[0])
        if args[0] == "gst-launch-1.0":
            if self.gst.returncode == 0:
                location = args[args.index("filesink") + 1].removeprefix("location=")
                (cast("Path", kwargs["cwd"]) / location).write_bytes(MIX)
            return subprocess.CompletedProcess(
                args, self.gst.returncode, self.gst.stdout, self.gst.stderr
            )
        assert args[1:4] == ["--token", TOKEN, "call"]
        tool, arguments = args[4], json.loads(args[5])
        self.calls.append(
            Call(
                tool,
                arguments,
                {
                    key: Path(value).read_bytes()
                    for key, value in arguments.items()
                    if key.endswith("_path")
                },
            )
        )
        self.during_call()
        queue = self.answers.get(tool, [])
        answer = queue.pop(0) if queue else Answer(stdout=json.dumps(usual_result(tool)) + "\n")
        return subprocess.CompletedProcess(args, answer.returncode, answer.stdout, answer.stderr)


def usual_result(tool: str) -> object:
    return {
        "add_meeting": {
            "page_id": PAGE,
            "recording_block_id": "01JREC",
            "link_block_id": "01JLINK",
            "attachments": [],
        },
        "update_block_content": {"id": PAGE, "block_type": "page", "content": "renamed"},
        "resolve": [{"name": "taken", "page_id": OTHER_PAGE, "tag_id": None}],
        "list_spaces": [
            {"id": "01HZPERSONAL", "name": "Personal", "is_default": True},
            {"id": SPACE, "name": "Work", "is_default": False},
        ],
    }[tool]


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture
def configured() -> None:
    settings.set_agaric_token(TOKEN)
    settings.set_agaric_space_id(SPACE)


def recording(
    started: datetime = START, tracks: tuple[str, ...] = store.TRACKS, *, transcript: bool = True
) -> str:
    recording_id = store.create_recording(started)["id"]
    store.mark_stopped(recording_id, started + timedelta(seconds=60), incomplete=False)
    folder = store.recording_dir(recording_id)
    for name in tracks:
        (folder / name).write_bytes(b"OggS")
    if transcript:
        (folder / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")
    return recording_id


def succeeded_run(title: str = "Meeting 2026-11-18", page: str = PAGE) -> dict[str, object]:
    return {
        "action": "publish_agaric",
        "status": "succeeded",
        "queued_at": "2026-11-18T13:30:00+00:00",
        "error": None,
        "details": "run-0.log",
        "output": f"agaric://page/{page}",
        "title": title,
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


@pytest.mark.usefixtures("configured")
def test_publish_makes_the_page_with_the_mixed_tracks_and_the_transcript(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    folder = store.recording_dir(recording_id)

    assert agaric.publish(recording_id, PUBLISHED) is True

    [check, call] = system.calls
    audio = Path(cast("str", call.arguments["audio_path"]))
    transcript = Path(cast("str", call.arguments["transcript_path"]))
    assert (check.tool, check.arguments, call.tool) == ("list_spaces", {}, "add_meeting")
    assert call.arguments == {
        "space_id": SPACE,
        "title": "Meeting",
        "date": "2026-11-18",
        "audio_path": str(audio),
        "transcript_path": str(transcript),
    }
    assert (audio.name, transcript.name) == (f"{FIRST}.opus", f"{FIRST}.md")
    assert audio.parent.parent == folder
    assert transcript.parent == audio.parent
    assert call.files == {
        "audio_path": MIX,
        "transcript_path": b"[00:00:01] **Me:** Hola.\n",
    }
    assert system.commands[1] == [*GST, *track_branch("mic.opus"), *track_branch("call.opus")]
    assert not audio.parent.exists()
    assert runs(recording_id) == [
        {
            "action": "publish_agaric",
            "status": "succeeded",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": None,
            "details": "run-0.log",
            "output": f"agaric://page/{PAGE}",
            "title": "Meeting 2026-11-18",
            "recording_block": "01JREC",
        }
    ]
    assert failed_in_status(recording_id) == []


@pytest.mark.usefixtures("configured")
def test_publish_mixes_only_the_tracks_that_hold_audio(system: FakeSystem) -> None:
    recording_id = recording(tracks=("call.opus",))

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert system.commands[1] == [*GST, *track_branch("call.opus")]


@pytest.mark.usefixtures("configured")
def test_publish_without_tracks_sends_the_transcript_alone_and_says_so(
    system: FakeSystem,
) -> None:
    recording_id = recording(tracks=())

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert sorted(system.calls[1].arguments) == ["date", "space_id", "title", "transcript_path"]
    assert [args[0] for args in system.commands] == ["agaric-mcp", "agaric-mcp"]
    run = runs(recording_id)[0]
    assert (run["status"], run.get("note")) == (
        "succeeded",
        "Published without the audio: its tracks were deleted",
    )


@pytest.mark.usefixtures("configured")
def test_publish_without_a_transcript_sends_the_audio_alone(system: FakeSystem) -> None:
    recording_id = recording(transcript=False)

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert sorted(system.calls[1].arguments) == ["audio_path", "date", "space_id", "title"]


@pytest.mark.usefixtures("configured")
def test_publish_dates_the_page_by_the_local_start_date(system: FakeSystem) -> None:
    # Half past midnight in Madrid is still the day before in UTC.
    recording_id = recording(datetime(2026, 11, 18, 0, 30, tzinfo=timezone(timedelta(hours=1))))

    agaric.publish(recording_id, PUBLISHED)

    assert system.calls[1].arguments["date"] == "2026-11-18"
    assert runs(recording_id)[0]["title"] == "Meeting 2026-11-18"


@pytest.mark.usefixtures("configured")
def test_publish_names_the_page_by_the_recordings_name(system: FakeSystem) -> None:
    recording_id = recording()
    store.set_typed_name(recording_id, "Weekly sync")

    agaric.publish(recording_id, PUBLISHED)

    assert system.calls[1].arguments["title"] == "Weekly sync"
    assert runs(recording_id)[0]["title"] == "Weekly sync 2026-11-18"


@pytest.mark.usefixtures("configured")
def test_publish_again_with_the_same_name_changes_nothing(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run()])

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert system.commands == []
    assert runs(recording_id)[1] == {
        **succeeded_run(),
        "queued_at": "2026-11-18T14:00:00+00:00",
        "details": None,
    }


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(
    "rename",
    [
        lambda recording_id: store.set_typed_name(recording_id, "Weekly sync"),
        lambda recording_id: store.set_calendar_event(recording_id, "Weekly sync", []),
    ],
    ids=["typed", "calendar"],
)
def test_publish_again_after_a_rename_renames_the_page(
    system: FakeSystem, rename: Callable[[str], None]
) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run()])
    rename(recording_id)

    assert agaric.publish(recording_id, PUBLISHED) is True

    [call] = system.calls
    assert (call.tool, call.arguments) == (
        "update_block_content",
        {"space_id": SPACE, "block_id": PAGE, "content": "Weekly sync 2026-11-18"},
    )
    assert runs(recording_id)[1] == {
        **succeeded_run(title="Weekly sync 2026-11-18"),
        "queued_at": "2026-11-18T14:00:00+00:00",
        "details": "run-1.log",
    }


@pytest.mark.usefixtures("configured")
def test_publish_again_renames_the_page_the_latest_success_left(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(
        recording_id,
        [
            succeeded_run(),
            succeeded_run(title="Standup 2026-11-18", page=OTHER_PAGE),
            {**succeeded_run(), "status": "failed", "output": None},
        ],
    )
    store.set_typed_name(recording_id, "Weekly sync")

    agaric.publish(recording_id, PUBLISHED)

    assert system.calls[0].arguments["block_id"] == OTHER_PAGE


SUMMARY = "## Decisions\n\n- Ship on Friday.\n"


def summarize_run(status: str = "succeeded") -> dict[str, object]:
    return {
        "action": "summarize",
        "status": status,
        "queued_at": "2026-11-18T13:40:00+00:00",
        "error": None if status == "succeeded" else "boom",
        "details": "run-1.log",
        "output": "summary.md" if status == "succeeded" else None,
        "prompt": "Summarize this meeting.",
    }


def with_summary(recording_id: str, text: str = SUMMARY) -> None:
    (store.recording_dir(recording_id) / "summary.md").write_text(text)


@pytest.mark.usefixtures("configured")
def test_publish_puts_the_summary_on_the_new_page(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [summarize_run("failed"), summarize_run()])
    with_summary(recording_id)

    assert agaric.publish(recording_id, PUBLISHED) is True

    call = system.calls[1]
    assert (call.tool, call.arguments.get("summary_markdown")) == ("add_meeting", SUMMARY)
    assert sorted(call.arguments) == [
        "audio_path",
        "date",
        "space_id",
        "summary_markdown",
        "title",
        "transcript_path",
    ]
    assert runs(recording_id)[2].get("summary_run") == 1


@pytest.mark.usefixtures("configured")
def test_publish_to_a_page_adds_the_summary_with_the_files(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [summarize_run()])
    with_summary(recording_id)

    agaric.publish(recording_id, PUBLISHED, page=OTHER_PAGE)

    call = system.calls[1]
    assert (call.arguments["page"], call.arguments.get("summary_markdown")) == (
        OTHER_PAGE,
        SUMMARY,
    )
    assert runs(recording_id)[1].get("summary_run") == 0
    # The block that holds this recording's files, which need not be the page's first.
    assert runs(recording_id)[1].get("recording_block") == "01JREC"


@pytest.mark.usefixtures("configured")
def test_publish_again_after_summarize_adds_the_summary_and_the_proposed_name(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    put_runs(recording_id, [{**succeeded_run(), "recording_block": "01JMINE"}, summarize_run()])
    with_summary(recording_id)
    store.set_summary_name(recording_id, "Release plan")

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert [(call.tool, call.arguments) for call in system.calls] == [
        (
            "update_block_content",
            {"space_id": SPACE, "block_id": PAGE, "content": "Release plan 2026-11-18"},
        ),
        ("add_meeting", {"space_id": SPACE, "block_id": "01JMINE", "summary_markdown": SUMMARY}),
    ]
    assert runs(recording_id)[2] == {
        **succeeded_run(title="Release plan 2026-11-18"),
        "queued_at": "2026-11-18T14:00:00+00:00",
        "details": "run-2.log",
        "recording_block": "01JMINE",
        "summary_run": 1,
    }


@pytest.mark.usefixtures("configured")
def test_a_page_published_before_its_block_was_kept_gets_the_summary_by_page(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run(), summarize_run()])
    with_summary(recording_id)

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert [(call.tool, call.arguments) for call in system.calls] == [
        ("add_meeting", {"space_id": SPACE, "page": PAGE, "summary_markdown": SUMMARY}),
    ]
    assert "recording_block" not in runs(recording_id)[2]


@pytest.mark.usefixtures("configured")
def test_publish_again_adds_a_newer_summary_and_never_the_same_one_twice(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    put_runs(
        recording_id,
        [
            {**succeeded_run(), "summary_run": 0, "recording_block": "01JMINE"},
            summarize_run(),
            summarize_run("failed"),
        ],
    )
    with_summary(recording_id, "The newer summary.\n")

    agaric.publish(recording_id, PUBLISHED)

    assert [call.arguments for call in system.calls] == [
        {"space_id": SPACE, "block_id": "01JMINE", "summary_markdown": "The newer summary.\n"}
    ]
    assert runs(recording_id)[3].get("summary_run") == 1

    agaric.publish(recording_id, PUBLISHED)

    assert len(system.calls) == 1
    assert (
        runs(recording_id)[4].get("summary_run"),
        runs(recording_id)[4].get("recording_block"),
    ) == (
        1,
        "01JMINE",
    )


@pytest.mark.usefixtures("configured")
def test_a_summary_that_did_not_reach_the_page_is_added_by_the_next_publish(
    system: FakeSystem,
) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run(), summarize_run()])
    with_summary(recording_id)
    system.answers["add_meeting"] = [Answer(1, stderr="Agaric MCP not running\n")]

    assert agaric.publish(recording_id, PUBLISHED) is False
    assert "summary_run" not in runs(recording_id)[2]

    assert agaric.publish(recording_id, PUBLISHED) is True

    assert [call.arguments.get("summary_markdown") for call in system.calls] == [SUMMARY, SUMMARY]


@pytest.mark.usefixtures("configured")
def test_a_taken_name_stops_and_offers_the_page_that_holds_it(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["add_meeting"] = [Answer(1, stderr=TAKEN_NEW)]

    assert agaric.publish(recording_id, PUBLISHED) is False

    assert [(call.tool, call.arguments) for call in system.calls[2:]] == [
        ("resolve", {"space_id": SPACE, "names": ["Meeting 2026-11-18"]})
    ]
    assert runs(recording_id) == [
        {
            "action": "publish_agaric",
            "status": "failed",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": "Agaric already has a page named Meeting 2026-11-18",
            "details": "run-0.log",
            "output": None,
            "choice": {"kind": "name_taken", "page_id": OTHER_PAGE},
        }
    ]
    assert failed_in_status(recording_id) == ["publish_agaric"]


@pytest.mark.usefixtures("configured")
def test_a_rename_to_a_taken_name_asks_only_for_another_name(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run()])
    store.set_typed_name(recording_id, "Weekly sync")
    system.answers["update_block_content"] = [Answer(1, stderr=TAKEN_RENAME)]

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[1]
    assert (run["status"], run["error"], run.get("choice")) == (
        "failed",
        "Agaric already has a page named Weekly sync 2026-11-18",
        {"kind": "rename_taken"},
    )
    assert [call.tool for call in system.calls] == ["update_block_content"]


@pytest.mark.usefixtures("configured")
def test_a_name_taken_by_an_alias_stops_and_offers_that_page(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["add_meeting"] = [
        Answer(
            1,
            stderr="agaric-mcp: Validation error: a page in this space has the alias "
            "'Meeting 2026-11-18'\n",
        )
    ]

    agaric.publish(recording_id, PUBLISHED)

    assert runs(recording_id)[0].get("choice") == {"kind": "name_taken", "page_id": OTHER_PAGE}


@pytest.mark.usefixtures("configured")
def test_publish_to_a_page_adds_the_files_to_it(system: FakeSystem) -> None:
    recording_id = recording()
    system.answers["add_meeting"] = [
        Answer(
            stdout=json.dumps(
                {**cast("dict[str, object]", usual_result("add_meeting")), "page_id": OTHER_PAGE}
            )
        )
    ]

    assert agaric.publish(recording_id, PUBLISHED, page=OTHER_PAGE) is True

    call = system.calls[1]
    assert sorted(call.arguments) == ["audio_path", "page", "space_id", "transcript_path"]
    assert (call.arguments["page"], call.arguments["space_id"]) == (OTHER_PAGE, SPACE)
    assert runs(recording_id)[0]["output"] == f"agaric://page/{OTHER_PAGE}"
    assert runs(recording_id)[0]["title"] == "Meeting 2026-11-18"


@pytest.mark.usefixtures("configured")
def test_publish_to_a_page_after_a_success_still_adds_the_files(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [succeeded_run()])

    agaric.publish(recording_id, PUBLISHED, page=OTHER_PAGE)

    assert [call.tool for call in system.calls] == ["list_spaces", "add_meeting"]


AGARIC_FAILURES = [
    (
        (
            "Agaric MCP not running (socket path: /home/u/.local/share/com.agaric.app/mcp.sock). "
            "Enable it in Settings → Agent access.\n(underlying error: No such file or directory "
            "(os error 2))\n"
        ),
        1,
        agaric.NOT_RUNNING,
    ),
    (
        (
            "agaric-mcp: no agent has this token: it was revoked, or made on another device. Copy "
            "this client's config from Settings → Agent access\n"
        ),
        1,
        agaric.TOKEN_REFUSED,
    ),
    (
        (
            "agaric-mcp: Not found: agent 'minutas' was revoked; add an agent in Settings → Agent "
            "access\n"
        ),
        1,
        agaric.TOKEN_REFUSED,
    ),
    (
        (
            "agaric-mcp: Validation error: tool `add_meeting`: `path` '/r/x.opus' is 52428801 "
            "bytes, over the 50 MB attachment cap\n"
        ),
        1,
        agaric.TOO_BIG,
    ),
    (
        "agaric-mcp: unknown argument: call\nTry `agaric-mcp --help` for usage.\n",
        2,
        agaric.UPDATE_AGARIC,
    ),
    (
        "agaric-mcp: unknown argument: --token\nTry `agaric-mcp --help` for usage.\n",
        2,
        agaric.UPDATE_AGARIC,
    ),
    (
        "agaric-mcp: Not found: space '01HZWORKSPACE0000000000000' not found\n",
        1,
        "Not found: space '01HZWORKSPACE0000000000000' not found",
    ),
    ("", 1, "agaric-mcp exited with status 1"),
]


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(("stderr", "returncode", "error"), AGARIC_FAILURES)
def test_an_agaric_failure_fails_the_run_with_the_fix(
    system: FakeSystem, stderr: str, returncode: int, error: str
) -> None:
    recording_id = recording()
    system.answers["add_meeting"] = [Answer(returncode, stderr=stderr)]

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"], run["output"]) == (
        "failed",
        error,
        "run-0.log",
        None,
    )
    assert "choice" not in run
    assert stderr in log_text(recording_id)
    assert failed_in_status(recording_id) == ["publish_agaric"]


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(
    ("stderr", "returncode", "error"),
    [
        failure
        for failure in AGARIC_FAILURES
        if failure[2] in {agaric.NOT_RUNNING, agaric.TOKEN_REFUSED, agaric.UPDATE_AGARIC}
    ],
)
def test_agaric_out_of_reach_fails_the_run_before_the_mix(
    system: FakeSystem, stderr: str, returncode: int, error: str
) -> None:
    recording_id = recording()
    system.answers["list_spaces"] = [Answer(returncode, stderr=stderr)]

    assert agaric.publish(recording_id, PUBLISHED) is False

    assert runs(recording_id)[0]["error"] == error
    assert [args[4] for args in system.commands] == ["list_spaces"]
    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "call.opus",
        "mic.opus",
        "recording.json",
        "run-0.log",
        "runs.json",
        "transcript.md",
    ]


@pytest.mark.usefixtures("configured")
def test_the_log_holds_each_call_and_its_output_but_not_the_token(system: FakeSystem) -> None:
    recording_id = recording(tracks=(), transcript=False)
    system.answers["add_meeting"] = [Answer(1, stdout="partial\n", stderr="agaric-mcp: boom\n")]

    agaric.publish(recording_id, PUBLISHED)

    spaces = json.dumps(usual_result("list_spaces"))
    arguments = json.dumps({"space_id": SPACE, "title": "Meeting", "date": "2026-11-18"})
    assert log_text(recording_id) == (
        f"$ agaric-mcp call list_spaces {{}}\n{spaces}\nexit status 0\n"
        f"$ agaric-mcp call add_meeting {arguments}\npartial\nagaric-mcp: boom\nexit status 1\n"
    )


@pytest.mark.usefixtures("configured")
@pytest.mark.parametrize(
    "stdout",
    ["not json\n", '{"recording_block_id": "01JREC"}\n', '{"page_id": "01JPAGE"}\n', "[]\n"],
)
def test_an_answer_without_the_page_or_its_recording_block_fails_the_run(
    system: FakeSystem, stdout: str
) -> None:
    recording_id = recording()
    system.answers["add_meeting"] = [Answer(stdout=stdout)]

    assert agaric.publish(recording_id, PUBLISHED) is False

    assert runs(recording_id)[0]["error"] == agaric.UNEXPECTED


@pytest.mark.usefixtures("configured")
def test_a_missing_bridge_fails_the_run(system: FakeSystem) -> None:
    recording_id = recording()
    system.missing = {"agaric-mcp"}

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["error"], run["details"]) == (agaric.BRIDGE_MISSING, "run-0.log")


def test_publish_without_a_token_fails_with_how_to_set_it(system: FakeSystem) -> None:
    settings.set_agaric_space_id(SPACE)
    recording_id = recording()

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"]) == ("failed", agaric.NO_TOKEN, None)
    assert system.commands == []
    assert failed_in_status(recording_id) == ["publish_agaric"]


def test_a_locked_keyring_fails_the_run(
    system: FakeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.set_agaric_space_id(SPACE)
    recording_id = recording()

    def dismissed(_service: str, _username: str) -> str | None:
        reason = "Failed to unlock the item!"
        raise KeyringLocked(reason)

    monkeypatch.setattr(keyring, "get_password", dismissed)

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"]) == (
        "failed",
        "Could not read the Agaric agent token from the keyring: Failed to unlock the item!",
    )
    assert system.commands == []
    assert failed_in_status(recording_id) == ["publish_agaric"]


def test_publish_without_a_space_fails_with_how_to_set_it(system: FakeSystem) -> None:
    settings.set_agaric_token(TOKEN)
    recording_id = recording()

    assert agaric.publish(recording_id, PUBLISHED) is False

    assert runs(recording_id)[0]["error"] == agaric.NO_SPACE
    assert system.commands == []


@pytest.mark.usefixtures("configured")
def test_a_failed_mix_fails_the_run_and_leaves_no_file(system: FakeSystem) -> None:
    recording_id = recording()
    system.gst = Answer(1, stderr=f"{GST_ERROR}\nAdditional debug info:\ngsttypefindelement.c\n")

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["error"], run["details"]) == (
        f"Could not mix the tracks: {GST_ERROR}",
        "run-0.log",
    )
    assert "Additional debug info:\ngsttypefindelement.c\nexit status 1\n" in log_text(recording_id)
    assert [call.tool for call in system.calls] == ["list_spaces"]
    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "call.opus",
        "mic.opus",
        "recording.json",
        "run-0.log",
        "runs.json",
        "transcript.md",
    ]


@pytest.mark.usefixtures("configured")
def test_a_mix_that_fails_silently_says_its_status(system: FakeSystem) -> None:
    recording_id = recording()
    system.gst = Answer(1)

    agaric.publish(recording_id, PUBLISHED)

    assert runs(recording_id)[0]["error"] == (
        "Could not mix the tracks: gst-launch-1.0 exited with status 1"
    )


@pytest.mark.usefixtures("configured")
def test_missing_gstreamer_fails_the_run(system: FakeSystem) -> None:
    recording_id = recording()
    system.missing = {"gst-launch-1.0"}

    assert agaric.publish(recording_id, PUBLISHED) is False

    assert runs(recording_id)[0]["error"] == agaric.GSTREAMER_MISSING


@pytest.mark.usefixtures("configured")
def test_publish_fails_a_run_a_crash_left_running(system: FakeSystem) -> None:
    recording_id = recording()
    (store.recording_dir(recording_id) / "run-0.log").write_text("$ agaric-mcp call …\n")
    stale = {**succeeded_run(), "status": "running", "details": None, "output": None}
    del stale["title"]
    transcribing = {**stale, "action": "transcribe"}
    put_runs(recording_id, [stale, transcribing])

    agaric.publish(recording_id, PUBLISHED)

    assert runs(recording_id)[:2] == [
        {**stale, "status": "failed", "error": "interrupted", "details": "run-0.log"},
        transcribing,
    ]
    assert runs(recording_id)[2]["status"] == "succeeded"
    assert system.calls[1].tool == "add_meeting"


@pytest.mark.usefixtures("configured")
def test_a_crashed_run_without_a_log_has_no_details(system: FakeSystem) -> None:
    recording_id = recording()
    stale = {**succeeded_run(), "status": "running", "details": None, "output": None}
    put_runs(recording_id, [stale])
    system.answers["add_meeting"] = [Answer(1, stderr="agaric-mcp: boom\n")]

    agaric.publish(recording_id, PUBLISHED)

    assert runs(recording_id)[0]["details"] is None


@pytest.mark.usefixtures("configured")
def test_sigterm_fails_the_run_as_interrupted_and_removes_the_files(system: FakeSystem) -> None:
    recording_id = recording()
    before = signal.getsignal(signal.SIGTERM)

    def logout() -> None:
        if system.calls[-1].tool == "add_meeting":
            signal.raise_signal(signal.SIGTERM)

    system.during_call = logout

    assert agaric.publish(recording_id, PUBLISHED) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"]) == ("failed", "interrupted", "run-0.log")
    assert failed_in_status(recording_id) == ["publish_agaric"]
    assert sorted(path.name for path in store.recording_dir(recording_id).iterdir()) == [
        "call.opus",
        "mic.opus",
        "recording.json",
        "run-0.log",
        "runs.json",
        "transcript.md",
    ]
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.usefixtures("configured")
def test_the_run_is_running_and_status_rewritten_while_it_publishes(system: FakeSystem) -> None:
    recording_id = recording()
    put_runs(recording_id, [{**succeeded_run(), "status": "failed", "output": None}])
    seen: list[object] = []
    system.during_call = lambda: seen.extend(
        [runs(recording_id)[1]["status"], failed_in_status(recording_id)]
    )

    agaric.publish(recording_id, PUBLISHED)

    assert seen == ["running", [], "running", []]


def test_list_spaces_gives_each_space_id_and_name(system: FakeSystem) -> None:
    settings.set_agaric_token(TOKEN)

    assert agaric.list_spaces() == [("01HZPERSONAL", "Personal"), (SPACE, "Work")]
    assert system.calls[0].arguments == {}


def test_list_spaces_fails_with_agarics_reason(system: FakeSystem) -> None:
    settings.set_agaric_token(TOKEN)
    system.answers["list_spaces"] = [Answer(2, stderr="agaric-mcp: unknown argument: call\n")]

    with pytest.raises(agaric.AgaricError, match=r"^Update Agaric to 0.26 or newer "):
        agaric.list_spaces()


def test_agarics_own_bridge_comes_before_the_one_on_the_path(system: FakeSystem) -> None:
    settings.set_agaric_token(TOKEN)
    agaric.list_spaces()
    own = store.data_dir().parent / "com.agaric.app" / "bin" / "agaric-mcp"
    own.parent.mkdir(parents=True)
    own.touch()

    agaric.list_spaces()

    assert [args[0] for args in system.commands] == ["agaric-mcp", str(own)]


@pytest.mark.usefixtures("system")
def test_list_spaces_without_a_token_says_how_to_set_it() -> None:
    with pytest.raises(agaric.AgaricError) as error:
        agaric.list_spaces()

    assert str(error.value) == agaric.NO_TOKEN


@given(
    name=st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), max_size=20),
    day=st.dates(),
)
def test_page_title_is_the_trimmed_name_or_meeting_then_the_date(name: str, day: date) -> None:
    title = agaric.page_title(name, day)

    head, _, tail = title.rpartition(" ")
    assert tail == day.isoformat()
    assert head == (name.strip() or "Meeting")


def test_page_title_example() -> None:
    assert agaric.page_title("  Weekly sync ", date(2026, 1, 5)) == "Weekly sync 2026-01-05"


AGENT_TOKEN = "agt_" + "0123456789abcdef" * 2  # a made-up agent token, of its shape


def agarics_config(token: str) -> str:
    """The config Agaric's Settings → Agent access gives an agent, laid out as Agaric lays it."""
    folder = "/home/me/.local/share/com.agaric.app"
    server = {
        "command": f"{folder}/bin/agaric-mcp",
        "args": ["--socket", f"{folder}/mcp.sock", "--token", token],
    }
    return json.dumps({"mcpServers": {"agaric-minutas-vptx4p": server}}, indent=2)


@pytest.mark.parametrize(
    "pasted",
    [
        AGENT_TOKEN,
        f"  {AGENT_TOKEN}\n",
        agarics_config(AGENT_TOKEN),
        # Agaric's other copy, the command that adds the agent to Claude Code.
        (
            'claude mcp add -s user agaric-minutas-vptx4p -- "/home/me/.local/share/com.agaric.app/'
            f'bin/agaric-mcp" --socket "/home/me/.local/share/com.agaric.app/mcp.sock" --token '
            f"{AGENT_TOKEN}"
        ),
    ],
)
def test_the_token_is_taken_from_what_agaric_gives_to_paste(pasted: str) -> None:
    assert agaric.token_in(pasted) == AGENT_TOKEN


@pytest.mark.parametrize(
    "pasted",
    [
        "",
        "agt_1",
        AGENT_TOKEN[:-1],
        f"{AGENT_TOKEN}0",
        f"x{AGENT_TOKEN}",
        agarics_config("agt_1"),
        '{"mcpServers": {}}',
    ],
)
def test_what_holds_no_agent_token_gives_none(pasted: str) -> None:
    assert agaric.token_in(pasted) is None


@given(digits=st.text("0123456789abcdef", min_size=32, max_size=32))
def test_any_agents_token_is_taken_from_its_config(digits: str) -> None:
    assert agaric.token_in(agarics_config(f"agt_{digits}")) == f"agt_{digits}"
