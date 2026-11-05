# Distributed Bridge threat model

Scope: the optional coordinator (`coordinator/`), the worker service
(`llm_tools/bridge_worker.py`, `bridge_terminal_stream.py`), the remote CLI
(`bridge_remote.py`) and the browser UI (`web/`). Standalone Bridge without an
enrolled worker has no network surface and is out of scope (SEC-001).

## Assets

| Asset | Where it lives |
|---|---|
| Provider credentials and environment | Worker host only, in each provider CLI's own files. Never read or sent by the worker (SEC-005). |
| Mission source trees, prompts, task text, events | Worker database (authoritative); events and mission summaries are replicated to the coordinator. |
| Ability to change mission state | Worker command layer; reachable remotely only on workers enrolled with `--allow-control`. |
| Ability to type into a provider terminal | Worker tmux station; reachable remotely only through a writer terminal session. |
| Administrator password, sessions, API tokens, worker tokens | Coordinator database, as Argon2id or SHA-256 hashes. |
| Coordinator CA key and server key | Coordinator data directory, 0600. |
| Worker token and trusted CA | `<state>/worker/`, 0600. |

## Trust boundaries

1. **Browser to coordinator.** HTTPS with the coordinator's own CA. The
   browser holds an HttpOnly, Secure, SameSite=Strict session cookie. State
   changes need the CSRF header and an exact `Origin` match. The UI is static
   files under a strict Content Security Policy with no inline script and no
   third-party origins.
2. **Remote CLI to coordinator.** HTTPS verified against the configured CA,
   with a revocable bearer API token.
3. **Worker to coordinator.** The worker opens the connection (no listening
   port on the worker), verifies the coordinator certificate against the CA
   from enrolment and authenticates with a per-worker token. Every message is
   validated against protocol v1 (1 MiB limit, strict payload fields).
4. **Coordinator to worker.** The worker does not trust the coordinator to
   enforce policy. It applies the same validation, state transitions, locking
   and idempotency as the local CLI (`bridge_commands.execute`), refuses
   mutations unless enrolled with `--allow-control`, rejects commands from an
   older connection generation, and grants the terminal writer role itself.
5. **Worker to provider processes.** Unchanged from standalone Bridge: the
   provider runs as the operator inside the mission folder.

## Threats and mitigations

| Threat | Mitigation | Residual risk |
|---|---|---|
| Someone on the LAN reaches the coordinator | Loopback bind by default; non-loopback needs `lan_enabled = true`. Login required for every API except health and auth state. Login rate limits per address and per user. | A LAN attacker can attempt logins and observe that the service exists. LAN placement is not an authentication substitute (NET-010). |
| Stolen session cookie | HttpOnly and Secure; sessions expire and are deleted on password change. | A compromised browser profile can act as the administrator until the session expires. |
| Cross-site request forgery | SameSite=Strict, CSRF token header, exact Origin check including WebSocket upgrades. | None known. |
| Cross-site scripting through mission or event text | The UI renders text with DOM text nodes, never as HTML; CSP forbids inline scripts. Terminal output goes to xterm.js, which does not execute it. | A malicious escape sequence can still change the terminal display. |
| Coordinator compromise | Read-only workers cannot be mutated. Control workers accept only typed operations that the local operator could also run; there is no shell or file access operation. The worker never sends credentials or environment. | A compromised coordinator can issue every allowed operation on control workers, view terminals and type into a station that has no local client attached. |
| Worker token theft | Token file is 0600 under the private state directory; revocable on the coordinator; stored only as a hash there. | The holder can impersonate the worker (send false events) until revoked; it cannot issue commands to other workers. |
| Replay or duplicate delivery | Request ID is the durable command ID on the worker; duplicates return the stored outcome. Generation fencing rejects commands from older connections. | None known. |
| Conflicting edits from two clients | `expected_revision` checked under the SQLite write lock; the loser gets `conflict`. | Clients that omit `expected_revision` apply last-writer-wins. |
| Terminal hijack | One writer per mission, granted by the worker; no writer while a local tmux client is attached; input limited to 64 KiB frames and typed with `send-keys -H` (no shell). Viewers cannot write. | A writer can type anything the operator could type into the provider. |
| Slow or hostile consumer exhausting memory | Message size limits, bounded queues, viewer backlog limit (256 KiB, then disconnect), tmux pause-after with snapshot resync, request body limit 1 MiB, event retention cap. | A flood of valid requests from an authenticated client is limited only by connection limits. |
| Secrets in logs | Worker audit and logs record identifiers, outcomes and byte counts only; tokens are never printed after enrolment; `worker status` and `remote status` omit tokens. | Prompt text is part of mission data and is replicated to the coordinator in events. |
| Downgrade to plain HTTP | Workers refuse non-`wss://` URLs and the remote CLI requires `https://`. The coordinator serves TLS only. | None known. |

## Out of scope

- Multi-user authorization: there is one administrator.
- Protection against a malicious provider or a compromised worker host.
- Internet exposure: the coordinator is designed for a home LAN or a VPN.

## Review

SEC-009 requires one focused security architecture review before production
network control is enabled. Its findings and their disposition are recorded
in `DECISIONS.md`.
