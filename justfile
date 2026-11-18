# Task shortcuts. Every recipe is one `uv run` line: nothing needs `just`.

# List the recipes.
default:
    @just --list

# Install the toolchain and wire the git hooks.
setup:
    uv sync
    uv run prek install

# Run the tests with the coverage gate.
test *ARGS:
    uv run pytest {{ ARGS }}

# Fix lint findings and format.
fmt:
    uv run ruff check --fix
    uv run ruff format

# Type-check.
typecheck:
    uv run mypy

# Every hook over the whole tree: CI's lint job.
lint:
    uv run prek run --all-files

# Lint and tests: what CI checks.
check: lint test
