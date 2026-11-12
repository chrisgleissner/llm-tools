# NET requirements

Requirement text is taken from SPEC.md.

- NET-001: All authenticated application access MUST use HTTPS. No plaintext credentials or management operations.
- NET-002: LAN-only operation MUST work without public DNS, internet access, cloud services or port forwarding.
- NET-003: Default to loopback binding until LAN access is explicitly enabled.
- NET-004: Prefer TLS 1.3; permit secure TLS 1.2 where required. Disable deprecated protocols and weak ciphers.
- NET-005: Support a locally generated CA/server certificate or administrator-provided certificates.
- NET-006: Provide clear Chrome/Firefox certificate-trust setup without requiring warning bypasses.
- NET-007: Certificates MUST contain appropriate hostname/IP SANs and support renewal/replacement.
- NET-008: Choose Rust-native TLS or a minimal reverse proxy such as Caddy based on secure deployment simplicity and measured resource cost.
- NET-009: Support a stable local hostname, preferably under `home.arpa`, where DNS is available.
- NET-010: Provide firewall guidance, interface restrictions, connection limits and timeouts. LAN placement is not an authentication substitute.
