"""Records the mic and the computer's audio as two Ogg Opus tracks in a systemd user unit.

A user unit outlives the lock screen and a GNOME Shell restart. One GStreamer pipeline records
both tracks, so they share a clock and stay aligned; minutas.capture runs it in the unit.
"""

import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from minutas import store

UNIT = "minutas-recorder.service"
# 16 kHz mono is what every speech model downstream takes, so no bits go above that band.
CAPTURE_CAPS = "audio/x-raw,rate=16000,channels=1"
# A runaway pipeline is killed at its limit instead of swapping the desktop to death; the tracks
# keep what it wrote and the recording is marked incomplete. Neither MemoryHigh nor swap: both
# would stall capture instead.
MEMORY_LIMITS = ("MemoryMax=256M", "MemorySwapMax=0")
# A working recorder fills both tracks within a fraction of a second; a broken one dies as fast.
CAPTURE_TIMEOUT_SECONDS = 2.0
# What the unit's program, minutas.capture, pauses and resumes on.
PAUSE_SIGNAL, RESUME_SIGNAL = signal.SIGUSR1, signal.SIGUSR2
POLL_SECONDS = 0.05
# PipeWire's echo canceller, in a pipewire process and a unit of its own: systemctl kill reaches
# every process of a unit, so in the recorder's the pause signal would end it.
ECHO_CANCEL_UNIT = "minutas-echo-cancel.service"
STOP_ECHO_CANCELLER = ["systemctl", "--user", "stop", ECHO_CANCEL_UNIT]
# The echo-cancelled mic's name in PipeWire, which is its device name for pulsesrc.
ECHO_CANCELLED_SOURCE = "minutas-echo-cancelled"
# A working canceller adds its source within a fraction of a second; a recording starts no later
# than this for one that never does.
ECHO_CANCEL_TIMEOUT_SECONDS = 2.0
# What a pipewire process needs to be a client of the session's, then the canceller. monitor.mode
# takes the far side from what the default sink plays, and adds no sink of its own, so the
# desktop's default devices stay as they are.
ECHO_CANCEL_CONF = f"""\
# Minutas writes this file each time it records with echo cancellation: a change here is lost.
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt
        flags = [ ifexists nofail ]
    }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            source.props = {{
                node.name = "{ECHO_CANCELLED_SOURCE}"
                node.description = "Minutas echo-cancelled microphone"
            }}
        }}
    }}
]
"""


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


def capturing(folder: Path, tracks: tuple[str, str]) -> bool:
    # filesink creates both files before the sources connect, so only bytes prove capture.
    return all(store.has_audio(folder / name) for name in tracks)


def echo_cancelled_source_exists() -> bool:
    # pw-link names each port after its node: one of the source's says pulsesrc will find it.
    ports = run(["pw-link", "--output"]).stdout.splitlines()
    return any(port.startswith(f"{ECHO_CANCELLED_SOURCE}:") for port in ports)


def start_echo_canceller(clock: Callable[[], float], wait: Callable[[float], None]) -> bool:
    """Whether the echo-cancelled mic is there to record; a canceller without one is stopped."""
    conf = store.data_dir() / "echo-cancel.conf"
    conf.write_text(ECHO_CANCEL_CONF)
    # Not checked: a unit left running by a start that died refuses this one, and its source is
    # there all the same.
    run(
        [
            *("systemd-run", "--user", f"--unit={ECHO_CANCEL_UNIT}", "--collect", "--quiet"),
            *("--", "pipewire", "-c", str(conf)),
        ]
    )
    deadline = clock() + ECHO_CANCEL_TIMEOUT_SECONDS
    # pulsesrc records the default mic, without a word, when its device is not there yet.
    while not echo_cancelled_source_exists():
        if clock() >= deadline or not is_running(ECHO_CANCEL_UNIT):
            run(STOP_ECHO_CANCELLER)
            return False
        wait(POLL_SECONDS)
    return True


def start(  # noqa: PLR0913 - all after folder go by name
    folder: Path,
    *,
    on_exit: list[str],
    tracks: tuple[str, str] = store.TRACKS,
    echo_cancel: bool = False,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[float], None] = time.sleep,
) -> bool:
    """Starts recording into folder and returns once both tracks hold audio.

    tracks: the files the mic and the call go to, the parts of a recording that is continued.
    on_exit runs when the recorder stops for any reason; systemd passes it the outcome in
    SERVICE_RESULT. A recorder that dies or captures nothing raises RecorderError, once it has
    stopped and its on_exit has run.

    echo_cancel: record the mic with what the speakers play taken out. Returns whether it is: an
    echo canceller that does not start costs no recording, which takes the plain mic instead.
    """
    echo_cancelled = echo_cancel and start_echo_canceller(clock, wait)
    mic = ["pulsesrc"]
    stop_canceller: list[str] = []
    if echo_cancelled:
        mic.append(f"device={ECHO_CANCELLED_SOURCE}")
        # The canceller stops when the recorder does, however that ends. With its failure
        # ignored ("-"): systemd runs no exit command after one that failed.
        stop_canceller.append(f"--property=ExecStopPost=-{shlex.join(STOP_ECHO_CANCELLER)}")
    try:
        run_or_raise(
            [
                *("systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet"),
                # minutas.capture turns SIGINT into end-of-stream, which closes both files
                # properly.
                "--property=KillSignal=SIGINT",
                "--property=TimeoutStopSec=5",
                *(f"--property={limit}" for limit in MEMORY_LIMITS),
                # Before on_exit, which may fail.
                *stop_canceller,
                f"--property=ExecStopPost={shlex.join(on_exit)}",
                # The exit command must find the same data and settings as the caller.
                *(
                    f"--setenv={name}={os.environ[name]}"
                    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")
                    if name in os.environ
                ),
                f"--working-directory={folder}",
                # The app's own Python, which is Ubuntu's: its PyGObject reaches GStreamer.
                *("--", sys.executable, "-m", "minutas.capture"),
                *track_branch(mic, tracks[0]),
                *track_branch(["pulsesrc", "device=@DEFAULT_MONITOR@"], tracks[1]),
            ]
        )
    except RecorderError:
        if echo_cancelled:
            # No recorder is there to stop it.
            run(STOP_ECHO_CANCELLER)
        raise
    # systemd-run returns once the process is forked, before GStreamer has opened anything.
    deadline = clock() + CAPTURE_TIMEOUT_SECONDS
    while not capturing(folder, tracks):
        if clock() >= deadline or not is_running():
            # Stop waits for on_exit too, so the caller finds the folder settled; on a unit that
            # is already gone it fails, which changes nothing.
            run(["systemctl", "--user", "stop", UNIT])
            reason = f"no audio reached its tracks; journalctl --user -u {UNIT} says why"
            raise RecorderError(reason)
        wait(POLL_SECONDS)
    return echo_cancelled


def is_running(unit: str = UNIT) -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def stop() -> None:
    """Returns once the recorder has closed its files and its exit command has run."""
    run_or_raise(["systemctl", "--user", "stop", UNIT])


def signal_unit(sent: signal.Signals) -> None:
    run_or_raise(["systemctl", "--user", "kill", f"--signal={sent.name}", UNIT])


def pause() -> None:
    """Stops capturing until resume; what was captured stays, and the tracks stay aligned."""
    signal_unit(PAUSE_SIGNAL)


def resume() -> None:
    signal_unit(RESUME_SIGNAL)
