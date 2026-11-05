# Distributed Bridge work log

Resume (TRACK-006): read this file's last entry, `SPEC_STATUS.md`,
`DECISIONS.md`, then `git status` and `git log --oneline -10`, and continue
from the last "Next action".

Test runs inside a Bridge-managed session must unset the inherited `LLM_*`,
`TMUX` and `TMUX_PANE` variables and use `.venv/bin/python` (the `python` on
PATH has no pytest). Otherwise `test_tui_compact_and_full_render_at_supported_sizes`
fails because the inherited station prefix changes the footer.

Format: `UTC | SPEC IDs | Changes | Verification | Next action`

2026-11-05T17:00Z | REPO-001..005 | Baseline on `feat/distributed-bridge` at 14ad2c4 (v1.4.0). Entry points: llm-bridge, llm-usage, llm-scheduler, ralph-robin, llm-sleep-soak. Bridge store schema v10, event schema v7. CI: test.yml (pytest, mypy, coverage), release.yml, pages.yml. Tooling present: cargo/rustc (~/.cargo), node 24 (nvm). Durable idempotent `commands` table and operator-command apply path already exist. | Fast suite (`-m "not tmux and not pty and not subprocess and not slow and not fuzz"`) passes in a clean environment; one failure only with inherited managed env (see above). | Typed command layer.
2026-11-05T17:40Z | TRACK-001/002, ARCH-005/006, CLI-001/002/003/005, WORK-006/007, QUAL-005 | Added `bridge_commands.py` (7 operations), `--target`, request-ID idempotency through `_apply_operator_command(command_id=)`, write-lock re-check, duplicate-delivery shape fix in `enqueue_command`. Tracking files and register check. | tests/test_bridge_commands.py, tests/test_distributed_spec_status.py; full suite + coverage gates (see commit). | Extend command layer to task operations (task.list/show/add/pause/resume/cancel/retry/verify approve, steer, proposal accept), then route console dispatch for those through it; then define protocol (PROTO-001/002) in docs/distributed/protocol.md + `bridge_protocol.py`.
