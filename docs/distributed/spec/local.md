# LOCAL requirements

Requirement text is taken from SPEC.md.

- LOCAL-001: Existing CLI/TUI workflows MUST operate offline without coordinator configuration or background services.
- LOCAL-002: Preserve `llm-bridge`, `llm-usage`, `llm-scheduler`, `ralph-robin` and `llm-sleep-soak`.
- LOCAL-003: Preserve documented arguments, defaults, exit codes, JSON output, confirmation behaviour and error semantics.
- LOCAL-004: Commands without explicit remote targeting MUST execute locally.
- LOCAL-005: Preserve SQLite compatibility through transactional, versioned and tested migrations.
- LOCAL-006: Preserve native tmux/PTY behaviour, attach/detach, locking, process ownership and recovery.
- LOCAL-007: Preserve verification gates, human approval, provider safety and prompt-injection-resistant dispatch.
- LOCAL-008: Installation MUST NOT automatically enable network services or remote access.
