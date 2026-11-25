"""The Recordings window: every recording with its name, its audio and its actions; and the
Settings, a dialog over it.

A thin GTK layer: window_view works out what a row shows, and this draws it. It never polls: it
redraws when status.json changes, which the program rewrites after every change.

Every handler is a method of the window, told which recording it is for. GTK keeps a handler
for as long as its widget lives, so one that held its own row or dialog would keep it, and
everything in it, for good.
"""

import contextlib
import shutil
import sys
import tempfile
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import metadata
from pathlib import Path
from typing import NamedTuple, cast, override

import gi
from keyring.errors import KeyringError

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402 - after the versions are chosen

from minutas import (  # noqa: E402
    github,
    gitlab,
    player,
    settings,
    store,
    summarizer,
    window_view,
    worker,
)
from minutas.window_view import (  # noqa: E402
    MIXED,
    PUBLISH,
    PUBLISH_GITHUB,
    PUBLISH_GITLAB,
    SUMMARIZE,
    TRANSCRIBE,
    ActionView,
    AgaricView,
    RowView,
)

APP_ID = "io.github.jfolcini.Minutas"
# The program itself, as the recorder's and the worker's units run it.
MINUTAS = [sys.executable, "-m", "minutas"]
TRASH_WARNING = (
    "Its folder goes to the Trash, with the audio, the transcript, the summary and the history "
    "of its runs, and can be restored from there. What was published stays in Agaric, GitLab "
    "and GitHub."
)
DELETE_WARNING = (
    "Both tracks are deleted for good. The transcript and everything published stay. "
    "Without the audio the recording can't be transcribed again, and publishing leaves it out."
)
CUSTOM_NAME = "Custom name…"
RENAME_NOTE = "A typed name is kept for good, and the next Publish to Agaric applies it."
ASK_LINK = "Paste an issue link to comment on it, or a project link to open a new issue"
NOT_A_LINK = "Not a GitLab issue or project link"
ASK_GITHUB_LINK = "Paste an issue link to comment on it, or a repository link to open a new issue"
NOT_A_GITHUB_LINK = "Not a GitHub issue or repository link"
GIST_NOTE = (
    "The transcript goes to a secret gist, which anyone with its link can read. GitHub takes no "
    "audio, so it is left out."
)


class LinkAsk(NamedTuple):
    """What Publish to GitLab or GitHub asks for, and how it reads the link typed."""

    parse: Callable[[str], gitlab.Target | github.Target | None]
    ask: str
    not_a_link: str
    # Said under every answer; empty for none.
    note: str
    # The publish command's word for it.
    command: str


LINK_ASKS = {
    PUBLISH_GITLAB: LinkAsk(gitlab.parse_link, ASK_LINK, NOT_A_LINK, "", "gitlab"),
    PUBLISH_GITHUB: LinkAsk(
        github.parse_link, ASK_GITHUB_LINK, NOT_A_GITHUB_LINK, GIST_NOTE, "github"
    ),
}
GITHUB_LINK_NOTE = (
    "Where Publish to GitHub starts: an issue link to comment on it, or a repository link to open "
    "a new issue. Empty for none."
)
AUDIO_LIMITS_NOTE = (
    "Audio over a limit is deleted, oldest first, once it has been transcribed: when you save, "
    "when a recording stops and when the Recordings window opens. The transcripts and everything "
    "published stay."
)
AUDIO_LIMITS_SAVED = "Audio limits saved"
UNSAVED_LIMITS = "The audio limits shown aren't saved. Closing drops them unless you save them."
ASKING_AGARIC = "Asking Agaric for the spaces the agent may use…"
PASTE_AGARIC = "Paste the agent's config from Agaric, or its token"
AGARIC_STORED = "Stored in the keyring"
AGARIC_NOT_SET = "Not set"
GET_AGARIC_CONFIG = (
    "To connect Agaric: in Agaric, open Settings → Agent access, add an agent with read-write "
    "access to the Work space, copy the config it shows, and paste it below."
)
ASKING_GLAB = "Asking glab whether it is logged in…"
GLAB_SILENT = "Asking glab stopped without saying why"
GITLAB_NOTE = (
    "Publish to GitLab posts with glab's login. A token pasted below goes to glab, which keeps it "
    "in the keyring; it is never shown back."
)
PASTE_GITLAB = "Paste a personal access token with the api scope"
# The command line's word for opening the Settings; no recording id is a word.
SETTINGS = "settings"
DEVINS_DEFAULT = "Devin's default"
ASKING_DEVIN = "Asking Devin for the models your account can use…"
BUILT_IN_SKILL = "Built-in: meeting notes"
VOCABULARY_NOTE = (
    "Names, products and jargon to listen for, one per line, besides each meeting's attendees. "
    "The vocabularies switched on are used from the next transcription, or Run again. Keep them "
    "to words your meetings use: a long list of common words can make them heard where they "
    "weren't said."
)
ECHO_CANCEL_NOTE = (
    "Records the microphone with the computer's sound taken out, so a call on speakers isn't "
    "transcribed twice. Applies from the next recording."
)
NEW_VOCABULARY_NOTE = "Its file then opens in the text editor: one word or phrase a line."
VOCABULARY_TRASH_WARNING = (
    "Its file goes to the Trash, where it can be restored. Transcripts already written stay as "
    "they are."
)
CONTEXT_NOTE = (
    "Glossaries, background and references a summary can rely on, a Markdown file each, ticked "
    "in the Summarize menu. Links aren't followed: the summarizer reads nothing but its prompt, "
    "so paste references in as text."
)
NEW_CONTEXT_NOTE = "Its name in the Summarize menu. Its file then opens in the text editor."
NAME_BAD = "A {kind}'s name can't start with a dot or hold a slash"
NAME_TAKEN = "There is a {kind} named {name} already"
CONTEXT_TRASH_WARNING = (
    "Its file goes to the Trash, where it can be restored. Summaries already written stay as "
    "they are."
)
# Read once: an update replaces the package on disk, not the one this window runs.
PROJECT = metadata("minutas")
ABOUT, UPDATE = "about", "update"
UPDATING = "Updating Minutas…"
PLAY_ICON, PAUSE_ICON = "media-playback-start-symbolic", "media-playback-pause-symbolic"
# The clock moves by whole seconds; four looks at the player's place in each keep the slider smooth.
POSITION_EVERY_MS = 250
MIXING = "Mixing the tracks into a copy…"


def now() -> datetime:
    return datetime.now(UTC).astimezone()


def load_views(running: Mapping[str, Collection[str]]) -> list[RowView]:
    """Every recording's row, newest first; running holds the recordings of each action whose
    command the window is running."""
    # Looked for at each refresh: one installed or updated while the window is open is found.
    missing_tools = {
        SUMMARIZE: summarizer.problem(),
        PUBLISH_GITLAB: gitlab.glab_problem(),
        PUBLISH_GITHUB: github.gh_problem(),
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


def read_transcript(recording_id: str) -> str:
    """Its transcript; empty until it has one."""
    path = store.recording_dir(recording_id) / store.TRANSCRIPT
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def text_row(title: str) -> Adw.ActionRow:
    # A name or an error is shown as it was written: its "&" and "<" are not markup.
    return Adw.ActionRow(title=title, use_markup=False, subtitle_selectable=True)


def button(label: str, pressed: Callable[..., None], *arguments: object) -> Gtk.Button:
    made = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    made.connect("clicked", pressed, *arguments)
    return made


def menu_item(label: str, action: str, target: GLib.Variant | None) -> Gio.MenuItem:
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


def last_line(said: str) -> str:
    lines = said.strip().splitlines()
    return lines[-1].strip() if lines else ""


def vocabulary_row(title: str, name: str, window: "HistoryWindow") -> Adw.ActionRow:
    row = text_row(title)
    row.add_suffix(button("Open", window.open_vocabulary, name))
    if name != settings.GLOSSARY:
        row.add_suffix(button("Delete", window.ask_to_delete_vocabulary, name))
    used = Gtk.Switch(active=name in settings.vocabularies(), valign=Gtk.Align.CENTER)
    used.connect("notify::active", window.vocabulary_switched, name)
    row.add_suffix(used)
    row.set_activatable_widget(used)
    return row


class SpaceRow(Adw.ComboRow):
    """The spaces the agent may use, by name, the one it publishes to chosen; shown once Agaric
    has listed any."""

    def __init__(self, chosen: Callable[..., None]) -> None:
        super().__init__(title="Space", visible=False)
        self.ids: tuple[str, ...] = ()
        self.chosen_handler = self.connect("notify::selected", chosen)

    def offer(self, view: AgaricView) -> None:
        # A new list selects its first space on the way: only the person's choice is saved.
        with self.handler_block(self.chosen_handler):
            self.ids = tuple(space_id for space_id, _ in view.choices)
            self.set_model(Gtk.StringList.new([name for _, name in view.choices]))
            self.set_selected(Gtk.INVALID_LIST_POSITION if view.chosen is None else view.chosen)
        self.set_visible(bool(view.choices))

    def chosen(self) -> str | None:
        place = self.get_selected()
        return self.ids[place] if place < len(self.ids) else None


def offer(
    row: Adw.ComboRow, handler: int, choices: Sequence[tuple[str, str | None]], saved: str | None
) -> None:
    """Each choice by its label, the saved one chosen, else the first, unheard by the handler."""
    values = [value for _, value in choices]
    with row.handler_block(handler):
        row.set_model(Gtk.StringList.new([label for label, _ in choices]))
        row.set_selected(values.index(saved) if saved in values else 0)


class SettingsDialog(Adw.PreferencesDialog):
    """The settings: each change is saved as it is made, but the audio limits, which delete
    audio, wait for Save and a warning; closing with them unsaved asks what to do with them."""

    def __init__(self, window: "HistoryWindow") -> None:
        super().__init__(title="Settings")
        self.found: list[summarizer.Model] = []
        titles = [tool.title for tool in summarizer.TOOLS]
        self.summarizer = Adw.ComboRow(title="Summarizer", model=Gtk.StringList.new(titles))
        self.summarizer.set_selected(summarizer.TOOLS.index(summarizer.chosen()))
        self.summarizer.connect("notify::selected", window.summarizer_chosen)
        self.model = Adw.ComboRow(title="Model", subtitle=ASKING_DEVIN, sensitive=False)
        self.model_handler = self.model.connect("notify::selected", window.model_chosen)
        self.effort = Adw.ComboRow(title="Effort", visible=False)
        self.effort_handler = self.effort.connect("notify::selected", window.effort_chosen)
        # A path is shown as written: its "&" is no markup.
        self.skill = Adw.ActionRow(title="Skill", subtitle_selectable=True, use_markup=False)
        self.skill.add_suffix(button("Open", window.open_skill))
        self.skill.add_suffix(button("Choose…", window.choose_skill))
        self.built_in = button("Use built-in", window.use_built_in_skill)
        self.skill.add_suffix(self.built_in)
        self.show_skill()
        summary = Adw.PreferencesGroup(title="Summary")
        for row in (self.summarizer, self.model, self.effort, self.skill):
            summary.add(row)

        self.agaric = Adw.PreferencesGroup(title="Agaric", description=ASKING_AGARIC)
        # Whether one is stored, never the token itself.
        self.token = Adw.ActionRow(title="Agent token", use_markup=False)
        # Left empty: a stored token is never shown back.
        self.pasted = Adw.PasswordEntryRow(title=PASTE_AGARIC, show_apply_button=True)
        self.pasted.connect("apply", window.agaric_pasted)
        self.space = SpaceRow(window.space_chosen)
        for row in (self.token, self.pasted, self.space):
            self.agaric.add(row)

        self.gitlab = self.gitlab_group(window)
        self.github = self.github_group(window)

        age_limit_days, size_limit_gb = settings.audio_limits()
        self.age = limit_row("Age limit", "Days", age_limit_days, settings.AUDIO_AGE_LIMIT_DAYS)
        self.size = limit_row("Size limit", "GB", size_limit_gb, settings.AUDIO_SIZE_LIMIT_GB)
        self.audio = Adw.PreferencesGroup(title="Audio limits")
        self.audio.add(self.age[0])
        self.audio.add(self.size[0])
        for switch, value in (self.age, self.size):
            switch.connect("notify::enable-expansion", window.limits_changed)
            value.connect("notify::value", window.limits_changed)
        # A widget that is no row goes below the group's rows.
        self.save_limits = Gtk.Button(
            label="Save", halign=Gtk.Align.END, margin_top=12, sensitive=False
        )
        self.save_limits.connect("clicked", window.save_audio_limits)
        self.audio.add(self.save_limits)
        self.connect("close-attempt", window.settings_closing)

        self.top_bar = self.top_bar_group(window)

        page = Adw.PreferencesPage()
        for group in (
            self.recording_group(window),
            self.transcription_group(window),
            summary,
            self.context_group(window),
            self.agaric,
            self.gitlab,
            self.github,
            self.audio,
            self.top_bar,
        ):
            page.add(group)
        self.add(page)

    def chosen_limits(self) -> tuple[int | None, int | None]:
        return chosen_limit(*self.age), chosen_limit(*self.size)

    def show_unsaved_limits(self) -> None:
        """Offers Save while the limits shown are not the saved ones, and asks before closing
        then."""
        unsaved = self.chosen_limits() != settings.audio_limits()
        self.save_limits.set_sensitive(unsaved)
        self.set_can_close(not unsaved)

    def top_bar_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """How many recent recordings the top-bar menu lists."""
        self.recent = Adw.SpinRow.new_with_range(1, settings.MOST_RECENT_IN_MENU, 1)
        self.recent.set_title("Recent recordings")
        self.recent.set_value(settings.recent_in_menu())
        self.recent.connect("notify::value", window.recent_chosen)
        group = Adw.PreferencesGroup(title="Top-bar menu")
        group.add(self.recent)
        return group

    def recording_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """Whether a recording takes the echo-cancelled mic."""
        echo_cancel = Adw.SwitchRow(
            title="Echo cancellation", subtitle=ECHO_CANCEL_NOTE, active=settings.echo_cancel()
        )
        echo_cancel.connect("notify::active", window.echo_cancel_switched)
        group = Adw.PreferencesGroup(title="Recording")
        group.add(echo_cancel)
        return group

    def transcription_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """The glossary, then the other vocabularies, each with whether it is used, Open and,
        but for the glossary, Delete; and New… to make one."""
        self.transcription = Adw.PreferencesGroup(
            title="Transcription", description=VOCABULARY_NOTE
        )
        self.transcription.set_header_suffix(button("New…", window.new_vocabulary))
        self.vocabulary = vocabulary_row("Vocabulary", settings.GLOSSARY, window)
        self.transcription.add(self.vocabulary)
        self.vocabulary_rows: list[Adw.ActionRow] = []
        self.show_vocabularies(window)
        return self.transcription

    def show_vocabularies(self, window: "HistoryWindow") -> None:
        for row in self.vocabulary_rows:
            self.transcription.remove(row)
        self.vocabulary_rows = [
            vocabulary_row(name, name, window) for name in worker.vocabulary_names()
        ]
        for row in self.vocabulary_rows:
            self.transcription.add(row)

    def context_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """The context presets, each with Open and Delete, and New… to make one."""
        self.context = Adw.PreferencesGroup(title="Summary context", description=CONTEXT_NOTE)
        self.context.set_header_suffix(button("New…", window.new_context))
        self.context_rows: list[Adw.ActionRow] = []
        self.show_context(window)
        return self.context

    def show_context(self, window: "HistoryWindow") -> None:
        for row in self.context_rows:
            self.context.remove(row)
        self.context_rows = [text_row(name) for name in summarizer.context_names()]
        for row in self.context_rows:
            row.add_suffix(button("Open", window.open_context, row.get_title()))
            row.add_suffix(button("Delete", window.ask_to_delete_context, row.get_title()))
            self.context.add(row)

    def gitlab_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """The default link Publish to GitLab starts from, and the token glab keeps for it."""
        self.gitlab_link = Adw.EntryRow(
            title="Default project or issue link",
            text=settings.gitlab_link() or "",
            show_apply_button=True,
        )
        self.gitlab_link.connect("apply", window.gitlab_link_applied)
        # Left empty: glab keeps the token, and it is never shown back.
        self.gitlab_token = Adw.PasswordEntryRow(title=PASTE_GITLAB, show_apply_button=True)
        self.gitlab_token.connect("apply", window.gitlab_token_pasted)
        group = Adw.PreferencesGroup(title="GitLab", description=ASKING_GLAB)
        group.add(self.gitlab_link)
        group.add(self.gitlab_token)
        return group

    def github_group(self, window: "HistoryWindow") -> Adw.PreferencesGroup:
        """The default link Publish to GitHub starts from."""
        self.github_link = Adw.EntryRow(
            title="Default link", text=settings.github_link() or "", show_apply_button=True
        )
        self.github_link.connect("apply", window.github_link_set)
        group = Adw.PreferencesGroup(title="GitHub", description=GITHUB_LINK_NOTE)
        group.add(self.github_link)
        return group

    def show_skill(self) -> None:
        chosen = settings.summary_skill()
        self.skill.set_subtitle(BUILT_IN_SKILL if chosen is None else chosen)
        self.built_in.set_visible(chosen is not None)

    def models_unknown(self, problem: str) -> None:
        """No model to choose, and why: Devin has yet to list its own, or can't."""
        self.found = []
        with self.model.handler_block(self.model_handler):
            self.model.set_model(Gtk.StringList.new([]))
        self.model.set_subtitle(problem)
        self.model.set_sensitive(False)
        self.effort.set_visible(False)

    def offer_claude(self) -> None:
        """Claude Code's models and efforts, the saved ones chosen; it lists neither itself."""
        self.found = []
        offer(self.model, self.model_handler, summarizer.CLAUDE_MODELS, settings.claude_model())
        offer(self.effort, self.effort_handler, summarizer.CLAUDE_EFFORTS, settings.claude_effort())
        self.model.set_subtitle(summarizer.claude_problem())
        self.model.set_sensitive(True)
        self.effort.set_visible(True)

    def offer_models(self, found: list[summarizer.Model], saved: str | None) -> None:
        """Devin's models, the saved one chosen; models() lists every saved one."""
        self.found = found
        family, effort = (-1, 0) if saved is None else summarizer.find(found, saved) or (-1, 0)
        with self.model.handler_block(self.model_handler):
            names = [DEVINS_DEFAULT, *(model.name for model in found)]
            self.model.set_model(Gtk.StringList.new(names))
            self.model.set_selected(family + 1)
        self.model.set_subtitle("")
        self.model.set_sensitive(True)
        self.offer_efforts(effort)

    def efforts(self) -> tuple[tuple[str, str], ...]:
        family = self.model.get_selected() - 1
        return self.found[family].efforts if 0 <= family < len(self.found) else ()

    def offer_efforts(self, chosen: int) -> None:
        """The chosen model's efforts, with one of them chosen; none for Devin's default."""
        efforts = self.efforts()
        with self.effort.handler_block(self.effort_handler):
            self.effort.set_model(Gtk.StringList.new([label for label, _ in efforts]))
            self.effort.set_selected(chosen)
        # A model with a single effort has nothing to choose.
        self.effort.set_visible(len(efforts) > 1)

    def chosen_model(self) -> str | None:
        """The model and effort chosen, as Devin names them; None for its default."""
        efforts = self.efforts()
        return efforts[self.effort.get_selected()][1] if efforts else None


def settings_of(widget: Gtk.Widget) -> SettingsDialog:
    # Found, not held: a handler that held its dialog would keep it for good.
    return cast("SettingsDialog", widget.get_ancestor(SettingsDialog))


def wrap_title_between_words(row: Adw.ActionRow) -> None:
    """libadwaita lets a row's title break inside a word; a player beside it split "Microphone"."""

    def labels(widget: Gtk.Widget) -> Iterator[Gtk.Label]:
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                yield child
            yield from labels(child)
            child = child.get_next_sibling()

    title = next(label for label in labels(row) if label.has_css_class("title"))
    title.set_wrap_mode(Pango.WrapMode.WORD)


class TrackControls:
    """A track's row, or the mix's: Play, which becomes the player, and Save a copy."""

    def __init__(self, title: str, key: str, recording_id: str, window: "HistoryWindow") -> None:
        self.row = text_row(title)
        wrap_title_between_words(self.row)
        self.play = button("Play", window.play, recording_id, key)
        self.pause = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.pause.connect("clicked", window.toggle_pause, recording_id, key)
        self.position = Gtk.Scale(
            draw_value=False, hexpand=True, width_request=120, valign=Gtk.Align.CENTER
        )
        self.position.update_property([Gtk.AccessibleProperty.LABEL], ["Position"])
        # The arrow keys, and Page Up and Page Down, in seconds.
        self.position.set_increments(5, 60)
        self.position.connect("change-value", window.seek, recording_id, key)
        self.time = Gtk.Label(css_classes=["numeric"])
        self.speed = Gtk.DropDown(
            model=Gtk.StringList.new(list(window_view.SPEED_LABELS)),
            tooltip_text="Speed",
            valign=Gtk.Align.CENTER,
        )
        self.speed_handler = self.speed.connect(
            "notify::selected", window.speed_chosen, recording_id, key
        )
        self.player = Gtk.Box(spacing=6, visible=False)
        for control in (self.pause, self.position, self.time, self.speed):
            self.player.append(control)
        self.save = button("Save a copy", window.save_copy, recording_id, key)
        for control in (self.play, self.player, self.save):
            self.row.add_suffix(control)
        self.playing: player.Player | None = None
        self.paused = False
        self.ticking = 0

    def start(self, playing: player.Player) -> None:
        self.playing = playing
        self.show_paused(paused=False)
        playing.play()
        self.show_position()
        self.ticking = GLib.timeout_add(POSITION_EVERY_MS, self.show_position)
        self.play.set_visible(False)
        self.player.set_visible(True)
        # The keyboard stays where it was working, not thrown to the top of the window.
        self.pause.grab_focus()

    def now_playing(self) -> player.Player:
        # Its controls show only while it plays.
        return cast("player.Player", self.playing)

    def show_paused(self, *, paused: bool) -> None:
        self.paused = paused
        self.pause.set_icon_name(PLAY_ICON if paused else PAUSE_ICON)
        self.pause.set_tooltip_text("Play" if paused else "Pause")

    def toggle_pause(self) -> None:
        playing = self.now_playing()
        if self.paused:
            playing.play()
        else:
            playing.pause()
        self.show_paused(paused=not self.paused)

    def show_position(self) -> bool:
        at, length = self.now_playing().position()
        self.position.set_range(0, length)
        self.position.set_value(at)
        self.time.set_label(f"{window_view.clock(at)} / {window_view.clock(length)}")
        return GLib.SOURCE_CONTINUE

    def stop(self) -> None:
        """Drops the player, and its decoder with it."""
        if self.playing is None:
            return
        self.playing.stop()
        self.playing = None
        GLib.source_remove(self.ticking)
        working = self.player.get_focus_child() is not None
        self.player.set_visible(False)
        self.play.set_visible(True)
        if working:
            self.play.grab_focus()
        # The next player starts at normal speed.
        with self.speed.handler_block(self.speed_handler):
            self.speed.set_selected(0)


def audio_of(recording_id: str, key: str) -> list[Path]:
    """What a row plays and saves: its track, or for the mix the tracks that hold audio."""
    folder = store.recording_dir(recording_id)
    names = store.TRACKS if key == MIXED else (key,)
    return [folder / name for name in names if store.has_audio(folder / name)]


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
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.failed = warning_icon()
        self.details = button("Details", window.show_details, recording_id, action)
        self.output = button(output_label, window.open_output, recording_id, action)
        self.use_page = button("Use that page", window.use_page, recording_id)
        self.rename = button("Rename", window.rename_page, recording_id)
        self.next_step = button("", window.next_step, recording_id, action)
        for control in (
            self.progress,
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
        self.progress.set_visible(view.progress is not None)
        self.progress.set_fraction(view.progress or 0.0)
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
    """What a recording expands to: its tracks, its actions and its speakers."""

    def __init__(self, view: RowView, expander: Adw.ExpanderRow, window: "HistoryWindow") -> None:
        recording_id = view.recording_id
        self.expander = expander
        self.speakers: dict[str, SpeakerControls] = {}
        self.tracks = {
            track.key: TrackControls(track.title, track.key, recording_id, window)
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
        self.publish_github = ActionControls(
            "Publish to GitHub", "Open in GitHub", PUBLISH_GITHUB, recording_id, window
        )
        for row in (
            *(controls.row for controls in self.tracks.values()),
            self.files,
            self.transcribe.row,
            self.summarize.row,
            self.publish.row,
            self.publish_gitlab.row,
            self.publish_github.row,
        ):
            expander.add_row(row)

    def list_speakers(self, view: RowView) -> None:
        """Puts the speakers a transcription found after it, and before Summarize."""
        speakers = [speaker.key for speaker in view.speakers]
        if speakers == list(self.speakers):
            return
        # An expander row only adds at its end: the rows below step out, and back in after them.
        below = (
            self.summarize.row,
            self.publish.row,
            self.publish_gitlab.row,
            self.publish_github.row,
        )
        for row in [*(controls.row for controls in self.speakers.values()), *below]:
            self.expander.remove(row)
        self.speakers = {key: SpeakerControls(view.recording_id, key) for key in speakers}
        for row in [*(controls.row for controls in self.speakers.values()), *below]:
            self.expander.add_row(row)

    def display(self, view: RowView) -> None:
        for track in view.tracks:
            controls = self.tracks[track.key]
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
        self.publish_github.display(view.publish_github)

    def stop_playing(self) -> None:
        for controls in self.tracks.values():
            controls.stop()


class RecordingRow(Adw.ExpanderRow):
    def __init__(self, view: RowView) -> None:
        super().__init__(use_markup=False)
        self.view = view
        self.warning = warning_icon()
        self.add_suffix(self.warning)
        target = GLib.Variant("s", view.recording_id)
        actions = Gio.Menu()
        actions.append_item(menu_item("Rename…", "win.rename-recording", target))
        actions.append_item(menu_item("Move to Trash…", "win.trash-recording", target))
        self.menu = Gtk.MenuButton(
            icon_name="view-more-symbolic",
            menu_model=actions,
            tooltip_text="Recording Menu",
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        self.add_suffix(self.menu)
        self.display(view)

    def display(self, view: RowView) -> None:
        self.view = view
        self.set_title(view.name)
        self.set_subtitle(view.subtitle)
        self.warning.set_visible(view.warning)


class HistoryWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application | None = None) -> None:
        super().__init__(
            application=application, title="Recordings", default_width=760, default_height=640
        )
        self.rows: dict[str, RecordingRow] = {}
        # Built when a row is first expanded: most never are.
        self.details: dict[str, Details] = {}
        # What a search looks in, by recording: made at the first search that needs it, and
        # kept until the row changes, so a keystroke reads no transcript.
        self.searched: dict[str, str] = {}
        # The recordings the search leaves in the list.
        self.matching: set[str] = set()
        # The commands this window is running, by recording.
        self.running: dict[str, dict[str, Gio.Subprocess]] = {
            PUBLISH: {},
            PUBLISH_GITLAB: {},
            PUBLISH_GITHUB: {},
            SUMMARIZE: {},
        }
        # How many runs a recording had when the window started its command for an action
        # (unrecorded_failure).
        self.runs_before: dict[tuple[str, str], int] = {}
        for name, parameter, activated in (
            ("summarize", "(ss)", self.summarize_with),
            ("custom-summary", "s", self.write_prompt),
            ("delete-preset", "s", self.delete_preset),
            ("rename-recording", "s", self.rename_recording),
            ("trash-recording", "s", self.ask_to_trash),
        ):
            action = Gio.SimpleAction(name=name, parameter_type=GLib.VariantType(parameter))
            action.connect("activate", activated)
            self.add_action(action)
        for name, activated in ((SETTINGS, self.open_settings), (ABOUT, self.show_about)):
            action = Gio.SimpleAction(name=name)
            action.connect("activate", activated)
            self.add_action(action)
        self.update_action = Gio.SimpleAction(name=UPDATE)
        self.update_action.connect("activate", self.update)
        self.add_action(self.update_action)
        self.list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            css_classes=["boxed-list"],
            valign=Gtk.Align.START,
            margin_top=24,
            margin_bottom=24,
            margin_start=12,
            margin_end=12,
        )
        self.list.set_filter_func(self.in_search)
        self.viewport = Gtk.Viewport(child=Adw.Clamp(child=self.list))
        self.scrolled = Gtk.ScrolledWindow(
            child=self.viewport, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        # The whole window, not a card in the empty list.
        self.empty = Adw.StatusPage(
            icon_name="audio-input-microphone-symbolic",
            title="No recordings yet",
            description="Record a meeting from the top bar.",
        )
        self.no_match = Adw.StatusPage(
            icon_name="system-search-symbolic",
            title="No recordings match",
            description="Try other words.",
        )
        self.pages = Gtk.Stack()
        self.pages.add_child(self.empty)
        self.pages.add_child(self.scrolled)
        self.pages.add_child(self.no_match)
        self.search = Gtk.SearchEntry(placeholder_text="Search recordings", hexpand=True)
        self.search_bar = self.search_bar_for(self.search)
        self.toasts = Adw.ToastOverlay(child=self.pages)
        menu = Gio.Menu()
        menu.append("Settings", f"win.{SETTINGS}")
        menu.append("Update Minutas", f"win.{UPDATE}")
        menu.append("About Minutas", f"win.{ABOUT}")
        header = Adw.HeaderBar()
        header.pack_start(
            Gtk.ToggleButton(
                icon_name="system-search-symbolic", tooltip_text="Search", action_name="win.search"
            )
        )
        header.pack_end(
            Gtk.MenuButton(
                icon_name="open-menu-symbolic",
                menu_model=menu,
                primary=True,
                tooltip_text="Main Menu",
            )
        )
        page = Adw.ToolbarView(content=self.toasts)
        page.add_top_bar(header)
        page.add_top_bar(self.search_bar)
        self.updating = Adw.Banner(title=UPDATING)
        page.add_top_bar(self.updating)
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

    def search_bar_for(self, search: Gtk.SearchEntry) -> Gtk.SearchBar:
        """The bar that holds the search, opened by win.search and Ctrl+F."""
        search.connect("search-changed", self.search_changed)
        bar = Gtk.SearchBar(child=Adw.Clamp(child=search, maximum_size=400))
        bar.connect_entry(search)
        self.add_action(Gio.PropertyAction.new("search", bar, "search-mode-enabled"))
        shortcuts = Gtk.ShortcutController()
        shortcuts.add_shortcut(
            Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                action=Gtk.NamedAction.new("win.search"),
            )
        )
        self.add_controller(shortcuts)
        return bar

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
            gone = self.details.pop(recording_id, None)
            if gone is not None:
                gone.stop_playing()
            self.searched.pop(recording_id, None)
        for position, view in enumerate(views):
            row = self.rows.get(view.recording_id)
            if row is None:
                row = self.rows[view.recording_id] = RecordingRow(view)
                row.connect("notify::expanded", self.expanded)
                self.list.insert(row, position)
            elif row.view != view:
                row.display(view)
                self.searched.pop(view.recording_id, None)
                if view.recording_id in self.details:
                    self.details[view.recording_id].display(view)
        self.show_matching()

    def search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self.show_matching()

    def show_matching(self) -> None:
        """Lists the recordings with every word the search holds, or says none has them."""
        typed = self.search.get_text()
        self.matching = {
            recording_id
            for recording_id, row in self.rows.items()
            # Nothing typed reads no transcript.
            if not typed.split() or window_view.matches(typed, self.search_text(row.view))
        }
        self.list.invalidate_filter()
        if not self.rows:
            self.pages.set_visible_child(self.empty)
        else:
            self.pages.set_visible_child(self.scrolled if self.matching else self.no_match)

    def search_text(self, view: RowView) -> str:
        if view.recording_id not in self.searched:
            transcript = read_transcript(view.recording_id)
            self.searched[view.recording_id] = window_view.searched(view, transcript)
        return self.searched[view.recording_id]

    def in_search(self, row: Gtk.ListBoxRow) -> bool:
        return cast("RecordingRow", row).view.recording_id in self.matching

    def reveal(self, recording_id: str) -> None:
        row = self.rows.get(recording_id)
        if row is not None:
            if recording_id not in self.matching:
                # Asked for from the top bar while a search hides it; closing the search clears it.
                self.search_bar.set_search_mode(False)
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
            PUBLISH_GITHUB: view.publish_github,
        }
        return actions[action]

    def say(self, problem: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=problem, use_markup=False))

    def report(self, error: GLib.Error) -> None:
        """Says what went wrong, unless it was the person closing a dialog."""
        if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
            self.say(error.message)

    def ask_name(
        self,
        heading: str,
        name: str,
        named: Callable[..., None],
        *arguments: object,
        body: str = "",
    ) -> None:
        """Asks for a name, starting from the one there is, and hands it on after the arguments."""
        entry = Gtk.Entry(text=name, activates_default=True)
        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
            extra_child=entry,
            default_response="save",
            close_response="cancel",
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

    def rename_recording(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        recording_id = target.get_string()
        view = self.rows[recording_id].view
        said = f"{view.name_source}. {RENAME_NOTE}"
        self.ask_name("Rename", view.name, self.name_recording, recording_id, body=said)

    def ask_to_trash(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        recording_id = target.get_string()
        refused = self.rows[recording_id].view.trash_disabled
        if refused:
            self.say(refused)
            return
        dialog = Adw.AlertDialog(
            heading="Move the recording to the Trash?", body=TRASH_WARNING, close_response="cancel"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Move to Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.choose(self, None, self.trash_chosen, recording_id)

    def trash_chosen(
        self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, recording_id: str
    ) -> None:
        if dialog.choose_finish(result) != "trash":
            return
        running = [action for action, ids in self.running.items() if recording_id in ids]
        try:
            # The worker takes its runs inside the lock: it cannot start one for the folder going.
            with store.lock():
                # Asked again: a transcription may have been queued while the dialog was open.
                refused = window_view.trash_refusal(
                    store.load_recording(recording_id), store.load_runs(recording_id), running
                )
                if not refused:
                    if recording_id in self.details:
                        self.details[recording_id].stop_playing()
                    # Never deleted instead, whatever the trash says: a recording is never lost.
                    Gio.File.new_for_path(str(store.recording_dir(recording_id))).trash(None)
        except GLib.Error as error:
            self.say(f"Can't move it to the Trash: {error.message}")
            return
        if refused:
            self.say(refused)
            return
        store.write_status()
        self.refresh()

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

    def play(self, _button: Gtk.Button, recording_id: str, key: str) -> None:
        # One at a time: two would talk over each other.
        self.stop_playing()
        # Made on Play and dropped when it stops: no decoder waits behind a row nobody plays.
        playing = player.Player(
            audio_of(recording_id, key), partial(self.play_ended, recording_id, key)
        )
        self.details[recording_id].tracks[key].start(playing)

    def stop_playing(self) -> None:
        for details in self.details.values():
            details.stop_playing()

    def play_ended(self, recording_id: str, key: str, problem: str | None) -> None:
        self.details[recording_id].tracks[key].stop()
        if problem is not None:
            self.say(f"Can't play it: {problem}")

    def toggle_pause(self, _button: Gtk.Button, recording_id: str, key: str) -> None:
        self.details[recording_id].tracks[key].toggle_pause()

    def seek(
        self,
        _scale: Gtk.Scale,
        _scroll: Gtk.ScrollType,
        seconds: float,
        recording_id: str,
        key: str,
    ) -> bool:
        self.details[recording_id].tracks[key].now_playing().seek(seconds)
        # The slider moves to where it was dragged.
        return False

    def speed_chosen(
        self, speed: Gtk.DropDown, _property: object, recording_id: str, key: str
    ) -> None:
        playing = self.details[recording_id].tracks[key].now_playing()
        playing.set_speed(window_view.SPEEDS[speed.get_selected()])

    @override
    def do_close_request(self) -> bool:
        self.stop_playing()
        # Then it closes.
        return False

    def save_copy(self, _button: Gtk.Button, recording_id: str, key: str) -> None:
        # The mix has the name Publish to Agaric gives it.
        name = f"{recording_id}.opus" if key == MIXED else f"{recording_id}-{key}"
        dialog = Gtk.FileDialog(initial_name=name)
        dialog.save(self, None, self.copy_chosen, recording_id, key)

    def copy_chosen(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, recording_id: str, key: str
    ) -> None:
        try:
            chosen = dialog.save_finish(result)
            if key == MIXED:
                self.save_mix(recording_id, chosen)
            else:
                # The dialog has already asked before choosing a file that exists.
                track = Gio.File.new_for_path(str(store.recording_dir(recording_id) / key))
                track.copy(chosen, Gio.FileCopyFlags.OVERWRITE, None, None)
        except GLib.Error as error:
            self.report(error)

    def save_mix(self, recording_id: str, chosen: Gio.File) -> None:
        """Mixes the tracks into a file of its own, then copies it where the person chose, as a
        track is copied: a mix that fails midway leaves nothing there."""
        mixed = Path(tempfile.mkdtemp(prefix="minutas-mix-")) / f"{recording_id}.opus"
        # An hour takes a while to mix: it says so until it is done.
        mixing = Adw.Toast(title=MIXING, timeout=0)
        self.toasts.add_toast(mixing)
        saved = partial(self.mix_saved, mixed, chosen, mixing)
        player.save_mix(audio_of(recording_id, MIXED), mixed, saved)

    def mix_saved(
        self, mixed: Path, chosen: Gio.File, mixing: Adw.Toast, problem: str | None
    ) -> None:
        mixing.dismiss()
        try:
            if problem is None:
                Gio.File.new_for_path(str(mixed)).copy(
                    chosen, Gio.FileCopyFlags.OVERWRITE, None, None
                )
            else:
                self.say(f"Can't save the mix: {problem}")
        except GLib.Error as error:
            self.report(error)
        finally:
            shutil.rmtree(mixed.parent)

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

    def open_settings(self, *_activated: object) -> None:
        if isinstance(self.get_visible_dialog(), SettingsDialog):
            return
        dialog = SettingsDialog(self)
        self.show_kept_audio(dialog)
        dialog.present(self)
        self.offer_models(dialog)
        self.ask_agaric(["--list-spaces"], None, self.spaces_listed, dialog)
        self.ask_glab(None, dialog)

    def offer_models(self, dialog: SettingsDialog) -> None:
        """Offers the chosen summarizer's models: Claude Code's own, or those Devin lists."""
        if summarizer.chosen().command == summarizer.CLAUDE:
            dialog.offer_claude()
            return
        dialog.models_unknown(ASKING_DEVIN)
        try:
            # Devin is asked, and Agaric, through commands: the window never waits on them.
            command = Gio.Subprocess.new(
                summarizer.LIST_MODELS,
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error:
            dialog.models_unknown(summarizer.DEVIN_MISSING)
        else:
            command.communicate_utf8_async(None, None, self.models_listed, dialog)

    def summarizer_chosen(self, row: Adw.ComboRow, _property: object) -> None:
        settings.set_summarizer(summarizer.TOOLS[row.get_selected()].command)
        self.offer_models(settings_of(row))
        # Summarize is off while the one chosen is missing.
        self.refresh()

    def models_listed(
        self, command: Gio.Subprocess, result: Gio.AsyncResult, dialog: SettingsDialog
    ) -> None:
        _, listed, said = command.communicate_utf8_finish(result)
        # Devin answers after Claude Code was chosen: its models are no longer the ones to offer.
        if summarizer.chosen().command != summarizer.DEVIN:
            return
        if not command.get_successful():
            status = command.get_exit_status()
            dialog.models_unknown(last_line(said or "") or f"devin exited with status {status}")
            return
        saved = settings.summary_model()
        try:
            dialog.offer_models(summarizer.models(listed or "", saved), saved)
        except summarizer.SummarizerError as error:
            dialog.models_unknown(str(error))

    def model_chosen(self, row: Adw.ComboRow, _property: object) -> None:
        if summarizer.chosen().command == summarizer.CLAUDE:
            settings.set_claude_model(summarizer.CLAUDE_MODELS[row.get_selected()][1])
            return
        dialog = settings_of(row)
        # The model's own first effort, as Devin lists it.
        dialog.offer_efforts(0)
        settings.set_summary_model(dialog.chosen_model())

    def effort_chosen(self, row: Adw.ComboRow, _property: object) -> None:
        if summarizer.chosen().command == summarizer.CLAUDE:
            settings.set_claude_effort(summarizer.CLAUDE_EFFORTS[row.get_selected()][1])
            return
        settings.set_summary_model(settings_of(row).chosen_model())

    def choose_skill(self, chooser: Gtk.Button) -> None:
        markdown = Gtk.FileFilter(name="Markdown", patterns=["*.md"], mime_types=["text/markdown"])
        dialog = Gtk.FileDialog(title="Summary skill", default_filter=markdown)
        dialog.open(self, None, self.skill_chosen, settings_of(chooser))

    def skill_chosen(
        self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, shown: SettingsDialog
    ) -> None:
        try:
            path = dialog.open_finish(result).get_path()
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                shown.add_toast(Adw.Toast(title=error.message, use_markup=False))
            return
        # Only a file on this computer has a path to keep.
        if path is not None:
            settings.set_summary_skill(path)
            shown.show_skill()

    def open_skill(self, _button: Gtk.Button) -> None:
        self.open(Gio.File.new_for_path(settings.summary_skill() or str(summarizer.BUILT_IN_SKILL)))

    def open_vocabulary(self, _button: Gtk.Button, name: str) -> None:
        # The text editor opens a file that exists: the first time, an empty one.
        file = worker.vocabulary_file(name)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
        self.open(Gio.File.new_for_path(str(file)))

    def new_vocabulary(self, new: Gtk.Button) -> None:
        self.ask_name(
            "New vocabulary", "", self.create_vocabulary, settings_of(new), body=NEW_VOCABULARY_NOTE
        )

    def create_vocabulary(self, dialog: SettingsDialog, name: str) -> None:
        """Makes the vocabulary's file, empty and used, and opens it in the text editor."""
        if self.created_preset(dialog, "vocabulary", name, worker.vocabulary_file(name)):
            used = [used for used in settings.vocabularies() if used != name]
            settings.set_vocabularies([*used, name])
            dialog.show_vocabularies(self)

    def vocabulary_switched(self, switch: Gtk.Switch, _property: object, name: str) -> None:
        used = [used for used in settings.vocabularies() if used != name]
        settings.set_vocabularies([*used, name] if switch.get_active() else used)

    def ask_to_delete_vocabulary(self, delete: Gtk.Button, name: str) -> None:
        dialog = settings_of(delete)
        trashed = partial(self.vocabulary_trashed, dialog, name)
        file = worker.vocabulary_file(name)
        self.ask_to_trash_preset(dialog, "vocabulary", VOCABULARY_TRASH_WARNING, file, trashed)

    def vocabulary_trashed(self, dialog: SettingsDialog, name: str) -> None:
        settings.set_vocabularies([used for used in settings.vocabularies() if used != name])
        dialog.show_vocabularies(self)

    def new_context(self, new: Gtk.Button) -> None:
        self.ask_name(
            "New context", "", self.create_context, settings_of(new), body=NEW_CONTEXT_NOTE
        )

    def create_context(self, dialog: SettingsDialog, name: str) -> None:
        """Makes the context preset's file, empty, and opens it in the text editor."""
        if self.created_preset(dialog, "context", name, summarizer.context_file(name)):
            dialog.show_context(self)

    def open_context(self, _button: Gtk.Button, name: str) -> None:
        self.open(Gio.File.new_for_path(str(summarizer.context_file(name))))

    def ask_to_delete_context(self, delete: Gtk.Button, name: str) -> None:
        dialog = settings_of(delete)
        trashed = partial(self.context_trashed, dialog, name)
        file = summarizer.context_file(name)
        self.ask_to_trash_preset(dialog, "context", CONTEXT_TRASH_WARNING, file, trashed)

    def context_trashed(self, dialog: SettingsDialog, name: str) -> None:
        settings.set_summary_context([kept for kept in settings.summary_context() if kept != name])
        dialog.show_context(self)

    def created_preset(self, dialog: SettingsDialog, kind: str, name: str, file: Path) -> bool:
        """Makes a context preset's or a vocabulary's file, empty, and opens it in the text
        editor; else says why not."""
        if settings.bad_preset_name(name):
            said = NAME_BAD.format(kind=kind)
        else:
            try:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.touch(exist_ok=False)
            except FileExistsError:
                said = NAME_TAKEN.format(kind=kind, name=name)
            except OSError as error:
                said = f"Can't create it: {error.strerror}"
            else:
                self.open(Gio.File.new_for_path(str(file)))
                return True
        dialog.add_toast(Adw.Toast(title=said, use_markup=False))
        return False

    def ask_to_trash_preset(
        self,
        dialog: SettingsDialog,
        kind: str,
        warning: str,
        file: Path,
        trashed: Callable[[], None],
    ) -> None:
        """Asks before moving a context preset's or a vocabulary's file to the Trash, and once it
        is there, has the rest done."""
        question = Adw.AlertDialog(
            heading=f"Move the {kind} to the Trash?", body=warning, close_response="cancel"
        )
        question.add_response("cancel", "Cancel")
        question.add_response("trash", "Move to Trash")
        question.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        question.choose(dialog, None, self.preset_trash_chosen, dialog, file, trashed)

    def preset_trash_chosen(
        self,
        question: Adw.AlertDialog,
        result: Gio.AsyncResult,
        dialog: SettingsDialog,
        file: Path,
        trashed: Callable[[], None],
    ) -> None:
        if question.choose_finish(result) != "trash":
            return
        try:
            # Never deleted instead, whatever the trash says: the person's notes are theirs.
            Gio.File.new_for_path(str(file)).trash(None)
        except GLib.Error as error:
            said = f"Can't move it to the Trash: {error.message}"
            dialog.add_toast(Adw.Toast(title=said, use_markup=False))
            return
        trashed()

    def use_built_in_skill(self, built_in: Gtk.Button) -> None:
        settings.set_summary_skill(None)
        settings_of(built_in).show_skill()

    def show_kept_audio(self, dialog: SettingsDialog) -> None:
        _, size_limit_gb = settings.audio_limits()
        kept = GLib.format_size(sum(store.audio_size(recording_id) for recording_id in self.rows))
        against = "" if size_limit_gb is None else f" of {size_limit_gb} GB"
        dialog.audio.set_description(f"Kept audio: {kept}{against}.\n\n{AUDIO_LIMITS_NOTE}")

    def limits_changed(self, row: Adw.PreferencesRow, _property: object) -> None:
        settings_of(row).show_unsaved_limits()

    def settings_closing(self, dialog: SettingsDialog) -> None:
        """Asks whether to save the audio limits shown, or discard them, before closing."""
        question = Adw.AlertDialog(
            heading="Unsaved audio limits",
            body=UNSAVED_LIMITS,
            default_response="save",
            close_response="cancel",
        )
        question.add_response("cancel", "Cancel")
        question.add_response("discard", "Discard")
        question.add_response("save", "Save")
        question.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        question.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        question.choose(dialog, None, self.closing_chosen, dialog)

    def closing_chosen(
        self, question: Adw.AlertDialog, result: Gio.AsyncResult, dialog: SettingsDialog
    ) -> None:
        chosen = question.choose_finish(result)
        if chosen == "discard":
            dialog.force_close()
        elif chosen == "save":
            self.warn_limits(dialog, closing=True)

    def save_audio_limits(self, save: Gtk.Button) -> None:
        self.warn_limits(settings_of(save), closing=False)

    def warn_limits(self, dialog: SettingsDialog, *, closing: bool) -> None:
        """Says what the limits shown would delete at once, and saves them if accepted; closing,
        the Settings close once they are saved."""
        over = store.recordings_over_limits(now(), *dialog.chosen_limits())
        size = GLib.format_size(sum(store.audio_size(recording_id) for recording_id in over))
        warning = Adw.AlertDialog(
            heading="Save the audio limits?",
            body=window_view.limits_warning(len(over), size),
            close_response="cancel",
        )
        warning.add_response("cancel", "Cancel")
        warning.add_response("accept", "Accept")
        appearance = Adw.ResponseAppearance
        warning.set_response_appearance(
            "accept", appearance.DESTRUCTIVE if over else appearance.SUGGESTED
        )
        warning.choose(dialog, None, self.limits_accepted, dialog, closing)

    def limits_accepted(
        self,
        warning: Adw.AlertDialog,
        result: Gio.AsyncResult,
        dialog: SettingsDialog,
        closing: bool,  # noqa: FBT001 - GTK hands a callback its arguments by position
    ) -> None:
        if warning.choose_finish(result) != "accept":
            return
        settings.set_audio_limits(*dialog.chosen_limits())
        self.delete_audio_over_limits()
        self.show_kept_audio(dialog)
        dialog.show_unsaved_limits()
        if closing:
            dialog.close()
            self.say(AUDIO_LIMITS_SAVED)
        else:
            dialog.add_toast(Adw.Toast(title=AUDIO_LIMITS_SAVED))

    def echo_cancel_switched(self, row: Adw.SwitchRow, _property: object) -> None:
        settings.set_echo_cancel(on=row.get_active())

    def recent_chosen(self, row: Adw.SpinRow, _property: object) -> None:
        settings.set_recent_in_menu(int(row.get_value()))
        # The menu follows now, not at the next change to a recording.
        store.write_status()

    def ask_agaric(
        self,
        options: list[str],
        pasted: str | None,
        answered: Callable[..., None],
        *arguments: object,
    ) -> None:
        """Runs `minutas settings agaric` with pasted on its stdin, and hands it to answered
        once it exits: the window never waits on Agaric."""
        command = Gio.Subprocess.new(
            [*MINUTAS, "settings", "agaric", *options],
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_PIPE,
        )
        command.communicate_utf8_async(pasted, None, answered, *arguments)

    def spaces_listed(
        self, command: Gio.Subprocess, result: Gio.AsyncResult, dialog: SettingsDialog
    ) -> None:
        """Says whether a token is stored, where it publishes or why it can't, or how to get
        one; and offers its spaces."""
        _, listed, said = command.communicate_utf8_finish(result)
        # Read once it exited: it may have chosen the space, or stored the token.
        view = window_view.agaric(listed or "", said or "", settings.agaric_space_id())
        shown = GET_AGARIC_CONFIG if self.token_missing(dialog) else view.said
        # A group's description is always markup; a space's name is not.
        dialog.agaric.set_description(GLib.markup_escape_text(shown))
        dialog.space.offer(view)

    def token_missing(self, dialog: SettingsDialog) -> bool:
        """Says whether a token is stored; True only when the keyring says there is none."""
        try:
            missing = settings.agaric_token() is None
        except KeyringError as error:
            dialog.token.set_subtitle(f"Can't read the keyring: {error}")
            return False
        dialog.token.set_subtitle(AGARIC_NOT_SET if missing else AGARIC_STORED)
        return missing

    def space_chosen(self, space: SpaceRow, _property: object) -> None:
        chosen = space.chosen()
        if chosen is not None:
            settings.set_agaric_space_id(chosen)

    def agaric_pasted(self, pasted: Adw.PasswordEntryRow) -> None:
        text = pasted.get_text()
        # Gone once taken: a token is never shown back.
        pasted.set_text("")
        # The command takes the token out of what was pasted, or refuses it.
        if text.strip():
            self.ask_agaric(["--token-from-stdin"], text, self.spaces_listed, settings_of(pasted))

    def ask_glab(self, token: str | None, dialog: SettingsDialog) -> None:
        """Runs `minutas settings gitlab`, handing it the token pasted on its stdin, and shows
        what it says once it exits: the window never waits on glab."""
        command = Gio.Subprocess.new(
            [*MINUTAS, "settings", "gitlab", *([] if token is None else ["--token-from-stdin"])],
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_SILENCE
            | Gio.SubprocessFlags.STDERR_PIPE,
        )
        command.communicate_utf8_async(token, None, self.glab_answered, dialog)

    def glab_answered(
        self, command: Gio.Subprocess, result: Gio.AsyncResult, dialog: SettingsDialog
    ) -> None:
        _, _, said = command.communicate_utf8_finish(result)
        # A group's description is always markup; what glab says is not.
        shown = GLib.markup_escape_text(last_line(said or "") or GLAB_SILENT)
        dialog.gitlab.set_description(f"{shown}\n\n{GITLAB_NOTE}")

    def gitlab_link_applied(self, row: Adw.EntryRow) -> None:
        """Saves the default link, once GitLab can post to it; an empty one clears it."""
        dialog = settings_of(row)
        typed = row.get_text().strip()
        target = gitlab.parse_link(typed)
        if typed and target is None:
            dialog.add_toast(Adw.Toast(title=NOT_A_LINK))
            return
        link = None if target is None else target.link
        settings.set_gitlab_link(link)
        row.set_text(link or "")
        # The login shown is the server's of the link.
        self.ask_glab(None, dialog)

    def gitlab_token_pasted(self, pasted: Adw.PasswordEntryRow) -> None:
        text = pasted.get_text()
        # Gone once taken: a token is never shown back.
        pasted.set_text("")
        if text.strip():
            self.ask_glab(text, settings_of(pasted))

    def next_step(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        shown = self.shown(recording_id, action)
        if action == PUBLISH:
            self.publish(recording_id)
        elif action == PUBLISH_GITLAB:
            self.ask_link(recording_id, action, shown.link or settings.gitlab_link() or "")
        elif action == PUBLISH_GITHUB:
            self.ask_link(recording_id, action, shown.link or settings.github_link() or "")
        elif action == SUMMARIZE:
            self.summarize(recording_id, str(shown.prompt), shown.context)
        elif shown.cancels is not None:
            worker.cancel(recording_id, shown.cancels)
        else:
            # A worker that does not start leaves a failed run, which says why.
            with contextlib.suppress(worker.WorkerError):
                worker.queue(recording_id, now())

    def show_details(self, _button: Gtk.Button, recording_id: str, action: str) -> None:
        self.show_log(
            store.recording_dir(recording_id) / str(self.shown(recording_id, action).details)
        )

    def show_log(self, log: Path) -> None:
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

    def ask_link(self, recording_id: str, action: str, link: str) -> None:
        """Asks where to publish to GitLab or GitHub, saying what the link typed will get."""
        entry = Gtk.Entry(text=link, activates_default=True)
        dialog = Adw.AlertDialog(
            heading=window_view.COMMAND_TITLES[action],
            extra_child=entry,
            default_response="publish",
            close_response="cancel",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("publish", "Publish")
        dialog.set_response_appearance("publish", Adw.ResponseAppearance.SUGGESTED)
        asks = LINK_ASKS[action]
        entry.connect("changed", self.link_typed, asks)
        dialog.choose(self, None, self.link_chosen, entry, recording_id, action)
        # Once presented: until then the entry is not inside the dialog.
        self.link_typed(entry, asks)

    def link_typed(self, entry: Gtk.Entry, asks: "LinkAsk") -> None:
        # The dialog is found, not held: a handler that held it would keep it for good.
        dialog = cast("Adw.AlertDialog", entry.get_ancestor(Adw.AlertDialog))
        typed = entry.get_text().strip()
        target = asks.parse(typed)
        said = target.plan if target is not None else (asks.not_a_link if typed else asks.ask)
        dialog.set_body(f"{said}\n\n{asks.note}" if asks.note else said)
        dialog.set_response_enabled("publish", target is not None)

    def link_chosen(
        self,
        dialog: Adw.AlertDialog,
        result: Gio.AsyncResult,
        entry: Gtk.Entry,
        recording_id: str,
        action: str,
    ) -> None:
        asks = LINK_ASKS[action]
        target = asks.parse(entry.get_text())
        if dialog.choose_finish(result) == "publish" and target is not None:
            self.start(action, recording_id, ["publish", asks.command, recording_id, target.link])

    def github_link_set(self, entry: Adw.EntryRow) -> None:
        """Saves the default GitHub link typed, or clears it when empty; refuses another."""
        dialog = settings_of(entry)
        typed = entry.get_text().strip()
        target = github.parse_link(typed)
        if typed and target is None:
            dialog.github.set_description(NOT_A_GITHUB_LINK)
            return
        settings.set_github_link(None if target is None else target.link)
        dialog.github.set_description(GITHUB_LINK_NOTE)

    def list_presets(self, presets: Gtk.MenuButton, recording_id: str) -> None:
        """The built-in and saved presets, Custom, the saved ones to delete, and the context
        presets to tick."""
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
        if names := summarizer.context_names():
            menu.append_section("Context", self.context_menu(names))
        presets.set_menu_model(menu)

    def context_menu(self, names: list[str]) -> Gio.Menu:
        """A check item for each context preset named, ticked while every summary includes it."""
        ticked = settings.summary_context()
        menu = Gio.Menu()
        for place, name in enumerate(names):
            # A check item's action holds whether it is ticked, and takes no target.
            action = Gio.SimpleAction.new_stateful(
                f"context-{place}", None, GLib.Variant("b", name in ticked)
            )
            action.connect("change-state", self.context_ticked, name)
            self.add_action(action)
            menu.append_item(menu_item(name, f"win.context-{place}", None))
        return menu

    def context_ticked(self, action: Gio.SimpleAction, state: GLib.Variant, name: str) -> None:
        action.set_state(state)
        # One deleted since it was ticked is dropped on the way.
        kept = [ticked for ticked in summarizer.chosen_context() if ticked != name]
        settings.set_summary_context([*kept, name] if state.get_boolean() else kept)

    def summarize_with(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        recording_id, prompt = target.unpack()
        self.summarize(recording_id, prompt, summarizer.chosen_context())

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
        self.summarize(recording_id, text, summarizer.chosen_context())

    def delete_preset(self, _action: Gio.SimpleAction, target: GLib.Variant) -> None:
        settings.delete_preset(target.get_string())

    def summarize(self, recording_id: str, prompt: str, context: Sequence[str]) -> None:
        # With "=": a prompt or name that starts with a dash is still itself, never an option.
        named = [f"--context={name}" for name in context]
        self.start(
            SUMMARIZE, recording_id, ["summarize", recording_id, f"--prompt={prompt}", *named]
        )

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

    def show_about(self, *_activated: object) -> None:
        links = dict(link.split(", ", 1) for link in PROJECT.get_all("Project-URL", []))
        Adw.AboutDialog(
            application_name="Minutas",
            application_icon="audio-input-microphone",
            version=PROJECT["Version"],
            comments=PROJECT["Summary"],
            license_type=Gtk.License.GPL_3_0,
            website=links["Repository"],
            issue_url=links["Issues"],
        ).present(self)

    def update(self, *_activated: object) -> None:
        """Runs `minutas update`, saying so until it ends, then what came of it.

        It prints to a file, not to this window: closing the window, which an update ends by
        asking for, must not cut it off midway.
        """
        log = store.data_dir() / "update.log"
        # The launcher writes over the file without truncating it: the last update's tail stays.
        log.unlink(missing_ok=True)
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDERR_MERGE)
        launcher.set_stdout_file_path(str(log))
        command = launcher.spawnv([*MINUTAS, "update"])
        self.update_action.set_enabled(False)
        self.updating.set_revealed(True)
        command.wait_async(None, self.updated, log)

    def updated(self, command: Gio.Subprocess, result: Gio.AsyncResult, log: Path) -> None:
        command.wait_finish(result)
        self.update_action.set_enabled(True)
        self.updating.set_revealed(False)
        exited_ok = command.get_successful()
        outcome = window_view.update(log.read_text(errors="replace"), exited_ok=exited_ok)
        dialog = Adw.AlertDialog(heading=outcome.heading, body=outcome.body, close_response="close")
        dialog.add_response("close", "Close")
        # What a failed update printed is worth reading.
        if not exited_ok:
            dialog.add_response("details", "Details")
        dialog.choose(self, None, self.update_answered, log)

    def update_answered(self, dialog: Adw.AlertDialog, result: Gio.AsyncResult, log: Path) -> None:
        if dialog.choose_finish(result) == "details":
            self.show_log(log)


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
        for argument in command_line.get_arguments()[1:]:
            if argument == SETTINGS:
                self.window.open_settings()
            else:
                self.window.reveal(argument)
        return 0


def run(recording_id: str | None, *, open_settings: bool = False) -> int:
    """Opens the window, or the one already open, on that recording if one is given, and the
    Settings over it if asked."""
    arguments = [] if recording_id is None else [recording_id]
    return History().run(["minutas", *arguments, *([SETTINGS] if open_settings else [])])
