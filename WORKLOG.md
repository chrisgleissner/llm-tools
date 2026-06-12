llm-scheduler worklog

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
