"""Records the mic and the computer's audio as two Ogg Opus tracks in a systemd user unit.

A user unit outlives the lock screen and a GNOME Shell restart. One GStreamer pipeline records
both tracks, so they share a clock and stay aligned.
"""

import os
import shlex
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from minutas import store

UNIT = "minutas-recorder.service"
# 16 kHz mono is what every speech model downstream takes, so no bits go above that band.
CAPTURE_CAPS = "audio/x-raw,rate=16000,channels=1"
MIC_TRACK, CALL_TRACK = store.TRACKS
# A working recorder fills both tracks within a fraction of a second; a broken one dies as fast.
CAPTURE_TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.05


class RecorderError(Exception):
    pass


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    # The commands are this module's own argument lists, never a shell string.
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603


def run_or_raise(args: list[str]) -> None:
    result = run(args)
    if result.returncode != 0:
        reason = result.stderr.strip() or f"{args[0]} exited with status {result.returncode}"
        raise RecorderError(reason)


def track_branch(source: list[str], file_name: str) -> list[str]:
    return [
        *source,
        *("!", "audioconvert", "!", "audioresample", "!", CAPTURE_CAPS),
        *("!", "opusenc", "bitrate=32000", "!", "oggmux"),
        # Unbuffered, so a killed recorder leaves every finished Ogg page on disk.
        *("!", "filesink", "buffer-mode=unbuffered", f"location={file_name}"),
    ]


def capturing(folder: Path) -> bool:
    # filesink creates both files before the sources connect, so only bytes prove capture.
    return all(store.has_audio(folder / name) for name in store.TRACKS)


def start(
    folder: Path,
    *,
    on_exit: list[str],
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> None:
    """Starts recording into folder and returns once both tracks hold audio.

    on_exit runs when the recorder stops for any reason; systemd passes it the outcome in
    SERVICE_RESULT. A recorder that dies or captures nothing raises RecorderError, once it has
    stopped and its on_exit has run.
    """
    # The exit command must find the same data folder as the caller.
    data_home = os.environ.get("XDG_DATA_HOME")
    run_or_raise(
        [
            *("systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet"),
            # gst-launch -e turns SIGINT into end-of-stream, which closes both files properly.
            "--property=KillSignal=SIGINT",
            "--property=TimeoutStopSec=5",
            f"--property=ExecStopPost={shlex.join(on_exit)}",
            *([f"--setenv=XDG_DATA_HOME={data_home}"] if data_home is not None else []),
            f"--working-directory={folder}",
            *("--", "gst-launch-1.0", "-e"),
            # Relative locations: gst-launch re-splits its arguments on spaces.
            *track_branch(["pulsesrc"], MIC_TRACK),
            *track_branch(["pulsesrc", "device=@DEFAULT_MONITOR@"], CALL_TRACK),
        ]
    )
    # systemd-run returns once the process is forked, before GStreamer has opened anything.
    deadline = clock() + CAPTURE_TIMEOUT_SECONDS
    while not capturing(folder):
        if clock() >= deadline or not is_running():
            # Stop waits for on_exit too, so the caller finds the folder settled; on a unit that
            # is already gone it fails, which changes nothing.
            run(["systemctl", "--user", "stop", UNIT])
            reason = f"no audio reached its tracks; journalctl --user -u {UNIT} says why"
            raise RecorderError(reason)
        wait(POLL_SECONDS)


def is_running() -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def stop() -> None:
    """Returns once the recorder has closed its files and its exit command has run."""
    run_or_raise(["systemctl", "--user", "stop", UNIT])
