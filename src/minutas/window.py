"""The history window: every recording with its name, its audio and its actions.

A thin GTK layer: window_view works out what a row shows, and this draws it. It never polls: it
redraws when status.json changes, which the program rewrites after every change.

Every handler is a method of the window, told which recording it is for. GTK keeps a handler
for as long as its widget lives, so one that held its own row or dialog would keep it, and
everything in it, for good.
"""

import contextlib
import sys
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import override

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402 - after the versions are chosen

from minutas import agaric, store, window_view, worker  # noqa: E402
from minutas.window_view import PUBLISH, TRANSCRIBE, ActionView, RowView  # noqa: E402

APP_ID = "io.github.jfolcini.Minutas"
# The program itself, as the recorder's and the worker's units run it.
MINUTAS = [sys.executable, "-m", "minutas"]
DELETE_WARNING = (
    "Both tracks are deleted for good. The transcript and everything published stay. "
    "Without the audio the recording can't be transcribed again, and publishing leaves it out."
)


def load_views(publishing: Collection[str]) -> list[RowView]:
    """Every recording's row, newest first."""
    # Inside the lock, so a recording being discarded is seen whole or not at all.
    with store.lock():
        logs = worker.runs_by_recording()
        return [
            window_view.row(
                store.load_recording(recording_id),
                logs[recording_id],
                [
                    name
                    for name in store.TRACKS
                    if store.has_audio(store.recording_dir(recording_id) / name)
                ],
                publishing=recording_id in publishing,
            )
            for recording_id in sorted(logs, reverse=True)
        ]


def text_row(title: str) -> Adw.ActionRow:
    # A name or an error is shown as it was written: its "&" and "<" are not markup.
    return Adw.ActionRow(title=title, use_markup=False, subtitle_selectable=True)


def button(label: str, pressed: Callable[..., None], *arguments: object) -> Gtk.Button:
    made = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    made.connect("clicked", pressed, *arguments)
    return made


def warning_icon() -> Gtk.Image:
    return Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["error"])


class TrackControls:
    """A track's row: Play, which becomes the player, and Save a copy."""

    def __init__(self, title: str, file: str, recording_id: str, window: "HistoryWindow") -> None:
        self.row = text_row(title)
        self.play = button("Play", window.play, recording_id, file)
        self.save = button("Save a copy", window.save_copy, recording_id, file)
        self.row.add_suffix(self.play)
        self.row.add_suffix(self.save)
        self.playing: tuple[Gtk.MediaStream, Gtk.MediaControls] | None = None

    def start(self, media: Gtk.MediaStream) -> None:
        player = Gtk.MediaControls(media_stream=media, valign=Gtk.Align.CENTER, width_request=280)
        self.playing = (media, player)
        self.row.add_suffix(player)
        self.play.set_visible(False)
        media.play()

    def stop(self) -> None:
        """Drops the player, and its decoder with it."""
        if self.playing is None:
            return
        media, player = self.playing
        media.set_playing(False)
        self.row.remove(player)
        self.playing = None
        self.play.set_visible(True)


class ActionControls:
    """An action's row: its last result, then Details, its output link and the next step."""

    def __init__(
        self, title: str, output_label: str, action: str, recording_id: str, window: "HistoryWindow"
    ) -> None:
        self.row = text_row(title)
        self.failed = warning_icon()
        self.details = button("Details", window.show_details, recording_id, action)
        self.output = button(output_label, window.open_output, recording_id, action)
        self.use_page = button("Use that page", window.use_page, recording_id)
        self.rename = button("Rename", window.rename_page, recording_id)
        self.next_step = button("", window.next_step, recording_id, action)
        for control in (
            self.failed,
            self.details,
            self.output,
            self.use_page,
            self.rename,
            self.next_step,
        ):
            self.row.add_suffix(control)

    def display(self, view: ActionView) -> None:
        self.row.set_subtitle(view.result)
        self.failed.set_visible(view.failed)
        self.details.set_visible(view.details is not None)
        self.output.set_visible(view.output is not None)
        self.use_page.set_visible(view.taken_by is not None)
        self.rename.set_visible(view.renames)
        self.next_step.set_visible(not view.renames)
        self.next_step.set_label(view.label)
        self.next_step.set_sensitive(not view.disabled)
        self.next_step.set_tooltip_text(view.disabled or None)


class Details:
    """What a recording expands to: its name, its tracks and its actions."""

    def __init__(self, view: RowView, window: "HistoryWindow") -> None:
        recording_id = view.recording_id
        self.name = text_row("")
        self.name.add_suffix(button("Edit", window.edit_name, recording_id))
        self.tracks = {
            track.file: TrackControls(track.title, track.file, recording_id, window)
            for track in view.tracks
        }
        self.files = text_row("Audio files")
        self.files.add_suffix(button("Open folder", window.open_folder, recording_id))
        self.delete = button("Delete now", window.delete_audio, recording_id)
        self.delete.add_css_class("destructive-action")
        self.files.add_suffix(self.delete)
        self.transcribe = ActionControls(
            "Transcribe", "Open transcript", TRANSCRIBE, recording_id, window
        )
        self.publish = ActionControls(
            "Publish to Agaric", "Open in Agaric", PUBLISH, recording_id, window
        )

    def rows(self) -> list[Adw.ActionRow]:
        return [
            self.name,
            *(controls.row for controls in self.tracks.values()),
            self.files,
            self.transcribe.row,
            self.publish.row,
        ]

    def display(self, view: RowView) -> None:
        self.name.set_title(view.name)
        self.name.set_subtitle(view.name_source)
        for track in view.tracks:
            controls = self.tracks[track.file]
            controls.row.set_subtitle(track.subtitle)
            controls.play.set_sensitive(track.available)
            controls.save.set_sensitive(track.available)
            if not track.available:
                controls.stop()
        self.delete.set_sensitive(not view.delete_disabled)
        self.delete.set_tooltip_text(view.delete_disabled or None)
        self.transcribe.display(view.transcribe)
        self.publish.display(view.publish)

    def stop_playing(self) -> None:
        for controls in self.tracks.values():
            controls.stop()


class RecordingRow(Adw.ExpanderRow):
    def __init__(self, view: RowView) -> None:
        super().__init__(use_markup=False)
        self.view = view
        self.warning = warning_icon()
        self.add_suffix(self.warning)
        self.display(view)

    def display(self, view: RowView) -> None:
        self.view = view
        self.set_title(view.name)
        self.set_subtitle(view.subtitle)
        self.warning.set_visible(view.warning)


class HistoryWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application | None = None) -> None:
        super().__init__(
            application=application, title="Minutas", default_width=760, default_height=640
        )
        self.rows: dict[str, RecordingRow] = {}
        # Built when a row is first expanded: most never are.
        self.details: dict[str, Details] = {}
        # The publish commands this window is running, by recording.
        self.publishing: dict[str, Gio.Subprocess] = {}
        self.list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            css_classes=["boxed-list"],
            valign=Gtk.Align.START,
            margin_top=24,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        self.list.set_placeholder(
            Adw.StatusPage(
                title="No recordings yet", description="Record a meeting from the top bar."
            )
        )
        self.viewport = Gtk.Viewport(child=Adw.Clamp(child=self.list))
        self.toasts = Adw.ToastOverlay(
            child=Gtk.ScrolledWindow(child=self.viewport, hscrollbar_policy=Gtk.PolicyType.NEVER)
        )
        page = Adw.ToolbarView(content=self.toasts)
        page.add_top_bar(Adw.HeaderBar())
        self.set_content(page)
        # GLib polls for a monitored file whose folder is missing; in one that exists it waits.
        store.data_dir().mkdir(parents=True, exist_ok=True)
        status = Gio.File.new_for_path(str(store.data_dir() / "status.json"))
        self.monitor = status.monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self.status_changed)
        self.refresh()

    def status_changed(
        self,
        _monitor: Gio.FileMonitor,
        _file: Gio.File,
        _other: Gio.File | None,
        event: Gio.FileMonitorEvent,
    ) -> None:
        # One rewrite is several events; this one comes once, when the new file is whole.
        if event == Gio.FileMonitorEvent.CHANGES_DONE_HINT:
            self.refresh()

    def refresh(self) -> None:
        """Brings the rows up to date; a row whose recording did not change is left alone."""
        views = load_views(self.publishing.keys())
        for recording_id in self.rows.keys() - {view.recording_id for view in views}:
            self.list.remove(self.rows.pop(recording_id))
            self.details.pop(recording_id, None)
        for position, view in enumerate(views):
            row = self.rows.get(view.recording_id)
            if row is None:
                row = self.rows[view.recording_id] = RecordingRow(view)
                row.connect("notify::expanded", self.expanded)
                self.list.insert(row, position)
            elif row.view != view:
                row.display(view)
                if view.recording_id in self.details:
                    self.details[view.recording_id].display(view)

    def reveal(self, recording_id: str) -> None:
        row = self.rows.get(recording_id)
        if row is not None:
            row.set_expanded(True)
            self.viewport.scroll_to(row, None)

    def expanded(self, row: RecordingRow, _property: object) -> None:
        recording_id = row.view.recording_id
        if recording_id not in self.details:
            details = self.details[recording_id] = Details(row.view, self)
            for detail in details.rows():
                row.add_row(detail)
            details.display(row.view)
        if not row.get_expanded():
            self.details[recording_id].stop_playing()

    def shown(self, recording_id: str, action: str) -> ActionView:
        view = self.rows[recording_id].view
        return view.transcribe if action == TRANSCRIBE else view.publish

    def say(self, problem: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=problem, use_markup=False))

    def report(self, error: GLib.Error) -> None:
        """Says what went wrong, unless it was the person closing a dialog."""
        if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
            self.say(error.message)

    def ask_name(self, heading: str, recording_id: str, named: Callable[[str, str], None]) -> None:
        entry = Gtk.Entry(text=self.rows[recording_id].view.name, activates_default=True)
        dialog = Adw.AlertDialog(
            heading=heading, extra_child=entry, default_response="save", close_response="cancel"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self.name_chosen, entry, recording_id, named)

    def name_chosen(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        entry: Gtk.Entry,
        recording_id: str,
        named: Callable[[str, str], None],
    ) -> None:
        name = window_view.clean_name(entry.get_text())
        if dialog.choose_finish(result) == "save" and name is not None:
            named(recording_id, name)

    def edit_name(self, _button: Gtk.Button, recording_id: str) -> None:
        self.ask_name("Name", recording_id, self.name_recording)

    def name_recording(self, recording_id: str, name: str) -> None:
        store.set_typed_name(recording_id, name)
        store.write_status()

    def play(self, _button: Gtk.Button, recording_id: str, track: str) -> None:
        # Made on Play and dropped when it stops: no decoder waits behind a row nobody plays.
        media = Gtk.MediaFile.new_for_filename(str(store.recording_dir(recording_id) / track))
        self.details[recording_id].tracks[track].start(media)

    def save_copy(self, _button: Gtk.Button, recording_id: str, track: str) -> None:
        dialog = Gtk.FileDialog(initial_name=f"{recording_id}-{track}")
        dialog.save(self, None, self.copy_chosen, str(store.recording_dir(recording_id) / track))

    def copy_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, track: str) -> None:
        try:
            # The dialog has already asked before choosing a file that exists.
            Gio.File.new_for_path(track).copy(
                dialog.save_finish(result), Gio.FileCopyFlags.OVERWRITE, None, None
            )
        except GLib.Error as error:
            self.report(error)

    def open_folder(self, _button: Gtk.Button, recording_id: str) -> None:
        self.open(Gio.File.new_for_path(str(store.recording_dir(recording_id))))

    def open(self, file: Gio.File) -> None:
        Gtk.FileLauncher(file=file).launch(self, None, self.opened)

    def opened(self, launcher: Gtk.FileLauncher | Gtk.UriLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            self.report(error)

    def delete_audio(self, _button: Gtk.Button, recording_id: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete the audio?", body=DELETE_WARNING, close_response="cancel"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.choose(self, None, self.delete_chosen, recording_id)

    def delete_chosen(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, recording_id: str
    ) -> None:
        if dialog.choose_finish(result) == "delete":
            store.delete_tracks(recording_id)
            self.refresh()

    def next_step(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        cancels = self.shown(recording_id, action).cancels
        if action == PUBLISH:
            self.publish(recording_id)
        elif cancels is not None:
            worker.cancel(recording_id, cancels)
        else:
            # A worker that does not start leaves a failed run, which says why.
            with contextlib.suppress(worker.WorkerError):
                worker.queue(recording_id, datetime.now(UTC).astimezone())

    def show_details(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        log = store.recording_dir(recording_id) / str(self.shown(recording_id, action).details)
        text = Gtk.TextView(
            editable=False,
            monospace=True,
            top_margin=12,
            bottom_margin=12,
            left_margin=12,
            right_margin=12,
        )
        text.get_buffer().set_text(log.read_text(errors="replace"))
        header = Adw.HeaderBar()
        header.pack_start(button("Copy", self.copy_details, text))
        page = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=text))
        page.add_top_bar(header)
        Adw.Dialog(title="Details", child=page, content_width=720, content_height=480).present(self)

    def copy_details(self, _button: Gtk.Button, text: Gtk.TextView) -> None:
        self.get_clipboard().set(text.get_buffer().props.text)

    def open_output(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        output = str(self.shown(recording_id, action).output)
        if "://" in output:
            Gtk.UriLauncher(uri=output).launch(self, None, self.opened)
        else:
            self.open(Gio.File.new_for_path(str(store.recording_dir(recording_id) / output)))

    def use_page(self, _button: Gtk.Button, recording_id: str) -> None:
        self.publish(recording_id, f"--page={self.shown(recording_id, PUBLISH).taken_by}")

    def rename_page(self, _button: Gtk.Button, recording_id: str) -> None:
        self.ask_name("Rename", recording_id, self.publish_as)

    def publish_as(self, recording_id: str, name: str) -> None:
        # With "=": a name that starts with a dash is still the name, never another option.
        self.publish(recording_id, f"--name={name}")

    def publish(self, recording_id: str, *options: str) -> None:
        command = Gio.Subprocess.new(
            [*MINUTAS, "publish", "agaric", recording_id, *options],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE,
        )
        self.publishing[recording_id] = command
        command.communicate_utf8_async(None, None, self.published, recording_id)
        self.refresh()

    def published(
        self, command: Gio.Subprocess, result: Gio.AsyncResult, recording_id: str
    ) -> None:
        _, _, stderr = command.communicate_utf8_finish(result)
        del self.publishing[recording_id]
        # The command records its own run, failure included; only one that died could not.
        problem = window_view.unrecorded_failure(
            store.load_runs(recording_id), stderr or "", exited_ok=command.get_successful()
        )
        if problem is not None:
            # One that died after starting its run left it running: it fails, and says why.
            agaric.fail_left_running(recording_id, problem)
            self.say(problem)
        self.refresh()


class History(Adw.Application):
    """One instance: a second `minutas history` hands its recording to the first and exits."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window: HistoryWindow | None = None

    @override
    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        if self.window is None:
            self.window = HistoryWindow(self)
        self.window.present()
        for recording_id in command_line.get_arguments()[1:]:
            self.window.reveal(recording_id)
        return 0


def run(recording_id: str | None) -> int:
    """Opens the window, or the one already open, on that recording if one is given."""
    return History().run(["minutas", *([] if recording_id is None else [recording_id])])
