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
| Administrator password, sessions, API tokens, worker tokens | Coordinator database, as Argon2id or SHA-256 hashes. An API token has the same power as the administrator session (see the threats table). |
| Coordinator CA key and server key | Coordinator data directory, 0600. The CA is name-constrained (see the threats table). |
| Passkey credential public keys and ceremony state | Coordinator database; credential keys are public, and in-flight state is session-bound, single-use and expires within five minutes. The coordinator does not store RSA private keys. |
| Worker token and trusted CA | `<state>/worker/`, 0600. |

## Trust boundaries

1. **Browser to coordinator.** HTTPS with the coordinator's own CA. The
   browser holds a session cookie and state changes need a CSRF header and an
   exact `Origin` match (cookie attributes and rules: `coordinator.md`, HTTP API).
   The UI is static files under a strict Content Security Policy with no inline
   script and no third-party origins.
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
| Someone on the LAN reaches the coordinator | Loopback bind by default; non-loopback needs `lan_enabled = true`. Login required for every API except health and auth state. Login rate limits per address and per user. An attempt is reserved before Argon2 runs, so concurrent guesses cannot exceed the limit, and the per-user limit does not apply to addresses with a recent successful login, so guessing `admin` from elsewhere cannot lock the administrator out of the usual addresses (limits and mechanism: `coordinator.md`, Login rate limiting). | A LAN attacker can attempt logins and observe that the service exists. Someone guessing `admin` from many addresses can block the administrator on a new address for up to a minute at a time. LAN placement is not an authentication substitute (NET-010). |
| A client spoofs its address through a reverse proxy | `Forwarded` and `X-Forwarded-For` are ignored unless the direct peer matches `trusted_proxies`. Chains are bounded and walked from the closest proxy toward the client; malformed or oversized headers fall back to the TCP peer. Listener groups can isolate console/REST, worker channel and health on independent addresses (`coordinator.md`, `install.md`). | A misconfigured trusted proxy that accepts client-supplied forwarding headers can still report a false address. Internet exposure is accepted only when `exposure = "internet"` passes the startup preconditions; VPN or a trusted proxy remains the recommended path. |
| Unauthenticated clients exhaust connection slots or memory (slow headers or bodies, many sockets, large bodies) | Per-address connection cap, header and whole-body timeouts, a small body limit on `/api/v1/auth/*`, login rate check before the body is read, a bound on large bodies buffered at once (taken only after authentication), a slot reserve usable only by workers with a valid token, one pre-hello connection per worker token with a short hello timeout (values: `coordinator.md`, Limits and timeouts). | Several addresses together can still fill the general slots and stop browser logins until their timeouts expire; workers keep the reserve. Behind a proxy all clients share one address and the per-address caps apply to them together. |
| Misuse of the local CA | The CA has `pathLenConstraint=0` and a critical name constraints extension that limits it to a short list of names (`coordinator.md`, CA constraints). A leaked `ca.key` or a browser that trusts `ca.pem` cannot be used for a certificate for any other site. `cert renew` refuses names outside the constraints; `cert new-ca` replaces the CA (browsers re-import, workers re-enrol). | The key can still mint certificates for the permitted names: it can impersonate the coordinator on this network. A CA from before this change is unconstrained until `cert new-ca` is run (`info` reports it). |
| API token theft or overreach | Tokens are stored as hashes, revocable (`token revoke`), scoped (`read`, `operate`, `terminal:view`, `terminal:write`, `admin`), expire by default (`coordinator.md`, `token create`), and are revoked by `admin reset-password` and by a password change through `POST /api/v1/auth/password` unless `--keep-api-tokens` / `keep_api_tokens` is given. Existing tokens migrate to `admin` for compatibility. | A token can exercise the operations its scope grants until it expires or is revoked. Tokens created before expiry existed never expire until recreated. |
| Restoring an old backup | `restore` deletes browser sessions and prints every worker and API token that is active again, with the revoke command. | Credentials revoked after the backup was taken are reinstated until the operator revokes them again. |
| Stolen session cookie | HttpOnly and Secure; sessions expire and are deleted on password change. | A compromised browser profile can act as the administrator until the session expires. |
| Cross-site request forgery | SameSite=Strict session cookie, CSRF token header, exact Origin check including WebSocket upgrades. | None known. |
| Cross-site scripting through mission or event text | The UI renders text with DOM text nodes, never as HTML; CSP forbids inline scripts. Terminal output goes to xterm.js, which does not execute it. | A malicious escape sequence can still change the terminal display. |
| Coordinator compromise | Read-only workers cannot be mutated. Control workers accept only typed operations that the local operator could also run; there is no shell or file access operation. The worker never sends credentials or environment. | A compromised coordinator can issue every allowed operation on control workers, view terminals and type into a station that has no local client attached. |
| Worker token theft | Token file is 0600 under the private state directory; revocable on the coordinator; stored only as a hash there. | The holder can impersonate the worker (send false events) until revoked; it cannot issue commands to other workers. |
| Replay or duplicate delivery | Request ID is the durable command ID on the worker; duplicates return the stored outcome. Generation fencing rejects commands from older connections. | None known. |
| Conflicting edits from two clients | `expected_revision` checked under the SQLite write lock; the loser gets `conflict`. | Clients that omit `expected_revision` apply last-writer-wins. |
| Control mode lets a remote party run commands on the worker host (finding M4) | A worker enrolled with `--allow-control` accepts typed operations such as adding or starting tasks, steering a mission and typing into a provider terminal. Those operations feed prompts and keystrokes to a provider agent that runs as the operator inside the mission folder and can run arbitrary commands, read and write files and use the network. | Whoever can use the coordinator administrator session, an API token, or a compromised coordinator can make the agent run arbitrary commands through a prompt or terminal input. Enrol `--allow-control` only on hosts where that is acceptable, and keep the provider agent's own permission prompts and sandbox on. |
| Terminal view of a read-only worker (finding L4) | Mutating operations are refused without `--allow-control`. The worker-side option `--allow-terminal-view` decides whether remote terminal sessions in view mode are accepted at all: without it a worker, including a read-only one, refuses `terminal.open`, so screen content (prompts, source code, secrets shown in the provider session) does not leave the host. With it (or with `--allow-control`, which implies it), view sessions are accepted on a worker without `--allow-control`; write sessions still need `--allow-control`. | A worker enrolled with `--allow-terminal-view` shows its provider terminal to anyone who can open a terminal on the coordinator. |
| Terminal hijack | One writer per mission, granted by the worker; no writer while a local tmux client is attached; input limited in frame size (`protocol.md`) and typed with `send-keys -H` (no shell). Viewers cannot write. | A writer can type anything the operator could type into the provider. |
| Slow or hostile consumer exhausting memory | Message size limits, bounded queues, a viewer backlog limit with disconnect, tmux pause-after with snapshot resync, a request body limit, an event retention cap (values: `protocol.md` and `coordinator.md`). | A flood of valid requests from an authenticated client is limited only by connection limits. |
| Secrets in logs | Worker audit and logs record identifiers, outcomes and byte counts only; tokens are never printed after enrolment; `worker status` and `remote status` omit tokens. | Prompt text is part of mission data and is replicated to the coordinator in events. |
| Downgrade to plain HTTP | Workers refuse non-`wss://` URLs and the remote CLI requires `https://`. The coordinator serves TLS only. | None known. |
| RSA private-key timing attacks in passkey verification | `webauthn-rs` 0.6.2 pulls `rsa 0.10.0-rc.19`, flagged by RUSTSEC-2023-0071. The coordinator uses the WebAuthn library only to verify authenticator signatures against stored public credential keys; it creates, stores, signs with and decrypts with no RSA private key. The exception is limited to this public-key verification path and recorded for both audit tools in `DECISIONS.md`. | The Rust dependency contains vulnerable private-key operations even though this coordinator path does not call them. Revisit on any auth-path change or when a maintained `webauthn-rs` release removes the dependency; do not reuse this exception for private-key operations. |

## Out of scope

- Multi-user authorization: there is one administrator.
- Protection against a malicious provider or a compromised worker host.
- Internet exposure is supported only with `exposure = "internet"` and all startup preconditions in `coordinator.md`. A VPN or identity-aware reverse proxy remains the recommended remote-access path; direct exposure requires an operator-provided public certificate and the configured security controls.

## Review

SEC-009 requires one focused security architecture review before production
network control is enabled. Its findings and their disposition are recorded
in `DECISIONS.md`.
