# Minutas

Minutas is a GNOME tool that records work meetings, transcribes them with who said what, and publishes them to Agaric and GitLab.

- Read `docs/design.md` before any work.
- Build in the order of its Milestones, starting at M0.
- The Decisions table is settled: ask before changing any decision in it.

## How we work

Read this section first. It outranks everything below it.

- **Be pragmatic. Simplicity is a value in itself.** The smallest change that solves the actual problem wins. Boring beats clever.
- **Climb the ladder before writing.** Stop at the first rung that holds: it does not need to exist; the codebase already does it; the standard library or the platform does it; an installed dependency does it; one plain line does it; only then the minimum that works.
- **Lazy about the solution, never about reading.** Read the code you touch and trace the real path before picking a rung.
- **Lazy, not negligent.** Trust-boundary validation, data-loss handling (a recording is never lost), security, and a test shown red are the floor under the ladder, not rungs on it.
- **Work and code earn their keep.** A guard, test, abstraction, rule, comment or issue exists because it prevents a real problem someone hit. A rule an existing linter provides (ruff, ty) may be turned on preventively when it encodes a best practice and its hits are real; turning it on means burning its infractions down to zero in the same PR.
- **Do not engineer for speculative situations.** "Could happen" is not a defect. Threat model: one user on their own desktop ([SECURITY.md](SECURITY.md)).
- **No gold-plating.** No scope creep, no refactor-because-we-were-here, no helper for a one-off, no option nobody asked for.
- **Reviews judge impact.** A finding needs a concrete failure and a named victim. Otherwise fix it if trivial, or let it go. Over-building is a finding too.
- **Say it once.** A rule and its one reason, in the same breath. An issue number is a pointer, not a justification.
- **Names do the explaining.** A comment says why, never what; when the what needs a comment, rename.
- **No figures that rot.** No line numbers, counts or measurements in comments or docs unless a committed command regenerates them.
- **Prefer deleting.** Removing a hook, a fallback, a config knob or a paragraph needs no more justification than this section.

## Documentation Map

| Document | Purpose |
| --- | --- |
| **AGENTS.md** (this file) | Principles and conventions |
| [docs/design.md](docs/design.md) | The design: decisions, pipeline, components, milestones |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Bootstrap, the checks, patch submission, DCO |
| [.github/AGENTS.md](.github/AGENTS.md) | CI and GitHub configuration: what an edit there can break |

## Layout

| Path | Holds |
| --- | --- |
| `src/minutas/` | The package (src layout; `minutas` console script in `cli.py`) |
| `tests/` | pytest suite, one `test_<module>.py` per module |
| `extension/` | The GNOME Shell extension; its pure logic in `status.js`, tested by `gjs -m extension/tests/status.test.js`; its checks in `extension/justfile` (`just ext`) |
| `scripts/` | Shell helpers the hooks call |
| `install.sh` | Installs and updates Minutas on the desktop, step by step, each skipped when already done (design § Installing and updating) |
| `pyproject.toml` | Metadata, dependencies, and the ruff, ty, pytest and coverage config |
| `prek.toml` | The git hooks, which CI also runs |

## Build Commands

uv manages the Python (`.python-version`), the virtualenv, the dependencies and every tool; `uv.lock` pins them all.

```bash
uv sync                          # install everything
uv run prek install              # wire the git hooks (a cloud session does this at start)
uv run pytest                    # tests + branch coverage, fails under the floor
uv run pytest tests/test_cli.py -k version   # one test
uv run ruff check --fix          # lint
uv run ruff format               # format
uv run ty check                  # type-check, every rule an error
uv run prek run --all-files      # every hook over the tree: CI's lint job
```

The `justfile` wraps these (`uv run just --list`): `just check` is the inner loop (lint, types, import contracts, the tests for what changed), `just verify` is everything CI runs, `just mutate` is mutation testing.

Add a dependency with `uv add <pkg>` (`--dev` for a tool) and commit `pyproject.toml` with `uv.lock`; never hand-edit the lock.

The extension's tools are npm dev dependencies in `extension/package.json`, at exact versions pinned by `extension/package-lock.json`; add one with `npm install --save-dev --save-exact <pkg>` in `extension/`. `uv run just ext setup` installs them (Node and npm); `uv run just ext check` runs ESLint, the type check, the gjs unit tests and the headless GNOME Shell smoke test (`gnome-shell` and `gjs` from apt).

## Code Quality Enforcement

Strict settings are project-wide; do not weaken them.

- ruff: `select = ["ALL"]`; the ignore list in `pyproject.toml` is the deliberate exceptions, each with its reason. Prefer fixing over `# noqa`; a `# noqa` names the rule and says why.
- ty: every rule is an error (`[tool.ty.rules] all = "error"`) over `src` and `tests`; ruff's `ANN` rules require the annotations ty checks. A suppression is `# ty: ignore[<rule>]`, only at an untyped third-party boundary.
- import-linter (`[tool.importlinter]` in `pyproject.toml`): the module layers; a new module takes its place in the contract in the same PR. deptry: every import is a declared dependency and every runtime dependency is imported.
- Python: 3.12 is the floor (Ubuntu 24.04's system Python, which the GTK window will run under).
- Extension: ESLint's recommended rules plus `eqeqeq` and `prefer-const` (`extension/eslint.config.js`). TypeScript `checkJs`, strict, against `@girs/gjs` (`extension/tsconfig.json`), over the Shell-free `status.js` and its test; `extension.js` is left to ESLint and the smoke test, because under strict every field `enable()` sets and `disable()` nulls is "possibly null" everywhere else. A suppression is `// @ts-expect-error: <why>`.

## Testing

- Every public function: happy path + error path.
- Assert exact values (`assert count == 5`), not inequalities.
- Coverage is branch coverage with a floor (`fail_under` in `pyproject.toml`), and CI requires every line a PR changes to be covered (diff-cover); it counts lines run, not behaviour checked, so it is a floor, not the goal.
- Pure logic with a rule over many inputs (merging turns, choosing a name, formatting a transcript, run states) gets a hypothesis property test.
- The suite runs in random order (pytest-randomly), with a timeout per test, with beartype checking every annotation in the package at run time, and with warnings as errors; child Pythons run in development mode (`conftest.py`). A test that only passes in one order or one timing is a bug.
- Mutation testing (`just mutate`, weekly in `.github/workflows/mutation.yml`) reports surviving mutants; a survivor in code you touched gets a test.
- No sleeps; no network; no real microphone, calendar, Agaric or GitLab in unit tests. Fakes sit at the module boundary the design names (recorder, calendar lookup, summarizer, publisher).

### Acceptance is falsification

A test that cannot fail covers nothing. Before calling a test done, break the code it covers, see it go red with the assertion you predicted, and restore it. A crash or import error is not a kill. Run `git diff` before your final message so no mutation ships. Ask of every test: *what production change would redden this?*

## Pre-commit and CI

- **Pre-commit:** `prek.toml` is the list. Formatters and fixers rewrite files and abort the commit once; re-stage and commit again.
- **Pre-push:** the full test suite with the coverage floor, `pip-audit`, and no direct push to `main`.
- **CI:** `.github/workflows/_validate.yml` runs every hook in both stages plus zizmor's online audits and a link check (`lint`), the suite on Python 3.12, the newest Python and the lowest allowed dependency versions with diff coverage on PRs (`test`), the extension's ESLint, type check, gjs unit tests and headless GNOME Shell 46 smoke test (`extension`), and the aggregate `validate-all`. The required contexts on `main` are `dco` and `validate / validate-all`.
- **Claude Code Review:** `claude-code-review.yml` reviews every non-draft PR that changes more than Markdown, and approves or requests changes. It is advisory: nothing requires it. A push does not re-run it; adding the `re-review` label does.
- **Commits:** Conventional Commits subjects and a DCO `Signed-off-by:` on every commit; the hooks enforce both locally, CI on the PR.

### Guards earn their keep

Add a hook or a custom check only when the defect has occurred, nothing already catches it (ruff, ty, a test, another hook), it is fast, and it fails loudly with file, line and fix. Every hook carries a `# WHY:` line directly above it. A guard that never fires on a real defect is a liability: delete it.

## Releases

Maintainer only. Never push a release tag on the maintainer's behalf.
