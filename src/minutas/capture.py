"""The recorder unit's program: runs the GStreamer pipeline its arguments describe, as
gst-launch-1.0 -e would, and pauses and resumes it when the recorder signals it.

A paused pipeline captures nothing and its running time stands still, so a pause leaves no gap
in the tracks; both sources pause together, so the tracks stay aligned.
"""

import signal
import sys
from collections.abc import Sequence
from typing import cast

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402 - after the version is chosen

from minutas import recorder  # noqa: E402


def run(pipeline: Gst.Pipeline) -> int:
    """Records until the pipeline ends, which SIGINT asks of it; 1 when it failed instead."""
    loop = GLib.MainLoop()
    errors: list[str] = []

    def set_state(state: Gst.State) -> bool:
        pipeline.set_state(state)
        return GLib.SOURCE_CONTINUE

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
    paused, playing = Gst.State.PAUSED, Gst.State.PLAYING
    watches = [
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, recorder.PAUSE_SIGNAL, set_state, paused),
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
