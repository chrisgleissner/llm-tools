//! SQLite storage (WAL, synchronous=FULL, `user_version` migrations).

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

pub const SESSION_IDLE_SECS: i64 = 8 * 3600;
pub const SESSION_ABSOLUTE_SECS: i64 = 7 * 86_400;
pub const COMPLETED_COMMAND_KEEP_SECS: i64 = 7 * 86_400;
pub const MAX_OPEN_COMMANDS_PER_WORKER: i64 = 1000;
const AUDIT_KEEP: i64 = 100_000;

const SCHEMA: &str = "
CREATE TABLE workers(
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, worker_id TEXT,
  generation INTEGER NOT NULL DEFAULT 0, event_cursor INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, last_seen INTEGER, hello_at INTEGER, last_event_at INTEGER,
  software_version TEXT, protocol_versions TEXT, platform TEXT, capabilities TEXT);
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
  name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, last_used INTEGER);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL, remote TEXT NOT NULL);
";

pub struct Db {
    conn: Mutex<Connection>,
    path: PathBuf,
}

#[derive(Debug, Clone)]
pub struct WorkerRow {
    pub name: String,
    pub worker_id: Option<String>,
    pub generation: u64,
    pub event_cursor: u64,
    pub last_seen: Option<i64>,
    pub hello_at: Option<i64>,
    pub last_event_at: Option<i64>,
    pub software_version: Option<String>,
    pub protocol_versions: Option<Value>,
    pub platform: Option<Value>,
    pub capabilities: Option<Value>,
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

pub enum InsertOutcome {
    New(CommandRow),
    Existing(CommandRow),
    Conflict,
    QueueFull,
}

pub enum EventsOutcome {
    Accepted { cursor: u64, stored: usize },
    Nack { expected: u64 },
}

pub enum HelloOutcome {
    Ok { generation: u64, cursor: u64 },
    WorkerIdMismatch,
    Unknown,
}

pub struct Session {
    pub csrf: String,
    pub token_hash: String,
}

fn json_col(s: Option<String>) -> Option<Value> {
    s.and_then(|s| serde_json::from_str(&s).ok())
}

const WCOLS: &str =
    "name, worker_id, generation, event_cursor, last_seen, hello_at, last_event_at, software_version, protocol_versions, platform, capabilities";

fn worker_row(r: &rusqlite::Row) -> rusqlite::Result<WorkerRow> {
    Ok(WorkerRow {
        name: r.get(0)?,
        worker_id: r.get(1)?,
        generation: r.get::<_, i64>(2)? as u64,
        event_cursor: r.get::<_, i64>(3)? as u64,
        last_seen: r.get(4)?,
        hello_at: r.get(5)?,
        last_event_at: r.get(6)?,
        software_version: r.get(7)?,
        protocol_versions: json_col(r.get(8)?),
        platform: json_col(r.get(9)?),
        capabilities: json_col(r.get(10)?),
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
pub const SCHEMA_VERSION: i64 = 1;

type R<T> = rusqlite::Result<T>;

impl Db {
    pub fn open(path: &Path) -> R<Db> {
        let conn = Connection::open(path)?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
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
            conn.execute_batch(&format!("BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"))?;
        }
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
        Ok(Db {
            conn: Mutex::new(conn),
            path: path.to_path_buf(),
        })
    }

    fn c(&self) -> MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|e| e.into_inner())
    }

    // ---- admin ----
    pub fn admin_get(&self) -> R<Option<(String, String)>> {
        self.c()
            .query_row("SELECT username, password_hash FROM admin WHERE id=1", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()
    }
    pub fn admin_create(&self, username: &str, hash: &str) -> R<bool> {
        Ok(self.c().execute(
            "INSERT OR IGNORE INTO admin(id, username, password_hash, created_at) VALUES(1,?,?,?)",
            params![username, hash, crate::now()],
        )? == 1)
    }
    /// Set the password and delete every session in one transaction.
    pub fn admin_set_password(&self, hash: &str) -> R<()> {
        let mut c = self.c();
        let tx = c.transaction()?;
        tx.execute("UPDATE admin SET password_hash=? WHERE id=1", params![hash])?;
        tx.execute("DELETE FROM sessions", [])?;
        tx.commit()
    }

    // ---- sessions ----
    pub fn session_create(&self, token_hash: &str, csrf: &str) -> R<()> {
        let now = crate::now();
        self.c().execute(
            "INSERT INTO sessions(token_hash, csrf, created_at, last_seen, expires_at) VALUES(?,?,?,?,?)",
            params![token_hash, csrf, now, now, now + SESSION_ABSOLUTE_SECS],
        )?;
        Ok(())
    }
    pub fn session_get(&self, token_hash: &str) -> R<Option<Session>> {
        let now = crate::now();
        let c = self.c();
        let row: Option<(String, i64, i64)> = c
            .query_row(
                "SELECT csrf, last_seen, expires_at FROM sessions WHERE token_hash=?",
                params![token_hash],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        let Some((csrf, last_seen, expires_at)) = row else { return Ok(None) };
        if now >= expires_at || now - last_seen >= SESSION_IDLE_SECS {
            c.execute("DELETE FROM sessions WHERE token_hash=?", params![token_hash])?;
            return Ok(None);
        }
        if now - last_seen >= 60 {
            c.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", params![now, token_hash])?;
        }
        Ok(Some(Session {
            csrf,
            token_hash: token_hash.to_string(),
        }))
    }
    pub fn session_delete(&self, token_hash: &str) -> R<()> {
        self.c().execute("DELETE FROM sessions WHERE token_hash=?", params![token_hash])?;
        Ok(())
    }
    /// Test hook and maintenance: age a session so idle/absolute expiry can be exercised.
    pub fn session_age(&self, token_hash: &str, last_seen: i64, expires_at: i64) -> R<()> {
        self.c().execute(
            "UPDATE sessions SET last_seen=?, expires_at=? WHERE token_hash=?",
            params![last_seen, expires_at, token_hash],
        )?;
        Ok(())
    }
    pub fn session_count(&self) -> R<i64> {
        self.c().query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))
    }

    // ---- api tokens ----
    pub fn token_create(&self, name: &str, hash: &str) -> R<bool> {
        Ok(self.c().execute(
            "INSERT OR IGNORE INTO api_tokens(name, token_hash, created_at) VALUES(?,?,?)",
            params![name, hash, crate::now()],
        )? == 1)
    }
    pub fn token_revoke(&self, name: &str) -> R<bool> {
        Ok(self.c().execute("DELETE FROM api_tokens WHERE name=?", params![name])? == 1)
    }
    /// Name of the API token with this hash, touching `last_used` at most once a minute.
    pub fn token_lookup(&self, hash: &str) -> R<Option<String>> {
        let c = self.c();
        let row: Option<(String, Option<i64>)> = c
            .query_row("SELECT name, last_used FROM api_tokens WHERE token_hash=?", params![hash], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .optional()?;
        let Some((name, last)) = row else { return Ok(None) };
        let now = crate::now();
        if last.is_none_or(|l| now - l >= 60) {
            c.execute("UPDATE api_tokens SET last_used=? WHERE name=?", params![now, name])?;
        }
        Ok(Some(name))
    }

    // ---- workers ----
    pub fn worker_add(&self, name: &str, hash: &str) -> R<bool> {
        Ok(self.c().execute(
            "INSERT OR IGNORE INTO workers(name, token_hash, created_at) VALUES(?,?,?)",
            params![name, hash, crate::now()],
        )? == 1)
    }
    pub fn worker_revoke(&self, name: &str) -> R<bool> {
        Ok(self.c().execute("DELETE FROM workers WHERE name=?", params![name])? == 1)
    }
    pub fn worker_by_token(&self, hash: &str) -> R<Option<WorkerRow>> {
        self.c()
            .query_row(&format!("SELECT {WCOLS} FROM workers WHERE token_hash=?"), params![hash], worker_row)
            .optional()
    }
    pub fn worker_get(&self, name: &str) -> R<Option<WorkerRow>> {
        self.c()
            .query_row(&format!("SELECT {WCOLS} FROM workers WHERE name=?"), params![name], worker_row)
            .optional()
    }
    pub fn workers_list(&self) -> R<Vec<WorkerRow>> {
        let c = self.c();
        let mut st = c.prepare(&format!("SELECT {WCOLS} FROM workers ORDER BY name"))?;
        let rows = st.query_map([], worker_row)?.collect();
        rows
    }
    pub fn worker_seen(&self, name: &str) -> R<()> {
        self.c().execute("UPDATE workers SET last_seen=? WHERE name=?", params![crate::now(), name])?;
        Ok(())
    }

    /// Record a validated hello: bind the stable worker id, bump the persisted generation, replace the mission index.
    pub fn worker_hello(&self, name: &str, worker_id: &str, hello: &serde_json::Map<String, Value>, capabilities: &Value) -> R<HelloOutcome> {
        let mut c = self.c();
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
        let generation = generation + 1;
        let text = |k: &str| hello.get(k).map(|v| v.to_string());
        tx.execute("UPDATE workers SET worker_id=?, generation=?, last_seen=?, hello_at=?, software_version=?, protocol_versions=?, platform=?, capabilities=? WHERE name=?",
            params![worker_id, generation, now, now, hello.get("software_version").and_then(Value::as_str), text("protocol_versions"), text("platform"), capabilities.to_string(), name])?;
        tx.execute("DELETE FROM missions WHERE worker=?", params![name])?;
        if let Some(missions) = hello.get("missions").and_then(Value::as_array) {
            for m in missions {
                if let Some(id) = m.get("id").and_then(Value::as_str) {
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
        let c = self.c();
        let mut st = c.prepare("SELECT worker, info FROM missions ORDER BY worker, mission_id")?;
        let rows = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        rows.map(|r| r.map(|(w, i)| (w, serde_json::from_str(&i).unwrap_or(Value::Null)))).collect()
    }

    // ---- events ----
    pub fn events_store(&self, worker: &str, after: u64, through: u64, events: &[Value], max: u64) -> R<EventsOutcome> {
        let mut c = self.c();
        let tx = c.transaction()?;
        let cursor: i64 = tx.query_row("SELECT event_cursor FROM workers WHERE name=?", params![worker], |r| r.get(0))?;
        if after != cursor as u64 {
            return Ok(EventsOutcome::Nack { expected: cursor as u64 });
        }
        for e in events {
            let id = e.get("id").and_then(Value::as_u64).unwrap_or(0) as i64;
            tx.execute(
                "INSERT OR REPLACE INTO events(worker, id, body) VALUES(?,?,?)",
                params![worker, id, e.to_string()],
            )?;
        }
        tx.execute(
            "UPDATE workers SET event_cursor=?, last_event_at=? WHERE name=?",
            params![through as i64, crate::now(), worker],
        )?;
        tx.execute(
            "DELETE FROM events WHERE worker=?1 AND id <= (SELECT id FROM events WHERE worker=?1 ORDER BY id DESC LIMIT 1 OFFSET ?2)",
            params![worker, max as i64],
        )?;
        tx.commit()?;
        Ok(EventsOutcome::Accepted {
            cursor: through,
            stored: events.len(),
        })
    }

    pub fn events_list(&self, worker: Option<&str>, after: u64, limit: u64) -> R<Vec<Value>> {
        let c = self.c();
        let mut st = c.prepare("SELECT worker, id, body FROM events WHERE (?1 IS NULL OR worker=?1) AND id > ?2 ORDER BY worker, id LIMIT ?3")?;
        let rows = st.query_map(params![worker, after as i64, limit as i64], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, String>(2)?))
        })?;
        rows.map(|r| r.map(|(w, id, b)| json!({"worker": w, "id": id, "event": serde_json::from_str::<Value>(&b).unwrap_or(Value::Null)})))
            .collect()
    }

    // ---- commands ----
    pub fn command_get(&self, id: &str) -> R<Option<CommandRow>> {
        self.c()
            .query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)
            .optional()
    }

    pub fn command_insert(&self, worker: &str, operation: &str, mutating: bool, request: &Value) -> R<InsertOutcome> {
        let id = request.get("request_id").and_then(Value::as_str).unwrap_or_default().to_string();
        let mut c = self.c();
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
        let open: i64 = tx.query_row("SELECT COUNT(*) FROM commands WHERE worker=? AND state!='done'", params![worker], |r| r.get(0))?;
        if open >= MAX_OPEN_COMMANDS_PER_WORKER {
            return Ok(InsertOutcome::QueueFull);
        }
        let now = crate::now();
        tx.execute(
            "INSERT INTO commands(request_id, worker, operation, mutating, request, state, created_at, updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
            params![id, worker, operation, mutating as i64, request.to_string(), now, now],
        )?;
        let row = tx.query_row(&format!("SELECT {CCOLS} FROM commands WHERE request_id=?"), params![id], command_row)?;
        tx.commit()?;
        Ok(InsertOutcome::New(row))
    }

    pub fn commands_resend(&self, worker: &str) -> R<Vec<CommandRow>> {
        let c = self.c();
        let mut st = c.prepare(&format!(
            "SELECT {CCOLS} FROM commands WHERE worker=? AND state IN ('pending','sent','acked') ORDER BY rowid"
        ))?;
        let rows = st.query_map(params![worker], command_row)?.collect();
        rows
    }

    fn transition(&self, sql: &str, args: &[&dyn rusqlite::ToSql], id: &str) -> R<Option<CommandRow>> {
        let c = self.c();
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
        let _ = self.c().execute(
            "INSERT INTO audit(ts, actor, action, detail, remote) VALUES(?,?,?,?,?)",
            params![crate::now(), actor, action, detail, remote],
        );
    }
    pub fn audit_list(&self, limit: i64) -> R<Vec<Value>> {
        let c = self.c();
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

    pub fn maintenance(&self) -> R<()> {
        let now = crate::now();
        let c = self.c();
        c.execute(
            "DELETE FROM sessions WHERE expires_at <= ?1 OR last_seen <= ?2",
            params![now, now - SESSION_IDLE_SECS],
        )?;
        c.execute(
            "DELETE FROM commands WHERE state='done' AND updated_at < ?",
            params![now - COMPLETED_COMMAND_KEEP_SECS],
        )?;
        c.execute("DELETE FROM audit WHERE id <= (SELECT COALESCE(MAX(id),0) FROM audit) - ?", params![AUDIT_KEEP])?;
        Ok(())
    }

    pub fn diagnostics(&self) -> R<Value> {
        let c = self.c();
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
