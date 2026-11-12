# PROTO requirements

Requirement text is taken from SPEC.md.

- PROTO-001: Define a versioned, documented worker-coordinator protocol. Prefer JSON initially.
- PROTO-002: Messages MUST contain appropriate version, type, request ID, worker/mission identity, sequence/revision and typed payload.
- PROTO-003: Command acknowledgements MUST be distinct from execution outcomes.
- PROTO-004: Use at-least-once delivery with durable deduplication. Do not claim exactly-once execution from transport acknowledgements.
- PROTO-005: Worker events MUST support monotonic cursors, replay and gap detection.
- PROTO-006: State-dependent mutations MUST enforce revision checks or equivalent concurrency protection.
- PROTO-007: Support capability negotiation and structured unsupported-operation errors.
- PROTO-008: Handle duplicate, lost, reordered and delayed messages, partitions, restarts and uncertain outcomes.
- PROTO-009: Isolate terminal streaming from control messages, with bounded buffering.
- PROTO-010: Prevent stale coordinator connections from issuing conflicting commands through generation fencing or equivalent protection.
