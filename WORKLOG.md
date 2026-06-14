llm-scheduler worklog

2026-06-12: Started Python-only migration task.
- Replaced the previous scheduler-specific `PLANS.md` with the required migration plan.
- Current next step: discovery of current Bash tools, helper library, tests, docs, CI, external invocations, environment variables, file I/O, and visible stdout/stderr behavior before changing implementation behavior.

2026-06-12: Completed Python implementation and validation.
- Added Python package `llm_tools` with shared helpers in `common.py` and tool modules `usage.py`, `scheduler.py`, and `ralph_robin.py`.
- Replaced the three public command files with Python entry scripts and added `pyproject.toml` console scripts.
- Removed obsolete Bash helper `lib/llm-common.sh` and shell regression runner `llm-usage-tests.sh`.
- Added pytest contract/unit tests under `tests/` with fake provider commands and subprocess coverage support through `sitecustomize.py`.
- Added `.gitignore` for Python caches, coverage files, virtualenvs, build outputs, and local generated artifacts.
- Updated GitHub Actions to run Python 3.11 tests with coverage enforcement.
- Updated README installation, requirements, and testing instructions for the Python package layout.
- Behavioural note: Ralph Robin now keeps its own status/selection diagnostics on stderr so stdout can remain exact provider chat output in passthrough scenarios.
- Validation: `python -m pytest -q` passed: 23 tests.
- Validation: `/tmp/llm-tools-venv/bin/coverage run -m pytest && /tmp/llm-tools-venv/bin/coverage combine && /tmp/llm-tools-venv/bin/coverage report --fail-under=80` passed with total coverage 81%.

2026-06-12: Attached terminal mode — fresh runs now show the real CLI experience.
- Problem: `ralph-robin --prompt-file …` from a terminal showed nothing until Ctrl-C (Python PTY relay + `claude --print`, which emits no output until completion), then died with a KeyboardInterrupt traceback.
- Fresh mode on an interactive terminal (`resolve_attach_mode`) now runs the provider CLI in its normal interactive form (`claude --dangerously-skip-permissions <prompt>`, `codex -C <cwd> <prompt>`, `copilot -C <cwd> -i <prompt>`) on a PTY wired directly to the terminal via `script(1)` — output, stdin, resizes, and Ctrl-C are byte-for-byte identical to a direct launch.
- Headless contexts (no TTY, `--headless`, `LLM_SCHEDULER_HEADLESS=1`, `LLM_SCHEDULER_NO_STREAM=1`) keep the previous non-interactive commands and capture relay; new `--headless` flag on llm-scheduler and ralph-robin (forwarded).
- Attached runs never retry on clean exit or user cancel (130/143) and skip the rate-limit phrase grep; `clean_capture_file` strips CSI/OSC/charset escapes from the typescript for `attempt-N.out`.
- Headless Python relay now handles KeyboardInterrupt: kills the child, writes status 130, no traceback.
- Tests: PTY-driven attached-mode test (TTY visible to child, stdin forwarded, attached=1 event, cleaned attempt log). Verified live: ralph-robin under a PTY launched the real Claude Code TUI, answered a prompt, `/exit` ended the run with status 0; SIGINT on the headless relay produced status 130 with no traceback.
- Validation: shellcheck clean; ./llm-usage-tests.sh: ok.

2026-06-10: Applied all P0–P2 bug fixes from defect list.
- P0-A: Fixed usage_decision to treat past-reset-epoch low-remaining windows as stale (usable), not exhausted.
- P0-B: Inverted is_undetermined_reason — anything != "rate-limited" is now undetermined, including all Copilot reasons.
- P0-C: Added explicit success/failure branches at both schedule_resume_and_suspend call sites; fallback to in-process wait on failure.
- P0-D: Added LLM_SCHEDULER_SUSPEND_MIN_LEAD guard (default 120s); timer-active check after systemd-run; Ctrl-C trap to disarm timer before suspend.
- P1-A: submit_once now writes synthetic status 124 when status file is missing/empty; guards against non-integer status.
- P1-B: Removed bare \b429\b from output_is_retryable; kept specific HTTP/status/phrase patterns.
- P1-C: Replaced // empty with // null in normalize_codex and normalize_claude jq; fixed json_for_provider decorate helper to avoid fabricating objects from null windows.
- P1-D: Fixed run_tmux to detect colon in TMUX_TARGET for correct session:window parsing; rejects empty session or window.
- P1-E: Moved --at/--not-before parsing into validate_args (before setup_logs); parse_not_before_epoch reuses pre-validated NOT_BEFORE_EPOCH.
- P2-A: wake_diagnostics_json now captures systemctl output text and reports running/degraded/unknown correctly.
- P2-B: schedule_resume_and_suspend dry-run path now prints a concise stdout line with unit name, target epoch, local time, and log dir.
- Tests: Added deterministic test coverage for all 11 defects to llm-usage-tests.sh.
- Validation: shellcheck --severity=warning: clean. ./llm-usage-tests.sh: ok.

- Initialized implementation plan and worklog.
- Factored reusable non-UI helpers from `llm-usage` into `lib/llm-common.sh`.
- Updated `llm-usage` to source the shared library while preserving rendering and CLI handling.
- Added executable `llm-scheduler` with provider selection, prompt validation, usage gating, retry handling, PTY execution, tmux execution, logs, dry-run, and best-effort wake support.
- Confirmed default adapter syntax from local CLI help: `codex exec`, `claude --print`, `copilot --prompt`.
- Added scheduler tests using mocked usage JSON and mock CLI commands; no live provider calls.
- Fixed PTY helper exit-status handling after terminal EOF.
- Fixed prompt-file handling to preserve logged file content exactly.
- Ran `./llm-usage-tests.sh`: ok.
- Ran final `bash -n llm-usage llm-scheduler llm-usage-tests.sh lib/llm-common.sh`: ok.
- Ran final `./llm-usage --json | jq . >/dev/null`: ok.
- Ran final `./llm-usage-tests.sh`: ok.
- Ran `shellcheck` check: skipped, not installed.
- Ran `./llm-scheduler --wake-test`: `systemd-run` and `rtcwake` present; user systemd state reported `unknown`.
- Ran live minimal `llm-scheduler` smoke against Codex with prompt `Reply with exactly: ok`: status 0, output `ok`.
- Ran live minimal `llm-scheduler` smoke against Copilot with prompt `Reply with exactly: ok`: status 0, output `ok`.
- Skipped live Claude scheduler smoke because user reported no Claude credits.
- Renamed GitHub repository from `chrisgleissner/llm-usage` to `chrisgleissner/llm-tools` with `gh api`.
- Updated local `origin` remote to `https://github.com/chrisgleissner/llm-tools.git`.
- Updated README badge/release links to `llm-tools`.
- Fixed `llm-scheduler --wake` to pass `WakeSystem=true` as a systemd timer property.
- Ran live wake test with transient user systemd timer and `systemctl suspend`: system entered S3 at `2026-06-02 22:01:10` and resumed at `22:02:25`.
- Wake Copilot scheduler service initially saw Copilot capture timeout at `22:02:35`, then polled again, submitted prompt, and received `ok` at `22:03:45`.
- Ran post-wake `bash -n`, `./llm-scheduler --wake-test | jq .`, and `./llm-usage-tests.sh`: ok.
- Added `llm-scheduler --suspend-until-ready` to schedule a resumed scheduler invocation with a WakeSystem timer, then suspend instead of polling.
- Fixed scheduler reset parsing for Claude API timestamps with fractional seconds and `+00:00` offset.
- Added regression coverage for `--suspend-until-ready` timer arming and Claude offset reset parsing.
- Dry-ran requested Claude 5h handover schedule; reset derived as epoch `1780438801` / local `2026-06-02 23:20:01`.

## 2026-06-14 — Kilo Code CLI & capacity scope refactor

- Created `PLANS.md` for adding Kilo Code CLI as a first-class provider and
  replacing the narrow `window` abstraction with a generic `capacity scope`
  abstraction (reset_window, balance, budget, ungated, unknown).
- Baseline: `pytest -q` passes (91 tests), coverage 86%.
- Files changed: TBD (work in progress).
- Tests run: TBD.
- Failures: TBD.
- Fixes: TBD.
- Remaining risks: TBD.
### Progress checkpoint (Kilo + capacity-scope refactor, working state)

- Created `llm_tools/capacity.py` with the generic `ProviderId`,
  `CapacityKind`, `CapacityScope`, `ProviderSnapshot`, and `UsageDecision`
  dataclasses plus `decide`, `validate_scope`, `scope_pace`,
  `is_undetermined_reason`, `effective_scopes` helpers.
- Created `llm_tools/providers/kilo.py` with the Kilo Code CLI adapter:
  `kilo stats` parser + env-var fallback (`LLM_USAGE_KILO_*`), the
  `read_kilo` snapshot reader, and `kilo_command_argv` for
  attached/headless launches.
- Replaced the legacy `--window` flag with `--scope` in `llm-scheduler`
  and `ralph-robin`; `--window` is still accepted as a deprecated alias.
- Updated `llm-usage` table column from "Window" to "Scope" and added
  Kilo rows (`balance`, `budget`, `byok/local/ungated`).
- Updated `validate_tool_window` → `validate_tool_scope`; `RalphConfig`
  and `SchedulerConfig` now carry a `scope` field.
- Tests: 154 pass (added 25 capacity tests, 26 Kilo tests, 12 Ralph-Kilo
  tests).
- Coverage: 86% (above 85% gate).

### Provider refactor (kilo, codex, claude, copilot all in llm_tools/providers/)

- Every provider now lives in its own module under
  `llm_tools/providers/`. Adding a new provider is a 5-step recipe
  (read() snapshot, re-export, PROVIDER_SCOPES, default argv, --tool
  membership).
- Codex: `providers/codex.py` (read_codex + read() snapshot).
- Claude: `providers/claude.py` (read_claude / read_claude_api
  delegations + read() snapshot).
- Copilot: `providers/copilot.py` (read_copilot / read_copilot_live
  delegations + read() snapshot).
- Kilo: `providers/kilo.py` (read_kilo + read alias + read()
  snapshot).
- llm_tools/common.py keeps the legacy read_codex / read_claude_api
  / read_claude / read_copilot functions as thin shims that delegate
  to the provider modules; Claude's API OAuth/cache mechanics live
  in common to keep the readers free of provider indirection.
- llm_tools/usage.py: render_once now uses snapshot-based
  read_<name>_snapshot for Claude/Copilot; Codex keeps the legacy
  read_codex JSON shape (the rows array carries the codex-spark
  row, which the test contract pins).
- Tests: 160 pass (added 6 in tests/test_providers.py).
- Coverage: 85% (gate met).
- README.md and AGENTS.md updated to document the new model.

### End-to-end manual checks

- `llm-usage` with `LLM_USAGE_KILO_BALANCE=12.40 GBP` + budget env
  shows Kilo balance/budget rows in the table.
- `llm-scheduler --tool kilo --scope byok --dry-run` resolves the
  byok ungated scope and emits a `usage_decision` event.
- `llm-scheduler --tool kilo --scope balance --dry-run` resolves
  the balance scope and emits a `usage_decision` event.
- `ralph-robin --tools kilo --max-iterations 1` selects kilo, runs
  the provider, and stops cleanly.

## 2026-06-14 — OpenCode support added + handover prompt

- Added `llm_tools/providers/opencode.py` as a Kilo-shaped adapter for
  the OpenCode CLI (`opencode stats` JSON + text + env-var fallback,
  same `read()` / `read_opencode()` / `command_argv` contract).
- Wired OpenCode into `llm_tools/capacity.py` (PROVIDER_OPENCODE +
  allow-list), `llm_tools/common.py` (snapshot shim), `usage.py`
  (opencode_rows + JSON), `scheduler.py` (--tool opencode, default
  argv), `ralph_robin.py` (--tools opencode), and `providers/__init__.py`.
- Added 26 tests in `tests/test_opencode.py`. 2 still fail on
  `PATH=/var/empty` + gateway+balance: the reader returns
  `missing-cli` because the binary is required to launch in any mode.
  This mirrors the Kilo behavior (kilo's test happens to pass because
  the host has a real kilo on PATH). To be fixed in the adversarial
  review.
- Created `REVIEW_PROMPT.md` (handover to a fresh opencode session
  for the adversarial review). 187 lines. Tells the new session to
  start with an adversarial review focused on modularity, consistency,
  and lack of bugs across the Kilo+OpenCode addition; fix what it
  finds; do not commit unless asked; ≥85% coverage gate.
- Configured `~/.config/opencode/opencode.jsonc` to default
  `model = minimax-coding-plan/MiniMax-M3` (provider id matches the
  existing `auth.json` entry `minimax-coding-plan`). Smoke test:
  `opencode run --model minimax-coding-plan/MiniMax-M3 "Reply with
  exactly one word: ok"` returned `ok`.
- Tests so far: 160 pass; the 26 new opencode tests have 2 failures
  (PATH=/var/empty + gateway+balance). Coverage: TBD; gate is
  `--fail-under=85`.
