"""The history window: every recording with its name, its audio and its actions.

A thin GTK layer: window_view works out what a row shows, and this draws it. It never polls: it
redraws when status.json changes, which the program rewrites after every change.

Every handler is a method of the window, told which recording it is for. GTK keeps a handler
for as long as its widget lives, so one that held its own row or dialog would keep it, and
everything in it, for good.
"""

import contextlib
import shutil
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast, override

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402 - after the versions are chosen

from minutas import gitlab, settings, store, summarizer, window_view, worker  # noqa: E402
from minutas.window_view import (  # noqa: E402
    PUBLISH,
    PUBLISH_GITLAB,
    SUMMARIZE,
    TRANSCRIBE,
    ActionView,
    RowView,
)

APP_ID = "io.github.jfolcini.Minutas"
# The program itself, as the recorder's and the worker's units run it.
MINUTAS = [sys.executable, "-m", "minutas"]
DELETE_WARNING = (
    "Both tracks are deleted for good. The transcript and everything published stay. "
    "Without the audio the recording can't be transcribed again, and publishing leaves it out."
)
CUSTOM_NAME = "Custom name…"
ASK_LINK = "Paste an issue link to comment on it, or a project link to open a new issue"
NOT_A_LINK = "Not a GitLab issue or project link"
AUDIO_LIMITS_NOTE = (
    "Audio over a limit is deleted, oldest first, once it has been transcribed: when you save, "
    "when a recording stops and when this window opens. The transcripts and everything published "
    "stay."
)


def now() -> datetime:
    return datetime.now(UTC).astimezone()


def load_views(running: Mapping[str, Collection[str]]) -> list[RowView]:
    """Every recording's row, newest first; running holds the recordings of each action whose
    command the window is running."""
    # Looked for at each refresh: one installed while the window is open is found.
    found = shutil.which(summarizer.DEVIN) is not None
    missing_tools = {
        SUMMARIZE: "" if found else summarizer.DEVIN_MISSING,
        PUBLISH_GITLAB: gitlab.glab_problem(),
    }
    age_limit_days, _ = settings.audio_limits()
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
                running=[action for action, ids in running.items() if recording_id in ids],
                missing_tools=missing_tools,
                age_limit_days=age_limit_days,
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


def menu_item(label: str, action: str, target: GLib.Variant) -> Gio.MenuItem:
    # A typed preset name's "_" is shown, not taken for a keyboard shortcut.
    item = Gio.MenuItem.new(label.replace("_", "__"), None)
    item.set_action_and_target_value(action, target)
    return item


def warning_icon() -> Gtk.Image:
    return Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["error"])


def limit_row(
    title: str, unit: str, limit: int | None, default: int
) -> tuple[Adw.ExpanderRow, Adw.SpinRow]:
    """A limit's switch, and its value in unit, which shows the default while it is off."""
    on = limit is not None
    switch = Adw.ExpanderRow(title=title, show_enable_switch=True, enable_expansion=on, expanded=on)
    # GTK needs an upper bound; nobody keeps audio that long, or that much of it.
    value = Adw.SpinRow.new_with_range(1, 10_000, 1)
    value.set_title(unit)
    value.set_value(default if limit is None else limit)
    switch.add_row(value)
    return switch, value


def chosen_limit(switch: Adw.ExpanderRow, value: Adw.SpinRow) -> int | None:
    return int(value.get_value()) if switch.get_enable_expansion() else None


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


def name_menu(recording_id: str, speaker: str, attendees: Sequence[str]) -> Gio.Menu:
    """The names on offer for a speaker: the meeting's attendees, then one to type."""
    menu = Gio.Menu()
    for attendee in attendees:
        # A menu takes "_" for the mark of a shortcut key; doubled, it is the letter.
        named = Gio.MenuItem.new(attendee.replace("_", "__"), None)
        named.set_action_and_target_value(
            "win.name-speaker", GLib.Variant("(sss)", (recording_id, speaker, attendee))
        )
        menu.append_item(named)
    custom = Gio.MenuItem.new(CUSTOM_NAME, None)
    custom.set_action_and_target_value(
        "win.type-speaker-name", GLib.Variant("(ss)", (recording_id, speaker))
    )
    menu.append_item(custom)
    return menu


class SpeakerControls:
    """A speaker's row: the name it goes by, and the menu that gives it one."""

    def __init__(self, recording_id: str, speaker: str) -> None:
        self.recording_id = recording_id
        self.speaker = speaker
        self.row = text_row(speaker)
        self.picker = Gtk.MenuButton(label="Name", valign=Gtk.Align.CENTER)
        self.row.add_suffix(self.picker)
        self.offered: tuple[str, ...] | None = None

    def display(self, name: str, attendees: tuple[str, ...]) -> None:
        self.row.set_subtitle(name)
        # A new menu is a new popover: only when what it offers changed.
        if attendees != self.offered:
            self.offered = attendees
            self.picker.set_menu_model(name_menu(self.recording_id, self.speaker, attendees))


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
        self.next_step.set_visible(not view.renames and bool(view.label))
        self.next_step.set_label(view.label)
        self.next_step.set_sensitive(not view.disabled)
        self.next_step.set_tooltip_text(view.disabled or None)


class Details:
    """What a recording expands to: its name, its tracks, its actions and its speakers."""

    def __init__(self, view: RowView, expander: Adw.ExpanderRow, window: "HistoryWindow") -> None:
        recording_id = view.recording_id
        self.expander = expander
        self.speakers: dict[str, SpeakerControls] = {}
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
        self.summarize = ActionControls(
            "Summarize", "Open summary", SUMMARIZE, recording_id, window
        )
        # Filled each time it opens, so it lists the presets as they are then.
        self.presets = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        self.presets.set_create_popup_func(window.list_presets, recording_id)
        self.summarize.row.add_suffix(self.presets)
        self.publish = ActionControls(
            "Publish to Agaric", "Open in Agaric", PUBLISH, recording_id, window
        )
        self.publish_gitlab = ActionControls(
            "Publish to GitLab", "Open in GitLab", PUBLISH_GITLAB, recording_id, window
        )
        for row in (
            self.name,
            *(controls.row for controls in self.tracks.values()),
            self.files,
            self.transcribe.row,
            self.summarize.row,
            self.publish.row,
            self.publish_gitlab.row,
        ):
            expander.add_row(row)

    def list_speakers(self, view: RowView) -> None:
        """Puts the speakers a transcription found after it, and before Summarize."""
        speakers = [speaker.key for speaker in view.speakers]
        if speakers == list(self.speakers):
            return
        # An expander row only adds at its end: the rows below step out, and back in after them.
        below = (self.summarize.row, self.publish.row, self.publish_gitlab.row)
        for row in [*(controls.row for controls in self.speakers.values()), *below]:
            self.expander.remove(row)
        self.speakers = {key: SpeakerControls(view.recording_id, key) for key in speakers}
        for row in [*(controls.row for controls in self.speakers.values()), *below]:
            self.expander.add_row(row)

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
        self.files.set_subtitle(view.kept_until)
        self.delete.set_sensitive(not view.delete_disabled)
        self.delete.set_tooltip_text(view.delete_disabled or None)
        self.transcribe.display(view.transcribe)
        self.list_speakers(view)
        for speaker in view.speakers:
            self.speakers[speaker.key].display(speaker.name, view.attendees)
        self.summarize.display(view.summarize)
        # Each new label is a new box and label inside the button, even an unchanged one.
        if self.presets.get_label() != view.summarize.menu:
            self.presets.set_label(view.summarize.menu or "")
        self.presets.set_sensitive(not view.summarize.disabled)
        self.presets.set_tooltip_text(view.summarize.disabled or None)
        self.publish.display(view.publish)
        self.publish_gitlab.display(view.publish_gitlab)

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
        # The commands this window is running, by recording.
        self.running: dict[str, dict[str, Gio.Subprocess]] = {
            PUBLISH: {},
            PUBLISH_GITLAB: {},
            SUMMARIZE: {},
        }
        # How many runs a recording had when the window started its command for an action
        # (unrecorded_failure).
        self.runs_before: dict[tuple[str, str], int] = {}
        for name, parameter, activated in (
            ("summarize", "(ss)", self.summarize_with),
            ("custom-summary", "s", self.write_prompt),
            ("delete-preset", "s", self.delete_preset),
        ):
            action = Gio.SimpleAction(name=name, parameter_type=GLib.VariantType(parameter))
            action.connect("activate", activated)
            self.add_action(action)
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
        header = Adw.HeaderBar()
        header.pack_end(button("Audio limits", self.edit_audio_limits))
        page = Adw.ToolbarView(content=self.toasts)
        page.add_top_bar(header)
        self.set_content(page)
        # GLib polls for a monitored file whose folder is missing; in one that exists it waits.
        store.data_dir().mkdir(parents=True, exist_ok=True)
        status = Gio.File.new_for_path(str(store.data_dir() / "status.json"))
        self.monitor = status.monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self.status_changed)
        # What a speaker's menu does: an action each, told which recording and speaker.
        for name, parameters, activated in (
            ("name-speaker", "(sss)", self.speaker_picked),
            ("type-speaker-name", "(ss)", self.type_speaker_name),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameters))
            action.connect("activate", activated)
            self.add_action(action)
        self.delete_audio_over_limits()

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

    def delete_audio_over_limits(self) -> None:
        """Bounds the kept audio, where that costs nothing, and shows what is left."""
        store.delete_audio_over_limits(now(), *settings.audio_limits())
        self.refresh()

    def refresh(self) -> None:
        """Brings the rows up to date; a row whose recording did not change is left alone."""
        views = load_views(self.running)
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
            self.details[recording_id] = Details(row.view, row, self)
            self.details[recording_id].display(row.view)
        if not row.get_expanded():
            self.details[recording_id].stop_playing()

    def shown(self, recording_id: str, action: str) -> ActionView:
        view = self.rows[recording_id].view
        actions = {
            TRANSCRIBE: view.transcribe,
            SUMMARIZE: view.summarize,
            PUBLISH: view.publish,
            PUBLISH_GITLAB: view.publish_gitlab,
        }
        return actions[action]

    def say(self, problem: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=problem, use_markup=False))

    def report(self, error: GLib.Error) -> None:
        """Says what went wrong, unless it was the person closing a dialog."""
        if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
            self.say(error.message)

    def ask_name(
        self, heading: str, name: str, named: Callable[..., None], *arguments: object
    ) -> None:
        """Asks for a name, starting from the one there is, and hands it on after the arguments."""
        entry = Gtk.Entry(text=name, activates_default=True)
        dialog = Adw.AlertDialog(
            heading=heading, extra_child=entry, default_response="save", close_response="cancel"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self.name_chosen, entry, named, *arguments)

    def name_chosen(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        entry: Gtk.Entry,
        named: Callable[..., None],
        *arguments: object,
    ) -> None:
        name = window_view.clean_name(entry.get_text())
        if dialog.choose_finish(result) == "save" and name is not None:
            named(*arguments, name)

    def edit_name(self, _button: Gtk.Button, recording_id: str) -> None:
        name = self.rows[recording_id].view.name
        self.ask_name("Name", name, self.name_recording, recording_id)

    def speaker_picked(self, _action: Gio.SimpleAction, picked: GLib.Variant) -> None:
        self.name_speaker(*picked.unpack())

    def type_speaker_name(self, _action: Gio.SimpleAction, asked: GLib.Variant) -> None:
        recording_id, speaker = asked.unpack()
        names = {shown.key: shown.name for shown in self.rows[recording_id].view.speakers}
        self.ask_name(f"Name {speaker}", names[speaker], self.name_speaker, recording_id, speaker)

    def name_speaker(self, recording_id: str, speaker: str, name: str) -> None:
        # The transcript is written again with the name, for whatever is published next.
        store.set_speaker_name(recording_id, speaker, name)
        store.write_status()

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

    def edit_audio_limits(self, _button: Gtk.Button) -> None:
        age_limit_days, size_limit_gb = settings.audio_limits()
        kept = GLib.format_size(sum(store.audio_size(recording_id) for recording_id in self.rows))
        against = "" if size_limit_gb is None else f" of {size_limit_gb} GB"
        age = limit_row("Age limit", "Days", age_limit_days, settings.AUDIO_AGE_LIMIT_DAYS)
        size = limit_row("Size limit", "GB", size_limit_gb, settings.AUDIO_SIZE_LIMIT_GB)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        rows.append(age[0])
        rows.append(size[0])
        dialog = Adw.AlertDialog(
            heading="Audio limits",
            body=f"Kept audio: {kept}{against}.\n\n{AUDIO_LIMITS_NOTE}",
            extra_child=rows,
            default_response="save",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self.audio_limits_chosen, age, size)

    def audio_limits_chosen(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        age: tuple[Adw.ExpanderRow, Adw.SpinRow],
        size: tuple[Adw.ExpanderRow, Adw.SpinRow],
    ) -> None:
        if dialog.choose_finish(result) == "save":
            settings.set_audio_limits(chosen_limit(*age), chosen_limit(*size))
            self.delete_audio_over_limits()

    def next_step(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        shown = self.shown(recording_id, action)
        if action == PUBLISH:
            self.publish(recording_id)
        elif action == PUBLISH_GITLAB:
            self.ask_link(recording_id, shown.link or "")
        elif action == SUMMARIZE:
            self.summarize(recording_id, str(shown.prompt))
        elif shown.cancels is not None:
            worker.cancel(recording_id, shown.cancels)
        else:
            # A worker that does not start leaves a failed run, which says why.
            with contextlib.suppress(worker.WorkerError):
                worker.queue(recording_id, now())

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
        self.ask_name("Rename", self.rows[recording_id].view.name, self.publish_as, recording_id)

    def publish_as(self, recording_id: str, name: str) -> None:
        # With "=": a name that starts with a dash is still the name, never another option.
        self.publish(recording_id, f"--name={name}")

    def publish(self, recording_id: str, *options: str) -> None:
        self.start(PUBLISH, recording_id, ["publish", "agaric", recording_id, *options])

    def ask_link(self, recording_id: str, link: str) -> None:
        """Asks where to publish to GitLab, saying what the link typed will get."""
        entry = Gtk.Entry(text=link, activates_default=True)
        dialog = Adw.AlertDialog(
            heading="Publish to GitLab",
            extra_child=entry,
            default_response="publish",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("publish", "Publish")
        dialog.set_response_appearance("publish", Adw.ResponseAppearance.SUGGESTED)
        entry.connect("changed", self.link_typed)
        dialog.choose(self, None, self.link_chosen, entry, recording_id)
        # Once presented: until then the entry is not inside the dialog.
        self.link_typed(entry)

    def link_typed(self, entry: Gtk.Entry) -> None:
        # The dialog is found, not held: a handler that held it would keep it for good.
        dialog = cast("Adw.AlertDialog", entry.get_ancestor(Adw.AlertDialog))
        typed = entry.get_text().strip()
        target = gitlab.parse_link(typed)
        if target is not None:
            dialog.set_body(target.plan)
        else:
            dialog.set_body(NOT_A_LINK if typed else ASK_LINK)
        dialog.set_response_enabled("publish", target is not None)

    def link_chosen(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, entry: Gtk.Entry, recording_id: str
    ) -> None:
        target = gitlab.parse_link(entry.get_text())
        if dialog.choose_finish(result) == "publish" and target is not None:
            self.start(
                PUBLISH_GITLAB, recording_id, ["publish", "gitlab", recording_id, target.link]
            )

    def list_presets(self, presets: Gtk.MenuButton, recording_id: str) -> None:
        """The built-in and saved presets, Custom, and the saved ones to delete."""
        chosen = Gio.Menu()
        for name, prompt in summarizer.presets().items():
            target = GLib.Variant("(ss)", (recording_id, prompt))
            chosen.append_item(menu_item(name, "win.summarize", target))
        others = Gio.Menu()
        others.append_item(
            menu_item("Custom…", "win.custom-summary", GLib.Variant("s", recording_id))
        )
        saved = settings.saved_presets()
        if saved:
            deletable = Gio.Menu()
            for name in saved:
                target = GLib.Variant("s", name)
                deletable.append_item(menu_item(name, "win.delete-preset", target))
            others.append_submenu("Delete a preset", deletable)
        menu = Gio.Menu()
        menu.append_section(None, chosen)
        menu.append_section(None, others)
        presets.set_menu_model(menu)

    def summarize_with(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        recording_id, prompt = target.unpack()
        self.summarize(recording_id, prompt)

    def write_prompt(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        prompt = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            accepts_tab=False,
            top_margin=8,
            bottom_margin=8,
            left_margin=8,
            right_margin=8,
        )
        name = Gtk.Entry(placeholder_text="Save as preset: its name", activates_default=True)
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        fields.append(
            Gtk.ScrolledWindow(child=prompt, min_content_height=120, css_classes=["card"])
        )
        fields.append(name)
        dialog = Adw.AlertDialog(
            heading="Custom summary",
            body="What should the summary cover?",
            extra_child=fields,
            default_response="summarize",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("summarize", "Summarize")
        dialog.set_response_appearance("summarize", Adw.ResponseAppearance.SUGGESTED)
        dialog.choose(self, None, self.prompt_written, prompt, name, target.get_string())

    def prompt_written(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        prompt: Gtk.TextView,
        name: Gtk.Entry,
        recording_id: str,
    ) -> None:
        text = prompt.get_buffer().props.text.strip()
        if dialog.choose_finish(result) != "summarize" or not text:
            return
        preset = window_view.clean_name(name.get_text())
        if preset is not None:
            settings.save_preset(preset, text)
        self.summarize(recording_id, text)

    def delete_preset(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        settings.delete_preset(target.get_string())

    def summarize(self, recording_id: str, prompt: str) -> None:
        # With "=": a prompt that starts with a dash is still the prompt, never another option.
        self.start(SUMMARIZE, recording_id, ["summarize", recording_id, f"--prompt={prompt}"])

    def start(self, action: str, recording_id: str, arguments: list[str]) -> None:
        """Runs the minutas command that does the action, and follows it until it exits."""
        # Counted before the spawn, whose command may record its run first.
        self.runs_before[recording_id, action] = len(store.load_runs(recording_id))
        command = Gio.Subprocess.new(
            [*MINUTAS, *arguments],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE,
        )
        self.running[action][recording_id] = command
        command.communicate_utf8_async(None, None, self.ended, action, recording_id)
        self.refresh()

    def ended(
        self, command: Gio.Subprocess, result: Gio.AsyncResult, action: str, recording_id: str
    ) -> None:
        _, _, stderr = command.communicate_utf8_finish(result)
        del self.running[action][recording_id]
        # The command records its own run, failure included; only one that died could not.
        problem = window_view.unrecorded_failure(
            store.load_runs(recording_id),
            action,
            stderr or "",
            exited_ok=command.get_successful(),
            known=self.runs_before.pop((recording_id, action)),
        )
        if problem is not None:
            # One that died after starting its run left it running: it fails, and says why.
            store.fail_left_running(recording_id, action, problem)
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
