import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import override

import keyring
import pytest
from keyring.backend import KeyringBackend

SCRATCH = Path(tempfile.mkdtemp(prefix="minutas-tests-"))
BUS_CONFIG = f"""<busconfig>
  <type>session</type>
  <listen>unix:path={SCRATCH}/bus</listen>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def start(command: list[str]) -> str:
    """Starts a server for as long as this process lives; returns what it prints once ready.

    The kernel stops it when this process dies, however it dies, and not a moment before: an X
    server stopped while GTK still holds widgets takes the process down with it, at exit, with
    the test report still unwritten.
    """
    ready, output = os.pipe()
    os.posix_spawnp(
        "setpriv",
        ["setpriv", "--pdeathsig", "TERM", *command],
        os.environ,
        file_actions=[
            (os.POSIX_SPAWN_DUP2, output, 1),
            (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
        ],
    )
    os.close(output)
    # Left open: a server that printed again into a closed pipe would be killed for it.
    return os.fdopen(ready, closefd=False).readline().strip()


def pytest_configure() -> None:
    """Gives the tests a display and a session bus of their own, before any is imported.

    PyGObject opens the display the moment Gtk is imported, so a fixture would come too late:
    the window's tests would draw on the real desktop and answer for the window open there.
    The display is Xvfb, an X server that draws to memory; GTK's own headless display,
    Broadway, draws no frames until a browser looks, so no dialog on it ever finishes closing.
    The bus starts no services.
    """
    # Ubuntu's PyGObject (python3-gi), which the window runs on, lives outside the virtualenv.
    # The tests, and the Pythons they start, find it, and nothing else of the system's, through
    # a folder that holds only a link to it.
    (SCRATCH / "gi").symlink_to("/usr/lib/python3/dist-packages/gi")
    sys.path.append(str(SCRATCH))
    os.environ["PYTHONPATH"] = str(SCRATCH)
    (SCRATCH / "bus.conf").write_text(BUS_CONFIG)
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["GDK_BACKEND"] = "x11"
    # Xvfb picks a free display and prints its number.
    os.environ["DISPLAY"] = ":" + start(
        ["Xvfb", "-displayfd", "1", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"]
    )
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = start(
        ["dbus-daemon", f"--config-file={SCRATCH / 'bus.conf'}", "--print-address"]
    )
    os.environ["XDG_RUNTIME_DIR"] = str(SCRATCH)
    # Drawn in software, with GTK's own settings and no accessibility bus: nothing of the
    # desktop's is asked for.
    os.environ["GSK_RENDERER"] = "cairo"
    os.environ["GSETTINGS_BACKEND"] = "memory"
    os.environ["GTK_A11Y"] = "none"


def pytest_unconfigure() -> None:
    shutil.rmtree(SCRATCH)


@pytest.fixture(autouse=True)
def xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points every XDG base directory into tmp_path so no test touches the real data."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


class MemoryKeyring(KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    @override
    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    @override
    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    @override
    def delete_password(self, service: str, username: str) -> None:
        del self.passwords[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    """Stands in for the GNOME keyring so no test reads or writes the real one."""
    fake = MemoryKeyring()
    keyring.set_keyring(fake)
    return fake
