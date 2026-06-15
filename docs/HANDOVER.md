# Handover — Kilo Code for ralph-robin + PR #7 convergence

Date: 2026-06-14. Repo: `/home/chris/dev/llm-tools`.
Branch: `fix/opencode-ralph-robin` → PR [#7](https://github.com/chrisgleissner/llm-tools/pull/7)
(`fix: use opencode --dir for headless runs`).

## Original tasks (this session)

1. Converge PR #7 to merge-ready (`/pr-converge`): address review comments,
   resolve threads, get all CI checks green.
2. Make sure **Kilo Code** works with `ralph-robin`. Hypothesis: it may need a
   fix similar to the OpenCode one. "Do your own checks."

## Task 1 — PR #7 convergence (DONE)

The branch's substantive change (commit `c9cfc8f`) switched the headless
OpenCode invocation from an invalid `-C <cwd>` flag to `opencode run --dir
<cwd> <prompt>`, and deliberately injects **no** permission-bypassing flag
(`--dangerously-skip-permissions` was rejected — see the resolved Copilot
thread; permissions are left to the user's OpenCode `permission` config).

State on arrival:

- Both Copilot review threads were already **resolved** with owner responses.
- `test` jobs ×2 green, `codecov/project` green, **`codecov/patch` failing**
  (50%, 1 uncovered line: the new headless `opencode` branch of
  `provider_default_argv` in `scheduler.py`).

Fix (commit `9e3a538`): added an assertion in
`tests/test_coverage_boost.py::test_provider_default_argv_kilo_and_opencode_cwd_handling`
covering the headless OpenCode argv. After push, **all four checks went green**
(`codecov/patch` 1s pass), `mergeStateStatus: CLEAN`, no unresolved threads.

> NOTE: commits landed after `9e3a538` (`8fae5b1` capacity_provider feature,
> `660a6b7` remove old HANDOVER, plus the Kilo fix below) so PR #7 should have
> its CI re-confirmed green before merge.

## Task 2 — Kilo Code for ralph-robin (DONE)

### Findings (verified against the real `kilo` CLI, v7.3.45)

Kilo Code is an **OpenCode fork**. `kilo run` is the headless entry point.
`kilo run --help` shows the relevant flags: `--dir` (directory to run in),
`--auto` ("auto-approve all permissions (for autonomous/pipeline usage)"), and
`--dangerously-skip-permissions`.

- Unlike the OpenCode `-C` bug, the old default `kilo run --auto <prompt>` was
  **not broken** — verified it actually launches the agent.
- But `--auto` auto-approves **all** permissions, which **overrides** the
  user's own `~/.config/kilo/kilo.jsonc` `permission` block. That config
  deliberately sets `bash/read/edit/skill/task = "allow"` **and**
  `external_directory."*" = "ask"`. `--auto` blows past the `ask` gate the
  user intentionally set.
- This is exactly the "the framework must not mandate a permission-bypass
  flag; leave it to the user's config" principle applied to OpenCode in this
  same PR.

### Fix — mirror OpenCode exactly

Headless Kilo now uses `kilo run --dir <cwd> <prompt>` and injects **no**
permission flag. Permissions are governed by the user's Kilo config. Because
chris's config already allows bash/read/edit/etc. within the project,
autonomous `ralph-robin` runs still work, while honoring the
`external_directory: ask` boundary. Attached/interactive Kilo is unchanged
(`kilo run <prompt>`; relies on the subprocess cwd) — not the ralph-robin path.

A user who wants full auto-approval can still opt in per run via
`--command-template "kilo run --auto {prompt}"` or by editing their kilo config.

### Files changed (Kilo fix)

- `llm_tools/providers/kilo.py` — `kilo_command_argv`: headless now
  `["kilo", "run", "--dir", cwd, prompt]`; docstring rewritten to match
  OpenCode's (explains the no-permission-flag decision).
- `llm_tools/scheduler.py` — `provider_default_argv` kilo branch →
  `["kilo", "run", "--dir", cfg.cwd, prompt]`; module docstring updated
  (`kilo run --auto` → `kilo run --dir`).
- `README.md` — "Default Provider Commands" table, Kilo headless cell →
  `kilo run --dir <cwd> <prompt>`.
- `tests/test_kilo.py`, `tests/test_coverage_boost.py`,
  `tests/test_additional_paths.py` — updated expected argv.

### Verification

- `tests/test_kilo.py` — all 28 pass.
- Pure argv assertions (`test_kilo_command_argv_attached/headless`,
  `test_provider_default_argv_kilo_and_opencode_cwd_handling`,
  `test_additional_paths.py::test_parser_option_coverage`) — pass.

## Environment gotchas (not code issues)

- A diagnostic `kilo run --help` probe spawned a **runaway `kilo` process
  (~99.8% CPU for 20+ min)** that starved the sandbox and made pytest appear to
  hang. Killed it (the VSCode `kilo serve` processes were left alone). If
  pytest crawls again, check `ps aux --sort=-%cpu` for a stray `kilo`.
- Some subprocess-spawning tests in `tests/test_additional_paths.py` (they
  launch real `ralph-robin --max-duration 3` children) can hang under sandbox
  load. They pass in CI and passed earlier this session — environmental, not
  the Kilo change.

## Open items / next steps

1. Re-confirm PR #7 CI is green after the Kilo commit, then merge.
2. Confirm with the user that dropping `--auto` for Kilo matches intent (it
   mirrors the OpenCode `--dangerously-skip-permissions` decision). Reversible
   via `--command-template` or kilo config.
3. Optional: the README "Default Provider Commands" table still omits OpenCode
   and MiniMax columns — could be completed for parity.
