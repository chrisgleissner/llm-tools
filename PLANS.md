llm-scheduler bug-fix plan

## Defect task list

- [x] P0-A: Post-wake deadlock in `usage_decision` — stale past-reset windows treated as exhausted
- [x] P0-B: Copilot / unknown reasons misclassified — `is_undetermined_reason` allowlist too narrow
- [x] P0-C: Silent death when `schedule_resume_and_suspend` fails — no explicit fallback at call sites
- [x] P0-D: Unsafe suspend scheduling — no min-lead guard, no timer-active check, no Ctrl-C cleanup
- [x] P1-A: `submit_once` dies on missing/empty status file
- [x] P1-B: Retry detection too broad — bare `\b429\b` matches innocent output
- [x] P1-C: `normalize_claude` / `normalize_codex` collapse partial windows with missing `resets_at`
- [x] P1-D: `--tmux foo:foo` misparses — window overwritten with default
- [x] P1-E: `--at` / `--not-before` validated after log dir creation
- [x] P2-A: `wake_diagnostics_json` reports `unknown` for degraded user manager
- [x] P2-B: Dry-run suspend path prints no useful stdout

## Approach

All fixes are in `llm-scheduler` and `lib/llm-common.sh`.
Tests are added to `llm-usage-tests.sh`.
No architectural changes; no option renames; no speculative features.
