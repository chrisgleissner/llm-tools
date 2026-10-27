# LLM Bridge implementation plan

Follow architecture.md. Preserve user-owned docs/features. Implementor owns
production code. Significant deterministic logic starts with an observable
failing test, then green/refactor and failure cases. No DONE without test and
independent review evidence.

| ID | Deliverable | Depends | Acceptance/evidence | Review |
| --- | --- | --- | --- | --- |
| M0 | Actual repository investigation | none | Reuse map: core/config/provider/tests/CI inspected | Final audit |
| M1 | Architecture and durable contracts | M0 | Terminal, persistence, state, hooks and CLI decisions | Architecture |
| M2 | Private tmux/native PTY slice | M1 | Fake real PTY proves ANSI/input/mouse/paste/resize/controls/cleanup plus bounded actual CLI evidence | Terminal gate |
| M3 | SQLite mission/task/commands | M1 | Transactions, concurrency, cycles, ordering, revisions, evidence, migrations, crash reopen | Persistence gate |
| M4 | Ralph/scheduler/controller integration | M2,M3 | Existing loop runs task, handoff/exhaustion/checkpoint events; no-hook behavior preserved | Execution gate |
| M5 | CLI/entry point/packaging | M3,M4 | Create/add/steer/status/events/verify, JSON, concurrent submission | Execution gate |
| M6 | Bridge/Orders/Stations/Systems/Log/Sessions/Palette | M2,M5 | Keyboard/mouse same handlers, 80x24/120x40/large/resize, no slow-read input blocking | Terminal gate |
| M7 | Attach/detach/stop/resume/restoration | M4,M6 | Reboot without old server, uncertainty reconciliation, validated native ids, owned cleanup | Restoration gate |
| M8 | Long-run reliability/progress guard | M7 | Kill windows, concurrent controllers, bounded retries/memory/fds/output and independent suspend | Restoration gate |
| M9 | Real CLI compatibility | M2,M7 | Exact versions, harmless bounded probes, unavailable explicitly NOT VERIFIED | Evidence audit |
| M10 | Independent review/remediation | M3-M9 | Critical/high resolved, medium fixed/justified, regressions verified | Final gate |
| M11 | Docs/config/help/CI/final verification | M10 | Full suite+85% branch coverage, direct/installed help, PTY CI installs tmux | Final gate |

Suggested modules: bridge.py CLI/dispatch, bridge_state.py transactions/state,
bridge_runtime.py lifecycle/controller/hooks, bridge_terminal.py tmux/TUI,
bridge_sessions.py small explicit capability functions. No plugin framework or
parallel provider adapters. Implementation may combine modules for clarity.

M2 precedes full UI; M3 tests can proceed independently. Consult planner again
only for evidence requiring a major architectural decision change. Update
progress with exact commands/results. Full gate: coverage run -m pytest;
coverage combine; coverage report --fail-under=85. Keep paid real probes opt-in
and separate from CI. Reviewer owns review-findings.md; root owns capability
ledger and real provider probe harness.
