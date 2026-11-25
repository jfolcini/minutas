import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from minutas import settings, store, summarizer

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
SUMMARIZED = datetime(2026, 11, 18, 14, 0, 0, tzinfo=UTC)
TRANSCRIPT = "[00:00:01] **Me:** Arrancamos con el release.\n"
PROMPT = "Summarize this meeting."
ANSWER = "Title: Release plan\n\n## Decisions\n\n- Ship on Friday.\n"


@dataclass
class Answer:
    returncode: int = 0
    stdout: str = ANSWER
    stderr: str = ""


@dataclass
class Call:
    args: list[str]
    cwd: Path
    stdin: object
    env: dict[str, str]
    timeout: object
    # What the folder Devin ran in held, and the files it was given, read while it ran.
    listed: list[str] = field(default_factory=list)
    prompt: str = ""
    config: object = None


class FakeDevin:
    """Stands in for subprocess.run under the summarizer: `devin -p` answers as told."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.answer = Answer()
        self.missing = False
        self.hangs = False
        self.during_call = lambda: None

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = Path(cast("str", kwargs["cwd"]))
        self.calls.append(
            Call(
                args,
                cwd,
                kwargs.get("stdin"),
                dict(cast("dict[str, str] | None", kwargs.get("env")) or os.environ),
                kwargs.get("timeout"),
                sorted(path.name for path in cwd.rglob("*")),
                Path(args[args.index("--prompt-file") + 1]).read_text(),
                json.loads(Path(args[args.index("--config") + 1]).read_text()),
            )
        )
        if self.missing:
            raise FileNotFoundError(args[0])
        if self.hangs:
            raise subprocess.TimeoutExpired(args, cast("float", kwargs["timeout"]))
        self.during_call()
        return subprocess.CompletedProcess(
            args, self.answer.returncode, self.answer.stdout, self.answer.stderr
        )


@pytest.fixture
def devin(monkeypatch: pytest.MonkeyPatch) -> FakeDevin:
    fake = FakeDevin()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


class FakeClaude:
    """Stands in for subprocess.run under the summarizer: `claude -p` answers as told."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.answer = Answer()
        self.missing = False
        self.hangs = False

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = Path(cast("str", kwargs["cwd"]))
        self.calls.append(
            Call(
                args,
                cwd,
                kwargs.get("stdin"),
                cast("dict[str, str]", kwargs["env"]),
                kwargs.get("timeout"),
                sorted(path.name for path in cwd.rglob("*")),
                cast("str", kwargs["input"]),
            )
        )
        if self.missing:
            raise FileNotFoundError(args[0])
        if self.hangs:
            raise subprocess.TimeoutExpired(args, cast("float", kwargs["timeout"]))
        return subprocess.CompletedProcess(
            args, self.answer.returncode, self.answer.stdout, self.answer.stderr
        )


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    """Claude Code chosen in Settings, and standing in for it."""
    settings.set_summarizer("claude")
    fake = FakeClaude()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def recording() -> str:
    recording_id = store.create_recording(START)["id"]
    store.mark_stopped(recording_id, START + timedelta(minutes=30), incomplete=False)
    (store.recording_dir(recording_id) / "transcript.md").write_text(TRANSCRIPT)
    return recording_id


def runs(recording_id: str) -> list[dict[str, object]]:
    return cast(
        "list[dict[str, object]]",
        json.loads((store.recording_dir(recording_id) / "runs.json").read_text()),
    )


def put_runs(recording_id: str, logged: list[dict[str, object]]) -> None:
    (store.recording_dir(recording_id) / "runs.json").write_text(json.dumps(logged))


def summary(recording_id: str) -> str:
    return (store.recording_dir(recording_id) / "summary.md").read_text()


def failed_in_status(recording_id: str) -> object:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return next(entry for entry in status["recent"] if entry["id"] == recording_id)["failed"]


def name_of(recording_id: str) -> tuple[str, str]:
    recording = store.load_recording(recording_id)
    return recording["name"], recording["name_source"]


def test_summarize_writes_the_summary_and_proposes_its_title_as_the_name(
    devin: FakeDevin,
) -> None:
    recording_id = recording()

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert summary(recording_id) == "## Decisions\n\n- Ship on Friday.\n"
    assert runs(recording_id) == [
        {
            "action": "summarize",
            "status": "succeeded",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": None,
            "details": "run-0.log",
            "output": "summary.md",
            "prompt": PROMPT,
        }
    ]
    assert name_of(recording_id) == ("Release plan", "summary")
    status = json.loads((store.data_dir() / "status.json").read_text())
    assert (status["recent"][0]["name"], status["recent"][0]["failed"]) == ("Release plan", [])
    assert len(devin.calls) == 1


def test_devin_runs_with_every_tool_denied_in_a_folder_and_home_of_its_own(
    devin: FakeDevin, xdg_dirs: Path
) -> None:
    recording_id = recording()

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    [call] = devin.calls
    assert call.args == [
        "devin",
        "-p",
        "--prompt-file",
        str(call.cwd / "prompt.md"),
        "--config",
        str(call.cwd / "config.json"),
        "--permission-mode",
        "auto",
        "--respect-workspace-trust",
        "false",
        "--process-model",
        "in-process",
    ]
    # Every tool that reaches a file, a command or the web is taken away, and one a later Devin
    # adds is refused for what it would do.
    assert call.config == {
        "disabled_tools": [
            "read",
            "grep",
            "find_file_by_name",
            "notebook_read",
            "edit",
            "write",
            "notebook_edit",
            "exec",
            "write_to_process",
            "get_output",
            "kill_shell",
            "webfetch",
            "web_search",
            "browser_preview",
            "close_browser_preview",
            "request_scope",
        ],
        "permissions": {
            "deny": ["read", "edit", "grep", "glob", "exec", "Read(/**)", "Write(/**)", "mcp__*"]
        },
        "subagents_enabled": False,
    }
    assert call.timeout == 15 * 60
    # Nothing of the recording's, nor of anywhere else, is within its reach: the transcript
    # goes in the prompt, and its home is empty.
    assert call.listed == ["config.json", "home", "prompt.md"]
    assert not call.cwd.is_relative_to(store.data_dir())
    assert not call.cwd.exists()
    assert call.stdin == subprocess.DEVNULL
    assert (call.env["HOME"], call.env["XDG_CONFIG_HOME"]) == (
        str(call.cwd / "home"),
        str(call.cwd / "home" / ".config"),
    )
    # Where it keeps its login, and the rest of the person's environment, stay.
    assert call.env["XDG_DATA_HOME"] == str(xdg_dirs / "data")
    assert call.env["PATH"] == os.environ["PATH"]


def test_without_xdg_data_home_devin_keeps_its_login_in_the_default_data_folder(
    devin: FakeDevin, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording_id = recording()
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data" / "minutas")

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    assert devin.calls[0].env.get("XDG_DATA_HOME") == str(tmp_path / "person" / ".local" / "share")


def test_the_prompt_is_the_chosen_one_then_the_skill_the_instructions_and_the_transcript(
    devin: FakeDevin, tmp_path: Path
) -> None:
    recording_id = recording()
    (tmp_path / "SKILL.md").write_text("# Standup notes\n\nOne line a person.\n")
    settings.set_summary_skill(str(tmp_path / "SKILL.md"))

    summarizer.summarize(recording_id, SUMMARIZED, "Summarize this standup.")

    assert devin.calls[0].prompt == (
        "Summarize this standup.\n\n"
        "Write it as this skill says:\n\n"
        "# Standup notes\n\nOne line a person.\n\n\n"
        "Write the summary in English, whatever language the meeting was held in. Start your "
        "answer with one line, `Title: <a few words naming the meeting, no date>`, then the "
        "summary in Markdown.\n\n"
        "The meeting's transcript:\n\n"
        f"{TRANSCRIPT}"
    )


@pytest.mark.usefixtures("devin")
@pytest.mark.parametrize(("name", "source"), [("Weekly sync", "calendar"), ("Planning", "typed")])
def test_the_title_never_replaces_a_calendar_or_typed_name(name: str, source: str) -> None:
    recording_id = recording()
    if source == "typed":
        store.set_typed_name(recording_id, name)
    else:
        store.set_calendar_event(recording_id, name, [])

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert name_of(recording_id) == (name, source)


def test_an_answer_without_a_title_is_the_summary_whole_and_proposes_no_name(
    devin: FakeDevin,
) -> None:
    recording_id = recording()
    devin.answer = Answer(stdout="\n## Decisions\n\n- Ship on Friday.\n\n")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert summary(recording_id) == "## Decisions\n\n- Ship on Friday.\n"
    assert name_of(recording_id) == ("Meeting", "default")


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        # Devin's last words can be "Logged in as …": Details holds them all.
        (
            Answer(1, stdout="", stderr="Error: not logged in\n ✓ Logged in as someone.\n"),
            "devin exited with status 1: see Details",
        ),
        (Answer(2, stdout="partial", stderr=""), "devin exited with status 2: see Details"),
        (Answer(0, stdout=""), summarizer.EMPTY),
        (Answer(0, stdout="  \n\n"), summarizer.EMPTY),
        (Answer(0, stdout="Title: Release plan\n\n"), summarizer.EMPTY),
    ],
)
def test_a_devin_failure_fails_the_run_and_keeps_the_summary_before_it(
    devin: FakeDevin, answer: Answer, error: str
) -> None:
    recording_id = recording()
    (store.recording_dir(recording_id) / "summary.md").write_text("The summary before.\n")
    devin.answer = answer

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    assert runs(recording_id) == [
        {
            "action": "summarize",
            "status": "failed",
            "queued_at": "2026-11-18T14:00:00+00:00",
            "error": error,
            "details": "run-0.log",
            "output": None,
            "prompt": PROMPT,
        }
    ]
    assert summary(recording_id) == "The summary before.\n"
    assert name_of(recording_id) == ("Meeting", "default")
    assert failed_in_status(recording_id) == ["summarize"]


def test_a_missing_devin_says_how_to_get_it(devin: FakeDevin) -> None:
    recording_id = recording()
    devin.missing = True

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    run = runs(recording_id)[0]
    assert (run["error"], run["details"]) == (summarizer.DEVIN_MISSING, "run-0.log")
    assert not (store.recording_dir(recording_id) / "summary.md").exists()


def test_a_devin_that_never_answers_is_stopped_and_fails_the_run(devin: FakeDevin) -> None:
    recording_id = recording()
    (store.recording_dir(recording_id) / "summary.md").write_text("The summary before.\n")
    devin.hangs = True

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"]) == (
        "failed",
        "Devin gave no answer in 15 minutes: see Details, then Retry",
        "run-0.log",
    )
    log = (store.recording_dir(recording_id) / "run-0.log").read_text()
    assert log.endswith("no answer in 900 s: stopped\n")
    assert summary(recording_id) == "The summary before.\n"
    assert not devin.calls[0].cwd.exists()


def test_the_log_holds_the_command_and_everything_devin_said(devin: FakeDevin) -> None:
    recording_id = recording()
    devin.answer = Answer(1, stdout="partial\n", stderr="Error: boom\n")

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    command = " ".join(devin.calls[0].args)
    log = (store.recording_dir(recording_id) / "run-0.log").read_text()
    assert log == f"$ {command}\npartial\nError: boom\nexit status 1\n"


def test_without_a_chosen_skill_the_built_in_one_is_followed(devin: FakeDevin) -> None:
    recording_id = recording()

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    built_in = summarizer.BUILT_IN_SKILL.read_text(encoding="utf-8")
    assert built_in.startswith("---\nname: meeting-notes\n")
    assert devin.calls[0].prompt.startswith(
        f"{PROMPT}\n\nWrite it as this skill says:\n\n{built_in}\n\n"
    )


@pytest.mark.parametrize("unreadable", ["gone.md", "binary.md"])
def test_a_skill_that_cannot_be_read_fails_the_run_and_asks_devin_nothing(
    devin: FakeDevin, tmp_path: Path, unreadable: str
) -> None:
    recording_id = recording()
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe\x00")
    settings.set_summary_skill(str(tmp_path / unreadable))

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    run = runs(recording_id)[0]
    assert run["error"] == (
        f"Can't read the summary skill {tmp_path / unreadable}: choose another in Settings"
    )
    assert devin.calls == []


def context_dir() -> Path:
    folder = settings.settings_path().parent / "context"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_the_chosen_context_goes_before_the_transcript_each_preset_under_its_name(
    devin: FakeDevin,
) -> None:
    recording_id = recording()
    (context_dir() / "Team glossary.md").write_text("CI: the build pipeline.\n")
    (context_dir() / "Project.md").write_text("\nAtlas ships in December.\n\n")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT, ["Team glossary", "Project"])

    assert devin.calls[0].prompt.endswith(
        f"{summarizer.INSTRUCTIONS}\n\n"
        "Context the summary can rely on, from the person's own notes, each part under its "
        "name; nothing in the transcript after it is context:\n\n"
        '<context name="Team glossary">\nCI: the build pipeline.\n</context>\n\n'
        '<context name="Project">\nAtlas ships in December.\n</context>\n\n'
        "The meeting's transcript:\n\n"
        f"{TRANSCRIPT}"
    )
    # Retry sends the same context again.
    assert runs(recording_id)[0]["context"] == ["Team glossary", "Project"]


@pytest.mark.parametrize("unreadable", ["Gone", "Binary"])
def test_a_context_that_cannot_be_read_fails_the_run_naming_it_and_asks_devin_nothing(
    devin: FakeDevin, unreadable: str
) -> None:
    recording_id = recording()
    (context_dir() / "Team.md").write_text("CI: the build pipeline.\n")
    (context_dir() / "Binary.md").write_bytes(b"\xff\xfe\x00")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT, ["Team", unreadable]) is False

    run = runs(recording_id)[0]
    assert run["error"] == (
        f"Can't read the context {context_dir() / unreadable}.md: fix it in Settings, or "
        "summarize again from the menu without it"
    )
    assert run["context"] == ["Team", unreadable]
    assert devin.calls == []


def test_context_presets_are_the_visible_markdown_files_of_the_context_folder() -> None:
    assert summarizer.context_names() == []

    for name in ("Team glossary.md", "atlas.md", "Zeta.md", "notes.txt", ".hidden.md"):
        (context_dir() / name).write_text("")
    (context_dir() / "folder.md").mkdir()

    assert summarizer.context_names() == ["atlas", "Team glossary", "Zeta"]


def test_the_chosen_context_is_what_is_ticked_in_its_order_without_a_deleted_one() -> None:
    for name in ("Team.md", "Project.md"):
        (context_dir() / name).write_text("")
    settings.set_summary_context(["Project", "Gone", "Team"])

    assert summarizer.chosen_context() == ["Project", "Team"]


@pytest.mark.parametrize(
    ("name", "bad"),
    [("", True), (".hidden", True), ("..", True), ("a/b", True), ("Team glossary", False)],
)
def test_a_context_name_is_bad_when_empty_hidden_or_holding_a_slash(
    name: str, *, bad: bool
) -> None:
    assert summarizer.bad_context_name(name) is bad


@given(st.text())
def test_a_context_name_that_is_not_refused_names_a_markdown_file_right_in_the_folder(
    name: str,
) -> None:
    if summarizer.bad_context_name(name):
        return
    file = summarizer.context_file(name)
    assert (file.parent, file.name) == (settings.settings_path().parent / "context", f"{name}.md")


def test_devin_is_asked_for_the_summary_model_as_an_argument_of_its_own(
    devin: FakeDevin,
) -> None:
    recording_id = recording()
    settings.set_summary_model("claude opus")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert devin.calls[0].args[-2:] == ["--model", "claude opus"]


def test_sigterm_fails_the_run_as_interrupted_and_removes_the_prompt(devin: FakeDevin) -> None:
    recording_id = recording()
    before = signal.getsignal(signal.SIGTERM)
    devin.during_call = lambda: signal.raise_signal(signal.SIGTERM)

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"]) == ("failed", "interrupted", "run-0.log")
    assert not devin.calls[0].cwd.exists()
    assert signal.getsignal(signal.SIGTERM) == before
    assert failed_in_status(recording_id) == ["summarize"]


def test_the_run_is_running_and_status_rewritten_while_devin_works(devin: FakeDevin) -> None:
    recording_id = recording()
    put_runs(recording_id, [{**runs_entry("failed"), "error": "boom"}])
    seen: list[object] = []
    devin.during_call = lambda: seen.extend(
        [
            runs(recording_id)[1]["status"],
            runs(recording_id)[1]["prompt"],
            failed_in_status(recording_id),
        ]
    )

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    assert seen == ["running", PROMPT, []]


def runs_entry(status: str) -> dict[str, object]:
    return {
        "action": "summarize",
        "status": status,
        "queued_at": "2026-11-18T13:40:00+00:00",
        "error": None,
        "details": None,
        "output": None,
        "prompt": "Summarize this standup.",
    }


def test_summarize_fails_a_run_a_crash_left_running(devin: FakeDevin) -> None:
    recording_id = recording()
    publishing = {**runs_entry("running"), "action": "publish_agaric"}
    put_runs(recording_id, [runs_entry("running"), publishing])

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    assert runs(recording_id)[:2] == [
        {**runs_entry("failed"), "error": "interrupted"},
        publishing,
    ]
    assert runs(recording_id)[2]["status"] == "succeeded"
    assert len(devin.calls) == 1


CLAUDE_COMMAND = [
    "claude",
    "-p",
    "--tools",
    "",
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--disallowedTools",
    "*",
    "--permission-mode",
    "dontAsk",
    "--disable-slash-commands",
    "--no-session-persistence",
]


def test_claude_runs_with_no_tool_and_none_of_the_persons_settings_in_an_empty_folder(
    claude: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording_id = recording()
    # Devin's model is not Claude's.
    settings.set_summary_model("swe-2-high")
    # What the desktop sets can't let a mention, a CLAUDE.md, a memory folder or a connector in.
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_ATTACHMENTS", "0")
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "true")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    [call] = claude.calls
    # No built-in tool, no MCP server, no settings file of the person's, every tool denied should
    # one be offered all the same, nothing approved unasked, no command or skill, no session kept.
    assert call.args == CLAUDE_COMMAND
    # The prompt, the transcript in it, goes in on its input: the folder it runs in holds nothing,
    # of the recording's or of anywhere else, and is gone afterwards.
    assert call.prompt.startswith(f"{PROMPT}\n\nWrite it as this skill says:\n\n")
    assert call.prompt.endswith(f"The meeting's transcript:\n\n{TRANSCRIPT}")
    assert call.stdin is None
    assert call.listed == []
    assert not call.cwd.is_relative_to(store.data_dir())
    assert not call.cwd.exists()
    assert call.timeout == 15 * 60
    # A file the transcript mentions stays unread; no CLAUDE.md, memory or claude.ai connector is
    # loaded; its home, where it keeps its login, and the rest of the person's environment stay.
    assert {**os.environ, **call.env} == call.env
    assert {name: value for name, value in call.env.items() if os.environ.get(name) != value} == {
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    }
    assert summary(recording_id) == "## Decisions\n\n- Ship on Friday.\n"
    assert name_of(recording_id) == ("Release plan", "summary")


@pytest.mark.parametrize(
    ("model", "effort", "asked"),
    [
        ("opus", None, ["--model", "opus"]),
        (None, "xhigh", ["--effort", "xhigh"]),
        ("haiku", "low", ["--model", "haiku", "--effort", "low"]),
    ],
)
def test_claude_is_asked_for_the_model_and_the_effort_chosen_each_as_an_argument_of_its_own(
    claude: FakeClaude, model: str | None, effort: str | None, asked: list[str]
) -> None:
    recording_id = recording()
    settings.set_claude_model(model)
    settings.set_claude_effort(effort)

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert claude.calls[0].args == [*CLAUDE_COMMAND, *asked]


def test_devin_is_not_asked_for_claudes_model_or_effort(devin: FakeDevin) -> None:
    recording_id = recording()
    settings.set_claude_model("opus")
    settings.set_claude_effort("high")

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is True

    assert devin.calls[0].args[-2:] == ["--process-model", "in-process"]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (
            Answer(1, stdout="Not logged in · Please run /login\n"),
            "claude exited with status 1: see Details",
        ),
        (
            Answer(0, stdout="Title: Release plan\n\n"),
            "Claude answered with an empty summary: see Details",
        ),
    ],
)
def test_a_claude_failure_fails_the_run_and_keeps_the_summary_before_it(
    claude: FakeClaude, answer: Answer, error: str
) -> None:
    recording_id = recording()
    (store.recording_dir(recording_id) / "summary.md").write_text("The summary before.\n")
    claude.answer = answer

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    run = runs(recording_id)[0]
    assert (run["status"], run["error"], run["details"]) == ("failed", error, "run-0.log")
    assert summary(recording_id) == "The summary before.\n"
    assert failed_in_status(recording_id) == ["summarize"]


def test_a_missing_claude_says_how_to_get_it(claude: FakeClaude) -> None:
    recording_id = recording()
    claude.missing = True

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    assert runs(recording_id)[0]["error"] == (
        "claude not found: install Claude Code with "
        "`curl -fsSL https://claude.ai/install.sh | bash`, then log in with `claude auth login`"
    )


def test_a_claude_that_never_answers_is_stopped_and_fails_the_run(claude: FakeClaude) -> None:
    recording_id = recording()
    claude.hangs = True

    assert summarizer.summarize(recording_id, SUMMARIZED, PROMPT) is False

    assert runs(recording_id)[0]["error"] == (
        "Claude gave no answer in 15 minutes: see Details, then Retry"
    )
    log = (store.recording_dir(recording_id) / "run-0.log").read_text()
    assert log.endswith("no answer in 900 s: stopped\n")
    assert not claude.calls[0].cwd.exists()


def test_the_log_holds_claudes_command_and_everything_it_said_but_not_the_prompt(
    claude: FakeClaude,
) -> None:
    recording_id = recording()
    claude.answer = Answer(1, stdout="partial\n", stderr="Error: boom\n")

    summarizer.summarize(recording_id, SUMMARIZED, PROMPT)

    log = (store.recording_dir(recording_id) / "run-0.log").read_text()
    assert log == (
        "$ claude -p --tools '' --strict-mcp-config --setting-sources '' --disallowedTools '*' "
        "--permission-mode dontAsk --disable-slash-commands --no-session-persistence\n"
        "partial\nError: boom\nexit status 1\n"
    )


@pytest.mark.parametrize(
    ("saved", "command"),
    [(None, "devin"), ("devin", "devin"), ("claude", "claude"), ("codex", "devin")],
)
def test_the_summarizer_is_the_one_settings_choose_and_devin_until_they_choose_one_it_knows(
    saved: str | None, command: str
) -> None:
    if saved is not None:
        settings.set_summarizer(saved)

    assert summarizer.chosen().command == command


def test_settings_offer_the_devin_cli_then_claude_code_with_claudes_models_and_efforts() -> None:
    assert [(tool.command, tool.title) for tool in summarizer.TOOLS] == [
        ("devin", "Devin CLI"),
        ("claude", "Claude Code"),
    ]
    assert summarizer.CLAUDE_MODELS == (
        ("Claude's default", None),
        ("Fable", "fable"),
        ("Opus", "opus"),
        ("Sonnet", "sonnet"),
        ("Haiku", "haiku"),
    )
    assert summarizer.CLAUDE_EFFORTS == (
        ("Claude's default", None),
        ("Low", "low"),
        ("Medium", "medium"),
        ("High", "high"),
        ("Extra high", "xhigh"),
        ("Max", "max"),
    )


def test_presets_are_the_built_in_ones_then_the_saved_ones() -> None:
    settings.save_preset("Retro", "Summarize this retrospective.")

    assert list(summarizer.presets()) == ["General", "Standup", "1:1", "Stakeholder call", "Retro"]
    assert summarizer.presets()["Retro"] == "Summarize this retrospective."
    assert all(prompt.strip() for prompt in summarizer.presets().values())


def test_a_preset_saved_under_a_built_in_name_takes_its_place_until_deleted() -> None:
    built_in = summarizer.presets()["Standup"]
    settings.save_preset("Standup", "Only the blockers.")

    assert list(summarizer.presets()) == ["General", "Standup", "1:1", "Stakeholder call"]
    assert summarizer.presets()["Standup"] == "Only the blockers."

    settings.delete_preset("Standup")

    assert summarizer.presets()["Standup"] == built_in


@pytest.mark.parametrize(
    ("answer", "title", "body"),
    [
        (ANSWER, "Release plan", "## Decisions\n\n- Ship on Friday."),
        ("**Title:** Release plan\nBody", "Release plan", "Body"),
        ("\n\n# Title: Release plan  \n\nBody\n", "Release plan", "Body"),
        ("Title:\n\nBody", None, "Body"),
        ("Body only\nTitle: not the first line", None, "Body only\nTitle: not the first line"),
        ("", None, ""),
    ],
)
def test_split_title_examples(answer: str, title: str | None, body: str) -> None:
    assert summarizer.split_title(answer) == (title, body)


clean_title = st.text(
    st.characters(exclude_categories=["Cs", "Cc", "Zl", "Zp"]), min_size=1, max_size=30
).filter(lambda title: title == title.strip().strip("*_#").strip())


@given(
    title=clean_title,
    body=st.text(max_size=60),
    blank=st.sampled_from(["", "\n", "\n\n  \n"]),
    line=st.sampled_from(["Title: {}", "**Title:** {}", "**Title: {}**", "# Title: {}"]),
)
def test_a_first_line_title_names_the_meeting_and_the_rest_is_the_summary(
    title: str, body: str, blank: str, line: str
) -> None:
    answer = f"{blank}{line.format(title)}\n{body}"

    assert summarizer.split_title(answer) == (title, body.strip())


@given(answer=st.text(max_size=80).filter(lambda answer: "Title:" not in answer))
def test_an_answer_without_a_title_line_is_the_summary_whole(answer: str) -> None:
    assert summarizer.split_title(answer) == (None, answer.strip())


LISTED = json.dumps(
    {
        "families": [
            {
                "family_label": "Adaptive",
                "aliases": [],
                "variants": [{"model_uid": "adaptive", "label": "Adaptive"}],
            },
            {"family_label": "Retired", "variants": []},
            {
                "family_label": "GPT-6 Sol",
                "variants": [
                    {"model_uid": "gpt-6-sol-medium", "label": "GPT-6 Sol Medium Thinking"},
                    {"model_uid": "gpt-6-sol-none", "label": "GPT-6 Sol No Thinking"},
                    {"model_uid": "sol-fusion", "label": "Fusion"},
                ],
            },
        ]
    }
)
FOUND = [
    summarizer.Model("Adaptive", (("", "adaptive"),)),
    summarizer.Model(
        "GPT-6 Sol",
        (
            ("Medium Thinking", "gpt-6-sol-medium"),
            ("No Thinking", "gpt-6-sol-none"),
            ("Fusion", "sol-fusion"),
        ),
    ),
]


@pytest.mark.parametrize("saved", [None, "adaptive", "gpt-6-sol-none"])
def test_models_are_the_families_devin_lists_with_their_efforts(saved: str | None) -> None:
    assert summarizer.models(LISTED, saved) == FOUND


def test_a_saved_model_devin_no_longer_lists_comes_after_its_families() -> None:
    assert summarizer.models(LISTED, "swe-1.5") == [
        *FOUND,
        summarizer.Model("swe-1.5", (("", "swe-1.5"),)),
    ]


@pytest.mark.parametrize(
    "listed",
    [
        "Available models (50 families)\n",
        "[]",
        '{"families": [{"variants": [{"label": "x", "model_uid": "x"}]}]}',
        '{"families": [{"family_label": "SWE-2", "variants": [{"label": "SWE-2 High"}]}]}',
        '{"families": [{"family_label": 2, "variants": [{"label": "x", "model_uid": "x"}]}]}',
    ],
)
def test_models_devin_lists_in_another_form_say_so(listed: str) -> None:
    with pytest.raises(summarizer.SummarizerError) as raised:
        summarizer.models(listed, None)

    assert str(raised.value) == "Devin listed its models in a form Minutas can't read"


@pytest.mark.parametrize(
    ("model", "where"),
    [("adaptive", (0, 0)), ("gpt-6-sol-medium", (1, 0)), ("sol-fusion", (1, 2)), ("opus", None)],
)
def test_find_gives_the_place_of_a_models_family_and_effort(
    model: str, where: tuple[int, int] | None
) -> None:
    assert summarizer.find(FOUND, model) == where
