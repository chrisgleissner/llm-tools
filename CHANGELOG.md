# Changelog

All notable changes to llm-tools are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). llm-tools uses
[semantic versioning](https://semver.org/); the version lives in `pyproject.toml`.
Release notes for each tag are also published on the
[GitHub releases page](https://github.com/chrisgleissner/llm-tools/releases).

## [Unreleased]

Changes on `main` since 1.4.0.

- Distributed Bridge: an optional Rust coordinator (`bridge-coordinator`) lets several machines run missions that are managed from one place.
- Worker service: `llm-bridge worker enroll|status|run|unenroll|install-service` enrols a machine with a coordinator and runs missions on it. Creation, restore, power and root access are separate opt-in enrolment flags that default off.
- Web console: a browser interface served by the coordinator shows missions, tasks, events and remote terminals, with password login and `reset-password` recovery.
- Remote CLI: `llm-bridge --target` and `remote` commands drive a coordinator from the command line, including on Windows.
- Platforms: CI now runs the coordinator and the Python suite on macOS ARM64 and Intel, and the Windows component tests on Windows x86-64 and ARM64. Linux ARM64 and a container image are also built and tested.
- Raspberry Pi Zero 2 W is a verified coordinator target, with a documented profile, memory and storage-wear measurements, and a container limit test.
- Backup and restore of coordinator data, with a serving test and recovery documentation.
- `llm-bridge mission delete` removes a finished mission and replicates the deletion to the coordinator.
- Authentication for the coordinator and worker connections, with certificate trust instructions.
- Fixes: native stop status, worker write timeouts, a worker cancel race, coordinator memory writes, a first-run folder-trust hint, a tmux review hint and a console navigation race.
- Documentation: the Bridge operations guide is rewritten around operator tasks, and install and distributed guides are new.
- Test suite: parallel runs with pytest-xdist, tmux start and soak flake fixes, soak and fault-injection harnesses, and a specification status register.
- Release hygiene: website install commands and README platform claims are checked against `pyproject.toml` and the CI job names; Rich is documented as the only runtime dependency.

## [1.4.0] - 2026-11-05

This release adds team guidance and a token-spend setting for new `llm-bridge` missions, a live Agents view, and a way to put the computer to sleep until provider credits recover. It also fixes four problems that affected unattended missions. No command was removed, and the database schema is unchanged (still version 10).

Upgrading:

```bash
pipx install --force git+https://github.com/chrisgleissner/llm-tools.git@1.4.0
```

A running mission keeps the old code until its controller is restarted. To switch it to 1.4.0, run `llm-bridge stop --mission ID` and then `llm-bridge start --mission ID`. `stop` waits for the current provider run to exit and does not cancel the mission. `start` can use paid provider capacity again. New missions default to team guidance on and token spend Medium. Existing missions keep their original prompts until you change the settings with `llm-bridge mission set`.

### Added

- Team guidance and token spend. New missions ask the provider to use its own sub-agents when it supports them and the work benefits. Token spend (Low, Medium, High) is a preference for how much extra reasoning and review is requested. It does not weaken acceptance criteria or the verifier. Turn it off or change it with `--teams off` and `--token-spend low|medium|high` on `mission create` or `mission set`, or with `teams` and `token_spend` under `[bridge]` in `~/.config/llm-tools/config.toml`. The guidance adds 104-106 words to each task prompt.
- Agents view. Press F9 in the console, or run `llm-bridge agents --mission ID`. Each row shows age, model, effort, status and last observed activity. Only Codex shows child agents. Other providers show only the Bridge root.
- Sleep until credits recover. `llm-bridge suspend-until-credits --mission ID` (or the Actions view entry) arms a wake timer for the earliest reset and suspends the computer. It refuses when there is no known future reset time. To suspend automatically when every provider is rate limited, use the opt-in `llm-bridge mission set --mission ID --suspend-when-rate-limited on`. Bridge does not suspend if the wake cannot be armed. Other tools that run critical work, such as firmware flashing, must hold an OS sleep inhibitor.
- Retrospective logs. Bridge records structured events (provider results, capacity observations, power events) so an unattended run can be reconstructed later.
- Usage colours in Bridge. The Usage view keeps the cell colours of `llm-usage`. `NO_COLOR` and `LLM_USAGE_NO_COLOR` still turn colour off.
- README: a diagram of what Bridge, Ralph, the provider CLI and the agent adapters each own.

### Fixed

- After every provider was blocked, the wait now arms a wake and suspends when the reset time is known. Before, it only logged "suspending".
- Quota failures no longer count against the provider-failure limit, so they no longer leave a task blocked.
- A saved command template now applies only to the providers it was written for. Before, a `codex exec` template kept running Codex after the rotation changed to Claude.
- A capacity wait now ends when the rotation changes or a new runnable task is added.

### Known limitations

- The all-blocked suspend path is covered by tests with a simulated power backend. It has not yet been run as a real suspend.
- Token overhead of team guidance was measured only in prompt bytes and words, not tokens.
- Agent map coverage depends on what each provider exposes. Today only Codex child agents are shown.
- Sleep and wake depend on your hardware, firmware and OS. Linux with systemd is the primary tested platform.

## [1.3.0] - 2026-10-31

This release considerably improves `llm-bridge` with review-controlled completion, a clearer terminal UI, stronger provider safeguards, and faster commands.

### Added and changed

- Review before completion. Tasks enter `VERIFYING` after a successful provider exit and complete after your approval or a passing configured verifier. Native missions pause before the next task unless `--auto-continue` is enabled.
- Easier navigation. A new home screen, mission picker, and eight console views provide access to mission status, actions, and usage.
- Stronger safeguards. Folder trust requires explicit approval unless `--trust-folder` is set. Provider credentials are isolated, headless prompts use stdin, and modified verifiers are blocked pending approval.
- Better reliability. Fixed all 19 defects found by fuzz and PTY chaos testing, added clean terminal-hangup handling, and prevented oversized prompts from leaving missions stuck.
- Faster commands. `--version` drops from 0.16 s to 0.04 s, with fewer tmux processes and fewer console refreshes.
- Faster usage reporting. The first `llm-usage` table appears in 3.2-3.8 s, previously 17.6-18.9 s. Output formats and `--watch` behaviour are unchanged.
- A new website and a manually verified first-run guide cover installation through mission completion.

Performance figures were measured on the author's machine. Validation passed 3,674 tests with 92% coverage; CI covers Python 3.11 and 3.15.

### Upgrade notes

- Back up before upgrading if you may downgrade. The database migrates from schema 6 to 10, which 1.2.1 cannot read. With no mission running, copy `bridge.sqlite3` from `${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge` or `$LLM_BRIDGE_STATE_DIR`.
- Bridge now prints text by default. Scripts parsing command output must add `--json`.
- Task selection changed. `--task N` uses the per-mission number; `--task-id ID` uses the global ID.
- Use `cancel` to cancel missions. `stop` no longer cancels them.
- New statuses: 76 means folder trust was declined; 78 means launch was refused.

### Limitations

Linux is the primary tested platform. Native and Background missions require tmux; Headless missions do not. Exact session restoration is verified live for Codex only. Bridge does not sandbox providers, and verifiers establish only what their checks cover.

## [1.2.1] - 2026-10-29

### Changed

- Improve `llm-bridge` shortcut wordings.

## [1.2.0] - 2026-10-29

LLM Bridge now keeps the native provider terminal primary, with lossless large-prompt composition and recovery, full-source Markdown viewing, integrated offline help, and clearer supervision controls.

- Exact UTF-8 prompt entry, transactional paste and private recovery; existing larger file imports remain supported.
- Provider-first focus/zoom, complete native input, mouse/scrollback fallback, task/state status and full-row mode.
- Searchable objective/task/help/artifact documents, exact export/copy and explicit follow mode.
- Durable, target-aware actions and truthful pause/active/verification feedback.

Validation: 1,426 tests; 89% overall coverage and 90% Bridge/terminal coverage; green Python 3.11/3.15 and Codecov checks. Normal installed-wheel Markdown/help and deliberate renderer fallback were verified outside the checkout.

## [1.1.0] - 2026-10-27

LLM Bridge adds durable terminal supervision for engineering missions across provider limits and controller restarts.

- Persistent missions, priority/dependency task queues, explicit proposal approval, revisioned steering and evidence-based verification.
- Private native tmux terminals, keyboard/mouse focus controls, exact-client station switching, safe stop/recovery and owned-session restoration.
- Full embedded Usage, model-scope forecasts, bounded logs and indexed history while reusing Ralph, scheduler and usage ownership.
- Actual terminal screenshot, dogfood improvements, complete operations documentation and independent specification review.

Validation: 1,262 tests pass on Python 3.11 and 3.15; CI retains global 85% coverage and separately enforces Bridge/terminal 90%. Provider model-selection clicks and paid Claude/Copilot completion/restoration remain unverified.

## [1.0.0] - 2026-10-09

### Fixed

- Claude usage disappearing from watch mode when the usage API returns HTTP 429 with `Retry-After: 0`.

### Changed

- Fresh Claude usage samples are shared across watch clients, the sampler, and scheduler to avoid duplicate requests.
- Concurrent reads are coordinated and a cooldown of at least 60 seconds is persisted; longer server retry delays are honoured without blocking the dashboard.
- Cached quota readings stay bounded by their freshness limits and recover automatically after throttling ends.
- The Python package version is 1.0.0 so the wheel and source archive match the release version.

Validation: 780 regression tests passed; total coverage 88% (85% required).
