# AUTH requirements

Requirement text is taken from SPEC.md.

- AUTH-001: Require login for all application pages, APIs, event streams and terminals except explicitly public login/minimal health endpoints.
- AUTH-002: Provide secure first-run administrator creation. No default password.
- AUTH-003: Use established password hashing, preferably Argon2id with unique salts and resource-appropriate parameters.
- AUTH-004: Prefer opaque server-side sessions with `HttpOnly`, `Secure`, `SameSite` cookies.
- AUTH-005: Implement logout, idle/absolute expiry and invalidation after password changes.
- AUTH-006: Rate-limit failed logins and prevent account enumeration.
- AUTH-007: Enforce CSRF protection, appropriate Origin checks and authenticated WebSocket handshakes.
- AUTH-008: Enforce authorisation server-side and at worker command boundaries.
- AUTH-009: A single administrator is sufficient initially. Multi-user RBAC and external OIDC are out of scope.
- AUTH-010: Never expose credentials, hashes, session secrets or provider tokens in logs, URLs or events.
