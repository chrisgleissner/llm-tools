# Coordinator design

`bridge-coordinator` is a single Rust binary in `coordinator/`. It indexes
workers and missions, routes commands, projects events and serves the web UI.
It never runs providers or decides mission state (ARCH-003, ARCH-004). The
worker protocol is `protocol.md`.

## Process and files

- `bridge-coordinator init [--hostname NAME]... [--ip ADDR]... [--bind HOST:PORT]`
  creates the data directory (0700, best effort; it never chowns, and an existing
  empty directory such as a volume mount point is accepted), `coordinator.sqlite3`,
  a local CA (`ca.pem`, `ca.key`) and a server certificate signed by it,
  `config.toml` and a one-time `setup-token` file used to create the
  administrator. Private keys and tokens are 0600. On a directory that already
  holds a configuration, CA, server certificate and database, `init` prints
  `already initialised` and changes nothing (exit 0), so it can run on every
  container start. After a successful init it prints the HTTPS URL(s), the path of
  `ca.pem` and its SHA-256 fingerprint, the setup-token path and the browser trust
  steps (`install.md`).
  - Certificate names. Without `--hostname`/`--ip`, the certificate covers
    `localhost`, `127.0.0.1`, `::1`, the machine host name, `<host>.home.arpa`, and
    the machine's non-loopback global IPv4/IPv6 addresses (`netinfo.rs`: host name
    from `/proc/sys/kernel/hostname`; IPv4 from `/proc/net/fib_trie` plus the source
    address the kernel selects for a private destination via a connected UDP socket,
    which sends nothing; IPv6 from `/proc/net/if_inet6`, global scope only;
    link-local addresses are skipped). With any explicit `--hostname` or `--ip`,
    discovery is not used and the certificate covers exactly the given names plus
    `localhost`, `127.0.0.1` and `::1`. `cert renew` follows the same rule.
  - The CA is constrained (`certs.rs`): `basicConstraints` CA with `pathLenConstraint=0`
    and a critical `nameConstraints` extension that permits only the names of the server
    certificate at `init` time (DNS names, and each address as a single-host range),
    `localhost`, `home.arpa` and its subdomains, `127.0.0.0/8` and `::1`. Validity is 5
    years. A leaf for any other name does not validate, so a leaked `ca.key` or a CA
    imported into a browser cannot be used for other sites. `cert renew` verifies the new
    certificate against `ca.pem` with webpki before replacing any file and refuses names
    outside the constraints, listing them. `cert new-ca [--hostname ...] [--ip ...]`
    creates a new constrained CA and server certificate for the given names; the previous
    files are kept as `ca.pem.old-<UTC>` and `ca.key.old-<UTC>`. Browsers must import the
    new `ca.pem` and workers must be re-enrolled with it. A CA from before name
    constraints existed keeps working; `info` reports it as unconstrained.
  - `--bind` (or `BRIDGE_COORDINATOR_BIND` when the flag is absent) is written to
    `config.toml`; a non-loopback bind also writes `lan_enabled = true`
    (or the value of `BRIDGE_COORDINATOR_LAN_ENABLED` when set). The default bind is
    `127.0.0.1:8443`.
- `bridge-coordinator serve --data-dir DIR` serves HTTPS (rustls; TLS 1.3 by
  default, with TLS 1.2 and AEAD suites only when `min_tls = "1.2"` is explicitly
  set for an older client). Session resumption tickets and 0-RTT are disabled. It
  refuses to start when the bind address is not
  loopback and `lan_enabled = true` is not set (NET-003). It holds an exclusive
  lock on `DIR/coordinator.lock` while running (released by the kernel on any
  exit). Logs go to stderr only; nothing is written outside the data directory
  (SQLite temporary storage is in memory). SIGTERM or SIGINT drains connections and
  exits within 10 s.
- `bridge-coordinator info` prints the URLs (flagging any that the server
  certificate does not cover), bind address, LAN state, CA path and SHA-256
  fingerprint and whether an administrator exists.
- `bridge-coordinator health` does an HTTPS `GET /healthz` against the configured
  bind (loopback for a wildcard bind: 127.0.0.1 for `0.0.0.0`, `::1` for `::`),
  verifies the certificate against `ca.pem` as `localhost`, and exits 0 when the
  answer is `200` with `{"status":"ok"}`, otherwise 1. The whole check is limited to
  3 s. It needs no shell, DNS or other tools, so it works as a container
  `HEALTHCHECK` and in monitoring.
- `bridge-coordinator check-config [--data-dir DIR] [--web-dir DIR]` validates
  `config.toml` and the `BRIDGE_COORDINATOR_*` environment variables without binding a
  port, opening the database or reading the keys. It reports every problem in one run,
  one line each, in the form
  `config error: <key> = <value>: <problem>; fix: <what to do>`, and exits 1; for a
  valid configuration it prints one `config ok: ...` line and exits 0. `serve` runs the
  same validation before it binds anything and prints the same lines. The checks
  (`config_check.rs`):
  - the file must be valid TOML; a syntax error is reported with its line number and
    stops the other checks, because no keys can be read;
  - an unknown key is an error. When a known key is within a small edit distance (or
    the unknown key is the start of a known one, such as `ws_ping_interval`), the line
    says `did you mean "<key>"?`; otherwise it lists the known keys;
  - each value must have the right type (`bind` string, `lan_enabled` boolean, limits
    whole numbers, `allowed_origins` list of strings, `web_dir` string) and range:
    `max_events_per_worker`, `max_connections`, `max_connections_per_ip` and the
    `*_secs` keys must be at least 1, `worker_reserve_connections` at least 0, and
    `ws_idle_timeout_secs` larger than `ws_ping_interval_secs` (compared with the
    default of the key that is not set);
  - `bind` must be `ip:port` with a numeric address and a port from 0 to 65535 (a host
    name is not accepted); a non-loopback `bind` needs `lan_enabled = true`, taking the
    environment variables into account;
  - each `allowed_origins` entry must be `scheme://host[:port]` with `http` or `https`
    and no path; `web_dir` must contain `index.html` (not checked when `--web-dir` is
    given, as `serve` ignores `web_dir` then);
  - an environment variable with a bad value is reported under its own name:
    `BRIDGE_COORDINATOR_BIND` (same rule as `bind`), `BRIDGE_COORDINATOR_LAN_ENABLED`
    (a boolean), `BRIDGE_COORDINATOR_PUBLIC_PORT` (1 to 65535) and
    `BRIDGE_COORDINATOR_PUBLIC_NAMES` (each item a DNS name or IP address).
  Commands other than `serve` and `check-config` (for example `info` and `health`)
  load the file with the same checks for syntax, keys, types and ranges, but do not
  require `lan_enabled` or an existing `web_dir`.
- `bridge-coordinator backup --output FILE` and `restore --input FILE` copy and
  replace the database; the procedure, the files a backup does not contain and the
  restore checks are in `install.md` (Backup and restore).
- `bridge-coordinator setup-token` replaces the setup token file; it fails once an
  administrator exists.
- Environment variables (names in `config.rs`):
  - `BRIDGE_COORDINATOR_DATA_DIR`: data directory when `--data-dir` is not given;
    default `/var/lib/bridge-coordinator`.
  - `BRIDGE_COORDINATOR_BIND` (`host:port`) and `BRIDGE_COORDINATOR_LAN_ENABLED`
    (`true`/`false`; `1/0`, `yes/no`, `on/off` are also accepted): take precedence
    over `config.toml`.
  - `BRIDGE_COORDINATOR_PUBLIC_NAMES`: comma-separated host names and addresses that
    browsers and workers use. When set, they replace discovery for the certificate
    (`init`, `cert renew`) and for the URLs `info` prints. It is needed in a
    container, where the discovered names are container-internal. `compose.yaml` sets
    it from `BRIDGE_PUBLIC_NAMES`.
  - `BRIDGE_COORDINATOR_PUBLIC_PORT`: port in the printed URLs when it differs from
    the bind port (a published container port). `compose.yaml` sets it from
    `BRIDGE_PORT`.
  - `BRIDGE_COORDINATOR_TLS_CERT_FILE`, `BRIDGE_COORDINATOR_TLS_KEY_FILE` and
    `BRIDGE_COORDINATOR_TLS_CERT_NAME`: optional operator-file certificate, private key
    and exact SNI hostname. The file identity is served only for that hostname; other
    names continue using the internal CA. Valid replacement pairs are picked up for
    new TLS handshakes without restarting the server.
  - `BRIDGE_COORDINATOR_CONTAINER`: set by the container image. Network exposure is
    then decided by the published port, not by `lan_enabled`.

  All other settings are keys of `config.toml`: `bind`, `lan_enabled`,
  `max_events_per_worker`, `allowed_origins` (extra exact `Origin` values accepted for
  cookie-authenticated writes), `web_dir` (default: `web` next to the executable, then
  `/usr/share/bridge-coordinator/web`) and the limit keys in the table below. All
  defaults are defined in `config.rs` (`Config::default`); `init` generates
  `config.toml` from them.
- `bridge-coordinator worker add NAME` prints a new worker token once and
  stores its SHA-256 hash; `worker revoke NAME` deletes it.
- `bridge-coordinator admin reset-password [--temporary] [--keep-api-tokens] [--username NAME]`
  (local shell only; over SSH use the wrapper `llm-bridge-coordinator reset-password`, which
  runs it through `docker compose exec` or `sudo`; `install.md`, Locked out or forgot the
  password). On a terminal it asks for the new password twice without echo; with standard
  input redirected it reads one line. The password rules are those of `POST /auth/setup`
  and `/auth/password` (at least 12, at most 256 bytes). It deletes all sessions and
  revokes every API token (the number is printed) unless `--keep-api-tokens` is given,
  and writes the audit entry `password.reset` (actor `cli`, remote `local`).
  `--temporary` generates a one-time password, prints it once, and sets the
  change-required flag (see below). `--username` must name the only administrator
  account (there is exactly one). The command runs as the service user or as root; as
  root it gives the database files back to the data directory's owner.
  The change-required flag and the reset time are kept in the `meta` table (keys
  `password_change_required` and `password_reset_at`), so no schema migration is involved;
  a password change through `POST /auth/password` deletes both keys.
- `bridge-coordinator cert renew [--hostname ...] [--ip ...]` replaces the
  server certificate from the existing CA (names must be within the CA's name
  constraints); `cert new-ca` replaces the CA; `cert install --cert F --key F`
  installs administrator-provided files.
- `bridge-coordinator token create NAME [--scope SCOPE]... [--expires-days N]` prints an API
  token once; only its SHA-256 hash and grants are stored, and `token revoke NAME` revokes it.
  New tokens default to `read`; repeat `--scope` for `operate`, `terminal:view`, `terminal:write`
  or `admin`. `admin` grants every scope. Tokens expire after 90 days by default;
  `--expires-days 0` creates one that never expires. Existing tokens are migrated to `admin`
  so their previous permissions remain unchanged.

## HTTP API (all under `/api/v1`, JSON)

The generated OpenAPI 3.1 description is served at `/api/v1/openapi.json`; a
script-free reference page is served locally at `/api/docs`. The document is
generated from the route policy table and Rust response schemas, and its
committed snapshot is checked in CI. See the [API reference and examples](api.md)
for resource requests, CLI parity decisions and curl walkthroughs. Resource
routes adapt to the existing typed command layer; `/commands` remains available
for low-level clients.

Public:

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | `{"status":"ok"}` only. |
| GET | `/api/v1/auth/state` | `{setup_required, authenticated, password_policy: {min_length, max_length}, session_policy: {remembered, default}}` (each policy entry has `idle_secs` and `absolute_secs`). With a session cookie it adds `csrf_token`, `username` and `session: {remember, expires_at, idle_timeout_secs}`. It also adds `password_change_required` (true while a temporary password from `admin reset-password --temporary` has not been replaced) and, after a reset on the host, `password_reset_notice: {at, message}` (`at` is the UTC epoch of the reset; the message is "Your password was reset by an administrator on the host."). The notice stays until the password is changed through `/auth/password` or seven days pass. The password policy is the one `POST /auth/setup` and `/auth/password` enforce (length in bytes), so the browser shows the real rules before submission. |
| POST | `/api/v1/auth/setup` | `{setup_token, username, password}`; only while no administrator exists; consumes the token. |
| POST | `/api/v1/auth/login` | `{username, password, remember?}`; `remember` (boolean, default false) selects the session lifetime: remembered sessions use an 8 h idle and 7 day absolute limit and a persistent cookie (`Max-Age`); other sessions use a 2 h idle and 12 h absolute limit and a browser-session cookie (no `Max-Age`). Sets cookie `__Host-bridge_session` (HttpOnly, Secure, SameSite=Strict, Path=/, no Domain; the `__Host-` prefix makes browsers refuse a copy set by another host name); returns `{csrf_token}`. Same error and comparable timing for unknown user and wrong password. |

Authenticated (session cookie, or `Authorization: Bearer <api token>`):

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/logout` | Deletes the session. |
| POST | `/auth/password` | `{current, new}`; deletes every session. While `password_change_required` is true, every request made with a session cookie except this one, `/auth/logout` and `GET /auth/state` is answered with `403` and error code `password-change-required`. API tokens are not affected. A successful change clears the flag and the reset notice. |
| POST | `/auth/passkeys/registration/start` | Browser session plus CSRF and exact Origin required; `{name}` returns a five-minute, session-bound registration challenge and WebAuthn creation options. Only HTTPS DNS origins are supported; IP origins are refused. |
| POST | `/auth/passkeys/registration/finish` | Browser session plus CSRF and the same Origin required; `{challenge_id, credential}` verifies once and stores the passkey. |
| POST | `/auth/passkeys/assertion/start` | Public sign-in start, exact HTTPS DNS Origin required; `{}` returns assertion options and sets a short-lived, single-use HttpOnly browser ticket. No registered credentials returns 404. |
| POST | `/auth/passkeys/assertion/finish` | `{challenge_id, credential}` plus the browser ticket and same Origin; verifies UV and creates a normal short-lived session cookie. |
| GET | `/workers` | name, stable id, state (`online`/`offline`), last_seen, versions, platform, capabilities, enrolled host name, and the active connection's peer address. The address is null while offline. |
| GET | `/workers/{name}` | The same safe worker summary fields as the list route. |
| POST | `/workers` | `{name}` → `{token}` shown once. |
| DELETE | `/workers/{name}` | Revoke; closes its connection. |
| GET | `/tokens` | Token name, scopes, creation, last-use and expiry metadata. Requires an administrator session or an `admin` token. |
| POST | `/tokens` | `{name, scopes?, expires_days?}`; `scopes` defaults to `read`, expiry to 90 days (`0` means no expiry). Returns the secret once. Requires an administrator session with CSRF and exact Origin, or an `admin` token. Creation is audited. |
| DELETE | `/tokens/{name}` | Revoke a token. Requires an administrator session with CSRF and exact Origin, or an `admin` token. Revocation is audited. |
| GET | `/missions` | Mission index across workers with `worker`, `stale` (worker offline or last hello older than its last event). A mission may include bounded `needs_you: {kind, message, command}` when its native provider is waiting on a structured question; the message contains only question/choice text and the local answer command. Optional query: `q` (case-insensitive substring of name, id, folder or worker), `worker`, `status` (comma list), `sort` (`name`, `status`, `worker`, `updated`), `order` (`asc`, `desc`), `limit` (default 100, maximum 500), `cursor`, or legacy `offset`. The reply adds `total`, `offset`, `next_cursor` and `statuses` (match counts per status, ignoring the status filter). Cursors are authenticated and encrypted, opaque, bound to the normalized filters and sort order, and cannot be combined with `offset`; use the returned `next_cursor` unchanged to fetch the next page. |
| POST | `/commands` | `{target, request}` where `request` is a `CommandRequest`. Optional `Idempotency-Key` replaces its `request_id`; replay returns the stored journal entry. A command for an offline worker is queued with `state: pending`, returns 202 and `Location: /api/v1/commands/{request_id}`, and is delivered on reconnect. The coordinator rejects unknown targets (404 `unknown-target`) and operations the worker did not advertise (`unsupported-operation`). Optional `wait_ms` (≤ 30000) waits for the result. |
| GET | `/commands/{request_id}` | Journal entry: `state` (`pending`/`sent`/`acked`/`done`), `result`. |
| GET | `/missions/{id}/tasks?worker=&limit=&cursor=` | Typed `task.list`; default limit 50, maximum 1000. Returns a command result with `next_cursor` when another page exists. |
| GET | `/missions/{id}/events?worker=&limit=&cursor=` | Typed `mission.events`; default limit 50, maximum 1000. Legacy `after` remains available. |
| GET | `/missions/{id}/operations?worker=&limit=&cursor=` | Typed `session.list`; default limit 100, maximum 1000; returns metadata only. |
| GET | `/events?worker=&after=&limit=` | Stored events, ascending. |
| GET | `/stream` | WebSocket; pushes `worker`, `event` and `command` updates. |
| GET | `/terminal?worker=&mission=&mode=view\|write&cols=&rows=` | WebSocket for one remote terminal session. Session cookie (with exact Origin) or API token. The coordinator opens the session on the worker with `terminal.open` and relays the frames of `protocol.md` (Terminal sessions) unchanged after validation; the frame and backlog bounds are specified there and the session counts are in the limits table below (over the cap: `x` frame, close 1013). Each open is audited as `terminal.open`. The worker, not the coordinator, decides the writer role. |
| GET | `/worker/terminal/{session_id}` | Worker side of a session; worker bearer token; accepted once, only for a session opened for that worker. |
| GET | `/diagnostics` | versions, uptime, RSS, DB size, connected workers, queue depths. |

Every state-changing request authenticated by cookie needs header
`X-CSRF-Token` equal to the session's token and an `Origin` equal to the
coordinator's own origin; WebSocket upgrades check `Origin` too (AUTH-007).
Cookie requests carrying Fetch Metadata must be `same-origin`, with the single
exception of a top-level `GET` navigation to a console HTML page; cross-site API,
asset and WebSocket requests are rejected. Requests with no Fetch Metadata remain
usable by non-browser clients and are still subject to authentication, exact
Origin and CSRF rules where applicable.
Bearer API tokens are not subject to CSRF checks because browsers never send
them automatically.

API errors are RFC 9457 problem details (`application/problem+json`) with a
stable type URI derived from the existing error code. The nested `error` object
is retained for the current web client. Unknown API paths return a problem
response; unsupported methods return 405 with an `Allow` header.

## Public API contract and compatibility

Use a token in the `Authorization: Bearer` header over HTTPS. New tokens default
to `read`; mission and task changes need `operate`; terminal viewing and control
need `terminal:view` and `terminal:write`; worker administration, token
management and diagnostics need `admin`. The browser console uses its session
cookie and CSRF token; worker channel routes accept only that worker's credential.
Bearer credentials are never accepted in URLs. `/tokens` and `/diagnostics` are
admin-only even though their methods are `GET`.

For a mission or task action that creates a durable worker command, send an
`Idempotency-Key` that is unique to the intended action. The coordinator stores
it as the durable command request ID; retrying the same key replays its stored
outcome. An offline worker returns `202 Accepted` and a
`Location` pointing at `/api/v1/commands/{request_id}`. Poll that resource until
the command is terminal. If the outcome is `uncertain`, treat it as unknown and
inspect mission state before deciding what to do next; it is not a success receipt.

Task reads return an `ETag` with the task revision when the worker answers
synchronously. Send that value in `If-Match` on an amend or verification request.
The coordinator passes it to the worker as `expected_revision`; a stale revision
returns `412 Precondition Failed`. A body revision and header must agree.

Lists have stable sorting and bounded pages. Mission lists default to 100 entries
and cap at 500; task and event lists default to 50, and operation lists to 100,
each with a maximum of 1000. Prefer the opaque `cursor` and the returned
`next_cursor`; reuse it unchanged with the same worker, mission, resource and
filters. Cursors are authenticated and encrypted. `offset` and event `after`
parameters are retained for compatibility where documented. All timestamps are
RFC 3339 UTC.

Errors use `application/problem+json`. The stable problem `type` and `code`
identify the existing coordinator error. Common statuses include `401` for a
missing/invalid credential, `403` for a missing scope, `409` for a conflicting
state, `412` for a stale task revision, `415` for a non-JSON mutation body, and
`429` for a rate limit. Bearer-authenticated mutations allow 30 requests per
token and 120 per client address in each rolling 60-second window. A limited
request includes `Retry-After`. Every bearer use and mutation is audited with
token metadata and request ID; the secret itself is never logged. Cookie-session
mutations and public passkey ceremony requests also consume the per-address
budget, without a per-token budget.

Within `/api/v1`, additive fields and operations may be introduced without a
version change. A breaking change requires `/api/v2`. Before retiring a v1
operation, responses will include `Deprecation` and `Sunset` headers and the API
reference will state the replacement and removal date. The sunset date will be
at least 90 days after the deprecation notice. The generated OpenAPI document
and compatibility test are the reviewable contract for each release.

## Limits and timeouts (COORD-004, NET-010)

| Limit | Default | Setting |
|---|---|---|
| Concurrent TCP connections, all kinds | 256; a connection over the cap is closed immediately after `accept` | `max_connections` |
| Concurrent connections per client address (IPv6 counted by /64) | 32; over the cap is closed immediately. Behind a proxy or a published container port every client has the proxy's address: raise it | `max_connections_per_ip` |
| Worker reserve | 16 of the 256 slots (at most a quarter of `max_connections`) are used only after the other slots are full. A connection in the reserve has a 5 s handshake and header limit and may only carry `/api/v1/worker/*` with a valid worker token; any other request gets 503 and the connection is closed. Unauthenticated clients that fill the general slots therefore cannot stop workers from reconnecting | `worker_reserve_connections` |
| TLS handshake | 10 s | fixed |
| Wait for the next HTTP request header (keep-alive idle and slow-header limit) | 30 s | `http_idle_timeout_secs` |
| Receive a complete request body once the handler reads it (slow-body limit); the connection is closed with 408 | 30 s | `request_body_timeout_secs` |
| Browser `/stream` and `/terminal` WebSockets | server ping every 30 s; closed with 1001 `idle timeout` when nothing (pong or message) arrived for 90 s, checked at each ping | `ws_ping_interval_secs`, `ws_idle_timeout_secs` |
| Worker connections | 64; ping every 20 s; closed after 60 s of silence | fixed |
| Terminal sessions | 16 per worker, 64 total | fixed |
| Request body | 1 MiB; 4 KiB on `/api/v1/auth/*` (413 above) | fixed |
| Request bodies above 64 KiB buffered at once | 8 (over that: 503 `busy`, so at most 8 MiB of large bodies in memory); a body above 64 KiB takes a slot only after authentication | fixed |
| Pre-hello worker connections | 1 per worker token, 10 s to send hello (second connection: 429) | fixed |
| Graceful shutdown | 10 s | fixed |
| Per-connection outbound queue | 256 messages (full queue closes the connection; the worker reconnects and the journal redelivers) | fixed |
| Stored events per worker | 50 000, oldest deleted first, in passes that keep 90 % of the cap once it is reached (worker databases keep the authoritative copy) | `max_events_per_worker` |
| Completed commands kept | 7 days | fixed |
| Login failures | 5 per minute per client address (IPv6 by /64) and per username; see below | fixed |
| Bearer mutations | 30 per token and 120 per client address by default in a rolling 60-second window; rejected requests receive 429 with `Retry-After` | `mutation_limit_per_token`, `mutation_limit_per_address` (1–10,000) |

Every bearer-authenticated API request also writes an audit row with the token name,
granted scopes, method, path, client address and supplied request ID. The bearer
secret is never recorded. Mutation counters are in memory and reset on coordinator
restart.

Login rate limiting. Before the body is read and before Argon2 runs, a login reserves
one attempt for the client address under the limiter lock. Attempts still in progress
count against the limit, so 60 simultaneous wrong passwords from one address let 5
reach Argon2 and answer the other 55 with 429. A failure is recorded when the attempt
ends (also when the client disconnects mid-check); a successful login releases the
reservation. When the table of tracked keys (4096) is full of live entries, a new key
is refused with 429 instead of being left untracked. The per-username limit stops
guessing from many addresses but is not applied to an address with a successful login
in the last 7 days (stored as a salted hash of the address, `known_addrs`), so bad
passwords sent for `admin` by others cannot lock the administrator out of the
addresses they normally use. The per-address limit applies to every address, and
known addresses may use a small reserve in the key table. The administrator logging in
from a new address while someone is guessing `admin` waits for the window (at most a
minute) or restarts the coordinator, which clears the in-memory counters.

`ws_idle_timeout_secs` must be larger than `ws_ping_interval_secs`. The worker-side
terminal WebSocket is not subject to the browser ping rule: the worker control
connection already has its own liveness check and ends the session when it drops.
The limits are per process; they bound resource use but are not a substitute for a
firewall (see `install.md`).

## Storage

SQLite (WAL, `synchronous=FULL`) with `PRAGMA user_version` migrations:
`workers`, `missions`, `events`, `commands`, `admin`, `sessions`,
`api_tokens` (with `expires_at`), `audit`, `worker_generations` (per-name
high-water mark of issued generations; survives `worker revoke`, so a worker that is
revoked and added again receives a generation above every earlier one),
`known_addrs` and `meta` (schema version 2). Passwords use Argon2id (19 MiB, t=2, p=1, unique salt);
session and API tokens are 32 random bytes stored as SHA-256 hashes.

## Content Security Policy

Static files are served with `default-src 'self'`, `script-src 'self'` and
`style-src 'self' 'unsafe-inline'`. Inline styles are allowed because xterm.js
6 creates `<style>` elements at runtime and has no nonce option; scripts stay
restricted to the coordinator's own files.
