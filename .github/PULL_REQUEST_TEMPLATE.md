## Why

<!--
The diff shows the "what"; this is for the "why" and any non-obvious design
choice the reviewer would otherwise have to reverse-engineer.
-->

## Kind of change

- [ ] Bug fix (non-breaking)
- [ ] Feature (non-breaking, additive)
- [ ] Refactor (no behavioural change)
- [ ] Docs only
- [ ] Chore — build / CI / deps / repo hygiene

## Verification

<!-- How did you check this works? Paste short command output. -->

- [ ] `uv run prek run --all-files` is green locally
- [ ] `uv run pytest` is green, coverage floor included
- [ ] New or changed functions have happy-path **and** error-path tests, each seen failing once with the code broken
- [ ] No decision in [docs/design.md § Decisions](../docs/design.md#decisions) changed *(if one did, link the discussion below)*

## Links

<!-- "Closes #123" / "Follow-up to <commit-sha>". -->

---

By submitting this PR I confirm that I have read [AGENTS.md](../AGENTS.md), [CONTRIBUTING.md](../CONTRIBUTING.md) and the [Code of Conduct](../CODE_OF_CONDUCT.md), and that my contribution is licensed under **GPL-3.0-or-later**.
