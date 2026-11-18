# Contributing to Minutas

## Before you start

Read [AGENTS.md](AGENTS.md) and [docs/design.md](docs/design.md). The design's Decisions table is settled: a change to one is discussed in an issue or a draft PR before any code.

## Bootstrap

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the Python the project pins (`.python-version`) and every development tool: ruff, mypy, pytest, prek and the hook tools are dev dependencies pinned by `uv.lock`.

```bash
git clone https://github.com/jfolcini/minutas
cd minutas
uv sync                 # the virtualenv, the package and the whole toolchain
uv run prek install     # wires the pre-commit, commit-msg, prepare-commit-msg and pre-push hooks
```

### Optional: `just` task runner

The [`justfile`](justfile) wraps the commands below (`just --list`). Nothing needs it; every recipe is one `uv run` line you can type yourself.

## Development workflow

| Task | Command |
| --- | --- |
| Run the tests with coverage | `uv run pytest` |
| Lint, fixing what can be fixed | `uv run ruff check --fix` |
| Format | `uv run ruff format` |
| Type-check | `uv run mypy` |
| Every hook over the whole tree (CI's lint job) | `uv run prek run --all-files` |

- **At commit**, the hooks fix whitespace, formatting and fixable lint findings in place and abort the commit once: re-stage and commit again. They also run mypy, typos, the Markdown, shell and workflow linters, and gitleaks.
- **At push**, the hooks run the test suite with its coverage floor and `pip-audit` over `uv.lock`, and refuse a direct push to `main`.
- **CI** runs the same hooks (`.github/workflows/_validate.yml`), the tests, and a link check over the Markdown.

Coverage is measured with branches, and `pytest` fails under the floor set in `pyproject.toml` (`[tool.coverage.report] fail_under`).

## Patch submission

- Open a pull request against `main`.
- Use a [Conventional Commits](https://www.conventionalcommits.org/) subject (`feat(recorder): …`, `fix(transcriber): …`, `docs: …`, `test: …`, `chore(ci): …`); the commit-msg hook enforces it. The body explains the *why* and any non-obvious choice.
- Keep commits focused. Rebasing and force-pushing your PR branch is fine; on `main` it is not.
- Adding or upgrading a dependency is `uv add <pkg>` (or `uv add --dev <pkg>`), which updates `pyproject.toml` and `uv.lock` together; commit both.

## Licensing and sign-off

Minutas is released under **GPL-3.0-or-later** (see [`LICENSE`](LICENSE)). By submitting a patch you agree to license your contribution under the same terms.

### Developer Certificate of Origin (DCO)

Every commit in a pull request carries a `Signed-off-by:` trailer whose name and email match the commit author, asserting the [Developer Certificate of Origin v1.1](https://developercertificate.org/): that you have the right to submit the change under the project's license. The `dco` check on pull requests enforces it.

With the hooks installed, the `prepare-commit-msg` hook adds the trailer for you. Without them, sign off with `-s`:

```bash
git commit -s -m "feat: your change"
```

If a commit lacks the sign-off:

- The most recent commit: `git commit --amend -s --no-edit && git push --force-with-lease`.
- Earlier commits: `git rebase --signoff <base-sha>`, then force-push.

Never use GitHub's "Update branch" button: its merge commit has no sign-off and fails `dco`. Rebase, or run `git merge --signoff origin/main`.

## Asking questions

Open a draft PR with `[WIP]` in the title, so the conversation lives next to the code.
