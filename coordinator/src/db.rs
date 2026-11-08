//! SQLite storage (WAL, synchronous=FULL, `user_version` migrations).

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, MutexGuard};

/// Session lifetime of a device the administrator chose to remember at sign-in.
pub const SESSION_IDLE_SECS: i64 = 8 * 3600;
pub const SESSION_ABSOLUTE_SECS: i64 = 7 * 86_400;
/// Session lifetime without "remember this device": the cookie is a browser-session cookie and the
/// server expires the session sooner as well.
pub const SESSION_IDLE_SECS_SHORT: i64 = 2 * 3600;
pub const SESSION_ABSOLUTE_SECS_SHORT: i64 = 12 * 3600;

/// `(idle, absolute)` seconds for a session.
pub const fn session_policy(remember: bool) -> (i64, i64) {
    if remember {
        (SESSION_IDLE_SECS, SESSION_ABSOLUTE_SECS)
    } else {
        (SESSION_IDLE_SECS_SHORT, SESSION_ABSOLUTE_SECS_SHORT)
    }
}
/// How stale a stored session `last_seen` or API token `last_used` may get before a request
/// rewrites it. Each rewrite is a committed, fsync'd transaction; a browser polling the API
/// would otherwise cause one a minute. The shortest idle timeout is two hours, so five minutes
/// moves an idle expiry by at most 4%.
pub const TOUCH_EVERY_SECS: i64 = 300;
pub const COMPLETED_COMMAND_KEEP_SECS: i64 = 7 * 86_400;
/// A finished command keeps its result body for this long. Read-only commands (status, list) return
/// several KB each and nobody reads the body after the requesting client has polled it once.
pub const READ_RESULT_KEEP_SECS: i64 = 3600;
/// Result bodies of finished mutating commands are kept for a day, then reduced to the outcome.
pub const MUTATING_RESULT_KEEP_SECS: i64 = 86_400;
/// Results at or below this size stay whole until the row is deleted.
const RESULT_STUB_ABOVE_BYTES: i64 = 1024;
/// At most this many finished commands are kept, newest first, whatever their age.
pub const MAX_DONE_COMMANDS: i64 = 5_000;
/// Rows deleted or rewritten per statement in the maintenance pass. The connection mutex is
/// released between statements so a large backlog never holds off the request handlers.
const MAINTENANCE_BATCH: i64 = 50;
/// Pause between maintenance statements; request handlers take the connection mutex in this gap.
const MAINTENANCE_PAUSE: std::time::Duration = std::time::Duration::from_millis(5);
/// Free pages returned per statement (512 KiB at the 4 KiB page size).
const VACUUM_CHUNK_PAGES: i64 = 128;
/// Free pages returned to the file per maintenance pass when the database uses incremental
/// auto-vacuum (about 8 MiB at the 4 KiB page size).
pub const INCREMENTAL_VACUUM_PAGES: i64 = 2_048;
/// The write-ahead log file is cut back to this size after a checkpoint has emptied it.
const WAL_SIZE_LIMIT: i64 = 16 * 1024 * 1024;
pub const MAX_OPEN_COMMANDS_PER_WORKER: i64 = 256;
/// The journal is global durable state; count limits alone let large requests pin unbounded RAM
/// during reconnect replay and consume the worker's finite disk. Keep both individual and total
/// outstanding payloads bounded before inserting them.
pub const MAX_COMMAND_REQUEST_BYTES: usize = 64 * 1024;
pub const MAX_OPEN_COMMAND_BYTES_PER_WORKER: i64 = 512 * 1024;
pub const MAX_OPEN_COMMAND_BYTES_TOTAL: i64 = 8 * 1024 * 1024;
const AUDIT_KEEP: i64 = 100_000;

const SCHEMA: &str = "
CREATE TABLE workers(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, worker_id TEXT,
  generation INTEGER NOT NULL DEFAULT 0, event_cursor INTEGER NOT NULL DEFAULT 0, mission_cursor INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, last_seen INTEGER, hello_at INTEGER, last_event_at INTEGER,
  software_version TEXT, protocol_versions TEXT, platform TEXT, capabilities TEXT,
  host_name TEXT, remote_address TEXT, worker_policy TEXT);
CREATE TABLE missions(
  worker TEXT NOT NULL REFERENCES workers(name) ON DELETE CASCADE, mission_id TEXT NOT NULL, info TEXT NOT NULL,
  PRIMARY KEY(worker, mission_id)) WITHOUT ROWID;
CREATE TABLE events(
  worker TEXT NOT NULL REFERENCES workers(name) ON DELETE CASCADE, id INTEGER NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY(worker, id)) WITHOUT ROWID;
CREATE TABLE commands(
  request_id TEXT PRIMARY KEY, worker TEXT NOT NULL REFERENCES workers(name) ON DELETE CASCADE,
  operation TEXT NOT NULL, mutating INTEGER NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL,
  result TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX commands_open ON commands(worker, state);
CREATE TABLE admin(
  id INTEGER PRIMARY KEY CHECK(id = 1), username TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE sessions(
  token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL,
  expires_at INTEGER NOT NULL);
CREATE TABLE api_tokens(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, last_used INTEGER,
  scopes TEXT NOT NULL DEFAULT '[\"admin\"]');
CREATE TABLE audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL, remote TEXT NOT NULL);
";

/// Run `f` (a database call: SQLite work or a wait for the connection) so that it never occupies an
/// async worker thread. On a multi-thread runtime the worker hands its queue to another thread for the
/// duration (`block_in_place`), so accepting connections, TLS handshakes and requests that need no
/// database continue while a commit is slow. Outside a runtime, and on a current-thread runtime, the
/// call just runs.
pub(crate) fn blocking<T>(f: impl FnOnce() -> T) -> T {
    match tokio::runtime::Handle::try_current() {
        Ok(h) if h.runtime_flavor() == tokio::runtime::RuntimeFlavor::MultiThread => tokio::task::block_in_place(f),
        _ => f(),
    }
}

/// A database call that takes at least this long (including the wait for the connection) is reported
/// on stderr with its name, so a request stall can be traced to the call that held the connection.
const SLOW_CALL: std::time::Duration = std::time::Duration::from_millis(500);

/// Report a maintenance step that held the connection for [`SLOW_CALL`] or longer.
fn report_slow_step(step: &str, started: std::time::Instant) {
    let took = started.elapsed();
    if took >= SLOW_CALL {
        eprintln!("slow database maintenance step {step}: {} ms", took.as_millis());
    }
}

/// [`blocking`] plus the slow-call report. `maintenance` is a long pass by design and is not reported.
pub(crate) fn blocking_named<T>(name: &str, f: impl FnOnce() -> T) -> T {
    let started = std::time::Instant::now();
    let out = blocking(f);
    let took = started.elapsed();
    if took >= SLOW_CALL && name != "maintenance" {
        eprintln!("slow database call {name}: {} ms", took.as_millis());
    }
    out
}

/// Wraps the body of every `pub fn` of an `impl` block in [`blocking_named`]; other functions are copied
/// unchanged (they are only called from wrapped ones).
macro_rules! offload_impl {
    () => {};
    ($(#[$m:meta])* pub fn $name:ident ($($args:tt)*) -> $ret:ty $body:block $($rest:tt)*) => {
        $(#[$m])* pub fn $name($($args)*) -> $ret { $crate::db::blocking_named(stringify!($name), || -> $ret { $body }) }
        $crate::db::offload_impl! { $($rest)* }
    };
    ($(#[$m:meta])* pub fn $name:ident ($($args:tt)*) $body:block $($rest:tt)*) => {
        $(#[$m])* pub fn $name($($args)*) { $crate::db::blocking_named(stringify!($name), || { $body }) }
        $crate::db::offload_impl! { $($rest)* }
    };
    ($(#[$m:meta])* $vis:vis fn $name:ident ($($args:tt)*) $(-> $ret:ty)? $body:block $($rest:tt)*) => {
        $(#[$m])* $vis fn $name($($args)*) $(-> $ret)? $body
        $crate::db::offload_impl! { $($rest)* }
    };
}
pub(crate) use offload_impl;

/// Longest a caller waits for the connection before getting a busy error.
static LOCK_WAIT_MS: AtomicU64 = AtomicU64::new(3_000);
/// Callers allowed to wait for the connection at once when the server has not set its own limit. Every
/// connection has at most one request or worker message in flight, and the default `max_connections`
/// is 256, so a burst can legitimately put 256 callers in the queue; the margin covers the retention
/// pass and the background tasks. Each waiter occupies a thread that the runtime handed over (the
/// blocking pool allows 512), so the number stays bounded.
pub const DEFAULT_MAX_LOCK_WAITERS: usize = 288;
/// Upper limit for [`Db::set_max_lock_waiters`], below the 512 threads of tokio's blocking pool.
pub const LOCK_WAITERS_CEILING: usize = 480;

pub fn lock_wait() -> std::time::Duration {
    std::time::Duration::from_millis(LOCK_WAIT_MS.load(Ordering::Relaxed))
}

/// Change the connection wait (process wide). Tests use it to keep a stall short.
pub fn set_lock_wait(d: std::time::Duration) {
    LOCK_WAIT_MS.store(d.as_millis() as u64, Ordering::Relaxed);
}

/// The error a caller gets when the database does not answer in time: `SQLITE_BUSY`, which the HTTP layer
/// reports as 503 `database-busy`.
pub(crate) fn busy(message: &str) -> rusqlite::Error {
    rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_BUSY), Some(message.to_string()))
}

/// True for the busy and locked errors of SQLite and of [`busy`].
pub fn is_busy(e: &rusqlite::Error) -> bool {
    matches!(
        e,
        rusqlite::Error::SqliteFailure(f, _) if matches!(f.code, rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked)
    )
}

/// The connection while a caller holds it. It records how long it was held, so the longest hold and the
/// most callers ever waiting at once can be read back ([`Db::lock_stats`]). Dropping it hands the
/// connection to the caller that has waited longest.
pub struct ConnGuard<'a> {
    inner: Option<MutexGuard<'a, Connection>>,
    gate: &'a Gate,
    stats: &'a LockStats,
    since: std::time::Instant,
}

impl std::ops::Deref for ConnGuard<'_> {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.inner.as_deref().expect("guard holds the connection until it is dropped")
    }
}

impl std::ops::DerefMut for ConnGuard<'_> {
    fn deref_mut(&mut self) -> &mut Connection {
        self.inner.as_deref_mut().expect("guard holds the connection until it is dropped")
    }
}

impl Drop for ConnGuard<'_> {
    fn drop(&mut self) {
        self.stats.max_hold_us.fetch_max(self.since.elapsed().as_micros() as u64, Ordering::Relaxed);
        drop(self.inner.take()); // release the connection first, then wake the next caller
        self.gate.release();
    }
}

/// First-come first-served admission to the connection. A caller that cannot get it at once joins a
/// queue and sleeps on its own condition variable; the caller that releases the connection wakes only
/// the head of the queue. (A polling `try_lock` loop is not fair: callers that arrive later take the
/// connection ahead of a sleeping poller, and on Windows a short sleep lasts about 15 ms, so a burst
/// of requests queued up faster than it drained.)
#[derive(Default)]
struct Gate {
    state: Mutex<GateState>,
}

#[derive(Default)]
struct GateState {
    held: bool,
    next_ticket: u64,
    queue: std::collections::VecDeque<(u64, std::sync::Arc<std::sync::Condvar>)>,
}

impl Gate {
    fn state(&self) -> MutexGuard<'_, GateState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn waiting(&self) -> usize {
        self.state().queue.len()
    }

    fn release(&self) {
        let mut st = self.state();
        st.held = false;
        if let Some((_, cv)) = st.queue.front() {
            cv.notify_one();
        }
    }

    /// Wait for the connection for at most `wait`; refuse at once when `max_waiters` callers are queued.
    fn enter(&self, wait: std::time::Duration, max_waiters: usize, stats: &LockStats) -> R<()> {
        let mut st = self.state();
        if !st.held && st.queue.is_empty() {
            st.held = true;
            return Ok(());
        }
        if st.queue.len() >= max_waiters {
            return Err(busy("too many requests are waiting for the database"));
        }
        let ticket = st.next_ticket;
        st.next_ticket += 1;
        let cv = std::sync::Arc::new(std::sync::Condvar::new());
        st.queue.push_back((ticket, cv.clone()));
        stats.peak_waiters.fetch_max(st.queue.len(), Ordering::Relaxed);
        let deadline = std::time::Instant::now() + wait;
        loop {
            if !st.held && st.queue.front().is_some_and(|(t, _)| *t == ticket) {
                st.queue.pop_front();
                st.held = true;
                return Ok(());
            }
            let now = std::time::Instant::now();
            if now >= deadline {
                st.queue.retain(|(t, _)| *t != ticket);
                if !st.held {
                    if let Some((_, next)) = st.queue.front() {
                        next.notify_one(); // this caller was the head and the connection is free
                    }
                }
                return Err(busy("the database is busy (a write is taking too long)"));
            }
            st = cv.wait_timeout(st, deadline - now).unwrap_or_else(|e| e.into_inner()).0;
        }
    }
}

#[derive(Default)]
struct LockStats {
    max_hold_us: AtomicU64,
    peak_waiters: AtomicUsize,
}

/// Longest time one caller held the connection, and the most callers that waited for it at once.
#[derive(Debug, Clone, Copy)]
pub struct LockSnapshot {
    pub max_hold: std::time::Duration,
    pub peak_waiters: usize,
}

pub struct Db {
    conn: Mutex<Connection>,
    gate: Gate,
    max_waiters: AtomicUsize,
    stats: LockStats,
    path: PathBuf,
    /// Stored event rows per worker, kept so that retention does not have to count them on every batch.
    /// It is an upper-bound estimate: a revoke from another process or a replaced row leaves it high,
    /// which only makes the next retention pass run early and correct the figure.
    event_counts: Mutex<HashMap<String, u64>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WebauthnChallengeRow {
    pub principal_id: String,
    pub binding: String,
    pub kind: String,
    pub state: String,
    pub expires_at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PasskeyCredentialRow {
    pub credential_id: String,
    pub principal_id: String,
    pub credential: String,
    pub user_name: String,
    pub created_at: i64,
    pub last_used: Option<i64>,
    pub aaguid: Option<String>,
    pub transports: String,
    pub backup_eligible: bool,
    pub backed_up: bool,
    pub sign_count: u32,
}

#[derive(Debug, Clone)]
pub struct WorkerRow {
    pub name: String,
    pub worker_id: Option<String>,
    pub generation: u64,
    pub event_cursor: u64,
    pub mission_cursor: u64,
    pub last_seen: Option<i64>,
    pub hello_at: Option<i64>,
    pub last_event_at: Option<i64>,
    pub software_version: Option<String>,
    pub protocol_versions: Option<Value>,
    pub platform: Option<Value>,
    pub capabilities: Option<Value>,
    pub host_name: Option<String>,
    pub remote_address: Option<String>,
    pub worker_policy: Option<Value>,
    pub client_cert_fingerprint: Option<String>,
}

impl WorkerRow {
    /// `Some(mutating)` when the last hello advertised `operation`.
    pub fn capability(&self, operation: &str) -> Option<bool> {
        self.capabilities
            .as_ref()?
            .as_array()?
            .iter()
            .find_map(|c| (c.get("operation").and_then(Value::as_str) == Some(operation)).then(|| c.get("mutating").and_then(Value::as_bool).unwrap_or(true)))
    }
}

#[derive(Debug, Clone)]
pub struct CommandRow {
    pub request_id: String,
    pub worker: String,
    pub operation: String,
    pub mutating: bool,
    pub request: Value,
    pub state: String,
    pub result: Option<Value>,
    pub created_at: i64,
    pub updated_at: i64,
}

impl CommandRow {
    pub fn to_json(&self) -> Value {
        json!({"request_id": self.request_id, "target": self.worker, "operation": self.operation, "mutating": self.mutating,
               "request": self.request, "state": self.state, "result": self.result,
               "created_at": self.created_at, "updated_at": self.updated_at})
    }
}

impl CommandRow {
    /// The change notice published on the browser stream: identity and state without the request
    /// parameters or the result body (a status result alone is several KB, a task list far more).
    /// A client that needs them reads `GET /api/v1/commands/{request_id}`.
    pub fn to_notice(&self) -> Value {
        let outcome = self.result.as_ref().and_then(|r| r.get("outcome")).cloned().unwrap_or(Value::Null);
        json!({"request_id": self.request_id, "target": self.worker, "operation": self.operation, "mutating": self.mutating,
               "state": self.state, "outcome": outcome, "created_at": self.created_at, "updated_at": self.updated_at})
    }
}

pub enum InsertOutcome {
    New(CommandRow),
    Existing(CommandRow),
    Conflict,
    QueueFull,
    RequestTooLarge,
}

pub enum EventsOutcome {
    Accepted { cursor: u64, stored: usize },
    Nack { expected: u64 },
}

pub struct EventBatch<'a> {
    pub worker: &'a str,
    pub after: u64,
    pub through: u64,
    pub events: &'a [Value],
    pub mission_cursor: Option<u64>,
    pub missions: Option<&'a [Value]>,
    pub max: u64,
}

pub enum HelloOutcome {
    Ok { generation: u64, cursor: u64 },
    WorkerIdMismatch,
    Unknown,
}

pub struct Session {
    pub principal_id: String,
    pub csrf: String,
    pub token_hash: String,
    pub remember: bool,
    pub expires_at: i64,
    pub idle_secs: i64,
}

#[derive(Debug, Clone)]
pub struct ApiToken {
    pub name: String,
    pub principal_id: String,
    pub scopes: Vec<String>,
    pub allowed_cidrs: Vec<String>,
    pub allowed_workers: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct ApiTokenInfo {
    pub name: String,
    pub principal_id: String,
    pub token_id: Option<String>,
    pub scopes: Vec<String>,
    pub created_at: i64,
    pub last_used: Option<i64>,
    pub last_address: Option<String>,
    pub expires_at: Option<i64>,
    pub allowed_cidrs: Vec<String>,
    pub allowed_workers: Vec<String>,
    pub sender_constraint_kind: Option<String>,
    pub sender_constraint_id: Option<String>,
}

pub struct ApiTokenCreate<'a> {
    pub name: &'a str,
    pub token_id: &'a str,
    pub secret_hash: &'a str,
    pub principal_id: &'a str,
    pub expires_at: Option<i64>,
    pub scopes: &'a [String],
    pub allowed_cidrs: &'a [String],
    pub allowed_workers: &'a [String],
}

fn json_col(s: Option<String>) -> Option<Value> {
    s.and_then(|s| serde_json::from_str(&s).ok())
}

const WCOLS: &str =
    "name, worker_id, generation, event_cursor, mission_cursor, last_seen, hello_at, last_event_at, software_version, protocol_versions, platform, capabilities, host_name, remote_address, worker_policy, client_cert_fingerprint";

fn worker_row(r: &rusqlite::Row) -> rusqlite::Result<WorkerRow> {
    Ok(WorkerRow {
        name: r.get(0)?,
        worker_id: r.get(1)?,
        generation: r.get::<_, i64>(2)? as u64,
        event_cursor: r.get::<_, i64>(3)? as u64,
        mission_cursor: r.get::<_, i64>(4)? as u64,
        last_seen: r.get(5)?,
        hello_at: r.get(6)?,
        last_event_at: r.get(7)?,
        software_version: r.get(8)?,
        protocol_versions: json_col(r.get(9)?),
        platform: json_col(r.get(10)?),
        capabilities: json_col(r.get(11)?),
        host_name: r.get(12)?,
        remote_address: r.get(13)?,
        worker_policy: json_col(r.get(14)?),
        client_cert_fingerprint: r.get(15)?,
    })
}

const CCOLS: &str = "request_id, worker, operation, mutating, request, state, result, created_at, updated_at";

fn command_row(r: &rusqlite::Row) -> rusqlite::Result<CommandRow> {
    Ok(CommandRow {
        request_id: r.get(0)?,
        worker: r.get(1)?,
        operation: r.get(2)?,
        mutating: r.get::<_, i64>(3)? != 0,
        request: json_col(r.get(4)?).unwrap_or(Value::Null),
        state: r.get(5)?,
        result: json_col(r.get(6)?),
        created_at: r.get(7)?,
        updated_at: r.get(8)?,
    })
}

/// Highest `PRAGMA user_version` this binary understands. Migrations are forward-only.
pub const SCHEMA_VERSION: i64 = 17;

/// Version 2: API token expiry, per-name generation high-water mark (survives worker revoke),
/// salted hashes of addresses with a recent successful login, and a small key/value table.
const MIGRATE_V2: &str = "
ALTER TABLE api_tokens ADD COLUMN expires_at INTEGER;
CREATE TABLE worker_generations(name TEXT PRIMARY KEY, high INTEGER NOT NULL);
INSERT INTO worker_generations(name, high) SELECT name, generation FROM workers;
CREATE TABLE known_addrs(hash TEXT PRIMARY KEY, last_success INTEGER NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
";

/// Version 3: sessions record whether the device was remembered at sign-in (it selects the
/// idle and absolute lifetime). Sessions that exist at migration keep the original, longer policy.
/// The statement list is built in code because a database may not have a `sessions` table yet.
const MIGRATE_V3_COLUMN: &str = "ALTER TABLE sessions ADD COLUMN remember INTEGER NOT NULL DEFAULT 1;";
const MIGRATE_V4_COLUMN: &str = "ALTER TABLE workers ADD COLUMN mission_cursor INTEGER NOT NULL DEFAULT 0;";
const MIGRATE_V5_COLUMN: &str = "ALTER TABLE api_tokens ADD COLUMN scopes TEXT NOT NULL DEFAULT '[\"admin\"]';";
const MIGRATE_V7_COLUMN: &str = "ALTER TABLE workers ADD COLUMN worker_policy TEXT;";
/// Version 8: short-lived, session-bound WebAuthn ceremony state and passkey credentials.
const MIGRATE_V8: &str = "
CREATE TABLE webauthn_challenges(
  id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, binding TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('registration','authentication')),
  state TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  CHECK(expires_at > created_at AND expires_at <= created_at + 300));
CREATE INDEX webauthn_challenges_expiry ON webauthn_challenges(expires_at);
CREATE TABLE passkeys(
  credential_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, credential TEXT NOT NULL,
  user_name TEXT NOT NULL, created_at INTEGER NOT NULL, last_used INTEGER,
  aaguid TEXT, transports TEXT NOT NULL DEFAULT '[]', backup_eligible INTEGER NOT NULL DEFAULT 0,
  backed_up INTEGER NOT NULL DEFAULT 0, sign_count INTEGER NOT NULL DEFAULT 0);
CREATE INDEX passkeys_principal ON passkeys(principal_id);
";
/// Version 9: canonical WebAuthn origin, fixed by the first completed registration.
const MIGRATE_V9: &str = "
CREATE TABLE auth_policy(
  principal_id TEXT PRIMARY KEY, passkey_origin TEXT);
";
/// Version 10: public token identifiers let authentication select a row before comparing
/// the secret hash in constant time; migrated legacy tokens keep a NULL identifier.
const MIGRATE_V10: &str = "
ALTER TABLE api_tokens ADD COLUMN token_id TEXT;
CREATE UNIQUE INDEX api_tokens_token_id ON api_tokens(token_id) WHERE token_id IS NOT NULL;
";
/// Version 11: expose the address of a token's most recent use to administrators.
const MIGRATE_V11: &str = "ALTER TABLE api_tokens ADD COLUMN last_address TEXT;";
/// Version 12: durable, dismissible security notifications. Payloads contain no credentials.
const MIGRATE_V12: &str = "
CREATE TABLE security_notifications(
  id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, category TEXT NOT NULL,
  created_at INTEGER NOT NULL, dismissed_at INTEGER);
CREATE INDEX security_notifications_principal ON security_notifications(principal_id, dismissed_at, created_at DESC);
";
/// Version 13: optionally restrict API tokens to explicitly configured client networks.
const MIGRATE_V13: &str = "ALTER TABLE api_tokens ADD COLUMN allowed_cidrs TEXT NOT NULL DEFAULT '[]';";
/// Version 14: bind optional worker client certificates and allow a bounded overlap during token rotation.
const MIGRATE_V14: &str = "
ALTER TABLE workers ADD COLUMN client_cert_fingerprint TEXT;
ALTER TABLE workers ADD COLUMN pending_token_hash TEXT;
ALTER TABLE workers ADD COLUMN pending_token_expires_at INTEGER;
CREATE UNIQUE INDEX workers_pending_token_hash ON workers(pending_token_hash) WHERE pending_token_hash IS NOT NULL;
";
/// Version 15: optionally bind API tokens to a bounded set of coordinator workers.
const MIGRATE_V15: &str = "ALTER TABLE api_tokens ADD COLUMN allowed_workers TEXT NOT NULL DEFAULT '[]';";
/// Version 16: salted device fingerprints for new-device sign-in notices; never stores user agents.
const MIGRATE_V16: &str = "CREATE TABLE known_login_devices(principal_id TEXT NOT NULL, fingerprint_hash TEXT NOT NULL, last_success INTEGER NOT NULL, PRIMARY KEY(principal_id, fingerprint_hash)); CREATE INDEX known_login_devices_recent ON known_login_devices(principal_id, last_success DESC);";
/// Version 17: durable principal ownership for security records and reserved sender-binding
/// columns for future DPoP/mTLS constrained API tokens.
const MIGRATE_V17_BASE: &str = "CREATE TABLE principals(id TEXT PRIMARY KEY, username TEXT UNIQUE, role TEXT NOT NULL, created_at INTEGER NOT NULL); INSERT OR IGNORE INTO principals(id,username,role,created_at) VALUES('system',NULL,'system',0); INSERT OR IGNORE INTO principals(id,username,role,created_at) VALUES('admin',NULL,'admin',0);";

/// Mission tombstones (PRD-038). A tombstone says that a worker deleted a mission, so a replication that
/// is still in flight or queued on an offline worker cannot bring the mission back. The table is created
/// with `IF NOT EXISTS` on every open instead of by a numbered migration: it adds no column to an existing
/// table, an older coordinator ignores it and the schema number stays as it was.
const MISSION_TOMBSTONES: &str = "CREATE TABLE IF NOT EXISTS mission_tombstones(
  worker TEXT NOT NULL REFERENCES workers(name) ON DELETE CASCADE, mission_id TEXT NOT NULL, deleted_at INTEGER NOT NULL,
  PRIMARY KEY(worker, mission_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS mission_tombstones_age ON mission_tombstones(deleted_at);";
/// A tombstone is kept this long. A worker replicates it as soon as it is connected, so the window only has
/// to cover a worker that was offline between deleting the mission and reconnecting.
pub const TOMBSTONE_KEEP_SECS: i64 = 90 * 86_400;
/// Tombstones kept per worker; the oldest go first.
pub const MAX_TOMBSTONES_PER_WORKER: i64 = 10_000;

/// An address with a successful login within this many seconds is a known address.
pub const KNOWN_ADDR_SECS: i64 = 7 * 86_400;

type R<T> = rusqlite::Result<T>;
type IdentifiedTokenRow = (String, String, Option<i64>, Option<i64>, String, String, String, String);

impl Db {
    offload_impl! {
    pub fn open(path: &Path) -> R<Db> {
        let conn = Connection::open(path)?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        // Incremental auto-vacuum has to be chosen before the first table exists; an existing
        // database keeps its mode until `bridge-coordinator compact` (or a restore of a backup,
        // which is written compacted with the mode set) rebuilds it.
        let new_database: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if new_database == 0 {
            conn.pragma_update(None, "auto_vacuum", 2)?;
        }
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "journal_size_limit", WAL_SIZE_LIMIT)?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        // No temporary files outside the data directory (read-only root file systems).
        conn.pragma_update(None, "temp_store", "MEMORY")?;
        let version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
        if version > SCHEMA_VERSION {
            return Err(rusqlite::Error::SqliteFailure(
                rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_SCHEMA),
                Some(format!(
                    "database schema version {version} is newer than this binary supports ({SCHEMA_VERSION}); install a newer coordinator or restore a backup"
                )),
            ));
        }
        if version < 1 {
            conn.execute_batch(&format!("BEGIN; {SCHEMA} PRAGMA user_version = 1; COMMIT;"))?;
        }
        if version < 2 {
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V2} PRAGMA user_version = 2; COMMIT;"))?;
        }
        if version < 3 {
            let has_column: bool = conn.query_row("SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='remember'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sessions'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_column || !has_table { "" } else { MIGRATE_V3_COLUMN };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 3; COMMIT;"))?;
        }
        if version < 4 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workers'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let has_column = has_table
                && conn.query_row("SELECT COUNT(*) FROM pragma_table_info('workers') WHERE name='mission_cursor'", [], |r| {
                    r.get::<_, i64>(0)
                })? > 0;
            let alter = if has_column || !has_table { "" } else { MIGRATE_V4_COLUMN };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 4; COMMIT;"))?;
        }
        if version < 5 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_tokens'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let has_column = has_table
                && conn.query_row("SELECT COUNT(*) FROM pragma_table_info('api_tokens') WHERE name='scopes'", [], |r| {
                    r.get::<_, i64>(0)
                })? > 0;
            let alter = if has_column || !has_table { "" } else { MIGRATE_V5_COLUMN };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 5; COMMIT;"))?;
        }
        if version < 6 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workers'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let has_host = has_table
                && conn.query_row("SELECT COUNT(*) FROM pragma_table_info('workers') WHERE name='host_name'", [], |r| {
                    r.get::<_, i64>(0)
                })? > 0;
            let has_address = has_table
                && conn.query_row("SELECT COUNT(*) FROM pragma_table_info('workers') WHERE name='remote_address'", [], |r| {
                    r.get::<_, i64>(0)
                })? > 0;
            let mut alter = String::new();
            if has_table && !has_host {
                alter.push_str("ALTER TABLE workers ADD COLUMN host_name TEXT;");
            }
            if has_table && !has_address {
                alter.push_str("ALTER TABLE workers ADD COLUMN remote_address TEXT;");
            }
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 6; COMMIT;"))?;
        }
        if version < 7 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workers'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let has_column = has_table
                && conn.query_row("SELECT COUNT(*) FROM pragma_table_info('workers') WHERE name='worker_policy'", [], |r| {
                    r.get::<_, i64>(0)
                })? > 0;
            let alter = if has_table && !has_column { MIGRATE_V7_COLUMN } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 7; COMMIT;"))?;
        }
        if version < 8 {
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V8} PRAGMA user_version = 8; COMMIT;"))?;
        }
        if version < 9 {
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V9} PRAGMA user_version = 9; COMMIT;"))?;
        }
        if version < 10 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_tokens'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_table { MIGRATE_V10 } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 10; COMMIT;"))?;
        }
        if version < 11 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_tokens'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_table { MIGRATE_V11 } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 11; COMMIT;"))?;
        }
        if version < 12 {
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V12} PRAGMA user_version = 12; COMMIT;"))?;
        }
        if version < 13 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_tokens'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_table { MIGRATE_V13 } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 13; COMMIT;"))?;
        }
        if version < 14 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='workers'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_table { MIGRATE_V14 } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 14; COMMIT;"))?;
        }
        if version < 15 {
            let has_table: bool = conn.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_tokens'", [], |r| {
                r.get::<_, i64>(0)
            })? > 0;
            let alter = if has_table { MIGRATE_V15 } else { "" };
            conn.execute_batch(&format!("BEGIN; {alter} PRAGMA user_version = 15; COMMIT;"))?;
        }
        if version < 16 {
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V16} PRAGMA user_version = 16; COMMIT;"))?;
        }
        if version < 17 {
            let has_table = |table: &str| -> R<bool> {
                conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)", [table], |row| {
                    row.get(0)
                })
            };
            let has_column = |table: &str, column: &str| -> R<bool> {
                let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
                let columns = stmt.query_map([], |row| row.get::<_, String>(1))?;
                let present = columns.filter_map(Result::ok).any(|name| name == column);
                Ok(present)
            };
            let mut alter = String::new();
            for (table, column, declaration) in [
                ("sessions", "principal_id", "TEXT NOT NULL DEFAULT 'admin'"),
                ("api_tokens", "principal_id", "TEXT NOT NULL DEFAULT 'admin'"),
                ("audit", "principal_id", "TEXT NOT NULL DEFAULT 'system'"),
                ("api_tokens", "sender_constraint_kind", "TEXT"),
                ("api_tokens", "sender_constraint_id", "TEXT"),
            ] {
                if has_table(table)? && !has_column(table, column)? {
                    alter.push_str(&format!("ALTER TABLE {table} ADD COLUMN {column} {declaration};\n"));
                }
            }
            if has_table("admin")? {
                alter.push_str("INSERT INTO principals(id,username,role,created_at) SELECT 'admin',username,'admin',created_at FROM admin WHERE 1 ON CONFLICT(id) DO UPDATE SET username=excluded.username,created_at=excluded.created_at;\n");
                if has_table("audit")? {
                    alter.push_str("UPDATE audit SET principal_id='admin' WHERE actor=(SELECT username FROM admin WHERE id=1);\n");
                }
            }
            conn.execute_batch(&format!("BEGIN; {MIGRATE_V17_BASE} {alter} PRAGMA user_version = 17; COMMIT;"))?;
        }
        conn.execute_batch(MISSION_TOMBSTONES)?;
        crate::restrict_permissions(path, 0o600);
        Ok(Db {
            conn: Mutex::new(conn),
            gate: Gate::default(),
            max_waiters: AtomicUsize::new(DEFAULT_MAX_LOCK_WAITERS),
            stats: LockStats::default(),
            path: path.to_path_buf(),
            event_counts: Mutex::new(HashMap::new()),
        })
    }

    /// The connection, waiting at most [`lock_wait`] for it. A caller that waits longer, or that finds
    /// the waiter limit already queued, gets a busy error (HTTP 503) instead of queueing
    /// without bound behind a slow commit.
    pub(crate) fn c(&self) -> R<ConnGuard<'_>> {
        self.gate.enter(lock_wait(), self.max_waiters.load(Ordering::Relaxed), &self.stats)?;
        // The gate admits one caller at a time, so this lock is never contended. A mutex poisoned by a
        // handler that panicked is recovered.
        let inner = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        Ok(ConnGuard { inner: Some(inner), gate: &self.gate, stats: &self.stats, since: std::time::Instant::now() })
    }

    /// Set how many callers may wait for the connection at once (the server uses `max_connections` plus a
    /// margin, because every connection has at most one call in flight).
    pub fn set_max_lock_waiters(&self, n: usize) {
        self.max_waiters.store(n.clamp(1, LOCK_WAITERS_CEILING), Ordering::Relaxed);
    }

    /// Callers waiting for the connection now.
    pub fn lock_waiting(&self) -> usize {
        self.gate.waiting()
    }

    /// Statistics of the connection lock since the process started.
    pub fn lock_stats(&self) -> LockSnapshot {
        LockSnapshot {
            max_hold: std::time::Duration::from_micros(self.stats.max_hold_us.load(Ordering::Relaxed)),
            peak_waiters: self.stats.peak_waiters.load(Ordering::Relaxed),
        }
    }

    /// Persist server-side ceremony state for at most five minutes. Binding is an opaque
    /// session/setup-token identifier, never a browser supplied value.
    pub fn webauthn_challenge_put(&self, id: &str, row: &WebauthnChallengeRow) -> R<()> {
        let now = crate::now();
        if id.is_empty()
            || row.principal_id.is_empty()
            || row.binding.is_empty()
            || !matches!(row.kind.as_str(), "registration" | "authentication")
            || row.expires_at <= now
            || row.expires_at > now + 300
        {
            return Err(rusqlite::Error::InvalidParameterName("invalid WebAuthn challenge bounds".into()));
        }
        let mut c = self.c()?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?", [now])?;
        tx.execute(
            "INSERT INTO webauthn_challenges(id,principal_id,binding,kind,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            params![id, row.principal_id, row.binding, row.kind, row.state, now, row.expires_at],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// Consume once, only for the matching principal and session/setup binding. Expired
    /// challenges are deleted; a mismatched caller cannot invalidate another session's challenge.
    pub fn webauthn_challenge_take(&self, id: &str, principal: &str, binding: &str, kind: &str) -> R<Option<WebauthnChallengeRow>> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let row = tx
            .query_row(
                "SELECT principal_id,binding,kind,state,created_at,expires_at FROM webauthn_challenges WHERE id=?",
                [id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                        r.get::<_, String>(3)?,
                        r.get::<_, i64>(4)?,
                        r.get::<_, i64>(5)?,
                    ))
                },
            )
            .optional()?;
        let result = if let Some((p, b, k, state, created, expires)) = row {
            let now = crate::now();
            if expires <= now || expires > created + 300 {
                tx.execute("DELETE FROM webauthn_challenges WHERE id=?", [id])?;
                None
            } else if p == principal && b == binding && k == kind {
                tx.execute("DELETE FROM webauthn_challenges WHERE id=?", [id])?;
                Some(WebauthnChallengeRow {
                    principal_id: p,
                    binding: b,
                    kind: k,
                    state,
                    expires_at: expires,
                })
            } else {
                None
            }
        } else {
            None
        };
        tx.commit()?;
        Ok(result)
    }

    pub fn passkey_put(&self, row: &PasskeyCredentialRow) -> R<()> {
        self.c()?.execute("INSERT INTO passkeys(credential_id,principal_id,credential,user_name,created_at,last_used,aaguid,transports,backup_eligible,backed_up,sign_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            params![row.credential_id,row.principal_id,row.credential,row.user_name,row.created_at,row.last_used,row.aaguid,row.transports,row.backup_eligible as i64,row.backed_up as i64,row.sign_count])?;
        Ok(())
    }

    /// Store the first credential and its canonical origin atomically. A racing first registration
    /// at a different origin loses without leaving a locked origin or a credential behind.
    pub fn passkey_put_at_origin(&self, row: &PasskeyCredentialRow, origin: &str) -> R<bool> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let existing: Option<String> = tx
            .query_row("SELECT passkey_origin FROM auth_policy WHERE principal_id=?", [&row.principal_id], |r| r.get(0))
            .optional()?
            .flatten();
        match existing {
            Some(current) if current != origin => {
                tx.commit()?;
                return Ok(false);
            }
            Some(_) => {}
            None => {
                tx.execute(
                    "INSERT INTO auth_policy(principal_id,passkey_origin) VALUES(?,?)",
                    params![row.principal_id, origin],
                )?;
            }
        }
        tx.execute(
            "INSERT INTO passkeys(credential_id,principal_id,credential,user_name,created_at,last_used,aaguid,transports,backup_eligible,backed_up,sign_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            params![row.credential_id,row.principal_id,row.credential,row.user_name,row.created_at,row.last_used,row.aaguid,row.transports,row.backup_eligible as i64,row.backed_up as i64,row.sign_count],
        )?;
        tx.commit()?;
        Ok(true)
    }

    pub fn passkeys_for_principal(&self, principal: &str) -> R<Vec<PasskeyCredentialRow>> {
        let c = self.c()?;
        let mut stmt = c.prepare("SELECT credential_id,principal_id,credential,user_name,created_at,last_used,aaguid,transports,backup_eligible,backed_up,sign_count FROM passkeys WHERE principal_id=? ORDER BY created_at,credential_id")?;
        let rows = stmt
            .query_map([principal], |r| {
                Ok(PasskeyCredentialRow {
                    credential_id: r.get(0)?,
                    principal_id: r.get(1)?,
                    credential: r.get(2)?,
                    user_name: r.get(3)?,
                    created_at: r.get(4)?,
                    last_used: r.get(5)?,
                    aaguid: r.get(6)?,
                    transports: r.get(7)?,
                    backup_eligible: r.get::<_, i64>(8)? != 0,
                    backed_up: r.get::<_, i64>(9)? != 0,
                    sign_count: r.get(10)?,
                })
            })?
            .collect();
        rows
    }

    pub fn passkey_update(&self, row: &PasskeyCredentialRow) -> R<bool> {
        Ok(self.c()?.execute(
            "UPDATE passkeys SET credential=?,last_used=?,backup_eligible=?,backed_up=?,sign_count=? WHERE credential_id=? AND principal_id=?",
            params![
                row.credential,
                row.last_used,
                row.backup_eligible as i64,
                row.backed_up as i64,
                row.sign_count,
                row.credential_id,
                row.principal_id
            ],
        )? == 1)
    }

    /// Bind the first passkey to the canonical origin. A caller cannot silently move an existing
    /// principal's credentials to a different RP ID; explicit re-enrolment must clear them first.
    pub fn passkey_origin_bind(&self, principal: &str, origin: &str) -> R<bool> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let existing: Option<String> = tx
            .query_row("SELECT passkey_origin FROM auth_policy WHERE principal_id=?", [principal], |r| r.get(0))
            .optional()?
            .flatten();
        let allowed = match existing {
            Some(current) => current == origin,
            None => {
                tx.execute("INSERT INTO auth_policy(principal_id,passkey_origin) VALUES(?,?)", params![principal, origin])?;
                true
            }
        };
        tx.commit()?;
        Ok(allowed)
    }

    /// Explicit password-confirmed re-enrolment: clear every credential and switch the RP origin
    /// in one transaction so no credential remains bound to the previous RP ID.
    pub fn passkey_rebind_origin(&self, principal: &str, origin: &str) -> R<()> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM passkeys WHERE principal_id=?", [principal])?;
        tx.execute(
            "INSERT INTO auth_policy(principal_id,passkey_origin) VALUES(?,?) ON CONFLICT(principal_id) DO UPDATE SET passkey_origin=excluded.passkey_origin",
            params![principal, origin],
        )?;
        tx.commit()
    }

    pub fn passkey_origin(&self, principal: &str) -> R<Option<String>> {
        self.c()?
            .query_row("SELECT passkey_origin FROM auth_policy WHERE principal_id=?", [principal], |r| r.get(0))
            .optional()
            .map(Option::flatten)
    }

    pub fn passkey_count(&self, principal: &str) -> R<i64> {
        self.c()?
            .query_row("SELECT COUNT(*) FROM passkeys WHERE principal_id=?", [principal], |r| r.get(0))
    }

    // ---- admin ----
    pub fn admin_get(&self) -> R<Option<(String, String)>> {
        self.c()?
            .query_row("SELECT username, password_hash FROM admin WHERE id=1", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()
    }
    pub fn admin_create(&self, username: &str, hash: &str) -> R<bool> {
        let now = crate::now();
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let inserted = tx.execute(
            "INSERT OR IGNORE INTO admin(id, username, password_hash, created_at) VALUES(1,?,?,?)",
            params![username, hash, now],
        )? == 1;
        if inserted {
            tx.execute(
                "INSERT INTO principals(id,username,role,created_at) VALUES('admin',?,'admin',?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,created_at=excluded.created_at",
                params![username, now],
            )?;
        }
        tx.commit()?;
        Ok(inserted)
    }
    pub fn admin_principal_id(&self) -> R<Option<String>> {
        self.c()?
            .query_row("SELECT id FROM principals WHERE role='admin' ORDER BY created_at,id LIMIT 1", [], |r| r.get(0))
            .optional()
    }
    pub fn principals_list(&self) -> R<Vec<Value>> {
        let c = self.c()?;
        let mut stmt = c.prepare("SELECT id,username,role,created_at FROM principals ORDER BY id")?;
        let rows = stmt
            .query_map([], |r| {
                Ok(json!({"id":r.get::<_,String>(0)?,"username":r.get::<_,Option<String>>(1)?,"role":r.get::<_,String>(2)?,"created_at":r.get::<_,i64>(3)?}))
            })?
            .collect();
        rows
    }
    /// Set the password and delete every session in one transaction. Unless `keep_api_tokens`,
    /// every API token is revoked in the same transaction; returns how many were revoked.
    pub fn admin_set_password(&self, hash: &str, keep_api_tokens: bool) -> R<usize> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        tx.execute("UPDATE admin SET password_hash=? WHERE id=1", params![hash])?;
        tx.execute("DELETE FROM sessions", [])?;
        let revoked = if keep_api_tokens { 0 } else { tx.execute("DELETE FROM api_tokens", [])? };
        // A new password ends a host reset (admin_reset.rs): no more change-required flag or notice.
        tx.execute("DELETE FROM meta WHERE key IN ('password_change_required', 'password_reset_at')", [])?;
        tx.commit()?;
        Ok(revoked)
    }

    // ---- sessions ----
    /// A session with the longer ("remembered device") lifetime.
    pub fn session_create(&self, token_hash: &str, csrf: &str) -> R<()> {
        self.session_create_with(token_hash, csrf, true)
    }
    pub fn session_create_with(&self, token_hash: &str, csrf: &str, remember: bool) -> R<()> {
        self.session_create_for("admin", token_hash, csrf, remember)
    }
    pub fn session_create_for(&self, principal_id: &str, token_hash: &str, csrf: &str, remember: bool) -> R<()> {
        let now = crate::now();
        let (_, absolute) = session_policy(remember);
        self.c()?.execute(
            "INSERT INTO sessions(token_hash, csrf, created_at, last_seen, expires_at, remember,principal_id) VALUES(?,?,?,?,?,?,?)",
            params![token_hash, csrf, now, now, now + absolute, remember, principal_id],
        )?;
        Ok(())
    }
    pub fn session_get(&self, token_hash: &str) -> R<Option<Session>> {
        let now = crate::now();
        let c = self.c()?;
        let row: Option<(String, i64, i64, bool, String)> = c
            .query_row(
                "SELECT csrf, last_seen, expires_at, remember,principal_id FROM sessions WHERE token_hash=?",
                params![token_hash],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get::<_, i64>(3)? != 0, r.get(4)?)),
            )
            .optional()?;
        let Some((csrf, last_seen, expires_at, remember, principal_id)) = row else {
            return Ok(None);
        };
        let (idle_secs, _) = session_policy(remember);
        if now >= expires_at || now - last_seen >= idle_secs {
            c.execute("DELETE FROM sessions WHERE token_hash=?", params![token_hash])?;
            return Ok(None);
        }
        if now - last_seen >= TOUCH_EVERY_SECS {
            c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", params![now, token_hash])?;
        }
        Ok(Some(Session {
            principal_id,
            csrf,
            token_hash: token_hash.to_string(),
            remember,
            expires_at,
            idle_secs,
        }))
    }
    pub fn session_delete(&self, token_hash: &str) -> R<()> {
        self.c()?.execute("DELETE FROM sessions WHERE token_hash=?", params![token_hash])?;
        Ok(())
    }
    /// Test hook and maintenance: age a session so idle/absolute expiry can be exercised.
    pub fn session_age(&self, token_hash: &str, last_seen: i64, expires_at: i64) -> R<()> {
        self.c()?.execute(
            "UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
            params![last_seen, expires_at, token_hash],
        )?;
        Ok(())
    }
    /// Test hook: backdate a command's last update so retention can be exercised without waiting.
    pub fn command_age(&self, id: &str, updated_at: i64) -> R<()> {
        self.c()?
            .execute("UPDATE commands SET updated_at=? WHERE request_id=?", params![updated_at, id])?;
        Ok(())
    }
    /// Test hook: backdate a worker's stored `last_seen`.
    pub fn worker_age(&self, name: &str, last_seen: i64) -> R<()> {
        self.c()?.execute("UPDATE workers SET last_seen=? WHERE name=?", params![last_seen, name])?;
        Ok(())
    }
    pub fn sessions_clear(&self) -> R<()> {
        self.c()?.execute("DELETE FROM sessions", [])?;
        Ok(())
    }
    pub fn session_count(&self) -> R<i64> {
        self.c()?.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))
    }

    // ---- api tokens ----
    /// `expires_at` is a Unix time; `None` never expires.
    pub fn token_create(&self, name: &str, hash: &str, expires_at: Option<i64>) -> R<bool> {
        self.token_create_scoped(name, hash, expires_at, &["read".to_string()])
    }
    /// Create a token with the explicitly granted API scopes.
    pub fn token_create_scoped(&self, name: &str, hash: &str, expires_at: Option<i64>, scopes: &[String]) -> R<bool> {
        Ok(self.c()?.execute(
            "INSERT OR IGNORE INTO api_tokens(name, token_hash, created_at, expires_at, scopes,principal_id) VALUES(?,?,?,?,?,'admin')",
            params![
                name,
                hash,
                crate::now(),
                expires_at,
                serde_json::to_string(scopes).unwrap_or_else(|_| "[]".into())
            ],
        )? == 1)
    }
    /// Create an identifiable token, storing only its secret hash and public ID.
    pub fn token_create_identified(&self, name: &str, token_id: &str, secret_hash: &str, expires_at: Option<i64>, scopes: &[String]) -> R<bool> {
        self.token_create_identified_with_cidrs(name, token_id, secret_hash, expires_at, scopes, &[])
    }
    pub fn token_create_identified_with_cidrs(
        &self,
        name: &str,
        token_id: &str,
        secret_hash: &str,
        expires_at: Option<i64>,
        scopes: &[String],
        allowed_cidrs: &[String],
    ) -> R<bool> {
        self.token_create_identified_with_restrictions(ApiTokenCreate {
            name,
            token_id,
            secret_hash,
            principal_id: "admin",
            expires_at,
            scopes,
            allowed_cidrs,
            allowed_workers: &[],
        })
    }
    pub fn token_create_identified_with_restrictions(&self, token: ApiTokenCreate<'_>) -> R<bool> {
        Ok(self.c()?.execute(
            "INSERT OR IGNORE INTO api_tokens(name, token_hash, created_at, expires_at, scopes, token_id, allowed_cidrs, allowed_workers,principal_id) VALUES(?,?,?,?,?,?,?,?,?)",
            params![
                token.name,
                token.secret_hash,
                crate::now(),
                token.expires_at,
                serde_json::to_string(token.scopes).unwrap_or_else(|_| "[]".into()),
                token.token_id,
                serde_json::to_string(token.allowed_cidrs).unwrap_or_else(|_| "[]".into()),
                serde_json::to_string(token.allowed_workers).unwrap_or_else(|_| "[]".into()),
                token.principal_id
            ],
        )? == 1)
    }
    /// (name, expires_at) of every API token, by name.
    pub fn tokens_list(&self) -> R<Vec<(String, Option<i64>)>> {
        let c = self.c()?;
        let mut st = c.prepare("SELECT name, expires_at FROM api_tokens ORDER BY name")?;
        let rows = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect();
        rows
    }
    pub fn api_tokens_list(&self) -> R<Vec<ApiTokenInfo>> {
        let c = self.c()?;
        let mut st = c.prepare(
            "SELECT name, principal_id, token_id, scopes, created_at, last_used, last_address, expires_at, allowed_cidrs, allowed_workers, sender_constraint_kind, sender_constraint_id FROM api_tokens ORDER BY name",
        )?;
        let rows = st.query_map([], |r| {
            Ok(ApiTokenInfo {
                name: r.get(0)?,
                principal_id: r.get(1)?,
                token_id: r.get(2)?,
                scopes: serde_json::from_str(&r.get::<_, String>(3)?).unwrap_or_default(),
                created_at: r.get(4)?,
                last_used: r.get(5)?,
                last_address: r.get(6)?,
                expires_at: r.get(7)?,
                allowed_cidrs: serde_json::from_str(&r.get::<_, String>(8)?).unwrap_or_default(),
                allowed_workers: serde_json::from_str(&r.get::<_, String>(9)?).unwrap_or_default(),
                sender_constraint_kind: r.get(10)?,
                sender_constraint_id: r.get(11)?,
            })
        })?;
        rows.collect()
    }
    /// Internet exposure requires every API token to have an explicit expiry.
    pub fn non_expiring_api_token_count(&self) -> R<i64> {
        self.c()?.query_row("SELECT COUNT(*) FROM api_tokens WHERE expires_at IS NULL", [], |r| r.get(0))
    }
    /// Record a bounded token-use projection. Repeated calls from the same address update only
    /// the timestamp at the existing low-write touch interval.
    pub fn api_token_record_use(&self, name: &str, address: &str) -> R<()> {
        let now = crate::now();
        let touch_before = now - TOUCH_EVERY_SECS;
        self.c()?.execute(
            "UPDATE api_tokens SET last_address=?, last_used=CASE WHEN last_used IS NULL OR last_used<=? THEN ? ELSE last_used END
             WHERE name=? AND (last_address IS NOT ? OR last_used IS NULL OR last_used<=?)",
            params![address, touch_before, now, name, address, touch_before],
        )?;
        Ok(())
    }
    pub fn token_revoke(&self, name: &str) -> R<bool> {
        Ok(self.c()?.execute("DELETE FROM api_tokens WHERE name=?", params![name])? == 1)
    }
    /// Look up a pre-identification API token without relying on SQLite's early-exit string
    /// equality. Every legacy digest is compared in constant time; new tokens use the indexed ID
    /// lookup below. Touch `last_used` at most every `TOUCH_EVERY_SECS`.
    pub fn token_lookup(&self, hash: &str) -> R<Option<ApiToken>> {
        let c = self.c()?;
        let now = crate::now();
        let mut stmt = c.prepare(
            "SELECT name, token_hash, last_used, expires_at, scopes, allowed_cidrs, allowed_workers,principal_id FROM api_tokens WHERE token_id IS NULL ORDER BY name",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<i64>>(2)?,
                r.get::<_, Option<i64>>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, String>(6)?,
                r.get::<_, String>(7)?,
            ))
        })?;
        let mut matched = None;
        for row in rows {
            let (name, stored_hash, last, expires_at, scopes_json, cidrs_json, workers_json, principal_id) = row?;
            let matches = crate::auth::ct_eq(hash, &stored_hash);
            let unexpired = expires_at.is_none_or(|expires| now < expires);
            if matches & unexpired {
                matched = Some((name, last, scopes_json, cidrs_json, workers_json, principal_id));
            }
        }
        drop(stmt);
        let Some((name, last, scopes_json, cidrs_json, workers_json, principal_id)) = matched else {
            return Ok(None);
        };
        if last.is_none_or(|l| now - l >= TOUCH_EVERY_SECS) {
            c.execute("UPDATE api_tokens SET last_used=? WHERE name=?", params![now, name])?;
        }
        let scopes = serde_json::from_str(&scopes_json).unwrap_or_default();
        Ok(Some(ApiToken {
            name,
            principal_id,
            scopes,
            allowed_cidrs: serde_json::from_str(&cidrs_json).unwrap_or_default(),
            allowed_workers: serde_json::from_str(&workers_json).unwrap_or_default(),
        }))
    }
    /// Look up an identifiable token by its public ID, then compare the supplied secret hash
    /// against the stored hash in constant time.
    pub fn token_lookup_identified(&self, token_id: &str, secret_hash: &str) -> R<Option<ApiToken>> {
        let c = self.c()?;
        let row: Option<IdentifiedTokenRow> = c
            .query_row(
                "SELECT name, token_hash, last_used, expires_at, scopes, allowed_cidrs, allowed_workers,principal_id FROM api_tokens WHERE token_id=?",
                params![token_id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?)),
            )
            .optional()?;
        let Some((name, stored_hash, last, expires_at, scopes_json, cidrs_json, workers_json, principal_id)) = row else {
            return Ok(None);
        };
        if !crate::auth::ct_eq(secret_hash, &stored_hash) || expires_at.is_some_and(|e| crate::now() >= e) {
            return Ok(None);
        }
        let now = crate::now();
        if last.is_none_or(|l| now - l >= TOUCH_EVERY_SECS) {
            c.execute("UPDATE api_tokens SET last_used=? WHERE name=?", params![now, name])?;
        }
        let scopes = serde_json::from_str(&scopes_json).unwrap_or_default();
        Ok(Some(ApiToken {
            name,
            principal_id,
            scopes,
            allowed_cidrs: serde_json::from_str(&cidrs_json).unwrap_or_default(),
            allowed_workers: serde_json::from_str(&workers_json).unwrap_or_default(),
        }))
    }

    // ---- known addresses ----
    /// Salted hash of a client address key; the salt is created on first use and stored in `meta`.
    fn addr_hash(c: &Connection, key: &str) -> R<String> {
        let salt: Option<String> = c.query_row("SELECT value FROM meta WHERE key='addr_salt'", [], |r| r.get(0)).optional()?;
        let salt = match salt {
            Some(s) => s,
            None => {
                let fresh = crate::auth::random_token();
                c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('addr_salt', ?)", params![fresh])?;
                c.query_row("SELECT value FROM meta WHERE key='addr_salt'", [], |r| r.get(0))?
            }
        };
        Ok(crate::auth::sha256_hex(&format!("{salt}|{key}")))
    }
    /// Remember that `addr_key` had a successful login now.
    pub fn known_addr_remember(&self, addr_key: &str) -> R<()> {
        let c = self.c()?;
        let h = Self::addr_hash(&c, addr_key)?;
        c.execute(
            "INSERT INTO known_addrs(hash, last_success) VALUES(?,?) ON CONFLICT(hash) DO UPDATE SET last_success=excluded.last_success",
            params![h, crate::now()],
        )?;
        Ok(())
    }
    /// True when `addr_key` had a successful login within [`KNOWN_ADDR_SECS`].
    pub fn known_addr(&self, addr_key: &str) -> R<bool> {
        let c = self.c()?;
        let h = Self::addr_hash(&c, addr_key)?;
        let last: Option<i64> = c
            .query_row("SELECT last_success FROM known_addrs WHERE hash=?", params![h], |r| r.get(0))
            .optional()?;
        Ok(last.is_some_and(|t| crate::now() - t < KNOWN_ADDR_SECS))
    }
    /// Test hook: age a known address.
    pub fn known_addr_age(&self, addr_key: &str, last_success: i64) -> R<()> {
        let c = self.c()?;
        let h = Self::addr_hash(&c, addr_key)?;
        c.execute("UPDATE known_addrs SET last_success=? WHERE hash=?", params![last_success, h])?;
        Ok(())
    }

    /// Remember a successful device fingerprint. Returns true only for a new fingerprint.
    /// The caller supplies a bounded, normalized User-Agent signature; only its salted hash is
    /// retained. Old entries and entries beyond the per-principal cap are pruned transactionally.
    pub fn known_login_device_remember(&self, principal_id: &str, fingerprint: &str) -> R<bool> {
        const RETAIN_SECS: i64 = 30 * 86_400;
        const MAX_DEVICES: i64 = 64;
        let now = crate::now();
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let hash = Self::addr_hash(&tx, &format!("device:{fingerprint}"))?;
        tx.execute(
            "DELETE FROM known_login_devices WHERE principal_id=? AND last_success<?",
            params![principal_id, now - RETAIN_SECS],
        )?;
        let known: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM known_login_devices WHERE principal_id=? AND fingerprint_hash=?)",
            params![principal_id, hash],
            |r| r.get(0),
        )?;
        tx.execute(
            "INSERT INTO known_login_devices(principal_id,fingerprint_hash,last_success) VALUES(?,?,?) ON CONFLICT(principal_id,fingerprint_hash) DO UPDATE SET last_success=excluded.last_success",
            params![principal_id, hash, now],
        )?;
        tx.execute(
            "DELETE FROM known_login_devices WHERE principal_id=? AND fingerprint_hash IN (SELECT fingerprint_hash FROM known_login_devices WHERE principal_id=? ORDER BY last_success DESC,fingerprint_hash LIMIT -1 OFFSET ?)",
            params![principal_id, principal_id, MAX_DEVICES],
        )?;
        tx.commit()?;
        Ok(!known)
    }

    pub fn security_notification_add(&self, principal_id: &str, category: &str) -> R<Option<String>> {
        let now = crate::now();
        let id = crate::auth::sha256_hex(&format!("{}:{}:{}", principal_id, category, crate::auth::random_token()))[..24].to_string();
        let mut c = self.c()?;
        let tx = c.transaction()?;
        tx.execute("DELETE FROM security_notifications WHERE created_at < ?", [now - 30 * 86_400])?;
        if category == "repeated_sign_in_failures" {
            let recent: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM security_notifications WHERE principal_id=? AND category=? AND created_at>? )",
                params![principal_id, category, now - 900],
                |r| r.get(0),
            )?;
            if recent {
                tx.commit()?;
                return Ok(None);
            }
        }
        tx.execute(
            "INSERT INTO security_notifications(id, principal_id, category, created_at) VALUES(?,?,?,?)",
            params![id, principal_id, category, now],
        )?;
        tx.execute(
            "DELETE FROM security_notifications WHERE id IN (SELECT id FROM security_notifications WHERE principal_id=? ORDER BY created_at DESC LIMIT -1 OFFSET 100)",
            [principal_id],
        )?;
        tx.commit()?;
        Ok(Some(id))
    }

    pub fn security_notifications_list(&self, principal_id: &str) -> R<Vec<Value>> {
        let c = self.c()?;
        let mut st = c.prepare("SELECT id, category, created_at FROM security_notifications WHERE principal_id=? AND dismissed_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 20")?;
        let rows = st
            .query_map([principal_id], |r| {
                Ok(json!({"id":r.get::<_,String>(0)?,"category":r.get::<_,String>(1)?,"created_at":r.get::<_,i64>(2)?}))
            })?
            .collect();
        rows
    }

    pub fn security_notification_dismiss(&self, principal_id: &str, id: &str) -> R<bool> {
        Ok(self.c()?.execute(
            "UPDATE security_notifications SET dismissed_at=? WHERE principal_id=? AND id=? AND dismissed_at IS NULL",
            params![crate::now(), principal_id, id],
        )? == 1)
    }

    // ---- workers ----
    pub fn worker_add(&self, name: &str, hash: &str) -> R<bool> {
        Ok(self.c()?.execute(
            "INSERT OR IGNORE INTO workers(name, token_hash, created_at) VALUES(?,?,?)",
            params![name, hash, crate::now()],
        )? == 1)
    }
    pub fn worker_add_with_client_certificate(&self, name: &str, hash: &str, fingerprint: &str) -> R<bool> {
        Ok(self.c()?.execute(
            "INSERT OR IGNORE INTO workers(name, token_hash, created_at, client_cert_fingerprint) VALUES(?,?,?,?)",
            params![name, hash, crate::now(), fingerprint],
        )? == 1)
    }
    pub fn worker_revoke(&self, name: &str) -> R<bool> {
        let revoked = self.c()?.execute("DELETE FROM workers WHERE name=?", params![name])? == 1;
        self.event_counts.lock().unwrap_or_else(|e| e.into_inner()).remove(name);
        Ok(revoked)
    }
    pub fn worker_by_token(&self, hash: &str) -> R<Option<WorkerRow>> {
        self.c()?
            .query_row(&format!("SELECT {WCOLS} FROM workers WHERE token_hash=?"), params![hash], worker_row)
            .optional()
    }
    /// Authenticate a worker channel handshake with either the active token or a staged
    /// rotation token. A pending token is not promoted until the worker sends a valid hello.
    pub fn worker_by_connection_token(&self, hash: &str, now: i64) -> R<Option<(WorkerRow, bool)>> {
        self.c()?
            .query_row(
                &format!(
                    "SELECT {WCOLS}, (pending_token_hash IS NOT NULL AND pending_token_hash=?1) FROM workers
                     WHERE token_hash=?1 OR (pending_token_hash=?1 AND pending_token_expires_at>?2)"
                ),
                params![hash, now],
                |r| Ok((worker_row(r)?, r.get::<_, i64>(16)? != 0)),
            )
            .optional()
    }
    /// Stage a new worker token while the old token remains valid for the given grace window.
    pub fn worker_stage_token_rotation(&self, name: &str, hash: &str, expires_at: i64) -> R<bool> {
        Ok(self.c()?.execute(
            "UPDATE workers SET pending_token_hash=?, pending_token_expires_at=? WHERE name=?",
            params![hash, expires_at, name],
        )? == 1)
    }
    /// Promote a staged token only after the matching token completed a valid worker hello.
    pub fn worker_promote_token_rotation(&self, name: &str, hash: &str, now: i64) -> R<bool> {
        Ok(self.c()?.execute(
            "UPDATE workers SET token_hash=pending_token_hash, pending_token_hash=NULL, pending_token_expires_at=NULL
             WHERE name=? AND pending_token_hash=? AND pending_token_expires_at>?",
            params![name, hash, now],
        )? == 1)
    }
    pub fn worker_by_client_certificate(&self, fingerprint: &str) -> R<Option<WorkerRow>> {
        self.c()?
            .query_row(
                &format!("SELECT {WCOLS} FROM workers WHERE client_cert_fingerprint=?"),
                params![fingerprint],
                worker_row,
            )
            .optional()
    }
    pub fn worker_client_certificate_matches(&self, name: &str, fingerprint: &str) -> R<bool> {
        Ok(self.c()?.query_row(
            "SELECT EXISTS(SELECT 1 FROM workers WHERE name=? AND client_cert_fingerprint=?)",
            params![name, fingerprint],
            |r| r.get::<_, i64>(0),
        )? != 0)
    }
    pub fn worker_get(&self, name: &str) -> R<Option<WorkerRow>> {
        self.c()?
            .query_row(&format!("SELECT {WCOLS} FROM workers WHERE name=?"), params![name], worker_row)
            .optional()
    }
    pub fn workers_list(&self) -> R<Vec<WorkerRow>> {
        let c = self.c()?;
        let mut st = c.prepare(&format!("SELECT {WCOLS} FROM workers ORDER BY name"))?;
        let rows = st.query_map([], worker_row)?.collect();
        rows
    }
    /// Stable database-local HMAC key used to sign opaque pagination cursors.
    pub fn cursor_key(&self) -> R<String> {
        let c = self.c()?;
        c.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('cursor_hmac_key',?1)",
            params![crate::auth::random_token()],
        )?;
        c.query_row("SELECT value FROM meta WHERE key='cursor_hmac_key'", [], |row| row.get(0))
    }
    /// Record that a worker was seen now. A connected worker's heartbeat calls this every ping;
    /// `min_interval` skips the update while the stored value is younger than that many seconds,
    /// so an idle connection does not commit (and fsync) a transaction per ping. An UPDATE that
    /// matches no row writes nothing to the database files.
    pub fn worker_seen(&self, name: &str, min_interval: i64) -> R<()> {
        let now = crate::now();
        self.c()?.execute(
            "UPDATE workers SET last_seen=?1 WHERE name=?2 AND (last_seen IS NULL OR last_seen <= ?1 - ?3)",
            params![now, name, min_interval],
        )?;
        Ok(())
    }

    /// Record the final disconnect time and clear the address of the closed socket.
    pub fn worker_disconnected(&self, name: &str) -> R<()> {
        self.c()?
            .execute("UPDATE workers SET last_seen=?, remote_address=NULL WHERE name=?", params![crate::now(), name])?;
        Ok(())
    }

    /// Record a validated hello: bind the stable worker id, bump the persisted generation, replace the mission index.
    pub fn worker_hello(
        &self,
        name: &str,
        worker_id: &str,
        hello: &serde_json::Map<String, Value>,
        capabilities: &Value,
        remote_address: &str,
    ) -> R<HelloOutcome> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let row: Option<(Option<String>, i64, i64)> = tx
            .query_row("SELECT worker_id, generation, event_cursor FROM workers WHERE name=?", params![name], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })
            .optional()?;
        let Some((bound, generation, cursor)) = row else {
            return Ok(HelloOutcome::Unknown);
        };
        if bound.as_deref().is_some_and(|b| b != worker_id) {
            return Ok(HelloOutcome::WorkerIdMismatch);
        }
        let now = crate::now();
        // Above everything ever issued for this name, including before a revoke and re-add.
        let high: i64 = tx
            .query_row("SELECT high FROM worker_generations WHERE name=?", params![name], |r| r.get(0))
            .optional()?
            .unwrap_or(0);
        let generation = generation.max(high) + 1;
        tx.execute(
            "INSERT INTO worker_generations(name, high) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET high=excluded.high",
            params![name, generation],
        )?;
        let text = |k: &str| hello.get(k).map(|v| v.to_string());
        tx.execute("UPDATE workers SET worker_id=?, generation=?, last_seen=?, hello_at=?, mission_cursor=?, software_version=?, protocol_versions=?, platform=?, capabilities=?, host_name=?, remote_address=?, worker_policy=? WHERE name=?",
            params![worker_id, generation, now, now, cursor, hello.get("software_version").and_then(Value::as_str), text("protocol_versions"), text("platform"), capabilities.to_string(), hello.get("host_name").and_then(Value::as_str), remote_address, text("worker_policy"), name])?;
        tx.execute("DELETE FROM missions WHERE worker=?", params![name])?;
        if let Some(missions) = hello.get("missions").and_then(Value::as_array) {
            for m in missions {
                if let Some(id) = m.get("id").and_then(Value::as_str) {
                    // The hello is the worker's own list: a mission it still has is not deleted, whatever an
                    // older tombstone says (for example after the worker restored an earlier backup).
                    tx.execute("DELETE FROM mission_tombstones WHERE worker=? AND mission_id=?", params![name, id])?;
                    tx.execute(
                        "INSERT OR REPLACE INTO missions(worker, mission_id, info) VALUES(?,?,?)",
                        params![name, id, m.to_string()],
                    )?;
                }
            }
        }
        tx.commit()?;
        Ok(HelloOutcome::Ok {
            generation: generation as u64,
            cursor: cursor as u64,
        })
    }

    pub fn missions_list(&self) -> R<Vec<(String, Value)>> {
        let c = self.c()?;
        let mut st = c.prepare("SELECT worker, info FROM missions ORDER BY worker, mission_id")?;
        let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        rows.map(|r| r.map(|(w, i)| (w, serde_json::from_str(&i).unwrap_or(Value::Null)))).collect()
    }

    // ---- events ----
    pub fn events_store(&self, batch: EventBatch<'_>) -> R<EventsOutcome> {
        let EventBatch {
            worker,
            after,
            through,
            events,
            mission_cursor,
            missions,
            max,
        } = batch;
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let cursor: i64 = tx.query_row("SELECT event_cursor FROM workers WHERE name=?", params![worker], |r| r.get(0))?;
        if after != cursor as u64 {
            return Ok(EventsOutcome::Nack { expected: cursor as u64 });
        }
        let tombstoned: std::collections::HashSet<String> = {
            let mut st = tx.prepare_cached("SELECT mission_id FROM mission_tombstones WHERE worker=?")?;
            let rows = st.query_map(params![worker], |r| r.get::<_, String>(0))?;
            rows.collect::<R<std::collections::HashSet<String>>>()?
        };
        let is_tombstoned = |value: &Value| value.get("mission_id").and_then(Value::as_str).is_some_and(|id| tombstoned.contains(id));
        let known = self.event_counts.lock().unwrap_or_else(|e| e.into_inner()).get(worker).copied();
        let known = match known {
            Some(n) => n,
            None => tx.query_row("SELECT COUNT(*) FROM events WHERE worker=?", params![worker], |r| r.get::<_, i64>(0))? as u64,
        };
        {
            let mut insert = tx.prepare_cached("INSERT OR REPLACE INTO events(worker, id, body) VALUES(?,?,?)")?;
            for e in events {
                if is_tombstoned(e) {
                    continue; // a late replication of a deleted mission; the cursor still moves past it
                }
                let id = e.get("id").and_then(Value::as_u64).unwrap_or(0) as i64;
                insert.execute(params![worker, id, e.to_string()])?;
            }
        }
        if let Some(missions) = missions {
            // A mission row is written only when its summary differs from the stored one: an unchanged
            // summary would otherwise dirty (and write to the log) its page on every batch.
            let mut upsert = tx.prepare_cached(
                "INSERT INTO missions(worker, mission_id, info) VALUES(?1, ?2, ?3) \
                 ON CONFLICT(worker, mission_id) DO UPDATE SET info=excluded.info WHERE info IS NOT excluded.info",
            )?;
            for mission in missions {
                if let Some(id) = mission.get("id").and_then(Value::as_str) {
                    if tombstoned.contains(id) {
                        continue;
                    }
                    upsert.execute(params![worker, id, mission.to_string()])?;
                }
            }
        }
        // One statement for the whole worker row update: the cursors and the time live on one page.
        tx.execute(
            "UPDATE workers SET event_cursor=?1, last_event_at=?2, mission_cursor=COALESCE(?3, mission_cursor) WHERE name=?4",
            params![through as i64, crate::now(), mission_cursor.map(|m| m as i64), worker],
        )?;
        let stored = events.iter().filter(|e| !is_tombstoned(e)).count();
        let mut count = known + stored as u64;
        if count > max {
            // Retention runs in one pass once the cap is exceeded and keeps `max - max / 10` events, so a
            // worker at its cap does not delete a few of the oldest rows (and rebalance the left edge
            // of the index) in every batch. The stored count never exceeds `max`.
            let keep = max - max / 10;
            tx.execute(
                "DELETE FROM events WHERE worker=?1 AND id <= (SELECT id FROM events WHERE worker=?1 ORDER BY id DESC LIMIT 1 OFFSET ?2)",
                params![worker, keep as i64],
            )?;
            count = tx.query_row("SELECT COUNT(*) FROM events WHERE worker=?", params![worker], |r| r.get::<_, i64>(0))? as u64;
        }
        tx.commit()?;
        self.event_counts.lock().unwrap_or_else(|e| e.into_inner()).insert(worker.to_string(), count);
        Ok(EventsOutcome::Accepted { cursor: through, stored })
    }

    /// Record that `worker` deleted these missions: remove the coordinator's copy (summary and stored events)
    /// and keep a tombstone. Idempotent: a repeated tombstone changes nothing and is not counted again.
    /// Returns the identifiers that were tombstoned by this call for the first time.
    pub fn missions_tombstone(&self, worker: &str, mission_ids: &[String]) -> R<Vec<String>> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let now = crate::now();
        let mut fresh = Vec::new();
        let mut removed_events = 0usize;
        for id in mission_ids {
            let added = tx.execute(
                "INSERT OR IGNORE INTO mission_tombstones(worker, mission_id, deleted_at) VALUES(?,?,?)",
                params![worker, id, now],
            )?;
            tx.execute("DELETE FROM missions WHERE worker=? AND mission_id=?", params![worker, id])?;
            removed_events += tx.execute(
                "DELETE FROM events WHERE worker=? AND json_extract(body, '$.mission_id')=?",
                params![worker, id],
            )?;
            if added > 0 {
                fresh.push(id.clone());
            }
        }
        tx.execute("DELETE FROM mission_tombstones WHERE deleted_at < ?", params![now - TOMBSTONE_KEEP_SECS])?;
        tx.execute(
            "DELETE FROM mission_tombstones WHERE worker=?1 AND mission_id NOT IN \
             (SELECT mission_id FROM mission_tombstones WHERE worker=?1 ORDER BY deleted_at DESC, mission_id LIMIT ?2)",
            params![worker, MAX_TOMBSTONES_PER_WORKER],
        )?;
        tx.commit()?;
        if removed_events > 0 {
            let mut counts = self.event_counts.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(n) = counts.get_mut(worker) {
                *n = n.saturating_sub(removed_events as u64);
            }
        }
        Ok(fresh)
    }

    pub fn mission_tombstoned(&self, worker: &str, mission_id: &str) -> R<bool> {
        self.c()?
            .query_row(
                "SELECT 1 FROM mission_tombstones WHERE worker=? AND mission_id=?",
                params![worker, mission_id],
                |_| Ok(()),
            )
            .optional()
            .map(|row| row.is_some())
    }

    /// Whether the coordinator holds a summary for exactly this mission of this worker.
    pub fn mission_known(&self, worker: &str, mission_id: &str) -> R<bool> {
        self.c()?
            .query_row(
                "SELECT 1 FROM missions WHERE worker=? AND mission_id=?",
                params![worker, mission_id],
                |_| Ok(()),
            )
            .optional()
            .map(|row| row.is_some())
    }

    pub fn events_list(&self, worker: Option<&str>, after: u64, limit: u64) -> R<Vec<Value>> {
        let c = self.c()?;
        let mut st = c.prepare("SELECT worker, id, body FROM events WHERE (?1 IS NULL OR worker=?1) AND id > ?2 ORDER BY worker, id LIMIT ?3")?;
        let rows = st.query_map(params![worker, after as i64, limit as i64], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
        })?;
        rows.map(|r| r.map(|(w, id, b)| json!({"worker": w, "id": id, "event": serde_json::from_str::<Value>(&b).unwrap_or(Value::Null)})))
            .collect()
    }

    // ---- commands ----
    pub fn command_get(&self, id: &str) -> R<Option<CommandRow>> {
        self.c()?
            .query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)
            .optional()
    }

    pub fn command_insert(&self, worker: &str, operation: &str, mutating: bool, request: &Value) -> R<InsertOutcome> {
        let id = request.get("request_id").and_then(Value::as_str).unwrap_or_default().to_string();
        let request_json = request.to_string();
        if request_json.len() > MAX_COMMAND_REQUEST_BYTES {
            return Ok(InsertOutcome::RequestTooLarge);
        }
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let existing: Option<CommandRow> = tx
            .query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)
            .optional()?;
        if let Some(row) = existing {
            return Ok(if row.worker == worker && &row.request == request {
                InsertOutcome::Existing(row)
            } else {
                InsertOutcome::Conflict
            });
        }
        let (open, worker_bytes): (i64, i64) = tx.query_row(
            "SELECT COUNT(*), COALESCE(SUM(length(CAST(request AS BLOB))),0) FROM commands WHERE worker=? AND state!='done'",
            params![worker],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let total_bytes: i64 = tx.query_row(
            "SELECT COALESCE(SUM(length(CAST(request AS BLOB))),0) FROM commands WHERE state!='done'",
            [],
            |r| r.get(0),
        )?;
        if open >= MAX_OPEN_COMMANDS_PER_WORKER
            || worker_bytes + request_json.len() as i64 > MAX_OPEN_COMMAND_BYTES_PER_WORKER
            || total_bytes + request_json.len() as i64 > MAX_OPEN_COMMAND_BYTES_TOTAL
        {
            return Ok(InsertOutcome::QueueFull);
        }
        let now = crate::now();
        tx.execute(
            "INSERT INTO commands(request_id, worker, operation, mutating, request, state, created_at, updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
            params![id, worker, operation, mutating as i64, request_json, now, now],
        )?;
        let row = tx.query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)?;
        tx.commit()?;
        Ok(InsertOutcome::New(row))
    }

    /// Unfinished mutating commands, oldest first, for redelivery after a reconnect.
    /// Read-only commands are not redelivered: see [`Db::commands_expire_reads`].
    pub fn commands_resend(&self, worker: &str) -> R<Vec<CommandRow>> {
        let c = self.c()?;
        let mut st = c.prepare(&format!(
            "SELECT {CCOLS} FROM commands WHERE worker=? AND mutating=1 AND state IN ('pending','sent','acked') ORDER BY rowid"
        ))?;
        let rows = st.query_map(params![worker], command_row)?.collect();
        rows
    }

    /// Finish every unfinished read-only command of `worker` with a `worker-offline` failure and
    /// return the rows. A read is only worth answering while a client waits for it; replaying a
    /// backlog of them after a reconnect sent hundreds of large results (18.6 MB in 30 s measured).
    /// The failure is the same `command.result` shape a worker sends, so a client polling the
    /// journal entry sees a normal failed read.
    pub fn commands_expire_reads(&self, worker: &str) -> R<Vec<CommandRow>> {
        let mut c = self.c()?;
        let tx = c.transaction()?;
        let rows: Vec<CommandRow> = {
            let mut st = tx.prepare(&format!(
                "SELECT {CCOLS} FROM commands WHERE worker=? AND mutating=0 AND state IN ('pending','sent','acked') ORDER BY rowid"
            ))?;
            let rows = st.query_map(params![worker], command_row)?.collect::<Result<_, _>>()?;
            rows
        };
        let now = crate::now();
        let mut done = Vec::with_capacity(rows.len());
        for mut row in rows {
            let result = json!({
                "request_id": row.request_id,
                "operation": row.operation,
                "outcome": "failed",
                "error": {"code": "worker-offline", "message": "the worker disconnected before it answered this read-only command; read-only commands are not queued for an offline worker"},
            });
            tx.execute(
                "UPDATE commands SET state='done', result=?1, updated_at=?2 WHERE request_id=?3 AND state!='done'",
                params![result.to_string(), now, row.request_id],
            )?;
            row.state = "done".into();
            row.result = Some(result);
            row.updated_at = now;
            done.push(row);
        }
        tx.commit()?;
        Ok(done)
    }

    fn transition(&self, sql: &str, args: &[&dyn rusqlite::ToSql], id: &str) -> R<Option<CommandRow>> {
        let c = self.c()?;
        if c.execute(sql, args)? == 1 {
            c.query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)
                .optional()
        } else {
            Ok(None)
        }
    }
    pub fn command_sent(&self, id: &str) -> R<Option<CommandRow>> {
        self.transition(
            "UPDATE commands SET state='sent', updated_at=?1 WHERE request_id=?2 AND state='pending'",
            &[&crate::now(), &id],
            id,
        )
    }
    pub fn command_acked(&self, worker: &str, id: &str) -> R<Option<CommandRow>> {
        self.transition(
            "UPDATE commands SET state='acked', updated_at=?1 WHERE request_id=?2 AND worker=?3 AND state IN ('pending','sent')",
            &[&crate::now(), &id, &worker],
            id,
        )
    }
    pub fn command_done(&self, worker: &str, id: &str, result: &Value) -> R<Option<CommandRow>> {
        self.transition(
            "UPDATE commands SET state='done', result=?1, updated_at=?2 WHERE request_id=?3 AND worker=?4 AND state!='done'",
            &[&result.to_string(), &crate::now(), &id, &worker],
            id,
        )
    }

    // ---- audit and maintenance ----
    pub fn audit(&self, actor: &str, action: &str, detail: &str, remote: &str) {
        let _ = self.audit_checked(actor, action, detail, remote);
    }

    pub fn audit_checked(&self, actor: &str, action: &str, detail: &str, remote: &str) -> R<()> {
        self.c()?.execute(
            "INSERT INTO audit(ts, actor, action, detail, remote,principal_id) VALUES(?,?,?,?,?,COALESCE((SELECT id FROM principals WHERE username=?),CASE WHEN ?='admin' THEN 'admin' WHEN substr(?,1,6)='token:' THEN (SELECT principal_id FROM api_tokens WHERE name=substr(?,7)) ELSE 'system' END))",
            params![crate::now(), actor, action, detail, remote, actor, actor, actor, actor],
        )?;
        Ok(())
    }
    pub fn audit_list(&self, limit: i64) -> R<Vec<Value>> {
        let c = self.c()?;
        let mut st = c.prepare("SELECT ts, actor, action, detail, remote FROM audit ORDER BY id DESC LIMIT ?")?;
        let rows = st
            .query_map(params![limit], |r| {
                Ok(
                    json!({"ts": r.get::<_, i64>(0)?, "actor": r.get::<_, String>(1)?, "action": r.get::<_, String>(2)?,
            "detail": r.get::<_, String>(3)?, "remote": r.get::<_, String>(4)?}),
                )
            })?
            .collect();
        rows
    }

    /// Run `sql` (which must take `?1` = a cut-off and `?2` = the batch size) until a pass touches fewer
    /// rows than the batch. The connection mutex is held for one statement at a time.
    fn in_batches(&self, sql: &str, cutoff: i64) -> R<usize> {
        let mut total = 0;
        loop {
            let n = self.c()?.execute(sql, params![cutoff, MAINTENANCE_BATCH])?;
            total += n;
            if (n as i64) < MAINTENANCE_BATCH {
                return Ok(total);
            }
            std::thread::sleep(MAINTENANCE_PAUSE);
        }
    }

    /// Retention and housekeeping, run hourly (and once at start). Every statement is bounded: rows
    /// are removed or rewritten a batch at a time and the connection mutex is released in between, so
    /// a large backlog (for example after a replication flood) is worked off without stalling requests.
    pub fn maintenance(&self) -> R<()> {
        let now = crate::now();
        {
            let c = self.c()?;
            c.execute(
                "DELETE FROM sessions WHERE expires_at <= ?1 OR last_seen <= ?2 - CASE remember WHEN 1 THEN ?3 ELSE ?4 END",
                params![now, now, SESSION_IDLE_SECS, SESSION_IDLE_SECS_SHORT],
            )?;
            c.execute("DELETE FROM known_addrs WHERE last_success < ?", params![now - KNOWN_ADDR_SECS])?;
            c.execute("DELETE FROM webauthn_challenges WHERE expires_at <= ?", params![now])?;
            c.execute("DELETE FROM api_tokens WHERE expires_at IS NOT NULL AND expires_at <= ?", params![now])?;
        }
        self.in_batches(
            "DELETE FROM commands WHERE rowid IN (SELECT rowid FROM commands WHERE state='done' AND updated_at < ?1 LIMIT ?2)",
            now - COMPLETED_COMMAND_KEEP_SECS,
        )?;
        self.c()?
            .execute("DELETE FROM mission_tombstones WHERE deleted_at < ?", params![now - TOMBSTONE_KEEP_SECS])?;
        // Result bodies: the request/outcome row stays, the body does not. A read-only result is dropped
        // after an hour and a mutating one after a day; short results stay until the row is deleted.
        let stub = "UPDATE commands SET result=json_object('outcome', json_extract(result,'$.outcome'), 'body_dropped', 1) \
                    WHERE rowid IN (SELECT rowid FROM commands WHERE state='done' AND mutating=?3 AND updated_at < ?1 \
                    AND length(result) > ?4 AND json_extract(result,'$.body_dropped') IS NULL LIMIT ?2)";
        for (mutating, keep) in [(0_i64, READ_RESULT_KEEP_SECS), (1, MUTATING_RESULT_KEEP_SECS)] {
            loop {
                let n = self
                    .c()?
                    .execute(stub, params![now - keep, MAINTENANCE_BATCH, mutating, RESULT_STUB_ABOVE_BYTES])?;
                if (n as i64) < MAINTENANCE_BATCH {
                    break;
                }
                std::thread::sleep(MAINTENANCE_PAUSE);
            }
        }
        // Row cap on finished commands, oldest first.
        loop {
            let c = self.c()?;
            let done: i64 = c.query_row("SELECT COUNT(*) FROM commands WHERE state='done'", [], |r| r.get(0))?;
            let excess = (done - MAX_DONE_COMMANDS).min(MAINTENANCE_BATCH);
            if excess <= 0 {
                break;
            }
            c.execute(
                "DELETE FROM commands WHERE rowid IN (SELECT rowid FROM commands WHERE state='done' ORDER BY updated_at, rowid LIMIT ?1)",
                params![excess],
            )?;
            drop(c);
            std::thread::sleep(MAINTENANCE_PAUSE);
        }
        self.c()?
            .execute("DELETE FROM audit WHERE id <= (SELECT COALESCE(MAX(id),0) FROM audit) - ?", params![AUDIT_KEEP])?;
        self.shrink()
    }

    /// Return a bounded number of free pages to the file (databases created with incremental
    /// auto-vacuum only) and fold the write-ahead log back. Nothing here runs a full VACUUM.
    fn shrink(&self) -> R<()> {
        let mode: i64 = self.c()?.query_row("PRAGMA auto_vacuum", [], |r| r.get(0))?;
        if mode == 2 {
            let mut left = INCREMENTAL_VACUUM_PAGES;
            while left > 0 {
                let c = self.c()?;
                let free: i64 = c.query_row("PRAGMA freelist_count", [], |r| r.get(0))?;
                if free == 0 {
                    break;
                }
                // Each step of this pragma frees one page, so the statement is stepped to its end. The
                // mutex is held for one small chunk at a time.
                let started = std::time::Instant::now();
                let mut st = c.prepare(&format!("PRAGMA incremental_vacuum({VACUUM_CHUNK_PAGES})"))?;
                let mut rows = st.query([])?;
                while rows.next()?.is_some() {}
                report_slow_step("incremental_vacuum", started);
                drop(rows);
                drop(st);
                drop(c);
                left -= VACUUM_CHUNK_PAGES;
                std::thread::sleep(MAINTENANCE_PAUSE);
            }
        }
        let started = std::time::Instant::now();
        self.c()?.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |_| Ok(()))?;
        report_slow_step("wal_checkpoint(PASSIVE)", started);
        Ok(())
    }

    /// Free pages that the file still holds and whether it can give them back without a rebuild.
    pub fn free_space(&self) -> R<(i64, i64, bool)> {
        let c = self.c()?;
        let size: i64 = c.query_row("PRAGMA page_size", [], |r| r.get(0))?;
        let free: i64 = c.query_row("PRAGMA freelist_count", [], |r| r.get(0))?;
        let total: i64 = c.query_row("PRAGMA page_count", [], |r| r.get(0))?;
        let mode: i64 = c.query_row("PRAGMA auto_vacuum", [], |r| r.get(0))?;
        Ok((free * size, total * size, mode == 2))
    }

    pub fn diagnostics(&self) -> R<Value> {
        let c = self.c()?;
        let pages: i64 = c.query_row("PRAGMA page_count", [], |r| r.get(0))?;
        let size: i64 = c.query_row("PRAGMA page_size", [], |r| r.get(0))?;
        let wal = std::fs::metadata(format!("{}-wal", self.path.display())).map(|m| m.len()).unwrap_or(0);
        let mut st = c.prepare("SELECT worker, state, COUNT(*) FROM commands WHERE state!='done' GROUP BY worker, state")?;
        let mut depths = serde_json::Map::new();
        for r in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?)))? {
            let (w, s, n) = r?;
            depths.entry(w).or_insert_with(|| json!({})).as_object_mut().map(|o| o.insert(s, json!(n)));
        }
        Ok(json!({"db_bytes": pages * size + wal as i64, "command_queue_depths": depths}))
    }
    }
}

/// Write cost of accepting an event batch. The cost is measured as the number of frames the commit appends to the
/// write-ahead log (a frame is one page image: 4096 bytes plus a 24-byte header), read from the size of the log file.
/// Automatic checkpoints are switched off so the log only grows, and every figure is a count of pages, not time.
#[cfg(test)]
mod batch_write_cost {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    const FRAME_BYTES: u64 = 4096 + 24;
    /// Measured 9.30 frames per batch of 25 events (about 330 bytes each) below the cap, with and without the
    /// 2026-11-07 change: the mission-row skip and the retention passes do not apply to this workload.
    /// The bound fails a change that adds a page or two to every batch.
    const AVERAGE_FRAMES_BELOW_CAP: f64 = 10.5;
    /// Measured 13.40 frames per batch at the cap with retention passes; 16.62 with a delete in every batch.
    const AVERAGE_FRAMES_AT_CAP: f64 = 15.0;
    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        dir: PathBuf,
        db: Db,
        next_id: u64,
    }

    impl Fixture {
        fn new() -> Fixture {
            let dir = std::env::temp_dir().join(format!("bridge-batch-cost-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            std::fs::create_dir_all(&dir).unwrap();
            let db = Db::open(&dir.join("test.sqlite3")).unwrap();
            db.c().unwrap().pragma_update(None, "wal_autocheckpoint", 0).unwrap();
            assert!(db.worker_add("w", "hash").unwrap());
            Fixture { dir, db, next_id: 1 }
        }

        fn wal_frames(&self) -> u64 {
            let len = std::fs::metadata(self.dir.join("test.sqlite3-wal")).map(|m| m.len()).unwrap_or(0);
            len.saturating_sub(32) / FRAME_BYTES
        }

        fn events(&mut self, count: u64) -> Vec<Value> {
            let events = (0..count)
                .map(|i| {
                    let id = self.next_id + i;
                    json!({"id": id, "kind": "task-output", "mission_id": "m-0123456789abcdef", "created_at": 1_780_000_000 + id,
                           "data": {"n": id, "text": "x".repeat(200)}})
                })
                .collect();
            self.next_id += count;
            events
        }

        /// Accept one batch of `count` events and return the number of log frames the commit wrote.
        fn batch(&mut self, count: u64, max: u64, missions: Option<&[Value]>) -> u64 {
            let events = self.events(count);
            let before = self.wal_frames();
            let outcome = self
                .db
                .events_store(EventBatch {
                    worker: "w",
                    after: self.next_id - 1 - count,
                    through: self.next_id - 1,
                    events: &events,
                    mission_cursor: Some(self.next_id - 1),
                    missions,
                    max,
                })
                .unwrap();
            assert!(matches!(outcome, EventsOutcome::Accepted { .. }));
            self.wal_frames() - before
        }

        fn stored(&self) -> Vec<u64> {
            self.db
                .events_list(Some("w"), 0, 1_000_000)
                .unwrap()
                .iter()
                .map(|e| e["id"].as_u64().unwrap())
                .collect()
        }

        fn average_frames(&mut self, batches: u64, count: u64, max: u64) -> f64 {
            let total: u64 = (0..batches).map(|_| self.batch(count, max, None)).sum();
            total as f64 / batches as f64
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    /// Production batches carry at most 25 events. Measured average per batch: see docs/distributed/benchmarks.md.
    #[test]
    fn a_production_sized_batch_writes_a_bounded_number_of_frames() {
        let mut f = Fixture::new();
        f.batch(25, 50_000, None); // the first batch also creates the first pages
        let average = f.average_frames(40, 25, 50_000);
        println!("FRAMES per batch of 25 events, below the cap: {average:.2}");
        assert!(average <= AVERAGE_FRAMES_BELOW_CAP, "{average} frames per batch");
    }

    #[test]
    fn a_batch_at_the_retention_cap_writes_a_bounded_number_of_frames() {
        let mut f = Fixture::new();
        for _ in 0..100 {
            f.batch(25, 2000, None);
        }
        assert!(f.stored().len() as u64 <= 2000);
        let average = f.average_frames(60, 25, 2000);
        println!("FRAMES per batch of 25 events, at the cap: {average:.2}");
        assert!(average <= AVERAGE_FRAMES_AT_CAP, "{average} frames per batch");
    }

    /// A batch without events that repeats a mission summary the coordinator already holds updates the worker row
    /// (cursor and time) and nothing else. SQLite counts a row it did not modify as no change, whereas the previous
    /// `INSERT OR REPLACE` rewrote the row, and its page, every time.
    #[test]
    fn an_unchanged_mission_summary_is_not_written_again() {
        let mut f = Fixture::new();
        let summary = json!({"id": "m1", "name": "n", "status": "RUNNING", "folder": "f", "updated_at": 1});
        f.batch(5, 50_000, Some(std::slice::from_ref(&summary)));
        let rows_changed = |f: &mut Fixture, summary: &Value| {
            let before = f.db.c().unwrap().total_changes();
            f.batch(0, 50_000, Some(std::slice::from_ref(summary)));
            f.db.c().unwrap().total_changes() - before
        };
        assert_eq!(rows_changed(&mut f, &summary), 1, "the worker row only");
        let changed = json!({"id": "m1", "name": "n", "status": "DONE", "folder": "f", "updated_at": 2});
        assert_eq!(rows_changed(&mut f, &changed), 2, "the worker row and the mission row");
        assert_eq!(f.db.missions_list().unwrap(), vec![("w".to_string(), changed)]);
    }

    /// Retention deletes in passes below the cap, never leaves more than the cap, and removes the oldest events first.
    #[test]
    fn retention_runs_in_passes_and_never_exceeds_the_cap() {
        let mut f = Fixture::new();
        let max = 100;
        let mut passes = 0;
        let mut previous = 0;
        for _ in 0..60 {
            f.batch(10, max, None);
            let ids = f.stored();
            assert!(ids.len() as u64 <= max, "{} events stored", ids.len());
            assert_eq!(*ids.last().unwrap(), f.next_id - 1, "the newest event is kept");
            assert!(
                ids.windows(2).all(|w| w[1] == w[0] + 1),
                "the oldest events go first, so the stored ids stay contiguous"
            );
            if ids.len() < previous {
                passes += 1;
            }
            previous = ids.len();
        }
        // 60 batches of 10 events against a cap of 100: a pass keeps 90, so one pass every second batch at most.
        assert!((20..=30).contains(&passes), "{passes} retention passes in 60 batches");
    }

    #[test]
    fn a_small_cap_is_exact() {
        let mut f = Fixture::new();
        f.batch(3, 4, None);
        f.batch(3, 4, None);
        assert_eq!(f.stored(), vec![3, 4, 5, 6]);
    }

    #[test]
    fn a_count_that_is_too_high_after_a_revoke_is_corrected_by_the_next_pass() {
        let mut f = Fixture::new();
        for _ in 0..9 {
            f.batch(10, 100, None);
        }
        assert!(f.db.worker_revoke("w").unwrap());
        assert!(f.db.worker_add("w", "hash2").unwrap());
        f.next_id = 1;
        f.batch(10, 100, None);
        f.batch(10, 100, None);
        assert_eq!(f.stored().len(), 20, "a new worker of the same name starts from an empty count");
    }
}

#[cfg(test)]
mod lock_wait_tests {
    use super::*;
    use std::sync::atomic::AtomicU64;
    use std::time::{Duration, Instant};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    fn open() -> (PathBuf, Db) {
        let dir = std::env::temp_dir().join(format!("bridge-lock-wait-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
        std::fs::create_dir_all(&dir).unwrap();
        let db = Db::open(&dir.join("test.sqlite3")).unwrap();
        (dir, db)
    }

    /// One test changes the process-wide wait, so the cases run in sequence inside it.
    #[test]
    fn a_caller_waits_a_bounded_time_and_the_queue_is_bounded_and_released() {
        let (dir, db) = open();
        let db = std::sync::Arc::new(db);
        set_lock_wait(Duration::from_millis(150));
        let held = db.c().unwrap();

        // A caller behind a held connection gets a busy error after the wait, not later and not never.
        let started = Instant::now();
        let err = db.worker_get("w").unwrap_err();
        let took = started.elapsed();
        assert!(is_busy(&err), "{err:?}");
        assert!(took >= Duration::from_millis(140) && took < Duration::from_secs(2), "{took:?}");

        // More waiters than the queue allows are refused at once.
        set_lock_wait(Duration::from_secs(5));
        let waiting: Vec<_> = (0..DEFAULT_MAX_LOCK_WAITERS)
            .map(|_| {
                let db = db.clone();
                std::thread::spawn(move || db.worker_get("w"))
            })
            .collect();
        let deadline = Instant::now() + Duration::from_secs(5);
        while db.lock_waiting() < DEFAULT_MAX_LOCK_WAITERS {
            assert!(Instant::now() < deadline, "waiters never queued: {}", db.lock_waiting());
            std::thread::sleep(Duration::from_millis(5));
        }
        let started = Instant::now();
        let err = db.worker_get("w").unwrap_err();
        assert!(
            is_busy(&err) && started.elapsed() < Duration::from_millis(500),
            "{err:?} after {:?}",
            started.elapsed()
        );

        // When the connection is released the queued callers are served and the count returns to zero.
        drop(held);
        for w in waiting {
            assert!(w.join().unwrap().is_ok());
        }
        assert_eq!(db.lock_waiting(), 0);
        assert!(db.worker_get("w").unwrap().is_none());
        set_lock_wait(Duration::from_secs(3));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn callers_are_served_in_the_order_they_arrived() {
        let (dir, db) = open();
        let db = std::sync::Arc::new(db);
        let held = db.c().unwrap();
        let order = std::sync::Arc::new(Mutex::new(Vec::new()));
        let mut threads = Vec::new();
        for i in 0..6 {
            let (caller, order) = (db.clone(), order.clone());
            threads.push(std::thread::spawn(move || {
                let _c = caller.c().unwrap();
                order.lock().unwrap().push(i);
            }));
            while db.lock_waiting() < i + 1 {
                std::thread::sleep(Duration::from_millis(2)); // the next caller starts after this one queued
            }
        }
        drop(held);
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(*order.lock().unwrap(), vec![0, 1, 2, 3, 4, 5]);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_poisoned_connection_mutex_does_not_stop_the_database() {
        let (dir, db) = open();
        let db = std::sync::Arc::new(db);
        let poisoner = db.clone();
        let _ = std::thread::spawn(move || {
            let _guard = poisoner.c().unwrap();
            panic!("a request handler panicked while holding the connection");
        })
        .join();
        assert!(db.worker_get("w").unwrap().is_none());
        let _ = std::fs::remove_dir_all(dir);
    }
}
