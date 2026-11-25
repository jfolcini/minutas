"""The transcription worker's joiner: in the recording's folder it runs in, joins what each track
recorded after Continue onto the track, as one Opus stream encoded as the recorder encodes.

Every joined file is written beside its track and checked first: it decodes, and it plays for as
long as its two parts together. Only then does each take its track's place and the part go, so a
join that fails, or is killed, loses nothing.
"""

import os
import sys
from pathlib import Path
from typing import cast

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402 - after the version is chosen

from minutas import recorder, store  # noqa: E402

# concat starts the second part at time zero again, where the encoder drops it as late; identity
# lays the two end to end on one timeline.
END_TO_END = ["concat", "name=joined", "!", "identity", "single-segment=true"]
# A few of Opus's 20 ms frames: the encoder rounds neither part by more.
TOLERANCE = Gst.SECOND // 10


class JoinError(Exception):
    pass


def play(description: list[str]) -> int:
    """Runs the pipeline to its end; returns how long what reached its sink plays for, in
    nanoseconds."""
    pipeline = cast("Gst.Pipeline", Gst.parse_launchv(description))
    pipeline.set_state(Gst.State.PLAYING)
    ended = cast(
        "Gst.Message",
        pipeline.get_bus().timed_pop_filtered(
            Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR
        ),
    )
    _, position = pipeline.query_position(Gst.Format.TIME)
    pipeline.set_state(Gst.State.NULL)
    if ended.type == Gst.MessageType.ERROR:
        error, debug = ended.parse_error()
        # To the run's log.
        sys.stderr.write(f"{error.message}\n{debug}\n")
        raise JoinError(error.message.rstrip("."))
    return position


def decoded(name: str) -> list[str]:
    source = ["filesrc", f"location={name}", "!", "oggdemux", "!", "opusdec"]
    return [*source, "!", recorder.CAPTURE_CAPS]


def length(name: str) -> int:
    """How long the file plays for, decoding all of it."""
    return play([*decoded(name), "!", "fakesink"])


def join(track: str, part: str, whole: str) -> None:
    """Writes the track and then its part into whole, as one stream, and checks it."""
    parts = length(track) + length(part)
    play(
        [
            *recorder.track_branch(END_TO_END, whole),
            *(*decoded(track), "!", "joined."),
            *(*decoded(part), "!", "joined."),
        ]
    )
    lasts = length(whole)
    if abs(lasts - parts) > TOLERANCE:
        reason = (
            f"the joined file plays for {lasts / Gst.SECOND:.1f} s, "
            f"its parts for {parts / Gst.SECOND:.1f} s"
        )
        raise JoinError(reason)
    with Path(whole).open("rb") as written:
        # A rename that reaches the disk before the data leaves a cut track after a power cut.
        os.fsync(written.fileno())


def main() -> int:
    Gst.init(None)
    joined: list[tuple[str, str]] = []
    for track, part, whole in zip(store.TRACKS, store.PARTS, store.JOINED, strict=True):
        if not store.has_audio(Path(part)):
            continue
        try:
            join(track, part, whole)
        except JoinError as error:
            for name in store.JOINED:
                Path(name).unlink(missing_ok=True)
            sys.stderr.write(
                f"Could not join what was recorded after Continue onto {track}: {error}. "
                "Both are kept in the recording's folder.\n"
            )
            return 1
        joined.append((whole, track))
    for whole, track in joined:
        Path(whole).replace(track)
    # Each part is on its track now, and one left beside it would be joined onto it again; a
    # part that holds no audio goes too, as does what a join that was cancelled had written.
    for name in (*store.PARTS, *store.JOINED):
        Path(name).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
