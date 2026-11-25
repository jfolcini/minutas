"""Renders the README's screenshots of the Recordings window from made-up recordings:
`just screenshots`, which runs it on an Xvfb display and a session bus of its own.

The recordings, the settings and HOME are a temporary folder's, and the keyring is the null one:
nothing of the real data, settings or keyring is read or written. GTK draws each shot itself,
at twice the size, into a PNG.
"""

# ruff: noqa: INP001 - a script run by path, not a module of a package

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

SCRATCH = Path(tempfile.mkdtemp(prefix="minutas-screenshots-"))
os.environ.update(
    HOME=str(SCRATCH),
    XDG_DATA_HOME=str(SCRATCH / "data"),
    XDG_CONFIG_HOME=str(SCRATCH / "config"),
    PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
    PATH=f"{SCRATCH}{os.pathsep}{os.environ['PATH']}",
    GDK_BACKEND="x11",
    GDK_SCALE="2",
    GSK_RENDERER="cairo",
    GSETTINGS_BACKEND="memory",
    GTK_A11Y="none",
)
# Ubuntu's PyGObject, found through a folder that holds only a link to it, as the tests find it.
(SCRATCH / "gi").symlink_to("/usr/lib/python3/dist-packages/gi")
sys.path.append(str(SCRATCH))

import gi  # noqa: E402 - after the environment GTK reads when it loads

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, GLib, Graphene, Gsk, Gtk  # noqa: E402

from minutas import settings, store, window, worker  # noqa: E402

ART = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=ART)
# 4000 bytes a second a track (32 kbps), as the recorder writes them, in files that take no disk.
BYTES_PER_SECOND = 4000
# What `minutas settings agaric` and `minutas settings gitlab` say, without Agaric or glab.
FAKE_SETTINGS = (
    "import sys; sys.stdin.read(); "
    "sys.stderr.write({'agaric': '', 'gitlab': 'glab is logged in to gitlab.com.'}[sys.argv[2]])"
)


def run(action: str, status: str, **changes: object) -> dict[str, object]:
    ended = {"action": action, "status": status, "queued_at": NOW.isoformat()}
    return {"error": None, "details": None, "output": None, **ended, **changes}


def recording(  # noqa: PLR0913 - each is a separate part of a made-up recording
    started: datetime,
    *,
    minutes: int,
    name: str,
    attendees: list[str],
    said: list[tuple[str, str]],
    runs: list[dict[str, object]],
    summary: str = "",
) -> None:
    """A stopped recording, with its tracks, its transcript under the attendees' names, its
    summary and its runs."""
    recording_id = store.create_recording(started)["id"]
    store.mark_stopped(recording_id, started + timedelta(minutes=minutes), incomplete=False)
    folder = store.recording_dir(recording_id)
    for track in store.TRACKS:
        (folder / track).touch()
        os.truncate(folder / track, minutes * 60 * BYTES_PER_SECOND)
    store.set_calendar_event(recording_id, name, attendees)
    if said:
        segments = [
            {"start": 40.0 * turn, "end": 40.0 * turn + 30, "speaker": speaker, "text": text}
            for turn, (speaker, text) in enumerate(said)
        ]
        (folder / store.SEGMENTS).write_text(json.dumps(segments))
        store.write_transcript(recording_id)
        heard = [speaker for speaker, _ in said if speaker != store.ME]
        for speaker, attendee in zip(dict.fromkeys(heard), attendees, strict=False):
            store.set_speaker_name(recording_id, speaker, attendee)
    if summary:
        (folder / store.SUMMARY).write_text(summary)
    for index, ran in enumerate(runs):
        if ran["details"] is not None:
            store.run_log(recording_id, index).write_text(f"{ran['error']}\n")
    (folder / "runs.json").write_text(json.dumps(runs))


def seed() -> None:
    transcribed = run("transcribe", "succeeded", output=store.TRANSCRIPT)
    summarized = run("summarize", "succeeded", output=store.SUMMARY)
    recording(
        datetime(2026, 11, 20, 16, 0, tzinfo=ART),
        minutes=31,
        name="1:1 Lucía",
        attendees=["Lucía Fernández"],
        said=[
            ("Speaker 1", "¿Cómo venís con la migración?"),
            ("Me", "Bien, the staging cluster is done, me falta el runbook."),
        ],
        runs=[
            transcribed,
            summarized,
            run("publish_agaric", "succeeded", output="agaric://page/01JD7Q2M4K"),
            run(
                "publish_github",
                "succeeded",
                output="https://github.com/acme/platform/issues/87#issuecomment-2471",
                link="https://github.com/acme/platform/issues/87",
            ),
        ],
        summary="## 1:1 Lucía\n",
    )
    recording(
        datetime(2026, 11, 24, 15, 0, tzinfo=ART),
        minutes=44,
        name="Acme rollout call",
        attendees=["Sofía Acosta", "Diego Paredes"],
        said=[
            ("Speaker 1", "We need the rollout plan before the release freeze."),
            ("Me", "Dale, I'll send it tomorrow with the dates."),
            ("Speaker 2", "Y el rollback, ¿quién lo prueba?"),
        ],
        runs=[
            transcribed,
            summarized,
            run(
                "publish_agaric",
                "failed",
                error="Agaric isn't running: start it and turn on Settings → Agent access",
                details="run-2.log",
            ),
        ],
        summary="## Acme rollout call\n",
    )
    recording(
        datetime(2026, 11, 25, 10, 0, tzinfo=ART),
        minutes=52,
        name="Weekly sync",
        attendees=["Ana Gómez", "Martín Ruiz", "Lucía Fernández"],
        said=[
            ("Speaker 1", "Arrancamos con el release."),
            ("Me", "CI is green since this morning."),
            ("Speaker 2", "Perfecto, then we deploy on Thursday."),
        ],
        runs=[
            transcribed,
            summarized,
            run("publish_agaric", "succeeded", output="agaric://page/01JDA3X9TN"),
            run(
                "publish_gitlab",
                "succeeded",
                output="https://gitlab.com/acme/platform/-/issues/42#note_1907",
                link="https://gitlab.com/acme/platform/-/issues/42",
            ),
        ],
        summary="## Weekly sync\n",
    )
    recording(
        datetime(2026, 11, 25, 11, 30, tzinfo=ART),
        minutes=15,
        name="Platform standup",
        attendees=["Ana Gómez", "Martín Ruiz"],
        said=[],
        runs=[run("transcribe", "running", progress={"phase": "transcribing", "percent": 62})],
    )
    settings.save(
        {
            "summarizer": "claude",
            "claude_model": "opus",
            "claude_effort": "high",
            "gitlab_link": "https://gitlab.com/acme/platform",
            "github_link": "https://github.com/acme/platform",
            "vocabularies": [settings.GLOSSARY, "Acme"],
        }
    )
    # A client's vocabulary besides the glossary, so the Settings show its switch and Delete.
    acme = worker.vocabulary_file("Acme")
    acme.parent.mkdir(parents=True)
    acme.write_text("Acme\n")


def wait_until(happened: Callable[[], bool]) -> None:
    context = GLib.MainContext.default()
    while not happened():
        context.iteration(may_block=True)


def draw(widget: Gtk.Widget) -> None:
    """Runs the main loop until the widget has drawn a few frames, laid out anew."""
    frames: list[object] = []

    def drawn(_widget: Gtk.Widget, clock: object) -> bool:
        frames.append(clock)
        return len(frames) < 3  # noqa: PLR2004 - a few frames

    widget.add_tick_callback(drawn)
    wait_until(lambda: len(frames) == 3)  # noqa: PLR2004 - a few frames


def save(widget: Gtk.Widget, path: Path) -> None:
    """Draws the widget as it is on screen into a PNG, at the display's scale."""
    draw(widget)
    scale = widget.get_scale_factor()
    width, height = widget.get_width() * scale, widget.get_height() * scale
    snapshot = Gtk.Snapshot()
    snapshot.scale(scale, scale)
    Gtk.WidgetPaintable(widget=widget).snapshot(snapshot, widget.get_width(), widget.get_height())
    viewport = Graphene.Rect().init(0, 0, width, height)
    renderer = cast("Gtk.Native", widget.get_native()).get_renderer()
    node = cast("Gsk.RenderNode", snapshot.to_node())
    cast("Gsk.Renderer", renderer).render_texture(node, viewport).save_to_png(str(path))


def descendants(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


def open_window(width: int, height: int) -> window.HistoryWindow:
    shown = window.HistoryWindow()
    shown.set_default_size(width, height)
    shown.present()
    draw(shown)
    return shown


def close(shown: window.HistoryWindow) -> None:
    shown.monitor.cancel()
    shown.destroy()


def row(shown: window.HistoryWindow, name: str) -> window.RecordingRow:
    return next(found for found in shown.rows.values() if found.view.name == name)


def main(out: Path) -> None:
    seed()
    window.now = lambda: NOW
    window.MINUTAS = [sys.executable, "-c", FAKE_SETTINGS]
    # Claude Code, glab and gh on the PATH, new enough: Summarize and the publishers are on.
    for command, version in (("claude", "2.1.166"), ("glab", "1.91.0"), ("gh", "2.45.0")):
        (SCRATCH / command).write_text(f"#!/bin/sh\necho {command} version {version}\n")
        (SCRATCH / command).chmod(0o755)
    Adw.init()
    gtk_settings = cast("Gtk.Settings", Gtk.Settings.get_default())
    gtk_settings.props.gtk_font_name = "Cantarell 11"
    gtk_settings.props.gtk_enable_animations = False
    out.mkdir(parents=True, exist_ok=True)

    shown = open_window(780, 910)
    row(shown, "Weekly sync").set_expanded(True)
    save(shown, out / "recordings.png")
    close(shown)

    shown = open_window(780, 570)
    row(shown, "Platform standup").set_expanded(True)
    save(shown, out / "transcribing.png")
    close(shown)

    # A search for a word two transcripts hold, and the failed run of one of them.
    shown = open_window(780, 845)
    shown.search_bar.set_search_mode(True)
    shown.search.set_text("release")
    wait_until(lambda: len(shown.matching) == 2)  # noqa: PLR2004 - the two that say it
    row(shown, "Acme rollout call").set_expanded(True)
    save(shown, out / "search.png")
    close(shown)

    # Tall enough for every setting at once.
    shown = open_window(780, 2400)
    shown.open_settings()
    dialog = cast("window.SettingsDialog", shown.get_visible_dialog())
    dialog.set_content_height(2300)
    wait_until(
        lambda: (
            dialog.gitlab.get_description() != window.ASKING_GLAB
            and dialog.agaric.get_description() != window.ASKING_AGARIC
        )
    )
    # The dialog's own card, without the dimmed window around it.
    sheet = next(part for part in descendants(dialog) if part.get_css_name() == "sheet")
    save(sheet, out / "settings.png")
    close(shown)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]))
    finally:
        shutil.rmtree(SCRATCH)
