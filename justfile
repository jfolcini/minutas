# Task shortcuts. `just` comes with the toolchain (`uv sync` installs it into
# .venv; run it as `uv run just …` or with the venv active). Every recipe is a
# few `uv run` lines: nothing needs `just`.

set shell := ["bash", "-euo", "pipefail", "-c"]

# The base a branch is compared against.
base := "origin/main"

# The GNOME Shell extension's recipes: `just --list ext`.
mod ext 'extension/justfile'

# List the recipes.
default:
    @just --list

# --- Setup -------------------------------------------------------------------

# Install the toolchain and wire the git hooks.
setup:
    uv sync
    uv run prek install

# Run the minutas command from this checkout.
run *ARGS:
    uv run minutas {{ ARGS }}

# --- Inner loop --------------------------------------------------------------

# The fast inner loop: lint, format check, types, import contracts, and the tests the change can break.
check: lint-py typecheck imports test-related

# ruff lint and format check, no fixes.
lint-py:
    uv run ruff check
    uv run ruff format --check

# Fix lint findings and format.
fmt:
    uv run ruff check --fix
    uv run ruff format

# Type-check (ty, every rule an error).
typecheck:
    uv run ty check

# Module boundaries (import-linter) and declared dependencies (deptry).
imports:
    uv run lint-imports
    uv run deptry src

# Run the tests with the coverage gate; ARGS go to pytest (`just test -k version`).
test *ARGS:
    uv run pytest {{ ARGS }}

# The tests for what changed since the base (committed or not); the whole suite when a change has no test file of its own.
test-related:
    #!/usr/bin/env bash
    set -euo pipefail
    mapfile -t changed < <({ git diff --name-only {{ base }}...HEAD; git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u)
    tests=()
    for path in "${changed[@]}"; do
        case "$path" in
            tests/test_*.py) [ -f "$path" ] && tests+=("$path") ;;
            src/minutas/*.py)
                test="tests/test_$(basename "$path")"
                if [ -f "$test" ]; then tests+=("$test"); else tests=(); break; fi ;;
        esac
    done
    if [ "${#tests[@]}" -eq 0 ]; then
        echo "test-related: running the whole suite"
        uv run pytest
    else
        mapfile -t tests < <(printf '%s\n' "${tests[@]}" | sort -u)
        uv run pytest --no-cov "${tests[@]}"
    fi

# Every line changed since the base is covered (CI's diff-cover step).
cov-diff:
    uv run pytest --cov-report=xml
    uv run diff-cover coverage.xml --compare-branch={{ base }} --fail-under=100

# Mutation testing, report only; PATTERN is a mutant glob (`just mutate 'minutas.store*'`).
mutate PATTERN="":
    uv run mutmut run --max-children 4 {{ PATTERN }}
    uv run mutmut results

# Browse the surviving mutants of the last `just mutate`.
mutants:
    uv run mutmut browse

# Render the README's screenshots of the window into docs/screenshots, from made-up recordings (needs xvfb).
screenshots:
    xvfb-run -a -s "-screen 0 2560x6400x24 -nolisten tcp" dbus-run-session -- uv run python scripts/screenshots.py docs/screenshots

# --- Outer loop --------------------------------------------------------------

# Every hook over the whole tree: CI's lint job.
lint:
    uv run prek run --all-files

# What CI checks: every hook in both stages, the whole suite, and the extension's checks.
verify:
    uv run prek run --all-files
    SKIP=no-commit-to-branch uv run prek run --all-files --hook-stage pre-push
    uv run just ext check

# Push the current branch (the pre-push hooks run) and confirm the remote has this commit.
push *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    branch=$(git branch --show-current)
    git push -u origin "$branch" {{ ARGS }}
    remote=$(git ls-remote origin "refs/heads/$branch" | cut -f1)
    if [ "$remote" != "$(git rev-parse HEAD)" ]; then
        echo "push: origin/$branch is '${remote:-missing}', not HEAD $(git rev-parse --short HEAD)" >&2
        exit 1
    fi
    echo "push: origin/$branch is at $(git rev-parse --short HEAD)"

# Open a pull request for the current branch (gh), as a draft.
pr *ARGS:
    gh pr create --base main --draft --fill {{ ARGS }}

# Is PR mergeable: required checks green on its head, no conflict, and which of its files moved on main.
merge-ready PR:
    #!/usr/bin/env bash
    set -euo pipefail
    gh pr view {{ PR }} --json headRefOid,baseRefName,mergeable,mergeStateStatus,isDraft \
        --jq '"head \(.headRefOid[:8])  base \(.baseRefName)  mergeable \(.mergeable)  state \(.mergeStateStatus)  draft \(.isDraft)"'
    gh pr checks {{ PR }} --required
    head_sha=$(gh pr view {{ PR }} --json headRefOid --jq .headRefOid)
    git fetch -q origin main "pull/{{ PR }}/head"
    base_sha=$(git merge-base origin/main "$head_sha")
    moved=$(comm -12 <(gh pr diff {{ PR }} --name-only | sort) <(git diff --name-only "$base_sha" origin/main | sort))
    if [ -n "$moved" ]; then
        echo "These files of the PR changed on main since its base; merge main in and let CI re-run:"
        echo "$moved"
        exit 1
    fi
    echo "No file of the PR changed on main since its base."

# Remove caches and build output.
clean:
    rm -rf .pytest_cache .ruff_cache .hypothesis htmlcov coverage.xml .coverage .coverage.* mutants build dist
