import fcntl
import gc
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import cast

import pytest

# Ubuntu builds its PyGObject for its own Python alone: under another, the window cannot run.
gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gtk  # noqa: E402

from minutas import store, window, worker  # noqa: E402

pytestmark = pytest.mark.usefixtures("no_handler_raised")

START = datetime(2026, 11, 18, 13, 5, 42, tzinfo=UTC)
FIRST = "2026-11-18T13-05-42"
SUBTITLE = "2026-11-18 13:05 · 30 min"
KILL = ["systemctl", "--user", "kill", "--signal=SIGUSR1", "minutas-transcribe.service"]
DISMISSED = GLib.Error(
    "Dismissed by user", GLib.quark_to_string(Gtk.dialog_error_quark()), Gtk.DialogError.DISMISSED
)
NO_HANDLER = GLib.Error("No application handles this", "g-io-error-quark", Gio.IOErrorEnum.FAILED)

# What `minutas publish agaric` does, as far as the window can tell: it appends the run its plan
# holds to the recording's log, says what the plan has it say, and exits as the plan has it exit.
FAKE_PUBLISH = """
import json, os, sys
from pathlib import Path
plan_file, *arguments = sys.argv[1:]
plan = json.loads(Path(plan_file).read_text())
Path(plan_file).with_name("called.json").write_text(json.dumps(arguments))
log = Path(os.environ["XDG_DATA_HOME"], "minutas", "recordings", arguments[2], "runs.json")
if plan["run"] is not None:
    log.write_text(json.dumps([*json.loads(log.read_text()), plan["run"]]))
sys.stderr.write(plan["stderr"])
sys.exit(plan["exit"])
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


class Desktop:
    """Stands in for what GTK would hand to the desktop: files and links to open, a place to
    save a copy, a track to play. Nothing is opened, asked or played."""

    def __init__(self) -> None:
        self.opened: list[str | None] = []
        self.open_error: GLib.Error | None = None
        self.save_as: Path | GLib.Error = DISMISSED
        self.suggested: list[str | None] = []
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

    def play(self, stream: Gtk.MediaFile) -> None:
        self.played.append(path_of(stream.get_file()))

    def set_playing(self, stream: Gtk.MediaFile, playing: bool) -> None:  # noqa: FBT001
        assert playing is False
        self.stopped.append(path_of(stream.get_file()))


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
def desktop_calls(monkeypatch: pytest.MonkeyPatch) -> Desktop:
    fake = Desktop()
    for gtk_class, name in (
        (Gtk.FileLauncher, "launch"),
        (Gtk.FileLauncher, "launch_finish"),
        (Gtk.UriLauncher, "launch"),
        (Gtk.UriLauncher, "launch_finish"),
        (Gtk.FileDialog, "save"),
        (Gtk.FileDialog, "save_finish"),
        (Gtk.MediaStream, "play"),
        (Gtk.MediaStream, "set_playing"),
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


def test_an_empty_history_says_so(history: Callable[[], window.HistoryWindow]) -> None:
    shown = history()

    assert listed(shown) == []
    assert "No recordings yet" in texts(shown)


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

    assert [view.recording_id for view in window.load_views(())] == [FIRST]
    assert held == [True]


def test_a_row_expands_to_its_name_its_tracks_and_its_actions(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    shown = history()
    row = shown.rows[FIRST]

    # Built only when the row is expanded: most rows never are.
    assert lines(row) == [("Meeting", SUBTITLE)]

    expand(shown)

    assert lines(row) == [
        ("Meeting", SUBTITLE),
        ("Meeting", "Default name"),
        ("Microphone", ""),
        ("Call", ""),
        ("Audio files", ""),
        ("Transcribe", "Not run yet"),
        ("Publish to Agaric", "Not published"),
    ]
    assert list(buttons(row)) == [
        "Edit",
        "Play",
        "Save a copy",
        "Open folder",
        "Delete now",
        "Transcribe",
        "Publish",
    ]


def test_names_and_errors_are_shown_as_written(history: Callable[[], window.HistoryWindow]) -> None:
    error = "Agaric already has a page named <R&D> 2026-11-18"
    recording([run("publish_agaric", "failed", error=error)])
    store.set_typed_name(FIRST, "<R&D>")
    shown = history()

    row = expand(shown)

    # Shown as markup, GTK would refuse the first and swallow the tag of the second.
    assert texts(row).count("<R&D>") == 2
    assert f"Failed: {error}" in texts(row)


def test_edit_keeps_the_typed_name(history: Callable[[], window.HistoryWindow]) -> None:
    recording()
    store.write_status()
    shown = history()
    row = expand(shown)

    press(row, "Edit")
    dialog = open_dialog(shown)
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Name"
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    assert entry.get_text() == "Meeting"
    entry.set_text("  Weekly\tsync ")
    press(dialog, "Save")

    saved = store.load_recording(FIRST)
    assert (saved["name"], saved["name_source"]) == ("Weekly sync", "typed")
    # The top-bar menu reads the name from status.json, and so does this window.
    assert status_names() == ["Weekly sync"]
    wait_until(lambda: row.get_title() == "Weekly sync")
    assert lines(row)[:2] == [("Weekly sync", SUBTITLE), ("Weekly sync", "Typed name")]


@pytest.mark.parametrize(("typed", "answer"), [("Weekly sync", "Cancel"), (" ", "Save")])
def test_edit_changes_nothing_when_cancelled_or_left_empty(
    history: Callable[[], window.HistoryWindow], typed: str, answer: str
) -> None:
    recording()
    shown = history()
    row = expand(shown)

    press(row, "Edit")
    dialog = open_dialog(shown)
    [entry] = [widget for widget in descendants(dialog) if isinstance(widget, Gtk.Entry)]
    entry.set_text(typed)
    press(dialog, answer)

    saved = store.load_recording(FIRST)
    assert (saved["name"], saved["name_source"]) == ("Meeting", "default")
    assert shown.get_visible_dialog() is None


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
    row.set_expanded(False)

    assert desktop_calls.stopped == [str(store.recording_dir(FIRST) / "call.opus")]
    assert [widget for widget in descendants(row) if isinstance(widget, Gtk.MediaControls)] == []
    assert list(buttons(call.row)) == ["Play", "Save a copy"]
    # Expanded again it is the same seven lines, not a second set.
    expand(shown)
    assert len(lines(row)) == 7


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
    assert lines(row)[2:6] == [
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

    wait_until(lambda: not shown.publishing)

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
    history: Callable[[], window.HistoryWindow], publish: FakePublish
) -> None:
    recording()
    publish.will(TAKEN, exit_status=1)
    shown = history()
    row = expand(shown)

    press(row, "Publish")
    wait_until(lambda: not shown.publishing)

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
    wait_until(lambda: not shown.publishing)

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
    wait_until(lambda: not shown.publishing)

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
    wait_until(lambda: not shown.publishing)

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
    wait_until(lambda: not shown.publishing)

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
    wait_until(lambda: not shown.publishing)

    reason = "Publish to Agaric stopped: RuntimeError: boom"
    assert store.load_runs(FIRST)[-1] == run("publish_agaric", "failed", error=reason)
    assert ("Publish to Agaric", f"Failed: {reason}") in lines(row)
    assert list(buttons(shown.details[FIRST].publish.row)) == ["Open in Agaric", "Retry"]
    assert row.warning.get_visible() is True


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


def test_reveal_expands_the_recording_and_ignores_one_that_is_gone(
    history: Callable[[], window.HistoryWindow],
) -> None:
    recording()
    recording(started=START + timedelta(hours=1))
    shown = history()

    shown.reveal(FIRST)
    shown.reveal("2026-11-18T15-05-42")

    assert [row.get_expanded() for row in shown.rows.values()] == [False, True]


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
    recording([run("transcribe", "failed", error="Out of memory", details="run-0.log")])
    (store.recording_dir(FIRST) / "run-0.log").write_text("Out of memory\n")
    shown = history()
    row = expand(shown)
    # Everything that hangs a handler on the row's widgets, dialogs included.
    buttons(shown.details[FIRST].tracks["mic.opus"].row)["Play"].emit("clicked")
    for label, answer in (("Edit", "Cancel"), ("Delete now", "Cancel"), ("Details", None)):
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
        widget for widget in descendants(row) if isinstance(widget, Gtk.Button | Adw.ActionRow)
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
    recording([run("transcribe", "queued")])
    shown = history()
    row = expand(shown)
    before = list(descendants(shown))

    for status in ("running", "failed", "cancelled", "succeeded", "queued"):
        store.save_runs(FIRST, [run("transcribe", status, error="Out of memory")])
        shown.refresh()

    # The same widgets, relabelled: hours of changes leave nothing behind to free.
    assert list(descendants(shown)) == before
    assert ("Transcribe", "Queued") in lines(row)


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
