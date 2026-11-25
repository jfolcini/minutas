"""install.sh's steps, each run in bash under a temporary HOME with fake commands on the PATH.

The PATH holds the fakes and a folder of links to the few system tools the steps use, so no
real gsettings, glab, uv or Agaric is ever reached, whatever this machine has installed.
"""

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from minutas import window_view

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"
UUID = "minutas@jfolcini.github.io"
PYTHON = "/usr/bin/python3"
EXTENSION_FILES = ("metadata.json", "extension.js", "status.js")
SYSTEM_TOOLS = ("bash", "cat", "chmod", "cmp", "cp", "diff", "dirname", "ln", "mkdir", "mv")
SYSTEM_TOOLS += ("readlink", "rm", "sed", "sh", "gzip", "sha256sum", "tar")


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.data = self.home / ".local" / "share"
        self.app = self.data / "minutas" / "app"
        self.local_bin = self.home / ".local" / "bin"
        self.fakes = root / "fakes"
        self.log = root / "calls.log"
        self.tools = root / "tools"
        for folder in (self.home, self.fakes, self.tools):
            folder.mkdir()
        self.log.touch()
        for name in SYSTEM_TOOLS:
            (self.tools / name).symlink_to(shutil.which(name) or name)
        self.path = [self.fakes, self.tools]

    def fake(self, name: str, body: str = "", folder: Path | None = None) -> Path:
        """A command that logs how it was called, then runs body."""
        command = (folder or self.fakes) / name
        command.parent.mkdir(parents=True, exist_ok=True)
        command.write_text(f'#!/bin/bash\necho "{name} $*" >>"$FAKE_LOG"\n{body}\n')
        command.chmod(0o755)
        return command

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "PATH": os.pathsep.join(map(str, self.path)),
            "FAKE_LOG": str(self.log),
        }

    def run(self, step: str, script: Path = INSTALL_SH) -> subprocess.CompletedProcess[str]:
        """Sources install.sh, which defines the steps without running them, and runs one."""
        self.log.write_text("")
        return subprocess.run(  # noqa: S603 - the test's own command
            [str(self.tools / "bash"), "-c", f'source "$1" && {step}', "bash", str(script)],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# --- uv ----------------------------------------------------------------------------------------

UV_ARCHIVE = "uv-x86_64-unknown-linux-gnu"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/0.13.6/{UV_ARCHIVE}.tar.gz"


def uv_release(sandbox: Sandbox) -> str:
    """uv 0.13.6's archive as GitHub serves it, to a curl that saves it; returns its sha256.

    The machine is x86_64, whatever this one is.
    """
    built = sandbox.home.parent / f"{UV_ARCHIVE}.tar.gz"
    with tarfile.open(built, "w:gz") as archive:
        for name in ("uv", "uvx"):
            script = f"#!/bin/sh\necho '{name} 0.13.6 (x86_64-unknown-linux-gnu)'\n".encode()
            member = tarfile.TarInfo(f"{UV_ARCHIVE}/{name}")
            member.size = len(script)
            member.mode = 0o755
            archive.addfile(member, io.BytesIO(script))
    sandbox.fake("uname", "echo x86_64")
    sandbox.fake(
        "curl", f'while (($#)); do [[ $1 == -o ]] && out=$2; shift; done; cp {built} "$out"'
    )
    return hashlib.sha256(built.read_bytes()).hexdigest()


def test_uv_is_downloaded_checked_against_its_pin_and_replaces_an_older_one(
    sandbox: Sandbox,
) -> None:
    sha256 = uv_release(sandbox)
    sandbox.fake("uv", "echo 'uv 0.13.5 (x86_64-unknown-linux-gnu)'", folder=sandbox.local_bin)

    result = sandbox.run(f"uv_sha256[x86_64]={sha256}; step_uv")

    assert result.stdout == f"uv: installed uv 0.13.6 in {sandbox.local_bin}\n"
    download = sandbox.home / ".cache" / "minutas" / f"{UV_ARCHIVE}.tar.gz"
    assert sandbox.calls() == ["uv --version", "uname -m", f"curl -fLsS -o {download} {UV_URL}"]
    for name in ("uv", "uvx"):
        said = subprocess.run(  # noqa: S603 - the test's own fake
            [sandbox.local_bin / name], capture_output=True, text=True, check=True
        )
        assert said.stdout == f"{name} 0.13.6 (x86_64-unknown-linux-gnu)\n"
    assert sandbox.run("step_uv").stdout == "uv: ok\n"


def test_uv_that_does_not_match_its_pin_is_never_unpacked(sandbox: Sandbox) -> None:
    # install.sh's own pin, which no archive built here matches.
    uv_release(sandbox)

    result = sandbox.run("step_uv")

    assert result.returncode == 1
    assert result.stderr == (
        "uv: failed: the uv 0.13.6 download doesn't match the checksum install.sh pins for it, "
        "so it wasn't installed. Run install.sh again; if it fails the same way, report it at "
        "https://github.com/jfolcini/minutas/issues\n"
    )
    assert not sandbox.local_bin.exists()


def test_uv_on_a_machine_without_a_pin_stops_the_script(sandbox: Sandbox) -> None:
    uv_release(sandbox)
    sandbox.fake("uname", "echo armv7l")

    result = sandbox.run("step_uv")

    assert result.returncode == 1
    assert result.stderr == (
        "uv: failed: uv 0.13.6 is pinned for x86_64 and aarch64 only, and this computer is "
        "armv7l.\n"
    )
    assert sandbox.calls() == ["uname -m"]


# --- glab --------------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["glab 1.91.0 (2026-10-02)", "glab version 2.0.1"])
def test_glab_new_enough_on_the_path_is_ok(sandbox: Sandbox, version: str) -> None:
    sandbox.fake("glab", f'echo "{version}"')

    result = sandbox.run("step_glab")

    assert result.stdout == "glab: ok\n"
    assert sandbox.calls() == ["glab --version"]


@pytest.mark.parametrize(
    ("version", "found"),
    [("glab version 1.90.3", "the PATH has glab 1.90.3"), (None, "glab isn't on the PATH")],
)
def test_glab_older_or_missing_says_where_to_get_it(
    sandbox: Sandbox, version: str | None, found: str
) -> None:
    if version is not None:
        sandbox.fake("glab", f'echo "{version}"')

    result = sandbox.run("step_glab")

    assert result.returncode == 0
    assert result.stdout == (
        f"glab: {found}; Publish to GitLab stays off until glab 1.91 or newer is installed from "
        "GitLab's own .deb: https://gitlab.com/gitlab-org/cli/-/releases\n"
    )


def test_glab_snap_says_to_install_the_deb_instead(sandbox: Sandbox) -> None:
    # What /snap/bin/glab is: a link to snap, which runs glab confined to the visible home.
    sandbox.fake("snap", 'echo "glab 1.91.0 (2026-10-02)"')
    (sandbox.fakes / "glab").symlink_to(sandbox.fakes / "snap")

    result = sandbox.run("step_glab")

    assert result.stdout == (
        "glab: the PATH has the glab snap, which can't read the recordings; Publish to GitLab "
        "stays off until glab 1.91 or newer is installed from GitLab's own .deb: "
        "https://gitlab.com/gitlab-org/cli/-/releases\n"
    )


# --- gh ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version", ["gh version 2.45.0 (2024-03-04)", "gh version 3.0.1 (2027-01-01)\nhttps://x"]
)
def test_gh_new_enough_on_the_path_is_ok(sandbox: Sandbox, version: str) -> None:
    sandbox.fake("gh", f'echo "{version}"')

    result = sandbox.run("step_gh")

    assert result.stdout == "gh: ok\n"
    assert sandbox.calls() == ["gh --version"]


@pytest.mark.parametrize(
    ("version", "found"),
    [
        ("gh version 2.44.1 (2024-02-01)", "the PATH has gh 2.44.1"),
        ("gh version 1.99.0 (2021-01-01)", "the PATH has gh 1.99.0"),
        (None, "gh isn't on the PATH"),
    ],
)
def test_gh_older_or_missing_says_where_to_get_it(
    sandbox: Sandbox, version: str | None, found: str
) -> None:
    if version is not None:
        sandbox.fake("gh", f'echo "{version}"')

    result = sandbox.run("step_gh")

    assert result.returncode == 0
    assert result.stdout == (
        f"gh: {found}; Publish to GitHub stays off until gh 2.45 or newer is installed: "
        "`sudo apt install gh`, or see https://cli.github.com\n"
    )


# --- App ---------------------------------------------------------------------------------------


def fake_uv(sandbox: Sandbox, body: str = "") -> None:
    """uv as far as the App step goes: venv makes the folders, sync copies the package in."""
    sandbox.fake(
        "uv",
        body
        + """
echo "  in $UV_PROJECT_ENVIRONMENT" >>"$FAKE_LOG"
case $1 in
  venv) dir=${@: -1}; rm -rf "$dir"; mkdir -p "$dir/bin" "$dir/lib/python3.12/site-packages" ;;
  sync)
    while (($#)); do [[ $1 == --project ]] && project=$2; shift; done
    cp -r "$project/src/minutas" "$UV_PROJECT_ENVIRONMENT/lib/python3.12/site-packages/"
    printf '#!/bin/sh\\n' >"$UV_PROJECT_ENVIRONMENT/bin/minutas"
    chmod +x "$UV_PROJECT_ENVIRONMENT/bin/minutas" ;;
esac""",
        folder=sandbox.local_bin,
    )


def test_app_installs_the_program_its_command_and_its_launcher(sandbox: Sandbox) -> None:
    fake_uv(sandbox)

    result = sandbox.run("step_app")

    assert result.stdout == f"App: installed {sandbox.app} and the minutas command\n"
    assert sandbox.calls() == [
        f"uv venv --quiet --clear --system-site-packages --python {PYTHON} {sandbox.app}",
        "  in ",
        (
            f"uv sync --quiet --project {REPO} --frozen --no-dev --no-editable "
            f"--reinstall-package minutas --python {PYTHON}"
        ),
        f"  in {sandbox.app}",
    ]
    assert (sandbox.local_bin / "minutas").readlink() == sandbox.app / "bin" / "minutas"
    assert (sandbox.app / "repository").readlink() == REPO
    assert (sandbox.data / "applications" / "io.github.jfolcini.Minutas.desktop").read_text() == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Minutas\n"
        "Comment=Recorded meetings, their transcripts and what was published\n"
        f'Exec="{sandbox.local_bin}/minutas" history\n'
        "Icon=audio-input-microphone\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Office;\n"
    )


def test_app_second_run_is_ok(sandbox: Sandbox) -> None:
    fake_uv(sandbox)
    sandbox.run("step_app")

    result = sandbox.run("step_app")

    assert result.stdout == "App: ok\n"
    assert sandbox.calls() == []


def test_app_run_through_the_repository_link_is_ok(sandbox: Sandbox) -> None:
    fake_uv(sandbox)
    sandbox.run("step_app")

    # How minutas update runs it.
    result = sandbox.run("step_app", sandbox.app / "repository" / "install.sh")

    assert result.stdout == "App: ok\n"
    assert sandbox.calls() == []


@pytest.mark.parametrize(
    "change",
    [
        "lib/python3.12/site-packages/minutas/cli.py",
        # A Python upgrade leaves the environment's interpreter gone.
        "bin/minutas",
        "uv.lock",
        "repository",
        "../../applications/io.github.jfolcini.Minutas.desktop",
    ],
)
def test_app_installs_again_when_anything_differs(sandbox: Sandbox, change: str) -> None:
    fake_uv(sandbox)
    sandbox.run("step_app")
    changed = sandbox.app / change
    changed.unlink()
    changed.write_text("something else\n")

    result = sandbox.run("step_app")

    assert result.stdout == f"App: installed {sandbox.app} and the minutas command\n"
    assert (sandbox.app / "repository").readlink() == REPO


def test_app_command_pointing_elsewhere_installs_again(sandbox: Sandbox) -> None:
    fake_uv(sandbox)
    sandbox.run("step_app")
    command = sandbox.local_bin / "minutas"
    command.unlink()
    command.symlink_to(sandbox.home / "elsewhere")

    result = sandbox.run("step_app")

    assert result.stdout == f"App: installed {sandbox.app} and the minutas command\n"
    assert command.readlink() == sandbox.app / "bin" / "minutas"


def test_app_uv_failure_stops_the_script(sandbox: Sandbox) -> None:
    fake_uv(sandbox, '[ "$1" = sync ] && exit 2')

    result = sandbox.run("step_app")

    assert result.returncode == 1
    assert result.stderr == (
        "App: failed: uv could not install it (see above). Fix that, then run "
        f"{REPO}/install.sh again.\n"
    )
    assert not (sandbox.local_bin / "minutas").exists()


# --- agaric-mcp --------------------------------------------------------------------------------


def agaric_mcp_hint(sandbox: Sandbox) -> str:
    return (
        f"agaric-mcp: no agaric-mcp with `call` at {agarics_own_bridge(sandbox)} or on the PATH; "
        "Publish to Agaric needs it. Install Agaric 0.26 or newer and start it once\n"
    )


def agarics_own_bridge(sandbox: Sandbox) -> Path:
    return sandbox.data / "com.agaric.app" / "bin" / "agaric-mcp"


WITH_CALL = 'echo "    call <tool> <json>   Run one tool with <json>"'
# The 0.20 bridge's usage.
WITHOUT_CALL = "echo '  agaric-mcp --token <token> [--socket <path>]'"


def test_agaric_mcp_with_call_is_ok(sandbox: Sandbox) -> None:
    sandbox.fake("agaric-mcp", WITH_CALL)

    result = sandbox.run("step_agaric_mcp")

    assert result.stdout == "agaric-mcp: ok\n"
    assert sandbox.calls() == ["agaric-mcp --help"]


def test_agarics_own_bridge_comes_before_the_one_on_the_path(sandbox: Sandbox) -> None:
    sandbox.fake("agaric-mcp", WITHOUT_CALL)
    own = agarics_own_bridge(sandbox)
    sandbox.fake("agaric-mcp", WITH_CALL, own.parent)

    assert sandbox.run("step_agaric_mcp").stdout == "agaric-mcp: ok\n"

    sandbox.fake("agaric-mcp", WITHOUT_CALL, own.parent)
    sandbox.fake("agaric-mcp", WITH_CALL)

    assert sandbox.run("step_agaric_mcp").stdout == agaric_mcp_hint(sandbox)


@pytest.mark.parametrize("bridge", [WITHOUT_CALL, None])
def test_agaric_mcp_without_call_says_what_to_put_there(
    sandbox: Sandbox, bridge: str | None
) -> None:
    if bridge is not None:
        sandbox.fake("agaric-mcp", bridge)

    result = sandbox.run("step_agaric_mcp")

    assert result.returncode == 0
    assert result.stdout == agaric_mcp_hint(sandbox)


# --- GNOME extension ---------------------------------------------------------------------------


def gsettings(sandbox: Sandbox, enabled: str = "@as []") -> Path:
    """gsettings over org.gnome.shell's keys, each kept in a file."""
    keys = sandbox.home / "gsettings"
    keys.mkdir()
    (keys / "enabled-extensions").write_text(enabled)
    sandbox.fake(
        "gsettings",
        f"""[ "$2" = org.gnome.shell ] || exit 1
case $1 in
  get) cat "{keys}/$3" ;;
  set) printf '%s' "$4" >"{keys}/$3" ;;
esac""",
    )
    return keys


def extension_dir(sandbox: Sandbox) -> Path:
    return sandbox.data / "gnome-shell" / "extensions" / UUID


def installed(sandbox: Sandbox, name: str) -> bytes:
    return (extension_dir(sandbox) / name).read_bytes()


def shipped(name: str) -> bytes:
    return (REPO / "extension" / name).read_bytes()


def test_extension_new_is_copied_enabled_and_needs_a_log_out(sandbox: Sandbox) -> None:
    keys = gsettings(sandbox, "['other@example.org']")

    result = sandbox.run("step_gnome_extension")

    assert result.stdout == (
        f"GNOME extension: installed in {extension_dir(sandbox)}; log out and back in once to "
        "load it\n"
    )
    assert sorted(path.name for path in extension_dir(sandbox).iterdir()) == sorted(EXTENSION_FILES)
    for name in EXTENSION_FILES:
        assert installed(sandbox, name) == shipped(name)
    assert (keys / "enabled-extensions").read_text() == f"['other@example.org', '{UUID}']"


def test_extension_first_one_enabled_is_the_whole_list(sandbox: Sandbox) -> None:
    keys = gsettings(sandbox)

    sandbox.run("step_gnome_extension")

    assert (keys / "enabled-extensions").read_text() == f"['{UUID}']"


def test_extension_second_run_is_ok(sandbox: Sandbox) -> None:
    gsettings(sandbox)
    sandbox.run("step_gnome_extension")

    result = sandbox.run("step_gnome_extension")

    assert result.stdout == "GNOME extension: ok\n"
    assert sandbox.calls() == [
        "gsettings get org.gnome.shell enabled-extensions",
    ]


def test_extension_changed_is_copied_again(sandbox: Sandbox) -> None:
    keys = gsettings(sandbox)
    sandbox.run("step_gnome_extension")
    (extension_dir(sandbox) / "status.js").write_text("// an older version\n")

    result = sandbox.run("step_gnome_extension")

    assert result.stdout == (
        f"GNOME extension: installed in {extension_dir(sandbox)}; log out and back in once to "
        "load it\n"
    )
    assert installed(sandbox, "status.js") == shipped("status.js")
    assert (keys / "enabled-extensions").read_text() == f"['{UUID}']"


def test_extension_unchanged_but_disabled_is_enabled_without_a_log_out(sandbox: Sandbox) -> None:
    keys = gsettings(sandbox)
    sandbox.run("step_gnome_extension")
    (keys / "enabled-extensions").write_text("['other@example.org']")

    result = sandbox.run("step_gnome_extension")

    assert result.stdout == "GNOME extension: enabled\n"
    assert (keys / "enabled-extensions").read_text() == f"['other@example.org', '{UUID}']"


def test_extension_without_gnome_shell_stops_the_script(sandbox: Sandbox) -> None:
    sandbox.fake("gsettings", 'echo "No such schema “org.gnome.shell”" >&2; exit 1')

    result = sandbox.run("step_gnome_extension")

    assert result.returncode == 1
    assert result.stderr.endswith(
        "GNOME extension: failed: could not read GNOME Shell's settings (see above). Install "
        "GNOME Shell 46 or newer, then run install.sh again.\n"
    )
    assert not extension_dir(sandbox).exists()


# --- Summarizer --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summarizer", "body"),
    [
        ("devin", ""),
        ("claude", 'echo "2.1.166 (Claude Code)"'),
        ("claude", 'echo "2.2.0 (Claude Code)"'),
        ("claude", 'echo "3.0.0 (Claude Code)"'),
    ],
)
def test_devin_or_a_new_enough_claude_on_the_path_is_ok(
    sandbox: Sandbox, summarizer: str, body: str
) -> None:
    sandbox.fake(summarizer, body)

    assert sandbox.run("step_summarizer").stdout == "Summarizer: ok\n"


@pytest.mark.parametrize("version", ["2.1.165", "2.0.999", "1.9.200"])
@pytest.mark.parametrize(("devin", "missing"), [(False, "Summarizer"), (True, "")])
def test_an_older_claude_says_to_update_it_and_is_missing_only_without_devin(
    sandbox: Sandbox, version: str, *, devin: bool, missing: str
) -> None:
    sandbox.fake("claude", f'echo "{version} (Claude Code)"')
    if devin:
        sandbox.fake("devin")

    result = sandbox.run('step_summarizer; echo "missing: ${missing_tools[*]}"')

    assert result.stdout == (
        f"Summarizer: the PATH has claude {version}; Summarize with Claude Code stays off until it "
        f"is updated to 2.1.166 or newer: `claude update`\nmissing: {missing}\n"
    )


def test_no_summarizer_says_how_to_install_either(sandbox: Sandbox) -> None:
    result = sandbox.run("step_summarizer")

    assert result.returncode == 0
    assert result.stdout == (
        "Summarizer: not installed; Summarize needs the Devin CLI or Claude Code. Install the "
        "Devin CLI with `curl -fsSL https://cli.devin.ai/install.sh | bash`, then log in with "
        "`devin auth login`; or Claude Code with `curl -fsSL https://claude.ai/install.sh | bash`, "
        "then log in with `claude auth login`\n"
    )


# --- Summary -----------------------------------------------------------------------------------

STEPS = "step_gnome_extension; step_agaric_mcp; step_glab; step_gh; step_summarizer"


def test_the_summary_says_to_log_out_and_names_the_optional_tools_missing(
    sandbox: Sandbox,
) -> None:
    gsettings(sandbox)
    sandbox.fake("agaric-mcp", WITH_CALL)

    result = sandbox.run(f"{{ {STEPS}; }} >/dev/null; summary")

    assert result.stdout == (
        "\n"
        "Left to do:\n"
        "- Log out and back in to load the top-bar extension.\n"
        "- Optional, still missing: glab, gh, Summarizer; their lines above say what needs them "
        "and how to get them.\n"
    )
    # What the Recordings window looks for after an update.
    assert f"- {window_view.LOG_OUT}\n" in result.stdout


def test_the_summary_is_left_out_when_nothing_is_left_to_do(sandbox: Sandbox) -> None:
    gsettings(sandbox)
    sandbox.run("step_gnome_extension")
    sandbox.fake("agaric-mcp", WITH_CALL)
    sandbox.fake("glab", 'echo "glab 1.91.0"')
    sandbox.fake("gh", 'echo "gh version 2.45.0"')
    sandbox.fake("devin")

    result = sandbox.run(f"{STEPS}; summary")

    assert result.stdout == (
        "GNOME extension: ok\nagaric-mcp: ok\nglab: ok\ngh: ok\nSummarizer: ok\n"
    )
