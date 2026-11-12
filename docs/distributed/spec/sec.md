# SEC requirements

Requirement text is taken from SPEC.md.

- SEC-001: Distributed access MUST be disabled by default.
- SEC-002: Worker connections MUST use authenticated encrypted transport.
- SEC-003: Worker identities MUST be stable and revocable.
- SEC-004: Never interpolate untrusted input into shell commands; use safe argument-vector execution.
- SEC-005: Keep provider credentials on workers.
- SEC-006: Allow multiple read-only terminal viewers but only one interactive writer, enforced by the worker.
- SEC-007: Audit security-relevant operations without leaking secrets.
- SEC-008: Document trust boundaries and a concise threat model.
- SEC-009: Consult Opus for one focused security architecture review before enabling production network control.
