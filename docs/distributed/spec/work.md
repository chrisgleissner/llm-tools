# WORK requirements

Requirement text is taken from SPEC.md.

- WORK-001: Implement an optional Python worker service reusing the existing Bridge engine.
- WORK-002: Workers SHOULD initiate outbound authenticated connections to the coordinator.
- WORK-003: Registration MUST include stable identity, protocol/software versions, capabilities, platform and mission inventory.
- WORK-004: Provider credentials and sensitive environment variables MUST remain local.
- WORK-005: Remote commands MUST use existing validation, state transitions, authorisation and locking.
- WORK-006: Mutating commands MUST use durable idempotency IDs and persist outcomes. Duplicate delivery MUST NOT repeat side effects.
- WORK-007: Distinguish accepted, applied, rejected, failed and uncertain command outcomes.
- WORK-008: Reconnection MUST reconcile missions, outstanding commands, event cursors and sessions without duplicating execution.
- WORK-009: Local and remote commands MUST share concurrency controls.
- WORK-010: Support clean startup, shutdown, reconnection and recovery.
