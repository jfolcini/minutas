# Minutas

Minutas records a work meeting on GNOME, transcribes it with who said what, and files it in [Agaric](https://github.com/jfolcini/agaric) as a meeting page; a summary and a GitLab comment or issue are one click away. Everything but the summary runs on your own machine.

**Status:** early development. Nothing is usable yet; the build order is in [docs/design.md § Milestones](docs/design.md#milestones).

**Install** on Ubuntu 24.04 or later: clone the repository and run `./install.sh`; to update to the latest release, run `minutas update`.

## Documentation

| Document | Purpose |
| --- | --- |
| [docs/design.md](docs/design.md) | The design: decisions, pipeline, components, milestones |
| [AGENTS.md](AGENTS.md) | Principles and conventions for anyone, human or agent, changing the code |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setting up, the checks, sending a patch |
| [SECURITY.md](SECURITY.md) | Reporting a vulnerability |

## License

[GPL-3.0-or-later](LICENSE).
