# Specification status register

One row per specification ID. Status: `[ ]` not started, `[~]` in progress, `[x]` verified complete, `[!]` blocked.

| ID | Status | Evidence |
|---|---|---|
| AGENT-001 | [~] | Single agent so far; ongoing process rule. |
| AGENT-002 | [ ] |  |
| AGENT-003 | [ ] |  |
| AGENT-004 | [~] | No subagents spawned; ongoing process rule. |
| AGENT-005 | [ ] |  |
| AGENT-006 | [ ] |  |
| AGENT-007 | [ ] |  |
| AGENT-008 | [ ] |  |
| AGENT-009 | [ ] |  |
| TRACK-001 | [x] | This register; checked by tests/test_distributed_spec_status.py. |
| TRACK-002 | [x] | tests/test_distributed_spec_status.py fails on a missing, duplicate or unknown ID. |
| TRACK-003 | [~] | WORKLOG.md started; ongoing. |
| TRACK-004 | [~] | DECISIONS.md started; ongoing. |
| TRACK-005 | [ ] |  |
| TRACK-006 | [~] | Resume procedure in WORKLOG.md header; ongoing. |
| TRACK-007 | [ ] |  |
| TRACK-008 | [ ] |  |
| REPO-001 | [x] | Baseline recorded in WORKLOG.md (entry points, schema v10, event schema v7, CI workflows). |
| REPO-002 | [x] | Authoritative owners recorded in DECISIONS.md D1. |
| REPO-003 | [x] | Fast suite baseline in WORKLOG.md; 1 env-dependent failure explained. |
| REPO-004 | [~] | Ongoing. |
| REPO-005 | [x] | Single reconnaissance pass, 2026-11-05. |
| ARCH-001 | [ ] |  |
| ARCH-002 | [ ] |  |
| ARCH-003 | [ ] |  |
| ARCH-004 | [x] | Coordinator stores, routes and projects only; commands execute on the worker via bridge_commands (D1, coordinator/src/hub.rs). |
| ARCH-005 | [~] | llm_tools/bridge_commands.py: 20 operations (mission list/status/events/pause/stop/cancel/continue, task list/show/add/amend/verify/reject/priority/reorder/dependency/pause/resume/cancel/retry). TUI/web pending. |
| ARCH-006 | [x] | bridge_commands.dispatch runs in-process; CLI status/mission list/events/pause/stop/cancel/continue call it (test_cli_mission_controls_route_through_the_command_layer). |
| ARCH-007 | [~] | Worker reconnects after coordinator restart and execution is local (test_worker_survives_coordinator_restart); long partition tests pending. |
| ARCH-008 | [ ] |  |
| ARCH-009 | [ ] |  |
| LOCAL-001 | [~] | Full suite passes with no coordinator configuration; final offline verification pending. |
| LOCAL-002 | [ ] |  |
| LOCAL-003 | [ ] |  |
| LOCAL-004 | [x] | Commands without --target run locally unchanged; full suite passes. |
| LOCAL-005 | [ ] |  |
| LOCAL-006 | [ ] |  |
| LOCAL-007 | [ ] |  |
| LOCAL-008 | [ ] |  |
| CLI-001 | [~] | CommandRequest/CommandResult/CommandError with operation IDs, typed params, request IDs (tests/test_bridge_commands.py). More operations pending. |
| CLI-002 | [x] | Every remote-capable mutation runs through `bridge_commands.dispatch`: CLI (test_cli_*_route_through_the_command_layer), TUI console (test_console_commands_reach_the_same_command_layer, console commands are CLI command lines), remote `--target` and web (coordinator forwards typed requests that the worker executes with `bridge_commands.execute`). Console read views use the bounded projection (`bridge_monitor`), which has no business rules. Local-only actions (start/resume launch, steer, station view, provider-originated `proposal create`) stay in the CLI. |
| CLI-003 | [x] | --target local|NAME|all; remote via coordinator (bridge_remote.py; test_remote_cli_has_local_semantics against the real coordinator). |
| CLI-004 | [x] | --target all aggregates read operations only; mutations refused (test_target_all_*, integration test). |
| CLI-005 | [x] | Unknown target 404 unknown-target exit 2; offline/silent exit 3 with queued request ID; no coordinator configured exit 2; never local fallback (tests/test_bridge_remote.py, integration). |
| CLI-006 | [ ] |  |
| CLI-007 | [x] | Remote transport carries typed CommandRequest JSON, never shell commands or human output (bridge_remote.request_for). |
| CLI-008 | [ ] |  |
| WORK-001 | [~] | llm_tools/bridge_worker.py reuses bridge_commands.execute; `llm-bridge worker run`. End-to-end with coordinator pending. |
| WORK-002 | [x] | Worker opens the outbound WebSocket (bridge_ws.WebSocketClient, websocket_connector); no listener (tests/test_bridge_ws.py, test_bridge_worker.py). |
| WORK-003 | [x] | hello carries worker_id (persisted), name, software/protocol versions, platform, capabilities, mission inventory, event cursor (test_hello_reports_inventory_and_only_permitted_operations). |
| WORK-004 | [~] | hello and results contain no environment or credentials; token kept 0600 and excluded from status/public(). Audit pending. |
| WORK-005 | [x] | Remote commands run through bridge_commands.execute, the same path as the local CLI (test_commands_ack_then_result_and_duplicates_do_not_repeat). |
| WORK-006 | [~] | Request ID is the durable command ID; duplicate delivery returns stored outcome, re-checked under write lock (test_duplicate_delivery_*, test_apply_rechecks_*). |
| WORK-007 | [~] | Outcome vocabulary ok/accepted/applied/rejected/failed/uncertain in bridge_commands.execute. |
| WORK-008 | [~] | Reconnect resends hello; event replication resumes from welcome cursor with nack resend; duplicate commands return stored outcome. Coordinator side pending. |
| WORK-009 | [ ] |  |
| WORK-010 | [x] | worker run: reconnect with jittered backoff, silence timeout, SIGINT/SIGTERM stop (test_run_*). `llm-bridge worker install-service` writes a systemd user unit (Restart=on-failure, UMask=0077, baked PATH/PYTHONPATH/state dir); unit checked with `systemd-analyze --user verify` and its ExecStart run against a closed port (test_service_unit_install_and_uninstall). |
| COORD-001 | [~] | Rust coordinator builds and tests on x86-64; ARM64 cross-build succeeds; ARM64 execution unverified. |
| COORD-002 | [x] | Tokio, axum, serde, rusqlite (bundled), rustls/ring; no aws-lc-rs (coordinator/Cargo.toml). |
| COORD-003 | [ ] |  |
| COORD-004 | [ ] |  |
| COORD-005 | [ ] |  |
| COORD-006 | [ ] |  |
| COORD-007 | [ ] |  |
| COORD-008 | [ ] |  |
| COORD-009 | [ ] |  |
| COORD-010 | [ ] |  |
| PROTO-001 | [~] | docs/distributed/protocol.md (v1, JSON over WebSocket); llm_tools/bridge_protocol.py parser. Coordinator side pending. |
| PROTO-002 | [x] | Envelope v/type/id/worker_id/generation/payload validated strictly by bridge_protocol.decode (tests/test_bridge_protocol.py). |
| PROTO-003 | [~] | command.ack distinct from command.result in protocol and parser; worker/coordinator implementation pending. |
| PROTO-004 | [x] | Coordinator journal redelivers until result; worker dedups by request_id (cargo test worker redelivery; test_duplicate_delivery_*; integration duplicate cancel). |
| PROTO-005 | [~] | Worker side: global event cursor, ascending batches, nack resend (test_control_messages_and_event_replication). |
| PROTO-006 | [x] | Task mutations accept expected_revision, checked under the SQLite write lock; mismatch is outcome rejected, code conflict (test_stale_revision_*, test_conflicting_amendments_only_one_wins). |
| PROTO-007 | [ ] |  |
| PROTO-008 | [ ] |  |
| PROTO-009 | [x] | Separate WebSocket per terminal session (`/api/v1/worker/terminal/<id>`, `/api/v1/terminal`); control connection only opens/closes. Worker: tests/test_bridge_terminal_stream.py; coordinator: coordinator/tests/terminal.rs. |
| PROTO-010 | [x] | Coordinator increments/persists generation and closes older connection (cargo test); worker persists max generation and rejects older (test_generation_fencing). |
| NET-001 | [ ] |  |
| NET-002 | [ ] |  |
| NET-003 | [ ] |  |
| NET-004 | [ ] |  |
| NET-005 | [ ] |  |
| NET-006 | [ ] |  |
| NET-007 | [ ] |  |
| NET-008 | [ ] |  |
| NET-009 | [ ] |  |
| NET-010 | [ ] |  |
| AUTH-001 | [ ] |  |
| AUTH-002 | [ ] |  |
| AUTH-003 | [ ] |  |
| AUTH-004 | [ ] |  |
| AUTH-005 | [ ] |  |
| AUTH-006 | [ ] |  |
| AUTH-007 | [ ] |  |
| AUTH-008 | [ ] |  |
| AUTH-009 | [ ] |  |
| AUTH-010 | [ ] |  |
| SEC-001 | [~] | Worker code runs only after explicit enroll + run; standalone never imports it. Coordinator binds loopback by default (pending). |
| SEC-002 | [~] | Worker refuses non-wss URLs and verifies the coordinator certificate against the enrolled CA (test_wss_verifies_the_server_against_the_given_ca). |
| SEC-003 | [ ] |  |
| SEC-004 | [ ] |  |
| SEC-005 | [ ] |  |
| SEC-006 | [x] | Worker `WriterRegistry`: one writer per mission, none while a local tmux client is attached, view only on read-only workers; coordinator never grants roles. Tests: test_second_writer_and_local_client_get_view_only, test_writer_role_is_dropped_when_a_local_client_attaches, Playwright terminal.spec.ts (second tab view only, input never reaches the pane). |
| SEC-007 | [~] | Worker: `<state>/worker/audit.jsonl` (0600, 1 MiB + one rotation) records enrolment, connections, mutating/refused commands and terminal sessions with input byte counts only; `llm-bridge worker audit` (test_audit_*). Coordinator: `audit` table for login, setup, password, worker and token actions. Review of coverage pending SEC-009. |
| SEC-008 | [x] | docs/distributed/threat-model.md: assets, five trust boundaries, threats with mitigations and residual risk, out-of-scope items. |
| SEC-009 | [ ] |  |
| WEB-001 | [ ] |  |
| WEB-002 | [ ] |  |
| WEB-003 | [ ] |  |
| WEB-004 | [ ] |  |
| WEB-005 | [ ] |  |
| WEB-006 | [ ] |  |
| WEB-007 | [x] | @xterm/xterm 6.0.0 with @xterm/addon-fit 0.11.0 (web/src/terminal.ts). |
| WEB-008 | [x] | Resize from `s` frames and `r` in write mode, keyboard input, copy (Ctrl+Shift+C/Cmd+C) and paste, reconnect with 0.5–8 s backoff and fresh snapshot, take/release writer. Playwright terminal.spec.ts against a real worker and tmux station on Chromium and Firefox, desktop and mobile (60 tests pass). |
| WEB-009 | [ ] |  |
| WEB-010 | [ ] |  |
| WEB-011 | [ ] |  |
| WEB-012 | [ ] |  |
| DATA-001 | [ ] |  |
| DATA-002 | [ ] |  |
| DATA-003 | [ ] |  |
| DATA-004 | [ ] |  |
| DATA-005 | [ ] |  |
| DATA-006 | [~] | Worker/standalone: `llm-bridge backup`/`restore` (online backup API, integrity check, refuses while controllers run, keeps the replaced database, raises the event id sequence), docs/distributed/operations.md, tests/test_bridge_backup.py. Coordinator database procedure pending. |
| DATA-007 | [ ] |  |
| OPS-001 | [ ] |  |
| OPS-002 | [ ] |  |
| OPS-003 | [ ] |  |
| OPS-004 | [ ] |  |
| OPS-005 | [ ] |  |
| TEST-001 | [ ] |  |
| TEST-002 | [ ] |  |
| TEST-003 | [~] | Local vs remote JSON parity for mission list, task list/show, status (test_remote_cli_has_local_semantics, test_remote_commands_match_local_semantics_and_are_idempotent). |
| TEST-004 | [ ] |  |
| TEST-005 | [~] | Registration, discovery, remote control, events and reconnection covered by tests/test_distributed_integration.py; approvals/terminal pending. |
| TEST-006 | [~] | tests/test_distributed_faults.py: storage failure mid-mutation (uncertain, rolled back, redelivery applies once), locked database read, partition after ack (redelivery acked duplicate, effect once); test_distributed_integration.py: coordinator restart and reconnect. Coordinator crash during journal write pending. |
| TEST-007 | [x] | Six real processes race the same request ID (one command row, one revision bump) and six conflicting amendments of one revision (one applied, five `conflict`) in tests/test_distributed_faults.py; in-process cases in tests/test_bridge_commands.py. |
| TEST-008 | [~] | Seeded byte/structure mutation fuzz of bridge_protocol.decode (8 seeds x 400 cases) and deep-nesting test. |
| TEST-009 | [ ] |  |
| TEST-010 | [ ] |  |
| TEST-011 | [ ] |  |
| TEST-012 | [ ] |  |
| TEST-013 | [~] | Coordinator cross-built for aarch64-unknown-linux-gnu with cargo-zigbuild (ELF aarch64, 3.15 MB). No qemu-user or ARM host available to run it; x86-64 tested. |
| TEST-014 | [ ] |  |
| TEST-015 | [ ] |  |
| TEST-016 | [~] | CI: coordinator job (fmt, clippy, test); Python job builds the coordinator and requires the cross-component tests. TypeScript pending. |
| TEST-017 | [ ] |  |
| QUAL-001 | [ ] |  |
| QUAL-002 | [ ] |  |
| QUAL-003 | [ ] |  |
| QUAL-004 | [ ] |  |
| QUAL-005 | [~] | Structured error codes in bridge_commands.error_for; unexpected mutation failure reported as uncertain. |
| QUAL-006 | [ ] |  |
| QUAL-007 | [ ] |  |
| QUAL-008 | [ ] |  |
| QUAL-009 | [ ] |  |
| QUAL-010 | [ ] |  |
| CONV-001 | [~] | Steps 1-2 in progress. |
| CONV-002 | [ ] |  |
| CONV-003 | [ ] |  |
| CONV-004 | [ ] |  |
| CONV-005 | [ ] |  |
| CONV-006 | [ ] |  |
| CONV-007 | [ ] |  |
| CONV-008 | [ ] |  |
| CONV-009 | [ ] |  |
| CONV-010 | [ ] |  |
| CONV-011 | [ ] |  |
| CONV-012 | [ ] |  |
