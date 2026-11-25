# Security Policy

## Threat model

Minutas runs on one person's own desktop. It records meeting audio, transcribes it locally, and sends content off the machine only when that person presses a button: the summary prompt to the summarizer chosen in Settings, the Devin CLI or Claude Code, the page, audio and transcript to Agaric on the same machine, and the summary, transcript and audio to the GitLab server they name. Its secrets are an Agaric agent token and a Hugging Face token, both kept in the GNOME keyring; GitLab credentials stay with `glab`.

## In scope

- Meeting content or a token leaving the machine other than through the action the user pressed.
- A token written anywhere but the keyring (a log, a run's Details, a file in the recording folder).
- Command or argument injection through a meeting name, a speaker name, a calendar field or a GitLab link.
- A supply-chain weakness in this repository's own build and CI.

## Out of scope

- An attacker who already runs code as the user or has the user's unlocked session.
- What Agaric, GitLab, `glab`, the Devin CLI, Claude Code or the models do with content once Minutas has handed it over; report those upstream.

## Supported versions

Minutas is pre-release. Only the latest commit on `main` is supported.

## How to report

**Do not open a public issue or pull request for a security finding.** Report it privately through a [GitHub Security Advisory](https://github.com/jfolcini/minutas/security/advisories/new).

Please include the affected commit, a reproduction, and your assessment of the impact.

## What to expect

- **Acknowledgement** within 7 days.
- **Triage** (in scope, out of scope, duplicate) within 14 days.
- For an in-scope report, a fix or a mitigation plan within 30 days where feasible, then a published advisory crediting the reporter unless they ask otherwise.

## Automated coverage

Every pull request runs gitleaks, ruff's security rules (bandit's `S` set), ty with every rule an error, zizmor over the workflows, and `pip-audit` over `uv.lock`. CodeQL and the OpenSSF Scorecard run weekly. Dependabot proposes dependency and Actions updates after a seven-day cooldown.

## Supply chain

- `install.sh` downloads uv's release archive from GitHub and unpacks it only when it matches the sha256 pinned beside uv's version. uv then installs only what `uv.lock` pins, checking each package against its hash there, and checks the Python builds it downloads itself.
- The models come from the Hugging Face commits `install.sh` pins.
- Every GitHub Action is pinned to a commit SHA, and the extension's tools are exact versions that `npm ci` installs from `extension/package-lock.json`, checking their integrity hashes.
