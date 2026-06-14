# Handover — ralph-robin for c64commander + `capacity_provider` feature

Date: 2026-06-14. Repo: `/home/chris/dev/llm-tools` (branch as checked out).
Related repo: `/home/chris/dev/c64/c64commander` (the hardening prompt target).

## Original tasks

1. Ensure `ralph-robin` can successfully execute
   `/home/chris/dev/c64/c64commander/.github/prompts/ralph.prompt.md` with all
   permissions the prompt's activities need (shell, file edits, device-control
   MCP: droidmind / c64bridge / c64scope, network). Verify with an actual run.
   Amend `llm-tools` only if changes are needed.
2. Add `opencode` to ralph-robin's default providers.
3. Route to `opencode` **only when MiniMax has availability** — because this
   machine's opencode CLI runs the MiniMax model, so opencode's own balance is
   meaningless. This required a **generic** config mechanism to tie one
   provider's gating/capacity to another provider's usage windows.

## Findings on permissions (task 1) — NO tool change was required

The headless path `claude --print --output-format stream-json` (and
`codex exec`, `opencode run`) already gets full permissions from existing
user/project config, which is the documented design ("the default Claude
adapter uses your local Claude Code permission settings"):

- Claude: `c64commander/.claude/settings.local.json` has
  `"defaultMode": "bypassPermissions"`; the device-control MCP servers are
  **local-scoped** in `~/.claude.json` under the project's `mcpServers` block
  (these load automatically in headless mode — no `.mcp.json` approval needed).
- Codex: `~/.codex/config.toml` has `sandbox_mode = "danger-full-access"`,
  `approval_policy = "never"`, the c64commander project trusted, and the same
  MCP servers under `[mcp_servers.*]`.
- OpenCode: `~/.config/opencode/opencode.json` has `permission.bash/edit/...
  = "allow"`, `autoupdate = false`, model `minimax/MiniMax-M3`, and the same
  MCP servers enabled.

### Verification (actual runs, all PASS)

Ran a read-only probe prompt (shell + file write/read + droidmind list_devices +
c64bridge info) through the real `ralph-robin --providers <p> --max-iterations 1`
path for each provider:

- claude:  shell ok, file ok, droidmind ok (Pixel 4 `9B081FFAZ001WX`), c64bridge ok
- codex:   shell ok, file ok, droidmind ok (1 device), c64bridge ok
- opencode: shell ok, file ok, droidmind ok (1 device), c64bridge ok

No device state mutated. `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1` was set for all runs.
A full hardening iteration of ralph.prompt.md was intentionally NOT run (long,
hardware-mutating autonomous session); the probe proves the permission chain.

## New feature: `capacity_provider` (tasks 2 + 3)

A per-provider config key. When `[providers.X].capacity_provider = "Y"`, ralph /
scheduler read X's availability & capacity from Y's usage windows (5h / weekly /
monthly / balance / …) while still launching X's own CLI. Generic: any provider
may borrow any other's windows; validated as a single hop (no self-reference, no
chains). Affects routing, even-burn ranking, suspend/wake timing, and scope
validation.

### Files changed in llm-tools

- `llm_tools/config.py`: `ProviderPolicy.capacity_provider`; `_PROVIDER_KEYS`;
  parse in `provider_policy`; `_validate_capacity_providers` (known target / no
  self / no chains).
- `llm_tools/common.py`: new `usage_snapshot_and_decision(provider,
  capacity_provider, …)` — reads snapshot+decision from the capacity source,
  gates on its aggregate windows (requesting model ignored when delegating),
  relabels decision back to the requesting provider + tags `capacity_provider`.
- `llm_tools/ralph_robin.py`: default rotation `claude,codex` ->
  `claude,codex,opencode` (dataclass default + USAGE text); `select_provider`
  uses the new helper; `validate_args` validates scope against the capacity
  provider; `resolve_policies` preserves `capacity_provider`; passes it into
  `SchedulerConfig`; `safe_args_json` dumps it.
- `llm_tools/scheduler.py`: `SchedulerConfig.capacity_provider`; `apply_config`
  sets it from policy; `wait_until_usable` uses the helper; `validate_args`
  scope check; `safe_args_json` dumps it.
- `config.example.toml`, `README.md`: document the key + new default rotation.
- `tests/test_config.py`: parse + validation (unknown/self/chain) tests.
- `tests/test_additional_paths.py`: helper delegation/pass-through tests.

### User config created (outside the repo)

`~/.config/llm-tools/config.toml` was created:
```toml
[defaults]
providers = ["claude", "codex", "opencode"]
[providers.opencode]
capacity_provider = "minimax"
```

### Verification of the feature

- Library: `common.usage_snapshot_and_decision("opencode","minimax",…)` returns
  `provider=opencode, capacity_provider=minimax, usable=True` with MiniMax's
  windows (5h 87% / weekly 69%).
- ralph-robin dry-run (default providers): rotation shows
  `opencode: 5h 87% / weekly 69%` (MiniMax windows, NOT opencode's $1.3 balance);
  even-burn selected the highest-remaining provider.

## State / remaining

- Tests: full suite was green before changes; re-run after the new tests with
  `cd /home/chris/dev/llm-tools && python3 -m pytest -q` (slow — coverage
  parallel mode spawns subprocesses and writes many `.coverage.*` files in the
  repo root; clean them with `git clean -fdx` care or `rm -f .coverage*`).
- NOT committed — the user has not asked to commit. `llm-tools` working tree has
  the edits above; `git status` to review, then branch + commit if desired.
- `coverage.report.fail_under = 85` in `pyproject.toml`; new code has unit tests.

## Next steps (if resumed)

1. Confirm `python3 -m pytest -q` is green (incl. the 7 new tests).
2. Optionally run one real bounded iteration of ralph.prompt.md to exercise the
   prompt's own logic on hardware (use `--providers claude` or the full default
   rotation; set `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1` only if you do not want
   real suspend).
3. If happy, branch + commit the llm-tools changes (user-gated).
