# Coordinator design

`bridge-coordinator` is a single Rust binary in `coordinator/`. It indexes
workers and missions, routes commands, projects events and serves the web UI.
It never runs providers or decides mission state (ARCH-003, ARCH-004). The
worker protocol is `protocol.md`.

## Process and files

- `bridge-coordinator init --data-dir DIR [--hostname NAME]... [--ip ADDR]...`
  creates `DIR` (0700), `coordinator.sqlite3`, a local CA (`ca.pem`,
  `ca.key`) and a server certificate signed by it with the given DNS/IP SANs,
  `config.toml` (bind `127.0.0.1:8443` by default) and a one-time
  `setup-token` file used to create the administrator. Existing files are never
  overwritten. Private keys and tokens are 0600.
- `bridge-coordinator serve --data-dir DIR` serves HTTPS (rustls; TLS 1.3 and
  1.2 with AEAD suites only). It refuses to start when the bind address is not
  loopback and `lan_enabled = true` is not set in `config.toml` (NET-003).
  SIGTERM drains connections and exits within 10 s.
- `bridge-coordinator worker add NAME` prints a new worker token once and
  stores its SHA-256 hash; `worker revoke NAME` deletes it.
- `bridge-coordinator admin reset-password` (local shell only) sets a new
  password from stdin and deletes all sessions.
- `bridge-coordinator cert renew [--hostname ...] [--ip ...]` replaces the
  server certificate from the existing CA; `cert install --cert F --key F`
  installs administrator-provided files.
- `bridge-coordinator token create NAME` prints an API token for the remote
  CLI; stored hashed, revocable with `token revoke NAME`.

## HTTP API (all under `/api/v1`, JSON)

Public:

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` only. |
| GET | `/api/v1/auth/state` | `{"setup_required": bool, "authenticated": bool}` |
| POST | `/api/v1/auth/setup` | `{setup_token, username, password}`; only while no administrator exists; consumes the token. |
| POST | `/api/v1/auth/login` | `{username, password}`; sets cookie `bridge_session` (HttpOnly, Secure, SameSite=Strict, Path=/); returns `{csrf_token}`. Same error and comparable timing for unknown user and wrong password. |

Authenticated (session cookie, or `Authorization: Bearer <api token>`):

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/logout` | Deletes the session. |
| POST | `/auth/password` | `{current, new}`; deletes every session. |
| GET | `/workers` | name, id, state (`online`/`offline`), last_seen, versions, platform, capabilities. |
| POST | `/workers` | `{name}` → `{token}` shown once. |
| DELETE | `/workers/{name}` | Revoke; closes its connection. |
| GET | `/missions` | Mission index across workers with `worker`, `stale` (worker offline or last hello older than its last event). |
| POST | `/commands` | `{target, request}` where `request` is a `CommandRequest`. A command for an offline worker is queued with `state: pending` and delivered on reconnect. The coordinator rejects unknown targets (404 `unknown-target`) and operations the worker did not advertise (`unsupported-operation`). Optional `wait_ms` (≤ 30000) waits for the result. Same `request_id` returns the existing journal entry. |
| GET | `/commands/{request_id}` | Journal entry: `state` (`pending`/`sent`/`acked`/`done`), `result`. |
| GET | `/events?worker=&after=&limit=` | Stored events, ascending. |
| GET | `/stream` | WebSocket; pushes `worker`, `event` and `command` updates. |
| GET | `/terminal?worker=&mission=&mode=view\|write&cols=&rows=` | WebSocket for one remote terminal session. Session cookie (with exact Origin) or API token. The coordinator opens the session on the worker with `terminal.open` and relays the frames of `protocol.md` (Terminal sessions) unchanged after validation: browser input at most 64 KiB decoded, `r` only with 1–1000 columns and rows. At most 16 sessions per worker and 64 in total (`x` frame, close 1013). A viewer more than 256 KiB behind is closed with 1013. Each open is audited as `terminal.open`. The worker, not the coordinator, decides the writer role. |
| GET | `/worker/terminal/{session_id}` | Worker side of a session; worker bearer token; accepted once, only for a session opened for that worker. |
| GET | `/diagnostics` | versions, uptime, RSS, DB size, connected workers, queue depths. |

Every state-changing request authenticated by cookie needs header
`X-CSRF-Token` equal to the session's token and an `Origin` equal to the
coordinator's own origin; WebSocket upgrades check `Origin` too (AUTH-007).
Bearer API tokens are not subject to CSRF checks because browsers never send
them automatically.

## Limits (COORD-004)

Request bodies 1 MiB; 64 concurrent worker connections; per-connection
outbound queue 256 messages (a full queue closes the connection; the worker
reconnects and the journal redelivers); stored events per worker capped at
`max_events_per_worker` (default 50 000, oldest deleted first; worker
databases keep the authoritative copy); completed commands kept 7 days;
login failures limited to 5 per minute per client address and per username.

## Storage

SQLite (WAL, `synchronous=FULL`) with `PRAGMA user_version` migrations:
`workers`, `missions`, `events`, `commands`, `admin`, `sessions`,
`api_tokens`, `audit`. Passwords use Argon2id (19 MiB, t=2, p=1, unique salt);
session and API tokens are 32 random bytes stored as SHA-256 hashes.

## Content Security Policy

Static files are served with `default-src 'self'`, `script-src 'self'` and
`style-src 'self' 'unsafe-inline'`. Inline styles are allowed because xterm.js
6 creates `<style>` elements at runtime and has no nonce option; scripts stay
restricted to the coordinator's own files.
