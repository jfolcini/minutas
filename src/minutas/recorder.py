"""Records the mic and the computer's audio as two Ogg Opus tracks in a systemd user unit.

A user unit outlives the lock screen and a GNOME Shell restart. One GStreamer pipeline records
both tracks, so they share a clock and stay aligned.
"""

import os
import shlex
import subprocess
from pathlib import Path

UNIT = "minutas-recorder.service"
# 16 kHz mono is what every speech model downstream takes, so no bits go above that band.
CAPTURE_CAPS = "audio/x-raw,rate=16000,channels=1"


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


def start(folder: Path, *, on_exit: list[str]) -> None:
    """Starts recording into folder; on_exit runs when the recorder stops for any reason.

    systemd passes on_exit the outcome in SERVICE_RESULT.
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
            *track_branch(["pulsesrc"], "mic.opus"),
            *track_branch(["pulsesrc", "device=@DEFAULT_MONITOR@"], "call.opus"),
        ]
    )


def is_running() -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0


def stop() -> None:
    """Returns once the recorder has closed its files and its exit command has run."""
    run_or_raise(["systemctl", "--user", "stop", UNIT])
