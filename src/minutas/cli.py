"""The `minutas` command."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minutas",
        description="Record, transcribe and publish work meetings.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('minutas')}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
