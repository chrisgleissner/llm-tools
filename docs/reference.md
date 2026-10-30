# Reference

Back to the [README](../README.md).

## Install paths

Primary route: `pipx install git+https://github.com/chrisgleissner/llm-tools.git`.

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
brew install pipx    # macOS alternative
```

Open a new shell, then check the commands:

```bash
command -v llm-usage llm-scheduler ralph-robin llm-sleep-soak llm-bridge
llm-bridge --version
llm-bridge doctor     # tmux, provider CLIs and sign-in, with a Next line
```

If `command -v` prints nothing, the pipx bin directory is not on your `PATH` yet; pipx prints the directory when it installs, and `pipx ensurepath` adds it for new shells. The first Bridge mission is described in the [README](../README.md#your-first-bridge-mission).

Other routes: a release wheel (`pipx install <wheel-url>` from the [releases page](https://github.com/chrisgleissner/llm-tools/releases)), a local checkout (`pipx install .`), or running the scripts straight from a checkout (`./llm-usage`, `./llm-scheduler`, `./ralph-robin`, `./llm-sleep-soak`, `./llm-bridge`). pipx keeps the package in its own virtual environment and works on externally managed Python installations such as Debian, Ubuntu and Homebrew Python.

## Quick reference

```bash
llm-usage --watch 60
llm-usage --service-install
llm-usage --service-status
llm-usage --service-stop
tail -f ~/.cache/llm-tools/llm-scheduler/logs/latest/run.log
```

Supported providers: Codex, Claude Code, GitHub Copilot, Kilo Code, MiniMax, OpenCode and Z.ai. The tools are local- and CLI-first: they use the provider CLIs you already have authenticated and add no authentication layer.

## Requirements

* Linux or macOS.
* Python 3.11 or newer.
* `tmux` for Bridge native and station modes. Optional for `llm-scheduler --tmux`.
* Optional: `systemd-run` or `rtcwake` for wake support.

Wake and suspend features (`--wake`, `--suspend-until-ready`) require Linux with systemd. The tool never modifies BIOS/UEFI settings and never silently requires `sudo`. Wake reliability depends on firmware, motherboard RTC support, the kernel, systemd user timers, and power state — run `llm-scheduler --wake-test` for diagnostics. Everything else works on Linux and macOS.

## Limitations

* Uses local data and locally authenticated CLIs only. Not an official billing dashboard.
* A cell with nothing to report is left blank. `unavailable` appears only when a read failed, and a `reason` explains why in `--json`.
* If usage stays unavailable beyond `--max-unavailable-wait`, the scheduler launches optimistically and lets provider rate-limit handling and retry behavior take over.
* Provider local data formats and CLI syntax can change.
* Copilot AI credits are parsed when requested, but scheduler gating uses monthly remaining usage.

## Tests

```bash
python -m pip install -e . pytest coverage
coverage run -m pytest
coverage combine
coverage report --fail-under=85
```

Tests use fixtures and mock commands. They do not require real provider installations, credentials, network access, or the user's real home directory. For manual end-to-end checks, run the examples above against installed and authenticated providers.

## Contributing

Small, focused pull requests are welcome. Before opening a PR, make sure total coverage is at or above `85%`:

```bash
coverage run -m pytest && coverage combine && coverage report --fail-under=85
```

Adding a new provider follows a small adapter contract — see the provider-adapter steps and implementation map in [AGENTS.md](../AGENTS.md).

## Environment variables

Every test- and user-facing variable is catalogued in [AGENTS.md](../AGENTS.md#environment-knobs).
