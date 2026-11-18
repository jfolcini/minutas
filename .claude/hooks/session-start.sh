#!/usr/bin/env bash
# Cloud sandbox bootstrap for Claude Code: install the toolchain and wire the
# git hooks, so a fresh clone commits and pushes through the same checks as a
# local one. Local sessions already have their toolchain and skip this.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"
uv sync --frozen
uv run --frozen prek install
