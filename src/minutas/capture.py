"""The recorder unit's program: runs the GStreamer pipeline its arguments describe, as
gst-launch-1.0 -e would, and pauses and resumes it when the recorder signals it.

A paused pipeline captures nothing and its running time stands still, so a pause leaves no gap
in the tracks; both sources pause together, so the tracks stay aligned.

It also tells the recording when neither track has been heard over its noise floor for as long
as the settings' silence reminder, counted in the pipeline's running time, which a pause starts
again.
"""

import signal
import sys
from collections import deque
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402 - after the version is chosen

from minutas import recorder, settings, store  # noqa: E402

# A second is quiet when its level is within the margin of its track's noise floor, the lowest
# level of the track's last FLOOR_READINGS seconds; anything louder is heard. Relative to the
# room, which the echo-cancelled mic hears far below the plain one, and the call track as digital
# silence when nothing plays. The margin is the measurements' (tests/test_capture.py keeps them):
# the echo-cancelled mic's quiet room rises up to 18 dB over its floor, while the plain mic's soft
# speech reaches only 20 dB over its. The measure is the level element's "rms" or "peak", in dB
# below full scale.
SILENCE_MEASURE = "rms"
SILENCE_MARGIN_DB = 18
# Longer than anyone speaks without a breath, so the floor stays under speech; a room that grows
# louder, a fan switched on, is heard for at most this long, and then is the floor.
FLOOR_READINGS = 120


class Hearing:
    """Since when, in the pipeline's running time, neither track has been heard over its floor."""

    def __init__(self) -> None:
        self.levels: dict[str, deque[float]] = {}
        self.since: int | None = None

    def reading(self, track: str, start: int, end: int, level: float) -> int | None:
        """Takes a track's level from start to end; returns how long neither track has been
        heard by its end, or None when this one was. A recording's loud first second sets no
        floor: the quieter seconds after it do."""
        recent = self.levels.setdefault(track, deque(maxlen=FLOOR_READINGS))
        recent.append(level)
        if level > min(recent) + SILENCE_MARGIN_DB:
            # Whatever the other track said of the same second, silence starts after it.
            self.since = end
            return None
        if self.since is None:
            self.since = start
        return end - self.since

    def pause(self) -> None:
        """Nothing is heard while paused, which is no silence: it counts again from the resume.
        The room's floor stays."""
        self.since = None


def local_now() -> datetime:
    return datetime.now(UTC).astimezone()


class Silence:
    """Tells the recording, in the unit's working directory, once neither track has been heard
    for as long as the settings' silence reminder, and again once either is."""

    def __init__(self, pipeline: Gst.Pipeline, now: Callable[[], datetime]) -> None:
        self.pipeline = pipeline
        self.now = now
        self.recording_id = Path.cwd().name
        self.hearing = Hearing()
        self.said = False

    def heard(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        """Takes a track's level reading: no other element of the pipeline posts a message."""
        # The level element's own fields, which it always sets: a dB value for each channel.
        reading = cast("Gst.Structure", message.get_structure())
        start = cast("int", reading.get_value("running-time"))
        end = start + cast("int", reading.get_value("duration"))
        level = max(cast("list[float]", reading.get_value(SILENCE_MEASURE)))
        track = cast("str", message.src.get_name())
        quiet = self.hearing.reading(track, start, end, level)
        if quiet is None:
            if self.said:
                self.said = False
                store.mark_heard(self.recording_id)
                store.write_status()
            return
        if self.said:
            return
        minutes = settings.silence_reminder_minutes()
        if minutes is None or quiet < minutes * 60 * Gst.SECOND:
            return
        # Before the write, so a recording that can't be written to fails once a silence, not
        # once a second.
        self.said = True
        store.mark_silent(self.recording_id, self.time_of_day(end - quiet))
        store.write_status()

    def time_of_day(self, running_time: int) -> datetime:
        clock = cast("Gst.Clock", self.pipeline.get_clock())
        running_now = clock.get_time() - self.pipeline.get_base_time()
        return self.now() - timedelta(microseconds=(running_now - running_time) // 1000)

    def paused(self) -> None:
        # The store forgets the silence it was told of when it marks the pause.
        self.hearing.pause()
        self.said = False


def run(pipeline: Gst.Pipeline, now: Callable[[], datetime] = local_now) -> int:
    """Records until the pipeline ends, which SIGINT asks of it; 1 when it failed instead.

    now: the time of day, which the silence is said in.
    """
    loop = GLib.MainLoop()
    errors: list[str] = []
    silence = Silence(pipeline, now)

    def set_state(state: Gst.State) -> bool:
        pipeline.set_state(state)
        return GLib.SOURCE_CONTINUE

    def pause() -> bool:
        silence.paused()
        return set_state(Gst.State.PAUSED)

    def finish() -> bool:
        # A buffer that reached a sink as the pipeline paused waits there, holding the stream lock
        # end-of-stream takes, until the sink plays; the sources stay paused.
        pipeline.iterate_sinks().foreach(lambda sink: sink.set_state(Gst.State.PLAYING))
        # End-of-stream closes both files properly.
        pipeline.send_event(Gst.Event.new_eos())
        # A paused pipeline passes it on only once it plays; its sources end before they capture.
        return set_state(Gst.State.PLAYING)

    def failed(_bus: Gst.Bus, message: Gst.Message) -> None:
        error, debug = message.parse_error()
        errors.append(error.message)
        # To the unit's journal.
        sys.stderr.write(f"{error.message}\n{debug}\n")
        loop.quit()

    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::eos", lambda *_: loop.quit())
    bus.connect("message::error", failed)
    # An exception in it reaches the unit's journal, and the recording goes on.
    bus.connect("message::element", silence.heard)
    playing = Gst.State.PLAYING
    watches = [
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, recorder.PAUSE_SIGNAL, pause),
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, recorder.RESUME_SIGNAL, set_state, playing),
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, finish),
    ]
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    for watch in watches:
        GLib.source_remove(watch)
    bus.remove_signal_watch()
    return 1 if errors else 0


def main(description: Sequence[str]) -> int:
    Gst.init(None)
    try:
        # More than one element makes a pipeline, and the recorder's has many.
        pipeline = cast("Gst.Pipeline", Gst.parse_launchv(list(description)))
    except GLib.Error as error:
        sys.stderr.write(f"{error.message}\n")
        return 1
    return run(pipeline)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
