import fcntl
import gc
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest
from keyring.errors import KeyringLocked

# Ubuntu builds its PyGObject for its own Python alone: under another, the window cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

from minutas import github, gitlab, settings, store, summarizer, window, window_view, worker  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_handler_raised")

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
GB = 10**9
SUBTITLE = "2026-11-18 13:05 · 30 min"
KILL = ["systemctl", "--user", "kill", "--signal=SIGUSR1", "minutas-transcribe.service"]
DISMISSED = GLib.Error(
    "Dismissed by user", GLib.quark_to_string(Gtk.dialog_error_quark()), Gtk.DialogError.DISMISSED
)
NO_HANDLER = GLib.Error("No application handles this", "g-io-error-quark", Gio.IOErrorEnum.FAILED)

# What `minutas publish agaric` or `minutas summarize` does, as far as the window can tell: it
# appends the run its plan holds to the recording's log, says what the plan has it say, and exits
# as the plan has it exit.
FAKE_PUBLISH = """
import json, os, sys
from pathlib import Path
plan_file, *arguments = sys.argv[1:]
plan = json.loads(Path(plan_file).read_text())
Path(plan_file).with_name("called.json").write_text(json.dumps(arguments))
recordings = Path(os.environ["XDG_DATA_HOME"], "minutas", "recordings")
recording_id = next(argument for argument in arguments if (recordings / argument).is_dir())
log = recordings / recording_id / "runs.json"
if plan["run"] is not None:
    log.write_text(json.dumps([*json.loads(log.read_text()), plan["run"]]))
sys.stderr.write(plan["stderr"])
sys.exit(plan["exit"])
"""

# What `minutas update` does, as far as the window can tell: it prints what its plan has it print,
# on stdout then on stderr, and exits as the plan has it exit.
FAKE_UPDATE = """
import json, sys
from pathlib import Path
plan_file, *arguments = sys.argv[1:]
plan = json.loads(Path(plan_file).read_text())
Path(plan_file).with_name("called.json").write_text(json.dumps(arguments))
sys.stdout.write(plan["stdout"])
sys.stdout.flush()
sys.stderr.write(plan["stderr"])
sys.exit(plan["exit"])
"""

# What `minutas settings agaric` or `minutas settings gitlab` does, as far as the window can tell:
# it keeps what it was given, sets the space its plan names, and answers the option it was given,
# or its own name given none, as its plan for that command has it answer.
FAKE_SETTINGS = """
import json, os, sys
from pathlib import Path
plan_file, *arguments = sys.argv[1:]
command = arguments[1]
plan = json.loads(Path(plan_file).read_text())[command]
given = Path(plan_file).with_name(f"given-{command}.json")
before = json.loads(given.read_text()) if given.exists() else []
given.write_text(json.dumps([*before, [arguments, sys.stdin.read()]]))
answer = plan[arguments[-1]]
if answer["space"] is not None:
    saved = Path(os.environ["XDG_CONFIG_HOME"], "minutas", "settings.json")
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"agaric_space_id": answer["space"]}))
sys.stdout.write(answer["listed"])
sys.stderr.write(answer["said"])
sys.exit(answer["exit"])
"""


def run(action: str, status: str, **changes: object) -> store.Run:
    ended = {
        "action": action,
        "status": status,
        "queued_at": "2026-11-18T13:36:16+00:00",
        "error": None,
        "details": None,
        "output": None,
    }
    return cast("store.Run", {**ended, **changes})


def recording(runs: list[store.Run] | None = None, started: datetime = START) -> str:
    """A stopped recording with audio in both tracks."""
    recording_id = store.create_recording(started)["id"]
    store.mark_stopped(recording_id, started + timedelta(seconds=1834), incomplete=False)
    for name in store.TRACKS:
        (store.recording_dir(recording_id) / name).write_bytes(b"OggS " + name.encode())
    store.save_runs(recording_id, runs or [])
    return recording_id


def settle() -> None:
    """Runs everything the main loop has ready."""
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(may_block=False)


def wait_until(happened: Callable[[], bool]) -> None:
    """Runs the main loop until something it brings about has happened.

    It waits on the loop's own events, never a sleep, and gives up, failing, after far longer
    than any of them takes.
    """
    context = GLib.MainContext.default()
    gave_up: list[bool] = []

    def give_up() -> bool:
        gave_up.append(True)
        return GLib.SOURCE_REMOVE

    deadline = GLib.timeout_add_seconds(5, give_up)
    while not happened() and not gave_up:
        context.iteration(may_block=True)
    if not gave_up:
        GLib.source_remove(deadline)
    assert happened()


def draw(shown: window.HistoryWindow) -> None:
    """Runs the main loop until the window has drawn a few frames, laid out anew."""
    frames: list[Gdk.FrameClock] = []

    def drawn(_widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        frames.append(clock)
        return len(frames) < 3

    shown.add_tick_callback(drawn)
    wait_until(lambda: len(frames) == 3)


def open_dialog(shown: window.HistoryWindow) -> Adw.Dialog:
    """The dialog a press opened, once it is on screen.

    A person presses nothing they cannot see yet, and libadwaita never finishes closing a dialog
    that was answered before it was drawn.
    """
    draw(shown)
    dialog = shown.get_visible_dialog()
    assert dialog is not None
    return dialog


def descendants(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    yield widget
    child = widget.get_first_child()
    while child is not None:
        # GTK moves a tooltip to whatever pops up under the pointer: it is no part of that.
        if child.get_css_name() != "tooltip":
            yield from descendants(child)
        child = child.get_next_sibling()


def buttons(root: Gtk.Widget) -> dict[str, Gtk.Button]:
    """The buttons on offer under root, by label."""
    return {
        label: widget
        for widget in descendants(root)
        if isinstance(widget, Gtk.Button)
        and widget.get_visible()
        and (label := widget.get_label()) is not None
    }


def press(root: Gtk.Widget, label: str) -> None:
    pressed = buttons(root)[label]
    assert pressed.get_sensitive()
    pressed.emit("clicked")
    settle()


def texts(root: Gtk.Widget) -> list[str]:
    return [label.get_text() for label in descendants(root) if isinstance(label, Gtk.Label)]


def lines(row: Gtk.Widget) -> list[tuple[str, str | None]]:
    """The title and subtitle of the row's own line and of each line it expands to."""
    return [
        (line.get_title(), line.get_subtitle())
        for line in descendants(row)
        if isinstance(line, Adw.ActionRow)
    ]


def listed(shown: window.HistoryWindow) -> list[str]:
    titles: list[str] = []
    while (row := shown.list.get_row_at_index(len(titles))) is not None:
        assert isinstance(row, window.RecordingRow)
        titles.append(f"{row.get_title()} | {row.get_subtitle()}")
    return titles


def expand(shown: window.HistoryWindow, recording_id: str = FIRST) -> window.RecordingRow:
    row = shown.rows[recording_id]
    row.set_expanded(True)
    settle()
    return row


def row_menu(shown: window.HistoryWindow, recording_id: str = FIRST) -> list[str]:
    model = shown.rows[recording_id].menu.get_menu_model()
    assert model is not None
    return list(menu_entries(model))


def choose_from_row_menu(
    shown: window.HistoryWindow, label: str, recording_id: str = FIRST
) -> None:
    """Picks an item of the menu on the recording's own line, as a click on it would."""
    menu = shown.rows[recording_id].menu
    model = menu.get_menu_model()
    assert model is not None
    action, target = menu_entries(model)[label]
    assert menu.activate_action(action, target) is True
    settle()


SAID = [
    store.Segment(start=4.0, end=9.0, speaker="Speaker 1", text="Arrancamos."),
    store.Segment(start=11.0, end=14.0, speaker="Me", text="Dale."),
    store.Segment(start=15.0, end=16.0, speaker="Speaker 2", text="Sí."),
]
CHOICES = ["Ana Díaz", "bob_ruiz", "Custom name…"]


def transcribe(segments: list[store.Segment], recording_id: str = FIRST) -> None:
    """What a transcription that succeeds leaves, and tells the window."""
    folder = store.recording_dir(recording_id)
    (folder / "transcript.json").write_text(json.dumps(segments), encoding="utf-8")
    store.write_transcript(recording_id)
    store.save_runs(recording_id, [run("transcribe", "succeeded", output="transcript.md")])
    store.write_status()


def transcribed() -> str:
    """A recording of a calendar event, transcribed: two speakers on the call."""
    recording_id = recording()
    store.set_calendar_event(recording_id, "Weekly sync", ["Ana Díaz", "bob_ruiz"])
    transcribe(SAID, recording_id)
    return recording_id


def read_transcript(recording_id: str = FIRST) -> str:
    return (store.recording_dir(recording_id) / "transcript.md").read_text(encoding="utf-8")


def pickers(row: Gtk.Widget) -> dict[str, Gtk.MenuButton]:
    """The menu that names a speaker, by the title of the speaker's line."""
    return {
        line.get_title(): picker
        for line in descendants(row)
        if isinstance(line, Adw.ActionRow) and line.get_title().startswith("Speaker ")
        for picker in descendants(line)
        if isinstance(picker, Gtk.MenuButton)
    }


def choices(shown: window.HistoryWindow, picker: Gtk.MenuButton) -> list[str]:
    """What the open menu reads, top to bottom."""
    picker.popup()
    draw(shown)
    popover = picker.get_popover()
    assert popover is not None
    offered = [text for text in texts(popover) if text]
    picker.popdown()
    settle()
    return offered


def choose(shown: window.HistoryWindow, picker: Gtk.MenuButton, choice: str) -> None:
    """Activates the menu's item, as a click on it does."""
    menu = picker.get_menu_model()
    assert menu is not None
    [item] = [place for place, label in enumerate(choices(shown, picker)) if label == choice]
    action = menu.get_item_attribute_value(item, "action", None)
    assert action is not None
    assert shown.activate_action(
        action.get_string(), menu.get_item_attribute_value(item, "target", None)
    )
    settle()


def status_names() -> list[object]:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return [entry["name"] for entry in status["recent"]]


@pytest.fixture
def no_handler_raised(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """PyGObject prints what a handler raises and carries on; here it fails the test."""
    raised: list[BaseException] = []

    def record(_kind: type[BaseException], error: BaseException, _trace: TracebackType) -> None:
        raised.append(error)

    monkeypatch.setattr(sys, "excepthook", record)
    yield
    assert raised == []


@pytest.fixture
def history() -> Iterator[Callable[[], window.HistoryWindow]]:
    """Opens the window, on the recordings made so far."""
    opened: list[window.HistoryWindow] = []

    def open_window() -> window.HistoryWindow:
        # A dialog is gone the moment it closes, not an animation later.
        settings = Gtk.Settings.get_default()
        assert settings is not None
        settings.props.gtk_enable_animations = False
        shown = window.HistoryWindow()
        shown.present()
        opened.append(shown)
        settle()
        return shown

    yield open_window
    for shown in opened:
        shown.monitor.cancel()
        shown.destroy()
    settle()


class FakeSystemd:
    """Stands in for subprocess.run under the worker: systemd-run and systemctl."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.start_code = 0

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(args)
        code = self.start_code if args[0] == "systemd-run" else 0
        return subprocess.CompletedProcess(args, code, "", "Failed to connect to bus\n")


@pytest.fixture(autouse=True)
def systemd(monkeypatch: pytest.MonkeyPatch) -> FakeSystemd:
    fake = FakeSystemd()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


class FakeDevin:
    """A devin on PATH: the window offers Summarize only when there is one, and the Settings
    offer the models it lists. It answers as told, and keeps what it was asked."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.will(LISTED_MODELS)

    def will(self, stdout: str, *, stderr: str = "", exit_status: int = 0) -> None:
        (self.folder / "stdout").write_text(stdout)
        (self.folder / "stderr").write_text(stderr)
        script = self.folder / "devin"
        quoted = shlex.quote(str(self.folder))
        script.write_text(
            f'#!/bin/sh\necho "$@" > {quoted}/asked\ncat {quoted}/stdout\n'
            f"cat {quoted}/stderr >&2\nexit {exit_status}\n"
        )
        script.chmod(0o755)

    def asked(self) -> str:
        return (self.folder / "asked").read_text()


@pytest.fixture(autouse=True)
def devin_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> FakeDevin:
    fake = FakeDevin(tmp_path_factory.mktemp("bin"))
    monkeypatch.setenv("PATH", f"{fake.folder}{os.pathsep}{os.environ['PATH']}")
    return fake


@pytest.fixture(autouse=True)
def glab_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A glab that can publish, never run: the window offers Publish to GitLab only then."""
    monkeypatch.setattr(gitlab, "glab_problem", lambda: "")


@pytest.fixture(autouse=True)
def gh_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gh that can publish, never run: the window offers Publish to GitHub only then."""
    monkeypatch.setattr(github, "gh_problem", lambda: "")


@pytest.fixture(autouse=True)
def window_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A day after the recordings start: the age limit, on by default, deletes no test's audio
    whenever the tests run."""
    monkeypatch.setattr(window, "now", lambda: START + timedelta(days=1))


class FakePublish:
    """Stands in for the `minutas` the window runs to publish."""

    def __init__(self, folder: Path) -> None:
        self.script = folder / "fake_publish.py"
        self.script.write_text(FAKE_PUBLISH)
        self.plan = folder / "plan.json"
        self.will(None)

    def will(self, recorded: store.Run | None, *, exit_status: int = 0, stderr: str = "") -> None:
        self.plan.write_text(json.dumps({"run": recorded, "exit": exit_status, "stderr": stderr}))

    def called(self) -> list[str]:
        """The arguments of its last run; none when it never ran."""
        calls = self.plan.with_name("called.json")
        return cast("list[str]", json.loads(calls.read_text())) if calls.exists() else []


@pytest.fixture
def publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakePublish:
    fake = FakePublish(tmp_path)
    monkeypatch.setattr(window, "MINUTAS", [sys.executable, str(fake.script), str(fake.plan)])
    return fake


class FakeUpdate:
    """Stands in for the `minutas update` the window runs."""

    def __init__(self, folder: Path) -> None:
        self.script = folder / "fake_update.py"
        self.script.write_text(FAKE_UPDATE)
        self.plan = folder / "update_plan.json"
        self.will("Already on the latest release, v0.4.0.\n")

    def will(self, stdout: str, *, stderr: str = "", exit_status: int = 0) -> None:
        self.plan.write_text(json.dumps({"stdout": stdout, "stderr": stderr, "exit": exit_status}))

    def called(self) -> list[str]:
        calls = self.plan.with_name("called.json")
        return cast("list[str]", json.loads(calls.read_text())) if calls.exists() else []


@pytest.fixture
def minutas_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeUpdate:
    fake = FakeUpdate(tmp_path)
    monkeypatch.setattr(window, "MINUTAS", [sys.executable, str(fake.script), str(fake.plan)])
    return fake


class FakeSettings:
    """Stands in for the `minutas settings agaric` and `minutas settings gitlab` the window runs
    for its Agaric and GitLab settings."""

    def __init__(self, folder: Path) -> None:
        self.script = folder / "fake_settings.py"
        self.script.write_text(FAKE_SETTINGS)
        self.plan = folder / "settings_plan.json"
        self.plan.write_text("{}")
        self.will("--list-spaces", "", f"{AGARIC_UNSET}\n", exit_status=1)
        self.will("gitlab", "", f"{GLAB_LOGGED_IN}\n", command="gitlab")

    def will(  # noqa: PLR0913 - each is a separate part of the answer; all after said go by name
        self,
        option: str,
        listed: str,
        said: str,
        *,
        space: str | None = None,
        exit_status: int = 0,
        command: str = "agaric",
    ) -> None:
        plan = json.loads(self.plan.read_text())
        answer = {"listed": listed, "said": said, "space": space, "exit": exit_status}
        plan.setdefault(command, {})[option] = answer
        self.plan.write_text(json.dumps(plan))

    def given(self, command: str = "agaric") -> list[list[object]]:
        """Each run's arguments and what it read on stdin."""
        given = self.plan.with_name(f"given-{command}.json")
        return cast("list[list[object]]", json.loads(given.read_text())) if given.exists() else []


@pytest.fixture
def agaric_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSettings:
    fake = FakeSettings(tmp_path)
    monkeypatch.setattr(window, "MINUTAS", [sys.executable, str(fake.script), str(fake.plan)])
    return fake


def store_lock_held() -> bool:
    with (store.data_dir() / "store.lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


class Desktop:
    """Stands in for what GTK would hand to the desktop: files and links to open, a place to
    save a copy, a track to play, a folder to move to the Trash. Nothing is opened, asked or
    played, and the desktop's Trash is never touched: GLib reads XDG_DATA_HOME once, before
    any test sets it, so a real trash would land in the real one."""

    def __init__(self, trash: Path) -> None:
        self.trash_into: Path | GLib.Error = trash
        # Each folder moved to the Trash, and whether the store lock was held meanwhile.
        self.trashed: list[tuple[str | None, bool]] = []
        self.opened: list[str | None] = []
        self.open_error: GLib.Error | None = None
        self.save_as: Path | GLib.Error = DISMISSED
        self.suggested: list[str | None] = []
        self.open_from: Path | GLib.Error = DISMISSED
        self.filters: list[object] = []
        self.played: list[str | None] = []
        self.stopped: list[str | None] = []

    def launch(
        self,
        launcher: Gtk.FileLauncher | Gtk.UriLauncher,
        _parent: Gtk.Window,
        _cancellable: None,
        done: Callable[[GObject.Object, Gio.AsyncResult], None],
    ) -> None:
        if isinstance(launcher, Gtk.UriLauncher):
            self.opened.append(launcher.get_uri())
        else:
            self.opened.append(path_of(launcher.get_file()))
        done(launcher, Gio.Task.new(None, None, None))

    def launch_finish(self, _launcher: GObject.Object, _result: Gio.AsyncResult) -> bool:
        if self.open_error is not None:
            raise self.open_error
        return True

    def save(
        self,
        dialog: Gtk.FileDialog,
        _parent: Gtk.Window,
        _cancellable: None,
        done: Callable[[GObject.Object, Gio.AsyncResult, str], None],
        track: str,
    ) -> None:
        self.suggested.append(dialog.get_initial_name())
        done(dialog, Gio.Task.new(None, None, None), track)

    def save_finish(self, _dialog: Gtk.FileDialog, _result: Gio.AsyncResult) -> Gio.File:
        if isinstance(self.save_as, GLib.Error):
            raise self.save_as
        return Gio.File.new_for_path(str(self.save_as))

    def open(
        self,
        dialog: Gtk.FileDialog,
        _parent: Gtk.Window,
        _cancellable: None,
        done: Callable[[GObject.Object, Gio.AsyncResult, object], None],
        shown: object,
    ) -> None:
        offered = dialog.get_default_filter()
        assert offered is not None
        self.filters.append(offered.to_gvariant().unpack())
        done(dialog, Gio.Task.new(None, None, None), shown)

    def open_finish(self, _dialog: Gtk.FileDialog, _result: Gio.AsyncResult) -> Gio.File:
        if isinstance(self.open_from, GLib.Error):
            raise self.open_from
        return Gio.File.new_for_path(str(self.open_from))

    def play(self, stream: Gtk.MediaFile) -> None:
        self.played.append(path_of(stream.get_file()))

    def set_playing(self, stream: Gtk.MediaFile, playing: bool) -> None:  # noqa: FBT001
        assert playing is False
        self.stopped.append(path_of(stream.get_file()))

    def trash(self, file: Gio.File, _cancellable: None) -> bool:
        self.trashed.append((path_of(file), store_lock_held()))
        if isinstance(self.trash_into, GLib.Error):
            raise self.trash_into
        self.trash_into.mkdir(exist_ok=True)
        folder = Path(str(path_of(file)))
        folder.rename(self.trash_into / folder.name)
        return True


def path_of(file: Gio.File | None) -> str | None:
    assert file is not None
    return file.get_path()


def handed_to(fake: Desktop, name: str) -> Callable[..., object]:
    """A function a GTK class takes for its method: it hands the object on to the fake's.

    The fake's own method would not do: already bound to the fake, it would never be given the
    GTK object it was called on.
    """

    def method(gtk_object: GObject.Object, *arguments: object) -> object:
        return getattr(fake, name)(gtk_object, *arguments)

    return method


@pytest.fixture
def desktop_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Desktop:
    fake = Desktop(tmp_path / "Trash")
    for gtk_class, name in (
        (Gtk.FileLauncher, "launch"),
        (Gtk.FileLauncher, "launch_finish"),
        (Gtk.UriLauncher, "launch"),
        (Gtk.UriLauncher, "launch_finish"),
        (Gtk.FileDialog, "save"),
        (Gtk.FileDialog, "save_finish"),
        (Gtk.FileDialog, "open"),
        (Gtk.FileDialog, "open_finish"),
        (Gtk.MediaStream, "play"),
        (Gtk.MediaStream, "set_playing"),
        (Gio.File, "trash"),
    ):
        monkeypatch.setattr(gtk_class, name, handed_to(fake, name))
    return fake


def test_recordings_are_listed_newest_first(history: Callable[[], window.HistoryWindow]) -> None:
    recording(started=START + timedelta(hours=1))
    recording()
    store.set_typed_name(FIRST, "Weekly sync")
    store.create_recording(START + timedelta(days=1))
    store.save_runs("2026-11-19T13-05-42", [])

    shown = history()

    assert listed(shown) == [
        "Meeting | 2026-11-19 13:05 · Recording",
        "Meeting | 2026-11-18 14:05 · 30 min",
        f"Weekly sync | {SUBTITLE}",
    ]


def test_an_empty_history_says_so_with_the_whole_window(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()

    assert listed(shown) == []
    assert shown.pages.get_visible_child() is shown.empty
    assert shown.empty.get_title() == "No recordings yet"

    recording()
    store.write_status()
    wait_until(lambda: listed(shown) == [f"Meeting | {SUBTITLE}"])

    assert shown.pages.get_visible_child() is shown.scrolled


def test_the_window_waits_on_a_folder_that_exists(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    watched: list[tuple[str | None, bool]] = []
    monitor_file = Gio.File.monitor_file

    def spy(file: Gio.File, flags: Gio.FileMonitorFlags, cancellable: None) -> Gio.FileMonitor:
        watched.append((file.get_path(), store.data_dir().is_dir()))
        return monitor_file(file, flags, cancellable)

    monkeypatch.setattr(Gio.File, "monitor_file", spy)

    history()

    # On the first run nothing is there yet, and GLib would poll for the missing folder, every
    # few seconds for as long as the window stays open, instead of waiting for a change.
    assert watched == [(str(store.data_dir() / "status.json"), True)]


def test_a_recording_is_listed_once_its_run_log_exists(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    (store.recording_dir(FIRST) / "runs.json").unlink()

    shown = history()

    assert listed(shown) == []

    store.save_runs(FIRST, [])
    shown.refresh()

    assert listed(shown) == [f"Meeting | {SUBTITLE}"]


def test_the_recordings_are_read_inside_the_store_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    recording()
    held: list[bool] = []
    load_recording = store.load_recording

    def spy(recording_id: str) -> store.Recording:
        with (store.data_dir() / "store.lock").open("a") as file:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(True)
            else:
                held.append(False)
        return load_recording(recording_id)

    monkeypatch.setattr(store, "load_recording", spy)

    assert [view.recording_id for view in window.load_views({})] == [FIRST]
    assert held == [True]


def test_a_row_has_its_menu_on_the_line_it_shows_collapsed_and_expands_to_the_rest(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()
    row = shown.rows[FIRST]

    # Built only when the row is expanded: most rows never are.
    assert lines(row) == [("Meeting", SUBTITLE)]
    assert row_menu(shown) == ["Rename…", "Move to Trash…"]
    assert row.menu.get_tooltip_text() == "Recording Menu"

    expand(shown)

    assert lines(row) == [
        ("Meeting", SUBTITLE),
        ("Microphone", ""),
        ("Call", ""),
        ("Audio files", ""),
        ("Transcribe", "Not run yet"),
        ("Summarize", "Not summarized"),
        ("Publish to Agaric", "Not published"),
        ("Publish to GitLab", "Not published"),
        ("Publish to GitHub", "Not published"),
    ]
    assert list(buttons(row)) == [
        "Play",
        "Save a copy",
        "Open folder",
        "Delete now",
        "Transcribe",
        "Publish",
        "Publish…",
    ]


def test_names_and_errors_are_shown_as_written(history: Callable[[], window.HistoryWindow]) -> None:
    error = "Agaric already has a page named <R&D> 2026-11-18"
    recording([run("publish_agaric", "failed", error=error)])
    store.set_typed_name(FIRST, "<R&D>")
    shown = history()

    row = expand(shown)

    # Shown as markup, GTK would refuse the first and swallow the tag of the second.
    assert texts(row).count("<R&D>") == 1
    assert f"Failed: {error}" in texts(row)


def rename_dialog(shown: window.HistoryWindow) -> tuple[Adw.AlertDialog, Gtk.Entry]:
    choose_from_row_menu(shown, "Rename…")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    return dialog, entry


def test_rename_keeps_the_typed_name_and_says_where_the_name_came_from(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    store.set_calendar_event(FIRST, "Weekly", [])
    store.write_status()
    shown = history()
    row = shown.rows[FIRST]

    dialog, entry = rename_dialog(shown)
    assert dialog.get_heading() == "Rename"
    assert dialog.get_body() == (
        "From the calendar event. "
        "A typed name is kept for good, and the next Publish to Agaric applies it."
    )
    assert entry.get_text() == "Weekly"
    entry.set_text("  Weekly\tsync ")
    press(dialog, "Save")

    saved = store.load_recording(FIRST)
    assert (saved["name"], saved["name_source"]) == ("Weekly sync", "typed")
    # The top-bar menu reads the name from status.json, and so does this window.
    assert status_names() == ["Weekly sync"]
    wait_until(lambda: row.get_title() == "Weekly sync")
    # Renaming never expands or collapses the row.
    assert row.get_expanded() is False
    dialog, entry = rename_dialog(shown)
    assert dialog.get_body().startswith("Typed name. ")
    assert entry.get_text() == "Weekly sync"
    press(dialog, "Cancel")


@pytest.mark.parametrize(("typed", "answer"), [("Weekly sync", "Cancel"), (" ", "Save")])
def test_rename_changes_nothing_when_cancelled_or_left_empty(
    history: Callable[[], window.HistoryWindow], typed: str, answer: str
) -> None:
    recording()
    shown = history()

    dialog, entry = rename_dialog(shown)
    entry.set_text(typed)
    press(dialog, answer)

    saved = store.load_recording(FIRST)
    assert (saved["name"], saved["name_source"]) == ("Meeting", "default")
    assert shown.get_visible_dialog() is None


def test_the_speakers_sit_between_the_transcription_and_the_summary(
    history: Callable[[], window.HistoryWindow],
) -> None:
    transcribed()
    store.set_speaker_name(FIRST, "Speaker 1", "Ana Díaz")
    shown = history()

    row = expand(shown)

    assert lines(row)[4:] == [
        ("Transcribe", "Succeeded"),
        ("Speaker 1", "Ana Díaz"),
        ("Speaker 2", ""),
        ("Summarize", "Not summarized"),
        ("Publish to Agaric", "Not published"),
        ("Publish to GitLab", "Not published"),
        ("Publish to GitHub", "Not published"),
    ]
    # An attendee's "_" is a letter of the name, not the mark of a shortcut key.
    assert {speaker: choices(shown, picker) for speaker, picker in pickers(row).items()} == {
        "Speaker 1": CHOICES,
        "Speaker 2": CHOICES,
    }


def test_without_a_calendar_event_a_speaker_can_still_be_named(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    transcribe(SAID)
    shown = history()

    row = expand(shown)

    assert choices(shown, pickers(row)["Speaker 1"]) == ["Custom name…"]


@pytest.mark.parametrize("attendee", ["Ana Díaz", "bob_ruiz"])
def test_picking_an_attendee_names_the_speaker_in_the_transcript(
    history: Callable[[], window.HistoryWindow], attendee: str
) -> None:
    transcribed()
    shown = history()
    row = expand(shown)

    choose(shown, pickers(row)["Speaker 2"], attendee)

    assert store.load_recording(FIRST).get("speakers") == {
        "Speaker 1": "Speaker 1",
        "Speaker 2": attendee,
    }
    assert read_transcript() == (
        "[00:00:04] **Speaker 1:** Arrancamos.\n"
        "\n"
        "[00:00:11] **Me:** Dale.\n"
        "\n"
        f"[00:00:15] **{attendee}:** Sí.\n"
    )
    wait_until(lambda: ("Speaker 2", attendee) in lines(row))
    assert ("Speaker 1", "") in lines(row)


def test_a_custom_name_is_typed_and_kept(history: Callable[[], window.HistoryWindow]) -> None:
    transcribed()
    shown = history()
    row = expand(shown)

    choose(shown, pickers(row)["Speaker 1"], "Custom name…")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Name Speaker 1"
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    assert entry.get_text() == ""
    entry.set_text("  Caro\tPaz ")
    press(dialog, "Save")

    assert store.load_recording(FIRST).get("speakers") == {
        "Speaker 1": "Caro Paz",
        "Speaker 2": "Speaker 2",
    }
    assert read_transcript().splitlines()[0] == "[00:00:04] **Caro Paz:** Arrancamos."
    wait_until(lambda: ("Speaker 1", "Caro Paz") in lines(row))

    # Typed again, it starts from the name it has.
    choose(shown, pickers(row)["Speaker 1"], "Custom name…")
    dialog = open_dialog(shown)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    assert entry.get_text() == "Caro Paz"
    press(dialog, "Cancel")


@pytest.mark.parametrize(("typed", "answer"), [("Caro Paz", "Cancel"), (" ", "Save")])
def test_a_custom_name_changes_nothing_when_cancelled_or_left_empty(
    history: Callable[[], window.HistoryWindow], typed: str, answer: str
) -> None:
    transcribed()
    before = read_transcript()
    shown = history()
    row = expand(shown)

    choose(shown, pickers(row)["Speaker 1"], "Custom name…")
    dialog = open_dialog(shown)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    entry.set_text(typed)
    press(dialog, answer)

    assert store.load_recording(FIRST).get("speakers") == {
        "Speaker 1": "Speaker 1",
        "Speaker 2": "Speaker 2",
    }
    assert read_transcript() == before
    assert shown.get_visible_dialog() is None


def test_the_speakers_follow_each_transcription(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()
    row = expand(shown)
    assert lines(row)[4:] == [
        ("Transcribe", "Not run yet"),
        ("Summarize", "Not summarized"),
        ("Publish to Agaric", "Not published"),
        ("Publish to GitLab", "Not published"),
        ("Publish to GitHub", "Not published"),
    ]

    transcribe(SAID)

    wait_until(lambda: ("Speaker 2", "") in lines(row))
    assert lines(row)[4:] == [
        ("Transcribe", "Succeeded"),
        ("Speaker 1", ""),
        ("Speaker 2", ""),
        ("Summarize", "Not summarized"),
        ("Publish to Agaric", "Not published"),
        ("Publish to GitLab", "Not published"),
        ("Publish to GitHub", "Not published"),
    ]

    # Run again hears one speaker fewer.
    transcribe(SAID[:2])

    wait_until(lambda: ("Speaker 2", "") not in lines(row))
    assert lines(row)[4:] == [
        ("Transcribe", "Succeeded"),
        ("Speaker 1", ""),
        ("Summarize", "Not summarized"),
        ("Publish to Agaric", "Not published"),
        ("Publish to GitLab", "Not published"),
        ("Publish to GitHub", "Not published"),
    ]


def test_a_failed_transcription_shows_why_with_retry_and_details(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording([run("transcribe", "failed", error="Out of memory", details="run-0.log")])
    log = "loading the model\ntranscribing turn 3 of 40\nOut of memory\n"
    (store.recording_dir(FIRST) / "run-0.log").write_text(log)
    shown = history()

    row = expand(shown)

    assert ("Transcribe", "Failed: Out of memory") in lines(row)
    assert shown.rows[FIRST].warning.get_visible() is True
    assert list(buttons(shown.details[FIRST].transcribe.row)) == ["Details", "Retry"]
    assert shown.details[FIRST].transcribe.failed.get_visible() is True
    assert shown.details[FIRST].publish.failed.get_visible() is False

    press(row, "Details")

    dialog = open_dialog(shown)
    assert dialog.get_title() == "Details"
    [text] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.TextView)]
    assert text.get_buffer().props.text == log
    assert text.get_editable() is False

    press(dialog, "Copy")

    copied: list[str | None] = []
    shown.get_clipboard().read_text_async(
        None, lambda clipboard, result: copied.append(clipboard.read_text_finish(result))
    )
    wait_until(lambda: copied != [])
    assert copied == [log]


def test_retry_queues_a_run_and_the_row_follows_it(
    history: Callable[[], window.HistoryWindow], systemd: FakeSystemd
) -> None:
    recording([run("transcribe", "failed", error="Out of memory")])
    shown = history()
    row = expand(shown)

    press(row, "Retry")

    assert [(r["action"], r["status"]) for r in store.load_runs(FIRST)] == [
        ("transcribe", "failed"),
        ("transcribe", "queued"),
    ]
    assert [command[0] for command in systemd.commands] == ["systemd-run"]
    # Nothing but the rewritten status.json tells the window.
    wait_until(lambda: ("Transcribe", "Queued") in lines(row))
    assert list(buttons(shown.details[FIRST].transcribe.row)) == ["Cancel"]
    assert row.get_subtitle() == f"{SUBTITLE} · Waiting to transcribe"
    assert shown.rows[FIRST].warning.get_visible() is False


def test_a_worker_that_does_not_start_shows_as_a_failed_run(
    history: Callable[[], window.HistoryWindow],
    systemd: FakeSystemd,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording()
    systemd.start_code = 1
    monkeypatch.setattr(worker, "START_TIMEOUT_SECONDS", 0.0)
    shown = history()
    row = expand(shown)

    press(row, "Transcribe")

    wait_until(lambda: ("Transcribe", "Failed: Failed to connect to bus") in lines(row))
    assert list(buttons(shown.details[FIRST].transcribe.row)) == ["Retry"]


def test_cancel_stops_the_running_transcription(
    history: Callable[[], window.HistoryWindow], systemd: FakeSystemd
) -> None:
    recording([run("transcribe", "succeeded"), run("transcribe", "running")])
    shown = history()
    row = expand(shown)
    assert buttons(row)["Delete now"].get_sensitive() is False
    assert buttons(row)["Delete now"].get_tooltip_text() == "Cancel the transcription first"

    press(row, "Cancel")

    assert [r["status"] for r in store.load_runs(FIRST)] == ["succeeded", "cancelled"]
    assert systemd.commands == [KILL]
    wait_until(lambda: ("Transcribe", "Cancelled") in lines(row))
    assert list(buttons(shown.details[FIRST].transcribe.row)) == ["Retry"]
    assert buttons(row)["Delete now"].get_sensitive() is True
    assert buttons(row)["Delete now"].get_tooltip_text() is None


def test_a_running_transcription_shows_how_far_it_has_got_until_it_ends(
    history: Callable[[], window.HistoryWindow],
) -> None:
    progress = store.Progress(phase="identifying speakers", percent=40)
    recording([run("transcribe", "running", progress=progress)])
    shown = history()
    row = expand(shown)
    controls = shown.details[FIRST]

    assert ("Transcribe", "Identifying speakers · 40%") in lines(row)
    assert row.get_subtitle() == f"{SUBTITLE} · Identifying speakers · 40%"
    assert controls.transcribe.progress.get_visible() is True
    assert controls.transcribe.progress.get_fraction() == 0.4
    assert [
        action.progress.get_visible()
        for action in (controls.summarize, controls.publish, controls.publish_gitlab)
    ] == [False] * 3

    # The worker says it got further, then ends the run.
    store.save_runs(FIRST, [run("transcribe", "running", progress={**progress, "percent": 75})])
    store.write_status()
    wait_until(lambda: ("Transcribe", "Identifying speakers · 75%") in lines(row))
    assert controls.transcribe.progress.get_fraction() == 0.75
    assert row.get_subtitle() == f"{SUBTITLE} · Identifying speakers · 75%"
    store.save_runs(FIRST, [run("transcribe", "succeeded", output="transcript.md")])
    store.write_status()
    wait_until(lambda: ("Transcribe", "Succeeded") in lines(row))
    assert controls.transcribe.progress.get_visible() is False


def test_outputs_and_the_folder_open_in_their_default_apps(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording(
        [
            run("transcribe", "succeeded", output="transcript.md"),
            run("publish_agaric", "succeeded", output="agaric://page/01HX"),
        ]
    )
    shown = history()
    row = expand(shown)
    assert list(buttons(shown.details[FIRST].transcribe.row)) == ["Open transcript", "Run again"]
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Publish again"]

    press(row, "Open transcript")
    press(row, "Open in Agaric")
    press(row, "Open folder")

    folder = store.recording_dir(FIRST)
    assert desktop_calls.opened == [
        str(folder / "transcript.md"),
        "agaric://page/01HX",
        str(folder),
    ]
    assert "No application handles this" not in texts(shown)


@pytest.mark.parametrize(("error", "said"), [(NO_HANDLER, True), (DISMISSED, False)])
def test_a_link_that_does_not_open_says_why_unless_the_person_gave_up(
    history: Callable[[], window.HistoryWindow],
    desktop_calls: Desktop,
    error: GLib.Error,
    *,
    said: bool,
) -> None:
    recording([run("publish_agaric", "succeeded", output="agaric://page/01HX")])
    desktop_calls.open_error = error
    shown = history()

    press(expand(shown), "Open in Agaric")

    assert ("No application handles this" in texts(shown)) is said
    assert "Dismissed by user" not in texts(shown)


def test_play_puts_a_player_for_the_track_in_its_row(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording()
    shown = history()
    row = expand(shown)
    call = shown.details[FIRST].tracks["call.opus"]

    buttons(call.row)["Play"].emit("clicked")

    assert desktop_calls.played == [str(store.recording_dir(FIRST) / "call.opus")]
    assert desktop_calls.stopped == []
    players = [widget for widget in descendants(row) if isinstance(widget, Gtk.MediaControls)]
    assert [player.is_ancestor(call.row) for player in players] == [True]
    assert list(buttons(call.row)) == ["Save a copy"]
    assert "Play" in buttons(shown.details[FIRST].tracks["mic.opus"].row)

    # Collapsing the row stops it and drops the player, and its decoder with it.
    expanded_to = lines(row)
    row.set_expanded(False)

    assert desktop_calls.stopped == [str(store.recording_dir(FIRST) / "call.opus")]
    assert [widget for widget in descendants(row) if isinstance(widget, Gtk.MediaControls)] == []
    assert list(buttons(call.row)) == ["Play", "Save a copy"]
    # Expanded again it is the same lines, not a second set.
    expand(shown)
    assert lines(row) == expanded_to


def test_save_a_copy_writes_the_track_where_the_person_chose(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    recording()
    desktop_calls.save_as = tmp_path / "copy.opus"
    desktop_calls.save_as.write_bytes(b"what the dialog asked to replace")
    shown = history()
    expand(shown)

    buttons(shown.details[FIRST].tracks["mic.opus"].row)["Save a copy"].emit("clicked")

    assert desktop_calls.suggested == [f"{FIRST}-mic.opus"]
    assert (tmp_path / "copy.opus").read_bytes() == b"OggS mic.opus"
    assert (store.recording_dir(FIRST) / "mic.opus").read_bytes() == b"OggS mic.opus"
    # Nothing went wrong, so nothing is said about the copy.
    assert [text for text in texts(shown) if "copy.opus" in text] == []


def test_save_a_copy_does_nothing_when_the_dialog_is_closed(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    recording()
    shown = history()
    expand(shown)

    buttons(shown.details[FIRST].tracks["call.opus"].row)["Save a copy"].emit("clicked")

    assert desktop_calls.suggested == [f"{FIRST}-call.opus"]
    assert "Dismissed by user" not in texts(shown)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data"]


def test_a_copy_that_cannot_be_written_says_why(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    recording()
    desktop_calls.save_as = tmp_path / "missing" / "copy.opus"
    shown = history()
    expand(shown)

    buttons(shown.details[FIRST].tracks["call.opus"].row)["Save a copy"].emit("clicked")

    said = [text for text in texts(shown) if "copy.opus" in text]
    assert ["No such file or directory" in text for text in said] == [True]


def test_delete_now_removes_the_audio_once_confirmed(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording([run("transcribe", "succeeded", output="transcript.md")])
    folder = store.recording_dir(FIRST)
    (folder / "transcript.md").write_text("[00:00:01] **Me:** Hola.\n")
    shown = history()
    row = expand(shown)
    buttons(shown.details[FIRST].tracks["mic.opus"].row)["Play"].emit("clicked")

    press(row, "Delete now")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Delete the audio?"
    assert dialog.get_body() == window.DELETE_WARNING
    assert sorted(path.name for path in folder.glob("*.opus")) == ["call.opus", "mic.opus"]
    press(dialog, "Delete")

    assert sorted(path.name for path in folder.iterdir()) == [
        "recording.json",
        "runs.json",
        "transcript.md",
    ]
    # The entry and its output stay; what needs the audio is off, and says why.
    assert lines(row)[1:5] == [
        ("Microphone", "Deleted"),
        ("Call", "Deleted"),
        ("Audio files", ""),
        ("Transcribe", "Succeeded"),
    ]
    offered = buttons(row)
    assert [offered[label].get_sensitive() for label in ("Play", "Save a copy")] == [False, False]
    assert offered["Run again"].get_sensitive() is False
    assert offered["Run again"].get_tooltip_text() == "The audio was deleted"
    assert offered["Delete now"].get_sensitive() is False
    assert offered["Open transcript"].get_sensitive() is True
    assert offered["Publish"].get_sensitive() is True
    # What was playing stopped with it.
    assert [widget for widget in descendants(row) if isinstance(widget, Gtk.MediaControls)] == []
    assert desktop_calls.played == [str(folder / "mic.opus")]


def test_delete_now_keeps_the_audio_when_not_confirmed(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()
    row = expand(shown)

    press(row, "Delete now")
    press(open_dialog(shown), "Cancel")

    assert store.holds_audio(FIRST) is True
    assert buttons(row)["Delete now"].get_sensitive() is True


def trash_dialog(shown: window.HistoryWindow) -> Adw.AlertDialog:
    choose_from_row_menu(shown, "Move to Trash…")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    return dialog


def test_move_to_trash_sends_the_whole_recording_there_once_confirmed(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    transcribed()
    later = recording(started=START + timedelta(hours=1))
    store.write_status()
    folder = store.recording_dir(FIRST)
    shown = history()
    expand(shown)
    buttons(shown.details[FIRST].tracks["mic.opus"].row)["Play"].emit("clicked")
    held = sorted(path.name for path in folder.iterdir())

    dialog = trash_dialog(shown)
    assert dialog.get_heading() == "Move the recording to the Trash?"
    assert dialog.get_body() == window.TRASH_WARNING
    assert desktop_calls.trashed == []
    press(dialog, "Move to Trash")

    # Inside the store lock, where the worker cannot take a run for it meanwhile.
    assert desktop_calls.trashed == [(str(folder), True)]
    assert folder.exists() is False
    assert sorted(path.name for path in (tmp_path / "Trash" / FIRST).iterdir()) == held
    # What was playing stopped first.
    assert desktop_calls.stopped == [str(folder / "mic.opus")]
    assert list(shown.rows) == [later]
    assert shown.details == {}
    # The top-bar menu drops it too.
    assert status_names() == ["Meeting"]


def test_move_to_trash_keeps_the_recording_when_not_confirmed(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording()
    shown = history()

    press(trash_dialog(shown), "Cancel")

    assert desktop_calls.trashed == []
    assert store.is_recording(FIRST) is True
    assert list(shown.rows) == [FIRST]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_move_to_trash_refuses_while_a_transcription_waits_or_runs(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, status: str
) -> None:
    recording([run("transcribe", status)])
    shown = history()

    choose_from_row_menu(shown, "Move to Trash…")

    assert shown.get_visible_dialog() is None
    assert "Cancel the transcription first" in texts(shown)
    assert desktop_calls.trashed == []
    assert store.is_recording(FIRST) is True


def test_move_to_trash_refuses_while_recording(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    store.create_recording(START)
    shown = history()

    choose_from_row_menu(shown, "Move to Trash…")

    assert shown.get_visible_dialog() is None
    assert "Still recording" in texts(shown)
    assert desktop_calls.trashed == []


def test_move_to_trash_refuses_while_the_window_publishes_it(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, publish: FakePublish
) -> None:
    recording()
    publish.will(run("publish_agaric", "succeeded", output="agaric://page/01HX"))
    shown = history()
    row = expand(shown)
    press(row, "Publish")

    choose_from_row_menu(shown, "Move to Trash…")

    assert shown.get_visible_dialog() is None
    assert "Publish to Agaric is still running" in texts(shown)
    assert desktop_calls.trashed == []
    wait_until(lambda: not shown.running[window.PUBLISH])
    assert store.is_recording(FIRST) is True


def test_move_to_trash_asks_again_once_confirmed(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording()
    shown = history()
    dialog = trash_dialog(shown)

    # Queued by another process while the dialog was open, not yet shown.
    store.save_runs(FIRST, [run("transcribe", "queued")])
    press(dialog, "Move to Trash")

    assert "Cancel the transcription first" in texts(shown)
    assert desktop_calls.trashed == []
    assert store.is_recording(FIRST) is True


def test_a_trash_that_fails_says_why_and_deletes_nothing(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    recording()
    store.write_status()
    desktop_calls.trash_into = GLib.Error(
        "Unable to find or create trash directory",
        "g-io-error-quark",
        Gio.IOErrorEnum.NOT_SUPPORTED,
    )
    shown = history()

    press(trash_dialog(shown), "Move to Trash")

    assert desktop_calls.trashed == [(str(store.recording_dir(FIRST)), True)]
    assert "Can't move it to the Trash: Unable to find or create trash directory" in texts(shown)
    assert store.holds_audio(FIRST) is True
    assert list(shown.rows) == [FIRST]
    assert status_names() == ["Meeting"]


def test_publish_runs_the_command_and_shows_its_result(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording()
    publish.will(run("publish_agaric", "succeeded", output="agaric://page/01HX"))
    shown = history()
    row = expand(shown)

    press(row, "Publish")

    # Until the command exits nothing else can be published from this row.
    assert ("Publish to Agaric", "Running") in lines(row)
    assert buttons(row)["Publish"].get_sensitive() is False
    assert buttons(row)["Publish"].get_tooltip_text() == "Publishing"

    wait_until(lambda: not shown.running[window.PUBLISH])

    assert publish.called() == ["publish", "agaric", FIRST]
    assert ("Publish to Agaric", "Succeeded") in lines(row)
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Publish again"]
    assert buttons(row)["Publish again"].get_sensitive() is True
    assert [text for text in texts(shown) if text.startswith("Publish to Agaric stopped")] == []


TAKEN = run(
    "publish_agaric",
    "failed",
    error="Agaric already has a page named Meeting 2026-11-18",
    details="run-0.log",
    choice={"kind": "name_taken", "page_id": "01HXPAGE"},
)


def test_a_taken_name_offers_that_page(
    history: Callable[[], window.HistoryWindow],
    publish: FakePublish,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording()
    publish.will(TAKEN, exit_status=1)
    spawn = Gio.Subprocess.new

    def spawn_and_let_it_run(argv: list[str], flags: Gio.SubprocessFlags) -> Gio.Subprocess:
        # The fastest command there is: it has recorded its run before the window looks.
        command = spawn(argv, flags)
        command.wait()
        return command

    monkeypatch.setattr(Gio.Subprocess, "new", spawn_and_let_it_run)
    shown = history()
    row = expand(shown)

    press(row, "Publish")
    wait_until(lambda: not shown.running[window.PUBLISH])

    assert ("Publish to Agaric", f"Failed: {TAKEN['error']}") in lines(row)
    assert list(buttons(shown.details[FIRST].publish.row)) == [
        "Details",
        "Use that page",
        "Rename",
    ]
    # The command said why in its run: the window adds nothing.
    assert [text for text in texts(shown) if text.startswith("Publish to Agaric stopped")] == []

    publish.will(run("publish_agaric", "succeeded", output="agaric://page/01HXPAGE"))
    press(row, "Use that page")
    wait_until(lambda: not shown.running[window.PUBLISH])

    assert publish.called() == ["publish", "agaric", FIRST, "--page=01HXPAGE"]
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Publish again"]


def test_a_taken_name_offers_a_rename(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TAKEN])
    publish.will(run("publish_agaric", "succeeded", output="agaric://page/01HY"))
    shown = history()
    row = expand(shown)

    press(row, "Rename")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Rename"
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    assert entry.get_text() == "Meeting"
    # A name that looks like an option is still the name.
    entry.set_text("--page 2")
    press(dialog, "Save")
    wait_until(lambda: not shown.running[window.PUBLISH])

    assert publish.called() == ["publish", "agaric", FIRST, "--name=--page 2"]
    assert ("Publish to Agaric", "Succeeded") in lines(row)


def test_a_typed_name_agaric_refused_offers_only_a_rename(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    refused = run(
        "publish_agaric",
        "failed",
        error="Agaric already has a page named Weekly sync 2026-11-18",
        choice={"kind": "rename_taken"},
    )
    recording([run("publish_agaric", "succeeded", output="agaric://page/01HX"), refused])
    publish.will(run("publish_agaric", "succeeded", output="agaric://page/01HX"))
    shown = history()
    row = expand(shown)

    # No other page to use: the recording has its own, which keeps its link.
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Rename"]

    press(row, "Rename")
    dialog = open_dialog(shown)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    entry.set_text("Weekly sync, part two")
    press(dialog, "Save")
    wait_until(lambda: not shown.running[window.PUBLISH])

    assert publish.called() == ["publish", "agaric", FIRST, "--name=Weekly sync, part two"]
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Publish again"]


def test_a_publish_command_that_dies_without_a_run_is_reported(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording()
    publish.will(None, exit_status=2, stderr="usage: minutas\nminutas: error: invalid choice\n")
    shown = history()
    row = expand(shown)

    press(row, "Publish")
    wait_until(lambda: not shown.running[window.PUBLISH])

    assert "Publish to Agaric stopped: minutas: error: invalid choice" in texts(shown)
    assert ("Publish to Agaric", "Not published") in lines(row)
    assert buttons(row)["Publish"].get_sensitive() is True


def test_a_publish_command_that_dies_mid_run_fails_its_run(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([run("publish_agaric", "succeeded", output="agaric://page/01HX")])
    traceback = "Traceback (most recent call last):\n  ...\nRuntimeError: boom\n"
    publish.will(run("publish_agaric", "running"), exit_status=1, stderr=traceback)
    shown = history()
    row = expand(shown)

    press(row, "Publish again")
    wait_until(lambda: not shown.running[window.PUBLISH])

    reason = "Publish to Agaric stopped: RuntimeError: boom"
    assert store.load_runs(FIRST)[-1] == run("publish_agaric", "failed", error=reason)
    assert ("Publish to Agaric", f"Failed: {reason}") in lines(row)
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Retry"]
    assert row.warning.get_visible() is True


ISSUE_LINK = "https://gitlab.example.com/group/project/-/issues/7"
SUMMARIZED = [
    run("transcribe", "succeeded", output="transcript.md"),
    run("summarize", "succeeded", output="summary.md"),
]


def link_dialog(shown: window.HistoryWindow, label: str) -> tuple[Adw.AlertDialog, Gtk.Entry]:
    press(shown.details[FIRST].publish_gitlab.row, label)
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    return dialog, entry


def test_publish_to_gitlab_says_what_the_link_will_get_and_posts_there(
    history: Callable[[], window.HistoryWindow], publish: FakePublish, desktop_calls: Desktop
) -> None:
    recording(SUMMARIZED)
    output = f"{ISSUE_LINK}#note_302"
    publish.will(run("publish_gitlab", "succeeded", output=output, link=ISSUE_LINK))
    shown = history()
    row = expand(shown)

    dialog, entry = link_dialog(shown, "Publish…")

    assert (dialog.get_heading(), entry.get_text()) == ("Publish to GitLab", "")
    said: list[tuple[str, bool]] = []
    for typed in (
        "",
        "https://gitlab.example.com/group",
        "https://gitlab.example.com/group/project",
        f" {ISSUE_LINK}#note_1 ",
    ):
        entry.set_text(typed)
        said.append((dialog.get_body(), dialog.get_response_enabled("publish")))
    assert said == [
        ("Paste an issue link to comment on it, or a project link to open a new issue", False),
        ("Not a GitLab issue or project link", False),
        ("Opens a new issue in group/project", True),
        ("Comments on issue #7 of group/project", True),
    ]

    press(dialog, "Publish")
    assert ("Publish to GitLab", "Running") in lines(row)
    wait_until(lambda: not shown.running[window.PUBLISH_GITLAB])

    assert publish.called() == ["publish", "gitlab", FIRST, ISSUE_LINK]
    assert ("Publish to GitLab", "Succeeded") in lines(row)
    assert list(buttons(shown.details[FIRST].publish_gitlab.row)) == [
        "Open in GitLab",
        "Publish again…",
    ]

    press(row, "Open in GitLab")

    assert desktop_calls.opened == [output]


def test_retry_asks_again_starting_from_the_failed_runs_link(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    error = "Could not upload the audio: it is over gitlab.example.com's upload limit"
    recording([*SUMMARIZED, run("publish_gitlab", "failed", error=error, link=ISSUE_LINK)])
    publish.will(run("publish_gitlab", "succeeded", output=f"{ISSUE_LINK}#note_1"))
    shown = history()
    row = expand(shown)
    assert ("Publish to GitLab", f"Failed: {error}") in lines(row)
    assert row.warning.get_visible() is True

    dialog, entry = link_dialog(shown, "Retry…")
    assert (entry.get_text(), dialog.get_body()) == (
        ISSUE_LINK,
        "Comments on issue #7 of group/project",
    )
    press(dialog, "Publish")
    wait_until(lambda: not shown.running[window.PUBLISH_GITLAB])

    assert publish.called() == ["publish", "gitlab", FIRST, ISSUE_LINK]


def test_cancelling_the_link_dialog_posts_nothing(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording(SUMMARIZED)
    shown = history()
    expand(shown)

    dialog, entry = link_dialog(shown, "Publish…")
    entry.set_text(ISSUE_LINK)
    press(dialog, "Cancel")
    settle()

    assert (shown.running[window.PUBLISH_GITLAB], publish.called()) == ({}, [])


PROJECT_LINK = "https://gitlab.example.com/group/project"


@pytest.mark.parametrize(
    ("runs", "label", "link"),
    [
        (SUMMARIZED, "Publish…", PROJECT_LINK),
        (
            [*SUMMARIZED, run("publish_gitlab", "failed", error="x", link=ISSUE_LINK)],
            "Retry…",
            ISSUE_LINK,
        ),
    ],
    ids=["never published", "published before"],
)
def test_publish_to_gitlab_starts_from_the_default_link_until_a_run_has_one(
    history: Callable[[], window.HistoryWindow], runs: list[store.Run], label: str, link: str
) -> None:
    settings.set_gitlab_link(PROJECT_LINK)
    recording(runs)
    shown = history()
    expand(shown)

    dialog, entry = link_dialog(shown, label)

    assert (entry.get_text(), dialog.get_response_enabled("publish")) == (link, True)
    press(dialog, "Cancel")


def test_publish_to_gitlab_waits_for_a_summary(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording([run("transcribe", "succeeded", output="transcript.md")])
    shown = history()
    expand(shown)
    publish_button = buttons(shown.details[FIRST].publish_gitlab.row)["Publish…"]

    assert publish_button.get_sensitive() is False
    assert publish_button.get_tooltip_text() == "Summarize first"


def test_without_glab_publish_to_gitlab_says_how_to_install_it(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    install = "glab not found: install glab 1.91.0 or newer"
    monkeypatch.setattr(gitlab, "glab_problem", lambda: install)
    recording(SUMMARIZED)
    shown = history()
    row = expand(shown)

    assert ("Publish to GitLab", f"Not published · {install}") in lines(row)
    publish_button = buttons(shown.details[FIRST].publish_gitlab.row)["Publish…"]
    assert publish_button.get_sensitive() is False
    assert publish_button.get_tooltip_text() == install


GITHUB_ISSUE = "https://github.com/owner/repo/issues/7"
GITHUB_REPOSITORY = "https://github.com/owner/repo"
GIST_NOTE = (
    "The transcript goes to a secret gist, which anyone with its link can read. GitHub takes no "
    "audio, so it is left out."
)


def github_dialog(shown: window.HistoryWindow, label: str) -> tuple[Adw.AlertDialog, Gtk.Entry]:
    press(shown.details[FIRST].publish_github.row, label)
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    return dialog, entry


def test_publish_to_github_says_what_the_link_will_get_and_posts_there(
    history: Callable[[], window.HistoryWindow], publish: FakePublish, desktop_calls: Desktop
) -> None:
    recording(SUMMARIZED)
    output = f"{GITHUB_ISSUE}#issuecomment-302"
    publish.will(run("publish_github", "succeeded", output=output, link=GITHUB_ISSUE))
    shown = history()
    row = expand(shown)

    dialog, entry = github_dialog(shown, "Publish…")

    assert (dialog.get_heading(), entry.get_text()) == ("Publish to GitHub", "")
    said: list[tuple[str, bool]] = []
    for typed in ("", "https://github.com/owner", GITHUB_REPOSITORY, f" {GITHUB_ISSUE}#x "):
        entry.set_text(typed)
        said.append((dialog.get_body(), dialog.get_response_enabled("publish")))
    assert said == [
        (f"{window.ASK_GITHUB_LINK}\n\n{GIST_NOTE}", False),
        (f"{window.NOT_A_GITHUB_LINK}\n\n{GIST_NOTE}", False),
        (f"Opens a new issue in owner/repo\n\n{GIST_NOTE}", True),
        (f"Comments on issue #7 of owner/repo\n\n{GIST_NOTE}", True),
    ]

    press(dialog, "Publish")
    assert ("Publish to GitHub", "Running") in lines(row)
    wait_until(lambda: not shown.running[window.PUBLISH_GITHUB])

    assert publish.called() == ["publish", "github", FIRST, GITHUB_ISSUE]
    assert ("Publish to GitHub", "Succeeded") in lines(row)
    assert list(buttons(shown.details[FIRST].publish_github.row)) == [
        "Open in GitHub",
        "Publish again…",
    ]

    press(shown.details[FIRST].publish_github.row, "Open in GitHub")

    assert desktop_calls.opened == [output]


def test_publish_to_github_starts_from_the_last_runs_link_else_the_default_one(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    settings.set_github_link(GITHUB_REPOSITORY)
    recording(SUMMARIZED)
    shown = history()
    expand(shown)

    dialog, entry = github_dialog(shown, "Publish…")
    assert (entry.get_text(), dialog.get_body()) == (
        GITHUB_REPOSITORY,
        f"Opens a new issue in owner/repo\n\n{GIST_NOTE}",
    )
    press(dialog, "Cancel")
    settle()
    assert (shown.running[window.PUBLISH_GITHUB], publish.called()) == ({}, [])

    store.save_runs(FIRST, [*SUMMARIZED, run("publish_github", "failed", link=GITHUB_ISSUE)])
    shown.refresh()

    dialog, entry = github_dialog(shown, "Retry…")
    assert entry.get_text() == GITHUB_ISSUE
    press(dialog, "Publish")
    wait_until(lambda: not shown.running[window.PUBLISH_GITHUB])

    assert publish.called() == ["publish", "github", FIRST, GITHUB_ISSUE]


def test_without_gh_publish_to_github_says_how_to_install_it_and_waits_for_a_summary(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    install = "gh not found: install gh 2.45.0 or newer"
    monkeypatch.setattr(github, "gh_problem", lambda: install)
    recording(SUMMARIZED)
    recording([run("transcribe", "succeeded", output="transcript.md")], START + timedelta(hours=1))
    shown = history()
    row = expand(shown)
    expand(shown, "2026-11-18T14-05-42")

    assert ("Publish to GitHub", f"Not published · {install}") in lines(row)
    without_gh = buttons(shown.details[FIRST].publish_github.row)["Publish…"]
    assert (without_gh.get_sensitive(), without_gh.get_tooltip_text()) == (False, install)

    monkeypatch.setattr(github, "gh_problem", lambda: "")
    shown.refresh()

    unsummarized = buttons(shown.details["2026-11-18T14-05-42"].publish_github.row)["Publish…"]
    assert (unsummarized.get_sensitive(), unsummarized.get_tooltip_text()) == (
        False,
        "Summarize first",
    )
    assert without_gh.get_sensitive() is True


def menu_entries(
    model: Gio.MenuModel, within: str = ""
) -> dict[str, tuple[str, GLib.Variant | None]]:
    """A menu's items, by label, with the action each activates and its target, which a check
    item has none of; a submenu's under its label."""
    entries: dict[str, tuple[str, GLib.Variant | None]] = {}
    for index in range(model.get_n_items()):
        label = model.get_item_attribute_value(index, "label", GLib.VariantType("s"))
        section = model.get_item_link(index, Gio.MENU_LINK_SECTION)
        submenu = model.get_item_link(index, Gio.MENU_LINK_SUBMENU)
        if section is not None:
            entries |= menu_entries(section, within)
        elif submenu is not None and label is not None:
            entries |= menu_entries(submenu, f"{within}{label.get_string()} > ")
        else:
            action = model.get_item_attribute_value(index, "action", GLib.VariantType("s"))
            target = model.get_item_attribute_value(index, "target", None)
            assert label is not None
            assert action is not None
            entries[f"{within}{label.get_string()}"] = (action.get_string(), target)
    return entries


def presets_menu(shown: window.HistoryWindow) -> dict[str, tuple[str, GLib.Variant | None]]:
    """What the Summarize menu offers once opened."""
    presets = shown.details[FIRST].presets
    presets.popup()
    settle()
    model = presets.get_menu_model()
    presets.popdown()
    settle()
    assert model is not None
    return menu_entries(model)


def choose_preset(shown: window.HistoryWindow, label: str) -> None:
    """Picks an item of the Summarize menu, as a click on it would."""
    action, target = presets_menu(shown)[label]
    assert shown.details[FIRST].presets.activate_action(action, target) is True
    settle()


TRANSCRIBED = run("transcribe", "succeeded", output="transcript.md")


def test_summarize_offers_the_presets_and_runs_the_one_chosen(
    history: Callable[[], window.HistoryWindow],
    publish: FakePublish,
    desktop_calls: Desktop,
) -> None:
    recording([TRANSCRIBED])
    settings.save_preset("Retro_2026", "Summarize this retrospective.")
    publish.will(
        run("summarize", "succeeded", output="summary.md", prompt=summarizer.presets()["Standup"])
    )
    shown = history()
    row = expand(shown)
    presets = shown.details[FIRST].presets
    assert (presets.get_label(), presets.get_sensitive()) == ("Summarize", True)
    # A typed name's "_" is shown, not taken for a keyboard shortcut.
    assert list(presets_menu(shown)) == [
        "General",
        "Standup",
        "1:1",
        "Stakeholder call",
        "Retro__2026",
        "Custom…",
        "Delete a preset > Retro__2026",
    ]

    choose_preset(shown, "Standup")

    assert ("Summarize", "Running") in lines(row)
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (False, "Summarizing")

    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, f"--prompt={summarizer.presets()['Standup']}"]
    assert ("Summarize", "Succeeded") in lines(row)
    assert presets.get_label() == "Summarize again"
    assert list(buttons(shown.details[FIRST].summarize.row)) == ["Open summary"]

    press(row, "Open summary")

    assert desktop_calls.opened == [str(store.recording_dir(FIRST) / "summary.md")]


def test_without_devin_summarize_says_how_to_install_it(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording([TRANSCRIBED])
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    shown = history()
    row = expand(shown)

    install = (
        "devin not found: install the Devin CLI with "
        "`curl -fsSL https://cli.devin.ai/install.sh | bash`, then log in with `devin auth login`"
    )
    presets = shown.details[FIRST].presets
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (False, install)
    assert ("Summarize", f"Not summarized · {install}") in lines(row)


def test_summarize_waits_for_a_transcript(history: Callable[[], window.HistoryWindow]) -> None:
    recording([run("transcribe", "failed", error="Out of memory")])
    shown = history()
    expand(shown)

    presets = shown.details[FIRST].presets
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (False, "Transcribe first")


def custom_prompt_dialog(
    shown: window.HistoryWindow,
) -> tuple[Adw.AlertDialog, Gtk.TextView, Gtk.Entry]:
    choose_preset(shown, "Custom…")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    [prompt] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.TextView)]
    [name] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    return dialog, prompt, name


def test_custom_sends_the_typed_prompt_and_saves_it_as_a_preset(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TRANSCRIBED])
    shown = history()
    expand(shown)

    dialog, prompt, name = custom_prompt_dialog(shown)
    assert dialog.get_heading() == "Custom summary"
    prompt.get_buffer().set_text("  Only the blockers.\n")
    name.set_text(" Blockers ")
    press(dialog, "Summarize")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, "--prompt=Only the blockers."]
    assert settings.saved_presets() == {"Blockers": "Only the blockers."}
    assert "Blockers" in presets_menu(shown)


@pytest.mark.parametrize(
    ("typed", "answer"), [("Only the blockers.", "Cancel"), (" \n", "Summarize")]
)
def test_custom_runs_nothing_when_cancelled_or_left_empty(
    history: Callable[[], window.HistoryWindow], publish: FakePublish, typed: str, answer: str
) -> None:
    recording([TRANSCRIBED])
    shown = history()
    expand(shown)

    dialog, prompt, name = custom_prompt_dialog(shown)
    prompt.get_buffer().set_text(typed)
    name.set_text("Blockers")
    press(dialog, answer)
    settle()

    assert (shown.running[window.SUMMARIZE], publish.called()) == ({}, [])
    assert settings.saved_presets() == {}


def test_custom_without_a_name_saves_no_preset(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TRANSCRIBED])
    shown = history()
    expand(shown)

    dialog, prompt, _ = custom_prompt_dialog(shown)
    prompt.get_buffer().set_text("Only the blockers.")
    press(dialog, "Summarize")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, "--prompt=Only the blockers."]
    assert settings.saved_presets() == {}


def test_a_saved_preset_can_be_deleted_and_a_built_in_one_cannot(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording([TRANSCRIBED])
    settings.save_preset("Retro", "Summarize this retrospective.")
    settings.save_preset("Planning", "List the plans.")
    shown = history()
    expand(shown)

    choose_preset(shown, "Delete a preset > Retro")

    assert settings.saved_presets() == {"Planning": "List the plans."}
    assert [label for label in presets_menu(shown) if label.startswith("Delete")] == [
        "Delete a preset > Planning"
    ]

    choose_preset(shown, "Delete a preset > Planning")

    assert list(presets_menu(shown)) == ["General", "Standup", "1:1", "Stakeholder call", "Custom…"]


def test_retry_sends_the_failed_runs_prompt_again(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    failed = run("summarize", "failed", error="Error: not logged in", prompt="Only the blockers.")
    recording([TRANSCRIBED, failed])
    publish.will(run("summarize", "succeeded", output="summary.md", prompt="Only the blockers."))
    shown = history()
    row = expand(shown)
    assert ("Summarize", "Failed: Error: not logged in") in lines(row)
    assert list(buttons(shown.details[FIRST].summarize.row)) == ["Retry"]
    assert shown.details[FIRST].presets.get_label() == "Summarize"

    press(row, "Retry")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, "--prompt=Only the blockers."]
    assert ("Summarize", "Succeeded") in lines(row)


def context_file(name: str) -> Path:
    return settings.settings_path().parent / "context" / f"{name}.md"


def make_context(*names: str) -> None:
    for name in names:
        context_file(name).parent.mkdir(parents=True, exist_ok=True)
        context_file(name).write_text(f"About {name}.\n")


def ticked(shown: window.HistoryWindow) -> list[str]:
    """The context items of the Summarize menu that are ticked."""
    found = []
    for label, (action, target) in presets_menu(shown).items():
        state = shown.get_action_state(action.removeprefix("win."))
        if target is None and state is not None and state.get_boolean():
            found.append(label)
    return found


def test_context_ticked_in_the_summarize_menu_goes_with_every_summary_until_unticked(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TRANSCRIBED])
    make_context("Team glossary", "Project_X")
    # Ticked once, and deleted since.
    settings.set_summary_context(["Gone"])
    shown = history()
    expand(shown)
    assert list(presets_menu(shown)) == [
        "General",
        "Standup",
        "1:1",
        "Stakeholder call",
        "Custom…",
        "Project__X",
        "Team glossary",
    ]
    assert ticked(shown) == []
    project, _ = presets_menu(shown)["Project__X"]

    choose_preset(shown, "Project__X")

    # Ticked at once, as the menu shows it while still open.
    state = shown.get_action_state(project.removeprefix("win."))
    assert state is not None
    assert state.get_boolean() is True

    choose_preset(shown, "Team glossary")

    assert ticked(shown) == ["Project__X", "Team glossary"]
    assert settings.summary_context() == ["Project_X", "Team glossary"]

    choose_preset(shown, "Project__X")

    assert ticked(shown) == ["Team glossary"]
    assert settings.summary_context() == ["Team glossary"]

    choose_preset(shown, "General")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    general = f"--prompt={summarizer.presets()['General']}"
    assert publish.called() == ["summarize", FIRST, general, "--context=Team glossary"]


def test_custom_goes_with_the_ticked_context_and_skips_a_deleted_one(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TRANSCRIBED])
    make_context("Team", "Project")
    settings.set_summary_context(["Project", "Team"])
    shown = history()
    expand(shown)
    context_file("Project").unlink()

    dialog, prompt, _ = custom_prompt_dialog(shown)
    prompt.get_buffer().set_text("Only the blockers.")
    press(dialog, "Summarize")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, "--prompt=Only the blockers.", "--context=Team"]


def test_retry_sends_the_failed_runs_context_again_whatever_is_ticked_now(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    make_context("Team", "Project")
    settings.set_summary_context(["Project"])
    failed = run("summarize", "failed", error="boom", prompt="P", context=["Team"])
    recording([TRANSCRIBED, failed])
    publish.will(run("summarize", "succeeded", output="summary.md", prompt="P", context=["Team"]))
    shown = history()
    row = expand(shown)

    press(row, "Retry")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    assert publish.called() == ["summarize", FIRST, "--prompt=P", "--context=Team"]


def test_a_summarize_command_that_dies_mid_run_fails_its_run(
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording([TRANSCRIBED])
    traceback = "Traceback (most recent call last):\n  ...\nRuntimeError: boom\n"
    publish.will(run("summarize", "running", prompt="Summarize."), exit_status=1, stderr=traceback)
    shown = history()
    row = expand(shown)

    choose_preset(shown, "General")
    wait_until(lambda: not shown.running[window.SUMMARIZE])

    reason = "Summarize stopped: RuntimeError: boom"
    assert store.load_runs(FIRST)[-1] == run(
        "summarize", "failed", error=reason, prompt="Summarize."
    )
    assert reason in texts(shown)
    assert ("Summarize", f"Failed: {reason}") in lines(row)
    assert row.warning.get_visible() is True


def with_size(recording_id: str, size: int) -> str:
    """Its tracks take size bytes, without the disk."""
    for name in store.TRACKS:
        with (store.recording_dir(recording_id) / name).open("wb") as track:
            track.truncate(size // 2)
    return recording_id


AGARIC_UNSET = "No Agaric agent token"
GLAB_LOGGED_IN = "glab is logged in to gitlab.com."
LISTED_MODELS = json.dumps(
    {
        "families": [
            {
                "family_label": "Adaptive",
                "variants": [{"model_uid": "adaptive", "label": "Adaptive"}],
            },
            {
                "family_label": "Claude Opus 5.5",
                "variants": [
                    {"model_uid": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 Medium"},
                    {"model_uid": "claude-opus-5-5-low", "label": "Claude Opus 5.5 Low"},
                    {"model_uid": "opus-high-fast", "label": "Claude Opus 5.5 High Fast"},
                ],
            },
        ]
    }
)
MODELS = ["Devin's default", "Adaptive", "Claude Opus 5.5"]
OPUS_EFFORTS = ["Medium", "Low", "High Fast"]


def open_settings(shown: window.HistoryWindow) -> window.SettingsDialog:
    """The Settings, as the main menu opens them, once Devin, Agaric and glab have answered
    them."""
    assert shown.activate_action(f"win.{window.SETTINGS}", None) is True
    dialog = open_dialog(shown)
    assert isinstance(dialog, window.SettingsDialog)
    wait_until(
        lambda: (
            dialog.model.get_subtitle() != window.ASKING_DEVIN
            and dialog.agaric.get_description() != window.ASKING_AGARIC
            and dialog.gitlab.get_description() != window.ASKING_GLAB
        )
    )
    return dialog


def attribute(menu: Gio.MenuModel, index: int, name: str) -> str:
    value = menu.get_item_attribute_value(index, name, GLib.VariantType("s"))
    assert value is not None
    return value.get_string()


def offered(row: Adw.ComboRow) -> list[str]:
    model = row.get_model()
    assert model is not None
    return [
        cast("Gtk.StringObject", model.get_item(place)).get_string()
        for place in range(model.get_n_items())
    ]


@pytest.mark.usefixtures("agaric_settings")
def test_the_main_menu_opens_the_settings_once(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()
    [main_menu] = [widget for widget in descendants(shown) if isinstance(widget, Gtk.MenuButton)]
    menu = main_menu.get_menu_model()
    assert menu is not None
    assert [
        (attribute(menu, index, "label"), attribute(menu, index, "action"))
        for index in range(menu.get_n_items())
    ] == [
        ("Settings", f"win.{window.SETTINGS}"),
        ("Update Minutas", "win.update"),
        ("About Minutas", "win.about"),
    ]

    dialog = open_settings(shown)
    shown.open_settings()
    settle()

    assert shown.get_visible_dialog() is dialog
    assert dialog.get_title() == "Settings"
    assert dialog.summarizer.get_selected_item() is not None
    assert offered(dialog.summarizer) == ["Devin CLI", "Claude Code"]
    assert dialog.summarizer.get_selected() == 0
    dialog.close()


def test_about_shows_the_version_the_license_and_where_to_report_an_issue(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()

    assert shown.activate_action("win.about", None) is True

    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AboutDialog)
    assert (dialog.get_application_name(), dialog.get_application_icon()) == (
        "Minutas",
        "audio-input-microphone",
    )
    assert dialog.get_version() == version("minutas")
    assert dialog.get_comments() == (
        "Records work meetings on GNOME, transcribes them and publishes them to Agaric, GitLab and "
        "GitHub."
    )
    assert dialog.get_license_type() == Gtk.License.GPL_3_0
    assert (dialog.get_website(), dialog.get_issue_url()) == (
        "https://github.com/jfolcini/minutas",
        "https://github.com/jfolcini/minutas/issues",
    )
    dialog.close()


def update(shown: window.HistoryWindow) -> Adw.AlertDialog:
    """Presses Update Minutas: it says so until the update ends, and then what came of it."""
    assert shown.activate_action("win.update", None) is True
    # Pressed once: a second update can't start while one runs.
    assert shown.updating.get_revealed() is True
    assert shown.update_action.get_enabled() is False
    wait_until(lambda: shown.get_visible_dialog() is not None)
    assert shown.updating.get_revealed() is False
    assert shown.update_action.get_enabled() is True
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    return dialog


def test_an_update_says_to_open_the_window_again_and_when_to_log_out(
    history: Callable[[], window.HistoryWindow], minutas_update: FakeUpdate
) -> None:
    minutas_update.will(
        f"Updating to v0.4.0.\nuv: ok\nApp: installed\n\nLeft to do:\n- {window_view.LOG_OUT}\n"
    )
    shown = history()

    dialog = update(shown)

    assert minutas_update.called() == ["update"]
    assert dialog.get_heading() == "Updated"
    assert dialog.get_body() == (
        "Updated to v0.4.0. Close this window and open it again to run the new version. "
        "Log out and back in to load the top-bar extension."
    )
    assert list(buttons(dialog)) == ["Close"]
    press(dialog, "Close")
    assert shown.get_visible_dialog() is None


def test_an_update_on_the_latest_release_says_only_that_whatever_the_last_one_printed(
    history: Callable[[], window.HistoryWindow], minutas_update: FakeUpdate
) -> None:
    shown = history()
    # A longer log from an earlier update, which said to log out.
    (store.data_dir() / "update.log").write_text(
        f"Updating to v0.3.0.\nuv: ok\nApp: installed\n\nLeft to do:\n- {window_view.LOG_OUT}\n"
    )

    dialog = update(shown)

    assert minutas_update.called() == ["update"]
    assert (dialog.get_heading(), dialog.get_body()) == (
        "Up to date",
        "Already on the latest release, v0.4.0.",
    )
    press(dialog, "Close")


def test_a_failed_update_says_the_fix_and_shows_all_it_printed(
    history: Callable[[], window.HistoryWindow], minutas_update: FakeUpdate
) -> None:
    printed = "Updating to v0.4.0.\nSystem packages: ok\n"
    # What install.sh says without a terminal to ask for the token in.
    fix = "Hugging Face token: failed: none in the GNOME keyring; run install.sh in a terminal"
    minutas_update.will(printed, stderr=f"{fix}\n", exit_status=1)
    shown = history()

    dialog = update(shown)

    assert (dialog.get_heading(), dialog.get_body()) == ("Update failed", fix)
    assert list(buttons(dialog)) == ["Close", "Details"]
    press(dialog, "Details")

    details = open_dialog(shown)
    assert details.get_title() == "Details"
    [text] = [widget for widget in descendants(details) if isinstance(widget, Gtk.TextView)]
    assert text.get_buffer().props.text == f"{printed}{fix}\n"
    details.close()


@pytest.mark.usefixtures("agaric_settings")
def test_the_models_devin_lists_are_offered_with_the_saved_one_chosen(
    history: Callable[[], window.HistoryWindow],
    devin_installed: FakeDevin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.set_summary_model("opus-high-fast")
    saved: list[str | None] = []
    monkeypatch.setattr(settings, "set_summary_model", saved.append)
    shown = history()

    dialog = open_settings(shown)

    assert devin_installed.asked() == "models list --format json --process-model in-process\n"
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == ("", True)
    assert (offered(dialog.model), dialog.model.get_selected()) == (MODELS, 2)
    assert (offered(dialog.effort), dialog.effort.get_selected()) == (OPUS_EFFORTS, 2)
    assert dialog.effort.get_visible() is True
    # Showing the saved model saves nothing, not even on the way.
    assert saved == []
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_model_devin_stopped_offering_still_shows_while_it_is_set(
    history: Callable[[], window.HistoryWindow],
) -> None:
    settings.set_summary_model("swe-1.5")
    shown = history()

    dialog = open_settings(shown)

    assert (offered(dialog.model), dialog.model.get_selected()) == ([*MODELS, "swe-1.5"], 3)
    assert dialog.effort.get_visible() is False
    assert settings.summary_model() == "swe-1.5"
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_model_is_saved_with_its_first_effort_and_an_effort_on_its_own(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()
    dialog = open_settings(shown)
    assert (dialog.model.get_selected(), dialog.effort.get_visible()) == (0, False)

    dialog.model.set_selected(2)
    assert settings.summary_model() == "claude-opus-5-5-medium"
    assert (offered(dialog.effort), dialog.effort.get_selected()) == (OPUS_EFFORTS, 0)
    assert dialog.effort.get_visible() is True

    dialog.effort.set_selected(1)
    assert settings.summary_model() == "claude-opus-5-5-low"

    # A model with a single effort has nothing to choose.
    dialog.model.set_selected(1)
    assert (settings.summary_model(), dialog.effort.get_visible()) == ("adaptive", False)

    dialog.model.set_selected(0)
    assert (settings.summary_model(), dialog.effort.get_visible()) == (None, False)
    dialog.close()


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (("", "Logged in as nobody\nError: not logged in\n", 1), "Error: not logged in"),
        (("", "", 3), "devin exited with status 3"),
        (("Available models (50 families)\n", "", 0), summarizer.MODELS_UNREADABLE),
    ],
)
@pytest.mark.usefixtures("agaric_settings")
def test_models_devin_does_not_list_say_why(
    history: Callable[[], window.HistoryWindow],
    devin_installed: FakeDevin,
    answer: tuple[str, str, int],
    said: str,
) -> None:
    stdout, stderr, exit_status = answer
    devin_installed.will(stdout, stderr=stderr, exit_status=exit_status)
    settings.set_summary_model("opus-high-fast")
    shown = history()

    dialog = open_settings(shown)

    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == (said, False)
    assert settings.summary_model() == "opus-high-fast"
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_without_devin_the_model_says_how_to_install_it(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    shown = history()

    dialog = open_settings(shown)

    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == (
        summarizer.DEVIN_MISSING,
        False,
    )
    dialog.close()


CLAUDE_MODELS = ["Claude's default", "Fable", "Opus", "Sonnet", "Haiku"]
CLAUDE_EFFORTS = ["Claude's default", "Low", "Medium", "High", "Extra high", "Max"]
CLAUDE_MISSING = (
    "claude not found: install Claude Code with "
    "`curl -fsSL https://claude.ai/install.sh | bash`, then log in with `claude auth login`"
)


@pytest.fixture
def claude_installed(devin_installed: FakeDevin) -> None:
    """A claude on PATH, never run: the window only looks for one."""
    script = devin_installed.folder / "claude"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)


@pytest.mark.usefixtures("agaric_settings", "claude_installed")
def test_choosing_claude_code_offers_its_models_and_efforts_and_saves_each_one_chosen(
    history: Callable[[], window.HistoryWindow],
) -> None:
    settings.set_summary_model("opus-high-fast")
    shown = history()
    dialog = open_settings(shown)

    dialog.summarizer.set_selected(1)

    assert settings.summarizer() == "claude"
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == ("", True)
    assert (offered(dialog.model), dialog.model.get_selected()) == (CLAUDE_MODELS, 0)
    assert (offered(dialog.effort), dialog.effort.get_selected()) == (CLAUDE_EFFORTS, 0)
    assert dialog.effort.get_visible() is True
    # Offering them chooses nothing.
    assert (settings.claude_model(), settings.claude_effort()) == (None, None)

    dialog.model.set_selected(2)
    assert (settings.claude_model(), settings.claude_effort()) == ("opus", None)

    dialog.effort.set_selected(4)
    assert (settings.claude_model(), settings.claude_effort()) == ("opus", "xhigh")

    dialog.model.set_selected(0)
    assert (settings.claude_model(), settings.claude_effort()) == (None, "xhigh")

    dialog.effort.set_selected(0)
    assert (settings.claude_model(), settings.claude_effort()) == (None, None)
    # Devin's model stays its own.
    assert settings.summary_model() == "opus-high-fast"
    dialog.close()


@pytest.mark.usefixtures("agaric_settings", "claude_installed")
def test_claudes_saved_model_and_effort_are_chosen_without_asking_devin(
    history: Callable[[], window.HistoryWindow],
    devin_installed: FakeDevin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.set_summarizer("claude")
    settings.set_claude_model("sonnet")
    settings.set_claude_effort("max")
    saved: list[str | None] = []
    monkeypatch.setattr(settings, "set_claude_model", saved.append)
    monkeypatch.setattr(settings, "set_claude_effort", saved.append)
    shown = history()

    dialog = open_settings(shown)

    assert dialog.summarizer.get_selected() == 1
    assert (dialog.model.get_selected(), dialog.effort.get_selected()) == (3, 5)
    assert dialog.effort.get_visible() is True
    # Showing the saved ones saves nothing, not even on the way.
    assert saved == []
    assert not (devin_installed.folder / "asked").exists()
    dialog.close()


@pytest.mark.usefixtures("agaric_settings", "claude_installed")
def test_choosing_devin_again_asks_it_for_its_models_and_offers_none_of_claudes_meanwhile(
    history: Callable[[], window.HistoryWindow],
) -> None:
    settings.set_summarizer("claude")
    settings.set_claude_model("opus")
    settings.set_summary_model("opus-high-fast")
    shown = history()
    dialog = open_settings(shown)

    dialog.summarizer.set_selected(0)

    assert settings.summarizer() == "devin"
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == (
        window.ASKING_DEVIN,
        False,
    )
    assert (offered(dialog.model), dialog.effort.get_visible()) == ([], False)

    wait_until(lambda: dialog.model.get_subtitle() != window.ASKING_DEVIN)

    assert (offered(dialog.model), dialog.model.get_selected()) == (MODELS, 2)
    assert (offered(dialog.effort), dialog.effort.get_selected()) == (OPUS_EFFORTS, 2)
    assert (dialog.model.get_sensitive(), dialog.effort.get_visible()) == (True, True)
    # Each keeps its own model through the change.
    assert (settings.summary_model(), settings.claude_model()) == ("opus-high-fast", "opus")
    dialog.close()


@pytest.mark.usefixtures("agaric_settings", "claude_installed")
def test_devins_models_arriving_once_claude_code_is_chosen_are_not_offered(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.set_summary_model("opus-high-fast")
    answered: list[bool] = []
    models_listed = window.HistoryWindow.models_listed

    def listed_and_counted(
        shown: window.HistoryWindow,
        command: Gio.Subprocess,
        result: Gio.AsyncResult,
        dialog: window.SettingsDialog,
    ) -> None:
        models_listed(shown, command, result, dialog)
        answered.append(True)

    monkeypatch.setattr(window.HistoryWindow, "models_listed", listed_and_counted)
    shown = history()
    dialog = open_settings(shown)
    assert answered == [True]

    # Devin is asked, and Claude Code chosen before it answers.
    dialog.summarizer.set_selected(1)
    dialog.summarizer.set_selected(0)
    dialog.summarizer.set_selected(1)
    wait_until(lambda: answered == [True, True])

    assert (offered(dialog.model), dialog.model.get_selected()) == (CLAUDE_MODELS, 0)
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == ("", True)
    assert offered(dialog.effort) == CLAUDE_EFFORTS
    assert (settings.summary_model(), settings.claude_model()) == ("opus-high-fast", None)
    dialog.close()


def test_with_claude_code_chosen_summarize_needs_claude_and_not_devin(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording([TRANSCRIBED])
    settings.set_summarizer("claude")
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir()
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(claude.parent))
    shown = history()
    expand(shown)

    presets = shown.details[FIRST].presets
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (True, None)


@pytest.mark.usefixtures("agaric_settings")
def test_summarize_is_off_while_the_summarizer_chosen_is_missing_and_says_how_to_install_it(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recording([TRANSCRIBED])
    settings.set_summarizer("claude")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    shown = history()
    row = expand(shown)

    presets = shown.details[FIRST].presets
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (False, CLAUDE_MISSING)
    assert ("Summarize", f"Not summarized · {CLAUDE_MISSING}") in lines(row)

    # Its models can be chosen before it is installed; the Settings say it is missing too.
    dialog = open_settings(shown)
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == (CLAUDE_MISSING, True)

    # Choosing the other summarizer is seen at once, without waiting for a recording to change.
    dialog.summarizer.set_selected(0)
    assert (presets.get_sensitive(), presets.get_tooltip_text()) == (
        False,
        summarizer.DEVIN_MISSING,
    )
    assert (dialog.model.get_subtitle(), dialog.model.get_sensitive()) == (
        summarizer.DEVIN_MISSING,
        False,
    )
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_the_skill_is_the_built_in_one_until_a_file_is_chosen(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    shown = history()
    dialog = open_settings(shown)
    assert dialog.skill.get_subtitle() == "Built-in: meeting notes"
    assert "Use built-in" not in buttons(dialog.skill)

    press(dialog.skill, "Open")
    assert desktop_calls.opened == [str(summarizer.BUILT_IN_SKILL)]

    desktop_calls.open_from = tmp_path / "standup-notes.md"
    press(dialog.skill, "Choose…")
    press(dialog.skill, "Open")

    assert desktop_calls.opened[1:] == [str(tmp_path / "standup-notes.md")]

    assert desktop_calls.filters == [("Markdown", [(0, "*.md"), (1, "text/markdown")])]
    assert settings.summary_skill() == str(tmp_path / "standup-notes.md")
    assert dialog.skill.get_subtitle() == str(tmp_path / "standup-notes.md")

    press(dialog.skill, "Use built-in")

    assert settings.summary_skill() is None
    assert dialog.skill.get_subtitle() == "Built-in: meeting notes"
    assert "Use built-in" not in buttons(dialog.skill)
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_vocabulary_opens_the_glossary_and_makes_it_empty_the_first_time(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    shown = history()
    dialog = open_settings(shown)
    glossary = worker.glossary()

    press(dialog.vocabulary, "Open")

    assert desktop_calls.opened == [str(glossary)]
    assert glossary.read_text() == ""

    glossary.write_text("deploy\nPR\n")
    press(dialog.vocabulary, "Open")

    assert desktop_calls.opened == [str(glossary), str(glossary)]
    assert glossary.read_text() == "deploy\nPR\n"
    dialog.close()


def vocabulary_row(dialog: window.SettingsDialog, title: str) -> Adw.ActionRow:
    [row] = [
        row
        for row in descendants(dialog.transcription)
        if isinstance(row, Adw.ActionRow) and row.get_title() == title
    ]
    return row


def vocabulary_rows(dialog: window.SettingsDialog) -> list[tuple[str, bool, list[str]]]:
    """Each vocabulary's title, whether its switch is on, and its buttons."""
    rows = [row for row in descendants(dialog.transcription) if isinstance(row, Adw.ActionRow)]
    return [(row.get_title(), used_switch(row).get_active(), list(buttons(row))) for row in rows]


def used_switch(row: Adw.ActionRow) -> Gtk.Switch:
    [switch] = [widget for widget in descendants(row) if isinstance(widget, Gtk.Switch)]
    return switch


def make_vocabulary(*names: str) -> None:
    for name in names:
        worker.vocabulary_file(name).parent.mkdir(parents=True, exist_ok=True)
        worker.vocabulary_file(name).write_text(f"{name}\n")


def name_vocabulary(shown: window.HistoryWindow, dialog: window.SettingsDialog, name: str) -> None:
    press(dialog.transcription, "New…")
    asked = open_dialog(shown)
    assert isinstance(asked, Adw.AlertDialog)
    assert asked.get_heading() == "New vocabulary"
    [entry] = [widget for widget in descendants(asked) if isinstance(widget, Gtk.Entry)]
    entry.set_text(name)
    press(asked, "Save")
    draw(shown)


@pytest.mark.usefixtures("agaric_settings")
def test_new_makes_a_vocabulary_empty_and_used_and_opens_it_and_each_is_listed_after_the_glossary(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    shown = history()
    dialog = open_settings(shown)
    assert vocabulary_rows(dialog) == [("Vocabulary", True, ["Open"])]
    assert dialog.transcription.get_description() == window.VOCABULARY_NOTE

    name_vocabulary(shown, dialog, " Client   Atlas ")

    atlas = worker.vocabulary_file("Client Atlas")
    assert atlas.read_text() == ""
    assert desktop_calls.opened == [str(atlas)]
    assert settings.vocabularies() == [settings.GLOSSARY, "Client Atlas"]

    make_vocabulary("Beta")
    name_vocabulary(shown, dialog, "Zeta")

    assert vocabulary_rows(dialog) == [
        ("Vocabulary", True, ["Open"]),
        ("Beta", False, ["Open", "Delete"]),
        ("Client Atlas", True, ["Open", "Delete"]),
        ("Zeta", True, ["Open", "Delete"]),
    ]
    press(vocabulary_row(dialog, "Beta"), "Open")

    assert desktop_calls.opened[-1] == str(worker.vocabulary_file("Beta"))
    assert worker.vocabulary_file("Beta").read_text() == "Beta\n"
    dialog.close()


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("Atlas", "There is a vocabulary named Atlas already"),
        (".Atlas", "A vocabulary's name can't start with a dot or hold a slash"),
    ],
)
@pytest.mark.usefixtures("agaric_settings")
def test_new_refuses_a_vocabulary_name_that_is_taken_or_no_file_name_and_says_why(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, name: str, said: str
) -> None:
    make_vocabulary("Atlas")
    shown = history()
    dialog = open_settings(shown)

    name_vocabulary(shown, dialog, name)

    assert said in texts(dialog)
    assert sorted(path.name for path in worker.vocabulary_dir().iterdir()) == ["Atlas.txt"]
    assert (desktop_calls.opened, settings.vocabularies()) == ([], [settings.GLOSSARY])
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_vocabulary_switched_off_or_on_is_used_or_not_from_then_on(
    history: Callable[[], window.HistoryWindow],
) -> None:
    make_vocabulary("Atlas", "Beta")
    shown = history()
    dialog = open_settings(shown)

    used_switch(dialog.vocabulary).set_active(False)
    used_switch(vocabulary_row(dialog, "Beta")).set_active(True)

    assert settings.vocabularies() == ["Beta"]

    used_switch(vocabulary_row(dialog, "Atlas")).set_active(True)
    used_switch(dialog.vocabulary).set_active(True)
    used_switch(vocabulary_row(dialog, "Beta")).set_active(False)

    assert settings.vocabularies() == ["Atlas", settings.GLOSSARY]
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_delete_moves_a_vocabulary_to_the_trash_once_confirmed_and_stops_using_it(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    make_vocabulary("Atlas", "Beta")
    settings.set_vocabularies(["Atlas", settings.GLOSSARY, "Beta"])
    shown = history()
    dialog = open_settings(shown)

    press(vocabulary_row(dialog, "Atlas"), "Delete")
    asked = open_dialog(shown)
    assert isinstance(asked, Adw.AlertDialog)
    assert asked.get_heading() == "Move the vocabulary to the Trash?"
    assert asked.get_body() == window.VOCABULARY_TRASH_WARNING
    press(asked, "Cancel")
    draw(shown)

    assert desktop_calls.trashed == []
    assert [title for title, _, _ in vocabulary_rows(dialog)] == ["Vocabulary", "Atlas", "Beta"]

    press(vocabulary_row(dialog, "Atlas"), "Delete")
    press(open_dialog(shown), "Move to Trash")
    draw(shown)

    assert [path for path, _ in desktop_calls.trashed] == [str(worker.vocabulary_file("Atlas"))]
    assert (tmp_path / "Trash" / "Atlas.txt").read_text() == "Atlas\n"
    assert vocabulary_rows(dialog) == [
        ("Vocabulary", True, ["Open"]),
        ("Beta", True, ["Open", "Delete"]),
    ]
    assert settings.vocabularies() == [settings.GLOSSARY, "Beta"]
    dialog.close()


def context_rows(dialog: window.SettingsDialog) -> list[str]:
    return [title for title, _ in lines(dialog.context)]


def name_context(shown: window.HistoryWindow, dialog: window.SettingsDialog, name: str) -> None:
    press(dialog.context, "New…")
    asked = open_dialog(shown)
    assert isinstance(asked, Adw.AlertDialog)
    assert asked.get_heading() == "New context"
    [entry] = [widget for widget in descendants(asked) if isinstance(widget, Gtk.Entry)]
    entry.set_text(name)
    press(asked, "Save")
    draw(shown)


@pytest.mark.usefixtures("agaric_settings")
def test_context_presets_are_listed_and_new_makes_an_empty_one_and_opens_it(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    shown = history()
    dialog = open_settings(shown)
    assert context_rows(dialog) == []
    assert dialog.context.get_description() == window.CONTEXT_NOTE

    name_context(shown, dialog, " Team   glossary ")

    assert context_file("Team glossary").read_text() == ""
    assert desktop_calls.opened == [str(context_file("Team glossary"))]
    assert context_rows(dialog) == ["Team glossary"]

    make_context("Atlas")
    name_context(shown, dialog, "Zeta")

    assert context_rows(dialog) == ["Atlas", "Team glossary", "Zeta"]
    [atlas, *_] = [row for row in descendants(dialog.context) if isinstance(row, Adw.ActionRow)]
    press(atlas, "Open")

    assert desktop_calls.opened[-1] == str(context_file("Atlas"))
    assert context_file("Atlas").read_text() == "About Atlas.\n"
    dialog.close()


@pytest.mark.parametrize(
    ("name", "said"),
    [
        ("Team", "There is a context named Team already"),
        ("../Team", "A context's name can't start with a dot or hold a slash"),
        ("x" * 300, "Can't create it: File name too long"),
    ],
)
@pytest.mark.usefixtures("agaric_settings")
def test_new_refuses_a_name_that_is_taken_or_no_file_name_and_says_why(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, name: str, said: str
) -> None:
    make_context("Team")
    shown = history()
    dialog = open_settings(shown)

    name_context(shown, dialog, name)

    assert said in texts(dialog)
    assert sorted(path.name for path in context_file("Team").parent.iterdir()) == ["Team.md"]
    assert context_file("Team").read_text() == "About Team.\n"
    assert desktop_calls.opened == []
    dialog.close()


def context_row(dialog: window.SettingsDialog, name: str) -> Adw.ActionRow:
    [row] = [
        row
        for row in descendants(dialog.context)
        if isinstance(row, Adw.ActionRow) and row.get_title() == name
    ]
    return row


@pytest.mark.usefixtures("agaric_settings")
def test_delete_moves_a_context_to_the_trash_once_confirmed_and_unticks_it(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop, tmp_path: Path
) -> None:
    make_context("Team", "Project")
    settings.set_summary_context(["Team", "Project"])
    shown = history()
    dialog = open_settings(shown)

    press(context_row(dialog, "Team"), "Delete")
    asked = open_dialog(shown)
    assert isinstance(asked, Adw.AlertDialog)
    assert asked.get_heading() == "Move the context to the Trash?"
    assert asked.get_body() == window.CONTEXT_TRASH_WARNING
    press(asked, "Cancel")
    draw(shown)

    assert (desktop_calls.trashed, context_rows(dialog)) == ([], ["Project", "Team"])

    press(context_row(dialog, "Team"), "Delete")
    press(open_dialog(shown), "Move to Trash")
    draw(shown)

    assert [path for path, _ in desktop_calls.trashed] == [str(context_file("Team"))]
    assert (tmp_path / "Trash" / "Team.md").read_text() == "About Team.\n"
    assert context_rows(dialog) == ["Project"]
    assert settings.summary_context() == ["Project"]
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_context_trash_that_fails_says_why_and_deletes_nothing(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    make_context("Team")
    settings.set_summary_context(["Team"])
    desktop_calls.trash_into = GLib.Error(
        "Unable to find or create trash directory",
        "g-io-error-quark",
        Gio.IOErrorEnum.NOT_SUPPORTED,
    )
    shown = history()
    dialog = open_settings(shown)

    press(context_row(dialog, "Team"), "Delete")
    press(open_dialog(shown), "Move to Trash")
    draw(shown)

    assert "Can't move it to the Trash: Unable to find or create trash directory" in texts(dialog)
    assert context_file("Team").read_text() == "About Team.\n"
    assert (context_rows(dialog), settings.summary_context()) == (["Team"], ["Team"])
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_skill_file_not_chosen_changes_nothing_and_a_failed_choice_says_why(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    settings.set_summary_skill("/home/me/notes.md")
    shown = history()
    dialog = open_settings(shown)

    press(dialog.skill, "Choose…")
    desktop_calls.open_from = NO_HANDLER
    press(dialog.skill, "Choose…")
    draw(shown)

    assert settings.summary_skill() == "/home/me/notes.md"
    assert texts(dialog).count("No application handles this") == 1
    dialog.close()


WORK, PERSONAL = "01HZWORK", "01HZPERSONAL"
BOTH_SPACES = f"{PERSONAL}\tPersonal\n{WORK}\tWork\n"
PUBLISHES_TO_WORK = "The stored agent token publishes to Work."
LIST_SPACES = ["settings", "agaric", "--list-spaces"]
PASTED = '{"mcpServers": {"agaric-minutas-vptx4p": {"args": ["--token", "agt_…"]}}}'
AGENT = "agt_77777777777777777777777777777777"


@pytest.fixture
def agent_token_stored() -> None:
    settings.set_agaric_token(AGENT)


def test_agaric_says_the_token_is_stored_and_shows_where_it_publishes_but_never_the_token(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    settings.set_agaric_token(AGENT)
    agaric_settings.will("--list-spaces", f"{WORK}\tWork\n", f"{PUBLISHES_TO_WORK}\n", space=WORK)
    shown = history()

    dialog = open_settings(shown)

    assert agaric_settings.given() == [[LIST_SPACES, ""]]
    assert dialog.token.get_subtitle() == "Stored in the keyring"
    assert dialog.agaric.get_description() == PUBLISHES_TO_WORK
    assert dialog.pasted.get_text() == ""
    assert AGENT not in texts(dialog)
    # The only space it may use shows, chosen.
    assert (offered(dialog.space), dialog.space.get_selected()) == (["Work"], 0)
    assert dialog.space.get_visible() is True
    assert settings.agaric_space_id() == WORK
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_without_a_token_agaric_says_how_to_get_its_config(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()

    dialog = open_settings(shown)

    assert dialog.token.get_subtitle() == "Not set"
    assert dialog.agaric.get_description() == (
        "To connect Agaric: in Agaric, open Settings → Agent access, add an agent with "
        "read-write access to the Work space, copy the config it shows, and paste it below."
    )
    assert dialog.space.get_visible() is False
    dialog.close()


def test_a_keyring_that_cannot_be_read_says_why(
    history: Callable[[], window.HistoryWindow],
    agaric_settings: FakeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "The keyring is locked"

    def locked() -> str | None:
        raise KeyringLocked(reason)

    said = f"Could not read the Agaric agent token from the keyring: {reason}"
    agaric_settings.will("--list-spaces", "", f"{said}\n", exit_status=1)
    monkeypatch.setattr(settings, "agaric_token", locked)
    shown = history()

    dialog = open_settings(shown)

    assert dialog.token.get_subtitle() == "Can't read the keyring: The keyring is locked"
    assert dialog.agaric.get_description() == said
    dialog.close()


@pytest.mark.usefixtures("agent_token_stored")
def test_agaric_offers_several_spaces_and_saves_the_one_chosen_at_once(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    settings.set_agaric_space_id(WORK)
    agaric_settings.will("--list-spaces", BOTH_SPACES, f"{PUBLISHES_TO_WORK}\n")
    shown = history()

    dialog = open_settings(shown)

    assert dialog.space.get_visible() is True
    assert (offered(dialog.space), dialog.space.get_selected()) == (["Personal", "Work"], 1)
    dialog.space.set_selected(0)
    assert settings.agaric_space_id() == PERSONAL
    dialog.close()


@pytest.mark.usefixtures("agent_token_stored")
def test_agaric_chooses_no_space_for_the_person(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    agaric_settings.will("--list-spaces", BOTH_SPACES, "Choose the one to publish to.\n")
    shown = history()

    dialog = open_settings(shown)

    assert dialog.agaric.get_description() == "Choose the one to publish to."
    assert dialog.space.get_selected() == Gtk.INVALID_LIST_POSITION
    assert settings.agaric_space_id() is None
    dialog.close()


@pytest.mark.usefixtures("agent_token_stored")
def test_agaric_out_of_reach_shows_the_fix(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    fix = "Agaric isn't running: start it and turn on Settings → Agent access"
    agaric_settings.will("--list-spaces", "", f"{fix}\n", exit_status=1)
    shown = history()

    dialog = open_settings(shown)

    assert dialog.agaric.get_description() == fix
    assert dialog.space.get_visible() is False
    dialog.close()


def test_a_pasted_config_is_kept_and_the_outcome_said(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    agaric_settings.will(
        "--token-from-stdin", f"{WORK}\tWork\n", f"{PUBLISHES_TO_WORK}\n", space=WORK
    )
    shown = history()
    dialog = open_settings(shown)
    assert dialog.token.get_subtitle() == "Not set"

    dialog.pasted.set_text(PASTED)
    # As the command does with what was pasted.
    settings.set_agaric_token(AGENT)
    dialog.pasted.emit("apply")

    assert dialog.pasted.get_text() == ""
    wait_until(lambda: dialog.agaric.get_description() == PUBLISHES_TO_WORK)
    assert dialog.token.get_subtitle() == "Stored in the keyring"
    assert agaric_settings.given()[1] == [["settings", "agaric", "--token-from-stdin"], PASTED]
    assert settings.agaric_space_id() == WORK
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_a_blank_paste_asks_agaric_nothing(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    shown = history()
    dialog = open_settings(shown)
    asked: list[object] = []
    monkeypatch.setattr(shown, "ask_agaric", lambda *arguments: asked.append(arguments))

    dialog.pasted.set_text("  \n")
    dialog.pasted.emit("apply")

    assert asked == []
    dialog.close()


@pytest.mark.usefixtures("agent_token_stored")
def test_names_paths_and_errors_in_the_settings_are_shown_as_written(
    history: Callable[[], window.HistoryWindow],
    agaric_settings: FakeSettings,
    devin_installed: FakeDevin,
    desktop_calls: Desktop,
    tmp_path: Path,
) -> None:
    publishes = "The stored agent token publishes to R&D <main>."
    agaric_settings.will("--list-spaces", f"{WORK}\tR&D <main>\n", f"{publishes}\n", space=WORK)
    devin_installed.will("", stderr="Error: <html> & co\n", exit_status=1)
    shown = history()
    dialog = open_settings(shown)

    desktop_calls.open_from = tmp_path / "R&D" / "notes.md"
    press(dialog.skill, "Choose…")

    assert publishes in texts(dialog)
    assert "Error: <html> & co" in texts(dialog)
    assert str(tmp_path / "R&D" / "notes.md") in texts(dialog)
    dialog.close()


PAT = "glpat-" + "a" * 20


def glab_said(said: str) -> str:
    """The GitLab group's description, once glab has said that."""
    return f"{said}\n\n{window.GITLAB_NOTE}"


def test_gitlab_says_whether_glab_is_logged_in_and_where_a_token_goes(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    settings.set_gitlab_link(ISSUE_LINK)
    on_example = "glab is logged in to gitlab.example.com."
    agaric_settings.will("gitlab", "", f"{on_example}\n", command="gitlab")
    shown = history()

    dialog = open_settings(shown)

    titles = [
        group.get_title()
        for group in descendants(dialog)
        if isinstance(group, Adw.PreferencesGroup)
    ]
    assert titles.index("GitLab") == titles.index("Agaric") + 1
    assert agaric_settings.given("gitlab") == [[["settings", "gitlab"], ""]]
    assert dialog.gitlab.get_description() == glab_said(on_example)
    assert (dialog.gitlab_link.get_text(), dialog.gitlab_token.get_text()) == (ISSUE_LINK, "")
    dialog.close()


@pytest.mark.parametrize(
    ("said", "shown_said"),
    [
        ("Logged in as nobody\nglab can't reach R&D <git>\n", "glab can't reach R&D <git>"),
        ("", "Asking glab stopped without saying why"),
    ],
)
def test_gitlab_shows_the_last_thing_glab_said_as_written(
    history: Callable[[], window.HistoryWindow],
    agaric_settings: FakeSettings,
    said: str,
    shown_said: str,
) -> None:
    agaric_settings.will("gitlab", "", said, exit_status=1, command="gitlab")
    shown = history()

    dialog = open_settings(shown)

    assert glab_said(shown_said) in texts(dialog)
    dialog.close()


def test_the_default_gitlab_link_is_saved_once_gitlab_can_post_to_it(
    history: Callable[[], window.HistoryWindow], agaric_settings: FakeSettings
) -> None:
    shown = history()
    dialog = open_settings(shown)
    link = dialog.gitlab_link

    link.set_text("https://gitlab.example.com/group")
    link.emit("apply")
    draw(shown)

    assert texts(dialog).count(window.NOT_A_LINK) == 1
    assert settings.gitlab_link() is None

    on_example = "glab is logged in to gitlab.example.com."
    agaric_settings.will("gitlab", "", f"{on_example}\n", command="gitlab")
    link.set_text(f" {ISSUE_LINK}#note_1 ")
    link.emit("apply")

    assert (settings.gitlab_link(), link.get_text()) == (ISSUE_LINK, ISSUE_LINK)
    # The login shown is the new link's server's.
    wait_until(lambda: dialog.gitlab.get_description() == glab_said(on_example))

    agaric_settings.will("gitlab", "", f"{GLAB_LOGGED_IN}\n", command="gitlab")
    link.set_text("  ")
    link.emit("apply")

    assert (settings.gitlab_link(), link.get_text()) == (None, "")
    wait_until(lambda: dialog.gitlab.get_description() == glab_said(GLAB_LOGGED_IN))
    assert agaric_settings.given("gitlab") == [[["settings", "gitlab"], ""]] * 3
    dialog.close()


def test_a_pasted_gitlab_token_goes_to_glab_and_is_never_shown_back(
    history: Callable[[], window.HistoryWindow],
    agaric_settings: FakeSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_logged_in = "glab isn't logged in to gitlab.com"
    agaric_settings.will("gitlab", "", f"{not_logged_in}\n", exit_status=1, command="gitlab")
    agaric_settings.will("--token-from-stdin", "", f"{GLAB_LOGGED_IN}\n", command="gitlab")
    shown = history()
    dialog = open_settings(shown)
    assert dialog.gitlab.get_description() == glab_said(not_logged_in)

    dialog.gitlab_token.set_text(f"{PAT}\n")
    dialog.gitlab_token.emit("apply")

    assert dialog.gitlab_token.get_text() == ""
    wait_until(lambda: dialog.gitlab.get_description() == glab_said(GLAB_LOGGED_IN))
    assert agaric_settings.given("gitlab")[1] == [
        ["settings", "gitlab", "--token-from-stdin"],
        f"{PAT}\n",
    ]
    assert PAT not in texts(dialog)

    asked: list[object] = []
    monkeypatch.setattr(shown, "ask_glab", lambda *arguments: asked.append(arguments))
    dialog.gitlab_token.set_text("  \n")
    dialog.gitlab_token.emit("apply")

    assert asked == []


@pytest.mark.usefixtures("agaric_settings")
def test_the_default_github_link_is_saved_cleared_or_refused_as_typed(
    history: Callable[[], window.HistoryWindow],
) -> None:
    settings.set_github_link(GITHUB_REPOSITORY)
    shown = history()
    dialog = open_settings(shown)
    assert dialog.github_link.get_text() == GITHUB_REPOSITORY
    assert dialog.github.get_description() == window.GITHUB_LINK_NOTE

    dialog.github_link.set_text("https://github.com/owner")
    dialog.github_link.emit("apply")

    assert dialog.github.get_description() == "Not a GitHub issue or repository link"
    assert settings.github_link() == GITHUB_REPOSITORY

    dialog.github_link.set_text(f" {GITHUB_ISSUE}#issuecomment-1 ")
    dialog.github_link.emit("apply")

    assert dialog.github.get_description() == window.GITHUB_LINK_NOTE
    assert settings.github_link() == GITHUB_ISSUE

    dialog.github_link.set_text(" ")
    dialog.github_link.emit("apply")

    assert settings.github_link() is None
    dialog.close()


def limits(dialog: Adw.Dialog) -> dict[str, tuple[Adw.ExpanderRow, Adw.SpinRow]]:
    """Each limit's switch and value, by the limit's title."""
    return {
        expander.get_title(): (expander, value)
        for expander in descendants(dialog)
        if isinstance(expander, Adw.ExpanderRow)
        for value in descendants(expander)
        if isinstance(value, Adw.SpinRow)
    }


def test_opening_the_window_deletes_the_audio_over_the_limits(
    history: Callable[[], window.HistoryWindow],
) -> None:
    old = recording([TRANSCRIBED], started=START - timedelta(days=30))
    recording([TRANSCRIBED])
    untranscribed = recording(started=START - timedelta(days=40))

    shown = history()

    assert [store.holds_audio(one) for one in (old, FIRST, untranscribed)] == [False, True, True]
    # As after Delete now; and the age limit says when it takes what is left.
    assert lines(expand(shown, old))[1:5] == [
        ("Microphone", "Deleted"),
        ("Call", "Deleted"),
        ("Audio files", ""),
        ("Transcribe", "Succeeded"),
    ]
    assert lines(expand(shown))[3] == ("Audio files", "Kept until 2026-12-18 13:05")
    assert lines(expand(shown, untranscribed))[3] == ("Audio files", "")


@pytest.mark.parametrize(
    ("saved", "said", "shown_limits"),
    [
        (None, " of 5 GB", {"Age limit": (True, 30.0), "Size limit": (True, 5.0)}),
        ((None, 2), " of 2 GB", {"Age limit": (False, 30.0), "Size limit": (True, 2.0)}),
        ((7, None), "", {"Age limit": (True, 7.0), "Size limit": (False, 5.0)}),
    ],
)
@pytest.mark.usefixtures("agaric_settings")
def test_audio_limits_show_the_kept_audio_against_the_limits(
    history: Callable[[], window.HistoryWindow],
    saved: tuple[int | None, int | None] | None,
    said: str,
    shown_limits: dict[str, tuple[bool, float]],
) -> None:
    if saved is not None:
        settings.set_audio_limits(*saved)
    with_size(recording(), GB // 2)
    with_size(recording(started=START + timedelta(hours=1)), GB)
    # Audio the limits can't delete counts all the same.
    with_size(store.create_recording(START + timedelta(hours=2))["id"], GB)
    shown = history()

    dialog = open_settings(shown)

    kept = GLib.format_size(5 * GB // 2)
    assert dialog.audio.get_title() == "Audio limits"
    assert dialog.audio.get_description() == (
        f"Kept audio: {kept}{said}.\n\n{window.AUDIO_LIMITS_NOTE}"
    )
    assert {
        title: (expander.get_enable_expansion(), value.get_value())
        for title, (expander, value) in limits(dialog).items()
    } == shown_limits
    dialog.close()


def save_limits(shown: window.HistoryWindow, dialog: window.SettingsDialog) -> Adw.AlertDialog:
    """The warning Save shows over the Settings."""
    press(dialog, "Save")
    warning = open_dialog(shown)
    assert isinstance(warning, Adw.AlertDialog)
    assert warning.get_heading() == "Save the audio limits?"
    return warning


@pytest.mark.usefixtures("agaric_settings")
def test_saving_audio_limits_says_what_goes_and_takes_it_once_accepted(
    history: Callable[[], window.HistoryWindow],
) -> None:
    ten_days_old = with_size(recording([TRANSCRIBED], started=START - timedelta(days=9)), GB)
    recording([TRANSCRIBED])
    shown = history()
    dialog = open_settings(shown)
    age, size = limits(dialog)["Age limit"], limits(dialog)["Size limit"]
    age[1].set_value(7)
    size[0].set_enable_expansion(False)

    warning = save_limits(shown, dialog)

    assert warning.get_body() == (
        f"The audio of 1 recording, {GLib.format_size(GB)}, is over the new limits and is deleted "
        "now, for good. The transcripts and everything published stay."
    )
    assert warning.get_response_appearance("accept") == Adw.ResponseAppearance.DESTRUCTIVE
    # Nothing is saved, or deleted, before it is accepted.
    assert settings.audio_limits() == (30, 5)
    assert store.holds_audio(ten_days_old) is True

    press(warning, "Accept")
    draw(shown)

    assert settings.audio_limits() == (7, None)
    assert (store.holds_audio(ten_days_old), store.holds_audio(FIRST)) == (False, True)
    kept = GLib.format_size(store.audio_size(FIRST))
    assert dialog.audio.get_description() == f"Kept audio: {kept}.\n\n{window.AUDIO_LIMITS_NOTE}"
    assert window.AUDIO_LIMITS_SAVED in texts(dialog)
    # Saved: nothing is left to save, or to ask about on closing.
    assert (buttons(dialog.audio)["Save"].get_sensitive(), dialog.get_can_close()) == (False, True)
    dialog.close()
    assert lines(expand(shown, ten_days_old))[1] == ("Microphone", "Deleted")
    assert lines(expand(shown))[3] == ("Audio files", "Kept until 2026-11-25 13:05")


@pytest.mark.usefixtures("agaric_settings")
def test_audio_limits_with_nothing_over_them_say_so(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording([TRANSCRIBED])
    shown = history()
    dialog = open_settings(shown)
    limits(dialog)["Age limit"][1].set_value(7)

    warning = save_limits(shown, dialog)

    assert warning.get_body() == "No audio is over the new limits, so nothing is deleted now."
    assert warning.get_response_appearance("accept") == Adw.ResponseAppearance.SUGGESTED
    press(warning, "Accept")
    assert settings.audio_limits() == (7, 5)
    assert store.holds_audio(FIRST) is True
    dialog.close()


@pytest.mark.usefixtures("agaric_settings")
def test_audio_limits_change_nothing_when_the_warning_is_cancelled_or_not_saved(
    history: Callable[[], window.HistoryWindow],
) -> None:
    ten_days_old = recording([TRANSCRIBED], started=START - timedelta(days=9))
    shown = history()
    dialog = open_settings(shown)
    limits(dialog)["Age limit"][1].set_value(7)

    press(save_limits(shown, dialog), "Cancel")
    dialog.close()
    press(open_dialog(shown), "Discard")
    settle()

    assert settings.audio_limits() == (30, 5)
    assert store.holds_audio(ten_days_old) is True


@pytest.mark.usefixtures("agaric_settings")
def test_save_sits_below_the_audio_limits_and_waits_for_a_change(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()
    dialog = open_settings(shown)
    age, size = limits(dialog)["Age limit"], limits(dialog)["Size limit"]
    save = buttons(dialog.audio)["Save"]

    found, top = save.compute_point(size[0], Graphene.Point())
    assert found is True
    assert top.y >= size[0].get_height()

    sensitive = [save.get_sensitive()]
    for change in (
        lambda: age[1].set_value(7),
        lambda: age[1].set_value(30),
        lambda: size[0].set_enable_expansion(False),
        lambda: size[0].set_enable_expansion(True),
    ):
        change()
        sensitive.append(save.get_sensitive())
    assert sensitive == [False, True, False, True, False]

    # Nothing unsaved: it closes without asking.
    dialog.close()
    settle()
    assert shown.get_visible_dialog() is None


def unsaved_limits_question(shown: window.HistoryWindow, dialog: Adw.Dialog) -> Adw.AlertDialog:
    """What closing the Settings with audio limits unsaved asks."""
    dialog.close()
    question = open_dialog(shown)
    assert isinstance(question, Adw.AlertDialog)
    assert (question.get_heading(), question.get_body()) == (
        "Unsaved audio limits",
        "The audio limits shown aren't saved. Closing drops them unless you save them.",
    )
    assert list(buttons(question)) == ["Cancel", "Discard", "Save"]
    return question


@pytest.mark.usefixtures("agaric_settings")
def test_closing_with_unsaved_audio_limits_asks_to_keep_them_or_discard_them(
    history: Callable[[], window.HistoryWindow],
) -> None:
    shown = history()
    dialog = open_settings(shown)
    limits(dialog)["Age limit"][1].set_value(7)

    press(unsaved_limits_question(shown, dialog), "Cancel")
    draw(shown)

    assert shown.get_visible_dialog() is dialog

    press(unsaved_limits_question(shown, dialog), "Discard")
    draw(shown)

    assert shown.get_visible_dialog() is None
    assert settings.audio_limits() == (30, 5)


@pytest.mark.usefixtures("agaric_settings")
def test_saving_on_close_warns_what_goes_and_closes_once_accepted(
    history: Callable[[], window.HistoryWindow],
) -> None:
    ten_days_old = recording([TRANSCRIBED], started=START - timedelta(days=9))
    shown = history()
    dialog = open_settings(shown)
    limits(dialog)["Age limit"][1].set_value(7)

    press(unsaved_limits_question(shown, dialog), "Save")
    warning = open_dialog(shown)
    assert isinstance(warning, Adw.AlertDialog)
    assert warning.get_heading() == "Save the audio limits?"
    press(warning, "Cancel")
    draw(shown)

    assert shown.get_visible_dialog() is dialog
    assert (settings.audio_limits(), store.holds_audio(ten_days_old)) == ((30, 5), True)

    press(unsaved_limits_question(shown, dialog), "Save")
    press(open_dialog(shown), "Accept")
    draw(shown)

    assert shown.get_visible_dialog() is None
    assert (settings.audio_limits(), store.holds_audio(ten_days_old)) == ((7, 5), False)
    assert window.AUDIO_LIMITS_SAVED in texts(shown)


def status_ids() -> list[object]:
    status = json.loads((store.data_dir() / "status.json").read_text())
    return [entry["id"] for entry in status["recent"]]


@pytest.mark.usefixtures("agaric_settings")
def test_the_top_bar_lists_as_many_recordings_as_set_and_follows_at_once(
    history: Callable[[], window.HistoryWindow],
) -> None:
    settings.set_recent_in_menu(12)
    for hour in range(3):
        recording(started=START + timedelta(hours=hour))
    store.write_status()
    shown = history()
    dialog = open_settings(shown)
    count = dialog.recent
    adjustment = count.get_adjustment()

    assert (dialog.top_bar.get_title(), count.get_title(), count.get_value()) == (
        "Top-bar menu",
        "Recent recordings",
        12.0,
    )
    assert (adjustment.get_lower(), adjustment.get_upper()) == (1.0, 20.0)
    assert len(status_ids()) == 3

    count.set_value(2)

    assert settings.recent_in_menu() == 2
    assert status_ids() == ["2026-11-18T15-05-42", "2026-11-18T14-05-42"]
    dialog.close()


def test_the_window_follows_what_other_processes_do(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording([run("transcribe", "running")])
    earlier = recording([run("transcribe", "queued")], START - timedelta(hours=1))
    shown = history()
    row = expand(shown)

    # The worker ends a run and fails the next, a recording starts and another is discarded:
    # each rewrites status.json, which is all the window watches.
    store.save_runs(FIRST, [run("transcribe", "succeeded", output="transcript.md")])
    store.save_runs(earlier, [run("transcribe", "failed", error="Out of memory")])
    store.write_status()
    wait_until(lambda: ("Transcribe", "Succeeded") in lines(row))
    assert row.get_subtitle() == SUBTITLE
    # A row that was never expanded shows it too: its mark, on its one line.
    assert shown.rows[earlier].warning.get_visible() is True
    assert lines(shown.rows[earlier]) == [("Meeting", "2026-11-18 12:05 · 30 min")]
    shutil.rmtree(store.recording_dir(earlier))

    store.create_recording(START + timedelta(hours=1))
    store.write_status()
    wait_until(lambda: listed(shown)[0] == "Meeting | 2026-11-18 14:05 · Recording")
    assert listed(shown) == ["Meeting | 2026-11-18 14:05 · Recording", f"Meeting | {SUBTITLE}"]

    store.discard_if_silent("2026-11-18T14-05-42")
    store.write_status()
    wait_until(lambda: len(shown.rows) == 1)
    assert listed(shown) == [f"Meeting | {SUBTITLE}"]


def found(shown: window.HistoryWindow) -> list[str]:
    """The titles of the rows the search leaves in the list, top to bottom."""
    return [
        row.get_title()
        for row in cast("list[window.RecordingRow]", list(shown.list))
        if row.get_child_visible()
    ]


def search(shown: window.HistoryWindow, typed: str) -> None:
    """Types into the search, as a person would, once it has taken what was typed."""
    taken: list[str] = []
    handler = shown.search.connect("search-changed", lambda entry: taken.append(entry.get_text()))
    shown.search.set_text(typed)
    wait_until(lambda: typed in taken)
    shown.search.disconnect(handler)


def search_toggle(shown: window.HistoryWindow) -> Gtk.ToggleButton:
    [toggle] = [
        widget
        for widget in descendants(shown)
        if isinstance(widget, Gtk.ToggleButton) and widget.get_tooltip_text() == "Search"
    ]
    return toggle


def ctrl_f(shown: window.HistoryWindow) -> None:
    """Does what the window's Ctrl+F does."""
    [action] = [
        shortcut.get_action()
        for controller in cast("list[Gtk.EventController]", list(shown.observe_controllers()))
        if isinstance(controller, Gtk.ShortcutController)
        for shortcut in cast("list[Gtk.Shortcut]", list(controller))
        if (trigger := shortcut.get_trigger()) is not None and trigger.to_string() == "<Control>f"
    ]
    assert action is not None
    assert action.activate(Gtk.ShortcutActionFlags.EXCLUSIVE, shown, None)
    settle()


def test_search_lists_the_recordings_with_every_word_in_their_name_date_or_transcript(
    history: Callable[[], window.HistoryWindow],
) -> None:
    transcribed()
    store.set_speaker_name(FIRST, "Speaker 1", "José Pérez")
    recording(started=START + timedelta(hours=1))
    store.set_typed_name("2026-11-18T14-05-42", "Retro")
    recording(started=START + timedelta(days=1))
    shown = history()
    toggle = search_toggle(shown)
    assert toggle.get_icon_name() == "system-search-symbolic"

    toggle.emit("clicked")
    settle()

    assert shown.search_bar.get_search_mode() is True
    for typed, titles in [
        ("arrancamos", ["Weekly sync"]),
        ("JOSE perez", ["Weekly sync"]),
        ("retro 11-18", ["Retro"]),
        ("2026-11-19", ["Meeting"]),
        ("meeting", ["Meeting"]),
        ("2026", ["Meeting", "Retro", "Weekly sync"]),
    ]:
        search(shown, typed)
        assert found(shown) == titles
        assert shown.pages.get_visible_child() is shown.scrolled


def test_a_search_nothing_matches_says_so_in_place_of_the_list(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()
    ctrl_f(shown)

    search(shown, "standup")

    assert found(shown) == []
    assert shown.pages.get_visible_child() is shown.no_match
    assert (shown.no_match.get_title(), shown.no_match.get_icon_name()) == (
        "No recordings match",
        "system-search-symbolic",
    )

    search(shown, "")

    assert found(shown) == ["Meeting"]
    assert shown.pages.get_visible_child() is shown.scrolled


def test_ctrl_f_opens_the_search_and_escape_closes_it_showing_every_recording(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()

    ctrl_f(shown)

    assert shown.search_bar.get_search_mode() is True
    assert search_toggle(shown).get_active() is True
    search(shown, "standup")
    assert found(shown) == []

    shown.search.emit("stop-search")
    wait_until(lambda: found(shown) == ["Meeting"])

    assert shown.search_bar.get_search_mode() is False
    assert search_toggle(shown).get_active() is False
    assert shown.pages.get_visible_child() is shown.scrolled

    ctrl_f(shown)
    ctrl_f(shown)

    assert shown.search_bar.get_search_mode() is False


def test_the_search_follows_what_changes_and_reads_a_transcript_only_once_it_did(
    history: Callable[[], window.HistoryWindow], monkeypatch: pytest.MonkeyPatch
) -> None:
    transcribed()
    recording(started=START + timedelta(hours=1))
    reads: list[str] = []
    read_transcript = window.read_transcript

    def spy(recording_id: str) -> str:
        reads.append(recording_id)
        return read_transcript(recording_id)

    monkeypatch.setattr(window, "read_transcript", spy)
    shown = history()
    store.write_status()
    wait_until(lambda: reads == [] and shown.rows[FIRST].get_child_visible())

    ctrl_f(shown)
    search(shown, "ana")
    search(shown, "ana d")

    assert found(shown) == []
    assert sorted(reads) == [FIRST, "2026-11-18T14-05-42"]

    store.set_speaker_name(FIRST, "Speaker 1", "Ana Díaz")
    store.write_status()
    wait_until(lambda: found(shown) == ["Weekly sync"])

    assert sorted(reads) == [FIRST, FIRST, "2026-11-18T14-05-42"]

    # A new recording the search leaves out stays out of the list.
    recording(started=START + timedelta(hours=2))
    store.write_status()
    wait_until(lambda: len(shown.rows) == 3)

    assert found(shown) == ["Weekly sync"]

    # A recording that goes takes what the search kept of it along.
    shutil.rmtree(store.recording_dir("2026-11-18T14-05-42"))
    store.write_status()
    wait_until(lambda: len(shown.rows) == 2)

    assert shown.searched.keys() == {FIRST, "2026-11-18T15-05-42"}


def test_reveal_expands_the_recording_and_ignores_one_that_is_gone(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    recording(started=START + timedelta(hours=1))
    shown = history()

    shown.reveal(FIRST)
    shown.reveal("2026-11-18T15-05-42")

    assert [row.get_expanded() for row in shown.rows.values()] == [False, True]


def test_reveal_closes_a_search_that_hides_the_recording_and_keeps_one_that_shows_it(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    recording(started=START + timedelta(hours=1))
    store.set_typed_name(FIRST, "Retro")
    shown = history()
    ctrl_f(shown)
    search(shown, "retro")

    shown.reveal(FIRST)

    assert (shown.search_bar.get_search_mode(), found(shown)) == (True, ["Retro"])

    shown.reveal("2026-11-18T14-05-42")

    assert (shown.search_bar.get_search_mode(), shown.search.get_text()) == (False, "")
    assert found(shown) == ["Meeting", "Retro"]
    assert shown.rows["2026-11-18T14-05-42"].get_expanded() is True


def test_reveal_scrolls_to_a_recording_far_down_the_list(
    history: Callable[[], window.HistoryWindow],
) -> None:
    for days in range(20):
        recording(started=START - timedelta(days=days))
    oldest = "2026-10-30T13-05-42"
    shown = history()
    draw(shown)

    shown.reveal(oldest)
    draw(shown)

    # Its own line in view: what it expanded to is laid out only after the scroll.
    found, top = shown.rows[oldest].compute_point(shown.viewport, Graphene.Point())
    assert found is True
    assert 0 <= top.y < shown.viewport.get_height()


def finalized(widgets: list[Gtk.Widget]) -> list[bool]:
    """Watches the widgets; the list fills as GTK frees them."""
    freed: list[bool] = []
    for widget in widgets:
        widget.weak_ref(freed.append, True)  # noqa: FBT003
    return freed


def test_a_row_that_goes_takes_everything_it_showed_with_it(
    history: Callable[[], window.HistoryWindow], desktop_calls: Desktop
) -> None:
    transcribed()
    store.save_runs(
        FIRST,
        [
            run("summarize", "succeeded", output="summary.md"),
            run("transcribe", "failed", error="Out of memory", details="run-1.log"),
        ],
    )
    (store.recording_dir(FIRST) / "run-1.log").write_text("Out of memory\n")
    shown = history()
    row = expand(shown)
    # Everything that hangs a handler on the row's widgets, dialogs included.
    buttons(shown.details[FIRST].tracks["mic.opus"].row)["Play"].emit("clicked")
    for label, answer in (
        ("Rename…", "Cancel"),
        ("Move to Trash…", "Cancel"),
        ("Delete now", "Cancel"),
        ("Details", None),
        ("Custom name…", "Cancel"),
        # Each publisher's own Publish… asks for its own link.
        ("publish_gitlab", "Cancel"),
        ("publish_github", "Cancel"),
    ):
        if label == "Custom name…":
            choose(shown, pickers(row)["Speaker 2"], label)
        elif label.startswith("publish_"):
            press(getattr(shown.details[FIRST], label).row, "Publish…")
        elif label in {"Rename…", "Move to Trash…"}:
            choose_from_row_menu(shown, label)
        else:
            press(row, label)
        dialog = open_dialog(shown)
        closed = finalized([dialog])
        if answer is None:
            dialog.close()
        else:
            press(dialog, answer)
        del dialog
        gc.collect()
        settle()
        assert shown.get_visible_dialog() is None
        assert closed == [True]
    watched = [
        widget
        for widget in descendants(row)
        if isinstance(widget, Gtk.Button | Gtk.MenuButton | Adw.ActionRow)
    ]
    freed = finalized([row, *watched])
    expected = len(watched) + 1
    del row, watched

    shutil.rmtree(store.recording_dir(FIRST))
    shown.refresh()
    gc.collect()
    settle()

    assert listed(shown) == []
    assert shown.details == {}
    assert desktop_calls.played == [str(store.recording_dir(FIRST) / "mic.opus")]
    assert freed == [True] * expected


def test_a_change_updates_the_row_in_place(history: Callable[[], window.HistoryWindow]) -> None:
    transcribed()
    store.save_runs(FIRST, [run("transcribe", "queued")])
    shown = history()
    row = expand(shown)
    before = list(descendants(shown))

    for status in ("running", "failed", "cancelled", "succeeded", "queued"):
        store.save_runs(FIRST, [run("transcribe", status, error="Out of memory")])
        store.set_speaker_name(FIRST, "Speaker 1", f"Ana {status}")
        shown.refresh()

    # The same widgets, relabelled: hours of changes leave nothing behind to free.
    assert list(descendants(shown)) == before
    assert ("Transcribe", "Queued") in lines(row)
    assert ("Speaker 1", "Ana queued") in lines(row)


def stop_after_a_while() -> int:
    """Ends an application that outstays its test: nothing else can, while its loop runs."""

    def stop() -> bool:
        app = Gio.Application.get_default()
        assert isinstance(app, Gtk.Application)
        for left_open in app.get_windows():
            left_open.destroy()
        return GLib.SOURCE_CONTINUE

    return GLib.timeout_add_seconds(10, stop)


def test_a_second_history_command_reveals_its_recording_in_the_window_already_open() -> None:
    recording()
    recording(started=START + timedelta(hours=1))
    seen: list[object] = []

    def second_command_exited(command: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        command.wait_finish(result)
        app = Gio.Application.get_default()
        assert isinstance(app, window.History)
        assert app.window is not None
        seen.extend(
            [
                command.get_exit_status(),
                len(app.get_windows()),
                [row.get_expanded() for row in app.window.rows.values()],
            ]
        )
        app.window.monitor.cancel()
        # Closing its last window ends the application.
        app.window.destroy()

    def run_second_command() -> bool:
        command = [sys.executable, "-m", "minutas", "history", "--recording", FIRST]
        Gio.Subprocess.new(command, Gio.SubprocessFlags.NONE).wait_async(
            None, second_command_exited
        )
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run_second_command)
    watchdog = stop_after_a_while()

    assert window.run(None) == 0

    GLib.source_remove(watchdog)

    assert seen == [0, 1, [False, True]]


def test_history_opens_on_the_recording_it_is_given() -> None:
    recording()
    seen: list[object] = []

    def look() -> bool:
        app = Gio.Application.get_default()
        assert isinstance(app, window.History)
        assert app.window is not None
        seen.extend([app.window.is_visible(), app.window.rows[FIRST].get_expanded()])
        app.window.monitor.cancel()
        app.window.destroy()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(look)
    watchdog = stop_after_a_while()

    assert window.run(FIRST) == 0

    GLib.source_remove(watchdog)

    assert seen == [True, True]


@pytest.mark.usefixtures("agaric_settings")
def test_settings_open_over_the_recordings_window() -> None:
    seen: list[object] = []

    def look() -> bool:
        app = Gio.Application.get_default()
        assert isinstance(app, window.History)
        assert app.window is not None
        dialog = app.window.get_visible_dialog()
        assert isinstance(dialog, window.SettingsDialog)
        # Once Devin, Agaric and glab have answered: nothing is left to answer a window that is
        # gone.
        if (
            dialog.model.get_subtitle() == window.ASKING_DEVIN
            or dialog.agaric.get_description() == window.ASKING_AGARIC
            or dialog.gitlab.get_description() == window.ASKING_GLAB
        ):
            return GLib.SOURCE_CONTINUE
        seen.extend([app.window.is_visible(), offered(dialog.model)])
        app.window.monitor.cancel()
        app.window.destroy()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(look)
    watchdog = stop_after_a_while()

    assert window.run(None, open_settings=True) == 0

    GLib.source_remove(watchdog)

    assert seen == [True, MODELS]
