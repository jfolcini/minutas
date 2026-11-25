# Contributing to Minutas

## Before you start

Read [AGENTS.md](AGENTS.md) and [docs/design.md](docs/design.md). The design's Decisions table is settled: a change to one is discussed in an issue or a draft PR before any code.

## Bootstrap

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the Python the project pins (`.python-version`) and every development tool: ruff, ty, pytest, prek and the hook tools are dev dependencies pinned by `uv.lock`.

```bash
git clone https://github.com/jfolcini/minutas
cd minutas
uv sync                 # the virtualenv, the package and the whole toolchain
uv run prek install     # wires the pre-commit, commit-msg, prepare-commit-msg and pre-push hooks
uv run just ext setup   # the extension's ESLint and TypeScript, from extension/package-lock.json
```

The extension's tools need Node and npm; its tests need GNOME Shell and gjs (`sudo apt install gnome-shell gjs` on Ubuntu 24.04).

The test suite runs the Recordings window for real, on Ubuntu's own PyGObject, GTK and libadwaita and on a display and session bus of its own, never your desktop's, and the recorder's pipeline on GStreamer with a test source: `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base xvfb dbus-daemon`.

### The `just` task runner

`uv sync` installs [`just`](https://just.systems/) into the virtualenv with the rest of the toolchain; run `uv run just --list` (or `just --list` with the venv active) for the recipes in the [`justfile`](justfile). Each recipe is a few `uv run` lines you can also type yourself.

## Development workflow

| Task | `just` | Command |
| --- | --- | --- |
| The inner loop: lint, types, import contracts, related tests | `just check` | |
| Run the tests with coverage | `just test` | `uv run pytest` |
| Every changed line covered | `just cov-diff` | `uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=100` |
| Lint and format, fixing what can be fixed | `just fmt` | `uv run ruff check --fix && uv run ruff format` |
| Type-check | `just typecheck` | `uv run ty check` |
| Module boundaries and declared dependencies | `just imports` | `uv run lint-imports && uv run deptry src` |
| Mutation testing (report only) | `just mutate` | `uv run mutmut run` |
| Render the README's screenshots into `docs/screenshots` | `just screenshots` | `xvfb-run -a -s "-screen 0 2560x5000x24 -nolisten tcp" dbus-run-session -- uv run python scripts/screenshots.py docs/screenshots` |
| Every hook over the whole tree (CI's lint job) | `just lint` | `uv run prek run --all-files` |
| Everything CI checks | `just verify` | |
| Push and confirm the remote has the commit | `just push` | |
| Is a PR ready to merge | `just merge-ready <pr>` | needs `gh` |
| The extension's checks (CI's extension job) | `just ext check` | |
| Lint the extension | `just ext lint` | `cd extension && npx eslint .` |
| Type-check the extension's Shell-free modules | `just ext typecheck` | `cd extension && npx tsc -p .` |
| The extension's unit tests | `just ext test` | `gjs -m extension/tests/status.test.js` |
| Load and drive the extension in a headless GNOME Shell | `just ext smoke [out-dir]` | `bash extension/tests/smoke.sh [out-dir]` |
| Install the extension into your session and enable it (log out once on Wayland) | `just ext install` | |
| Try the installed extension in a GNOME Shell window | `just ext nested` | |
| Follow GNOME Shell's log | `just ext logs` | `journalctl -f -o cat /usr/bin/gnome-shell` |

- **At commit**, the hooks fix whitespace, formatting and fixable lint findings in place and abort the commit once: re-stage and commit again. They also run ty, typos, ESLint on the extension, the Markdown, shell and workflow linters, and gitleaks.
- **At push**, the hooks run the test suite with its coverage floor and `pip-audit` over `uv.lock`, and refuse a direct push to `main`.
- **CI** runs the same hooks (`.github/workflows/_validate.yml`), the tests, the extension's checks, and a link check over the Markdown.

The README's screenshots are the real Recordings window and its Settings, drawn by GTK from made-up recordings that `scripts/screenshots.py` writes into a temporary folder, on a display and session bus of its own: it never reads or writes your recordings, settings or keyring. Run `just screenshots` after a change to what the window shows, and commit the images it rewrites. It needs the window's packages above, and `sudo apt install fonts-cantarell` for GNOME's font.

Coverage is measured with branches, and `pytest` fails under the floor set in `pyproject.toml` (`[tool.coverage.report] fail_under`); on a pull request every changed line must be covered. The tests run in random order (pytest-randomly; rerun a failure with the `--randomly-seed=…` it prints), with a per-test timeout, with every annotation checked at run time by beartype, and with warnings as errors.

### Echo cancellation on speakers

The suite has no PipeWire, microphone or speakers, so whether the echo-cancelled mic keeps the call out of the mic track is checked by hand, after a change to the recorder's echo canceller. It needs only what install.sh installs. First make the speakers the output, unmuted and at the volume of a call, in GNOME's Settings › Sound or with `wpctl status`, `wpctl set-default <sink id>` and `wpctl set-mute <sink id> 0`: the canceller takes out what the default output plays, so headphones left as the output prove nothing. Then, with Echo cancellation on in Minutas's Settings:

```bash
# The far side: any speech, here the oldest recording's call track, picked before this one starts.
speech=$(ls ~/.local/share/minutas/recordings/*/call.opus | head -n 1)
minutas record start
systemctl --user is-active minutas-echo-cancel.service   # active
pw-link --output | grep minutas-echo-cancelled           # the source the mic track records
cd "$(mktemp -d)"
wav="audioconvert ! audioresample ! audio/x-raw,rate=16000,channels=1,format=S16LE ! wavenc ! filesink"
# The plain mic and the echo-cancelled one, over the same 20 seconds.
timeout --signal=INT 20 gst-launch-1.0 -e \
  pulsesrc ! $wav location=plain.wav \
  pulsesrc device=minutas-echo-cancelled ! $wav location=cancelled.wav &
# Say nothing while the far side plays.
sleep 2; timeout 15 pw-play "$speech"; wait
minutas record stop
systemctl --user is-active minutas-echo-cancel.service   # inactive: it stopped with the recorder
python3 - plain.wav cancelled.wav <<'LEVELS'
import array, math, sys, wave

for name in sys.argv[1:]:
    with wave.open(name) as audio:
        samples = array.array("h", audio.readframes(audio.getnframes()))
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    print(f"{name}: {20 * math.log10(max(rms, 1) / 32768):.1f} dBFS")
LEVELS
```

It cancels when `cancelled.wav` is at least 15 dB below `plain.wav` and, played, holds the speech faintly or not at all; the recording's own Microphone track, played from the Recordings window, then sounds as `cancelled.wav` does, and its line doesn't say No echo cancellation. Run it once more talking over the speech: your voice must stay in `cancelled.wav`, about as loud as in `plain.wav`.

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
