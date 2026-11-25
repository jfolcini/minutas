"""Plays a recording's tracks through the sound card, one alone or the two mixed, at a speed that
keeps the voices' pitch; and saves their mix as Publish to Agaric mixes it.

Both run on the main loop, which hears from them only how they ended: through the function they
were given, with None at the end, else what went wrong.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402 - after the version is chosen

from minutas import agaric  # noqa: E402

# Where the sound reaches the sound card.
SINK = ["autoaudiosink"]
# scaletempo goes faster by leaving out slivers of the sound, not by playing it faster, so a voice
# keeps its pitch.
AT_SPEED = ["scaletempo", "!", "audioconvert", "!", "audioresample", "!"]

Ended = Callable[[str | None], None]


class Pipeline:
    """A GStreamer pipeline on the main loop, until it ends, fails or is stopped."""

    def __init__(self, description: list[str], ended: Ended) -> None:
        Gst.init(None)
        self.pipeline = cast("Gst.Pipeline", Gst.parse_launchv(description))
        self.ended = ended
        # The watch holds this object until stop() removes it: nothing else need keep it.
        self.pipeline.get_bus().add_watch(GLib.PRIORITY_DEFAULT, self.heard)

    def heard(self, _bus: Gst.Bus, message: Gst.Message) -> bool:
        if message.type == Gst.MessageType.EOS:
            self.stop()
            self.ended(None)
        elif message.type == Gst.MessageType.ERROR:
            error, _ = message.parse_error()
            self.stop()
            self.ended(error.message)
        return GLib.SOURCE_CONTINUE

    def stop(self) -> None:
        """Lets go of the files and the sound card; it says nothing more."""
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline.get_bus().remove_watch()


class Player(Pipeline):
    """The tracks played through the sound card, mixed when there are two."""

    def __init__(self, tracks: Sequence[Path], ended: Ended) -> None:
        super().__init__(agaric.mixed([str(track) for track in tracks], [*AT_SPEED, *SINK]), ended)
        self.speed = 1.0

    def play(self) -> None:
        self.pipeline.set_state(Gst.State.PLAYING)

    def pause(self) -> None:
        self.pipeline.set_state(Gst.State.PAUSED)

    def position(self) -> tuple[float, float]:
        """Where it is and how long it lasts, in seconds of the recording; 0 for what it cannot
        tell yet."""
        _, at = self.pipeline.query_position(Gst.Format.TIME)
        _, length = self.pipeline.query_duration(Gst.Format.TIME)
        return max(at, 0) / Gst.SECOND, max(length, 0) / Gst.SECOND

    def seek(self, seconds: float) -> None:
        # The slider's keys step past the start, and GStreamer reads a time before it as the end.
        seconds = max(seconds, 0)
        self.pipeline.seek(
            self.speed,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH,
            Gst.SeekType.SET,
            round(seconds * Gst.SECOND),
            Gst.SeekType.NONE,
            -1,
        )

    def set_speed(self, speed: float) -> None:
        """Goes on at that speed from where it is."""
        self.speed = speed
        self.seek(self.position()[0])


def save_mix(tracks: Sequence[Path], into: Path, saved: Ended) -> None:
    """Writes the tracks mixed into one Ogg Opus file, as Publish to Agaric mixes them."""
    encoded = [*agaric.ENCODED, "!", "filesink", f"location={into}"]
    mixing = Pipeline(agaric.mixed([str(track) for track in tracks], encoded), saved)
    mixing.pipeline.set_state(Gst.State.PLAYING)
