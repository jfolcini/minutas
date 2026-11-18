# CI and GitHub configuration

Workflows, Dependabot, zizmor and CodeQL config. What runs where is in root [`AGENTS.md`](../AGENTS.md) § Pre-commit and CI and the header of [`workflows/_validate.yml`](workflows/_validate.yml). This file holds what an edit here can break.

## The gate

- **The required context is the check run's full name, `validate / validate-all`,** because `ci.yml`'s `validate` job calls `_validate.yml`. A job that must gate goes into `validate-all`'s `needs`; a job left out gates nothing. `validate-all` runs `if: always()` and fails on any result but `success`, a skipped job included.
- **Never gate a required workflow with `on: paths:`.** A workflow skipped by `paths` reports no check run, and a required context that never reports blocks the merge forever.
- **DCO:** every PR commit carries a `Signed-off-by:` matching its author. GitHub's "Update branch" makes an unsigned merge commit; rebase or `git merge --signoff origin/main` instead.

## Conventions

- **Pins:** every `uses:` is a full commit SHA with the version in a trailing comment; Dependabot moves both. Tools come from `uv.lock` through `uv sync`, and the extension's from `extension/package-lock.json` through `npm ci`, never a second install in a workflow.
- **Permissions:** `contents: read` at the top of every workflow; a job that needs more says so on the job.
- **Checkout:** `persist-credentials: false` on every checkout.
- **Timeouts:** every job sets `timeout-minutes`.
- **Runner slots are shared** by every workflow and PR on the account. A check that is a few lines of shell is a step of an existing job, not a job of its own.
- `actionlint` and `zizmor` run as hooks on every workflow edit; a zizmor finding is fixed, not suppressed, unless `zizmor.yml` says why.
