import subprocess
import sys
from importlib.metadata import version

import pytest

from minutas.cli import main


def test_version_prints_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"minutas {version('minutas')}\n"


def test_no_arguments_exits_zero() -> None:
    assert main([]) == 0


def test_unknown_option_exits_with_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--no-such-option"])

    assert exit_info.value.code == 2
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err


def test_module_entry_point_runs_the_cli() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "minutas", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout == f"minutas {version('minutas')}\n"
