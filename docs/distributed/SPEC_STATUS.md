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
| ARCH-004 | [ ] |  |
| ARCH-005 | [~] | llm_tools/bridge_commands.py: 20 operations (mission list/status/events/pause/stop/cancel/continue, task list/show/add/amend/verify/reject/priority/reorder/dependency/pause/resume/cancel/retry). TUI/web pending. |
| ARCH-006 | [x] | bridge_commands.dispatch runs in-process; CLI status/mission list/events/pause/stop/cancel/continue call it (test_cli_mission_controls_route_through_the_command_layer). |
| ARCH-007 | [ ] |  |
| ARCH-008 | [ ] |  |
| ARCH-009 | [ ] |  |
| LOCAL-001 | [ ] |  |
| LOCAL-002 | [ ] |  |
| LOCAL-003 | [ ] |  |
| LOCAL-004 | [x] | Commands without --target run locally unchanged; full suite passes. |
| LOCAL-005 | [ ] |  |
| LOCAL-006 | [ ] |  |
| LOCAL-007 | [ ] |  |
| LOCAL-008 | [ ] |  |
| CLI-001 | [~] | CommandRequest/CommandResult/CommandError with operation IDs, typed params, request IDs (tests/test_bridge_commands.py). More operations pending. |
| CLI-002 | [~] | CLI mission controls and task show/add/mutations dispatch through the layer (test_cli_*_route_through_the_command_layer); TUI and web pending. |
| CLI-003 | [~] | --target local implemented; remote targets pending coordinator. |
| CLI-004 | [ ] |  |
| CLI-005 | [~] | Named non-local targets exit 2 without local fallback (test_target_option); remote pending. |
| CLI-006 | [ ] |  |
| CLI-007 | [ ] |  |
| CLI-008 | [ ] |  |
| WORK-001 | [ ] |  |
| WORK-002 | [ ] |  |
| WORK-003 | [ ] |  |
| WORK-004 | [ ] |  |
| WORK-005 | [ ] |  |
| WORK-006 | [~] | Request ID is the durable command ID; duplicate delivery returns stored outcome, re-checked under write lock (test_duplicate_delivery_*, test_apply_rechecks_*). |
| WORK-007 | [~] | Outcome vocabulary ok/accepted/applied/rejected/failed/uncertain in bridge_commands.execute. |
| WORK-008 | [ ] |  |
| WORK-009 | [ ] |  |
| WORK-010 | [ ] |  |
| COORD-001 | [ ] |  |
| COORD-002 | [ ] |  |
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
| PROTO-004 | [ ] |  |
| PROTO-005 | [ ] |  |
| PROTO-006 | [x] | Task mutations accept expected_revision, checked under the SQLite write lock; mismatch is outcome rejected, code conflict (test_stale_revision_*, test_conflicting_amendments_only_one_wins). |
| PROTO-007 | [ ] |  |
| PROTO-008 | [ ] |  |
| PROTO-009 | [ ] |  |
| PROTO-010 | [ ] |  |
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
| SEC-001 | [ ] |  |
| SEC-002 | [ ] |  |
| SEC-003 | [ ] |  |
| SEC-004 | [ ] |  |
| SEC-005 | [ ] |  |
| SEC-006 | [ ] |  |
| SEC-007 | [ ] |  |
| SEC-008 | [ ] |  |
| SEC-009 | [ ] |  |
| WEB-001 | [ ] |  |
| WEB-002 | [ ] |  |
| WEB-003 | [ ] |  |
| WEB-004 | [ ] |  |
| WEB-005 | [ ] |  |
| WEB-006 | [ ] |  |
| WEB-007 | [ ] |  |
| WEB-008 | [ ] |  |
| WEB-009 | [ ] |  |
| WEB-010 | [ ] |  |
| WEB-011 | [ ] |  |
| WEB-012 | [ ] |  |
| DATA-001 | [ ] |  |
| DATA-002 | [ ] |  |
| DATA-003 | [ ] |  |
| DATA-004 | [ ] |  |
| DATA-005 | [ ] |  |
| DATA-006 | [ ] |  |
| DATA-007 | [ ] |  |
| OPS-001 | [ ] |  |
| OPS-002 | [ ] |  |
| OPS-003 | [ ] |  |
| OPS-004 | [ ] |  |
| OPS-005 | [ ] |  |
| TEST-001 | [ ] |  |
| TEST-002 | [ ] |  |
| TEST-003 | [ ] |  |
| TEST-004 | [ ] |  |
| TEST-005 | [ ] |  |
| TEST-006 | [ ] |  |
| TEST-007 | [~] | Conflicting amendments and duplicate delivery tests in tests/test_bridge_commands.py; multi-process concurrency pending. |
| TEST-008 | [~] | Seeded byte/structure mutation fuzz of bridge_protocol.decode (8 seeds x 400 cases) and deep-nesting test. |
| TEST-009 | [ ] |  |
| TEST-010 | [ ] |  |
| TEST-011 | [ ] |  |
| TEST-012 | [ ] |  |
| TEST-013 | [ ] |  |
| TEST-014 | [ ] |  |
| TEST-015 | [ ] |  |
| TEST-016 | [ ] |  |
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
