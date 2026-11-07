//! `bridge-coordinator admin reset-password`: the host-level recovery for a lost administrator
//! password (docs/distributed/install.md, "Locked out or forgot the password").
//!
//! The state lives in the existing `meta` key/value table, so no schema migration is needed:
//! `password_change_required` is present while a temporary password has not been replaced, and
//! `password_reset_at` holds the UTC time of the last reset so the web interface can tell the
//! administrator at the next sign-in that the host reset the password. A password change through
//! the API (`Db::admin_set_password`) removes both keys.

use crate::auth;
use crate::db::Db;
use rand::Rng;
use rusqlite::{params, OptionalExtension};
use std::io::{BufRead, IsTerminal};
use std::path::Path;

/// Error code of the 403 the API answers while a temporary password is still in place.
pub const CHANGE_REQUIRED_CODE: &str = "password-change-required";
/// Shown by the web interface after sign-in.
pub const RESET_NOTICE: &str = "Your password was reset by an administrator on the host.";
/// The reset notice is dropped from the API after this long if the password was not changed.
pub const NOTICE_KEEP_SECS: i64 = 7 * 86_400;

const KEY_REQUIRED: &str = "password_change_required";
const KEY_RESET_AT: &str = "password_reset_at";
/// Characters of a generated temporary password: no digits or letters that look alike.
const TEMP_ALPHABET: &[u8] = b"abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";

/// Where the new password comes from. The terminal implementation prompts without echo; tests
/// inject their own.
pub trait PasswordInput {
    /// True when a person is typing: the password is asked for twice, with a prompt.
    fn interactive(&self) -> bool;
    /// One password, without the line ending. `prompt` is empty when not interactive.
    fn read_secret(&mut self, prompt: &str) -> Result<String, String>;
}

pub struct Options {
    pub username: Option<String>,
    pub keep_api_tokens: bool,
    pub temporary: bool,
}

pub struct Report {
    /// The generated password, with `--temporary`. It is shown once and not stored.
    pub temporary_password: Option<String>,
    pub revoked_tokens: usize,
}

impl Report {
    pub fn text(&self, keep_api_tokens: bool) -> String {
        let tokens = if keep_api_tokens {
            "API tokens kept (--keep-api-tokens)".to_string()
        } else {
            format!("{} API token(s) revoked", self.revoked_tokens)
        };
        match &self.temporary_password {
            Some(p) => format!(
                "password reset; all sessions deleted; {tokens}\n\
                 temporary password (shown once): {p}\n\
                 It must be changed at the next sign-in; until then the web interface only accepts the password change.\n"
            ),
            None => format!("password changed; all sessions deleted; {tokens}\n"),
        }
    }
}

/// A random password of four groups of four characters, for example `kQ7m-Xe2p-...`.
pub fn temporary_password() -> String {
    let mut rng = rand::rngs::OsRng;
    let mut out = String::new();
    for group in 0..4 {
        if group > 0 {
            out.push('-');
        }
        for _ in 0..4 {
            out.push(TEMP_ALPHABET[rng.gen_range(0..TEMP_ALPHABET.len())] as char);
        }
    }
    out
}

/// Reset the administrator password. Nothing is changed unless every check passes.
pub fn reset_password(db: &Db, opts: &Options, input: &mut dyn PasswordInput) -> Result<Report, String> {
    let (username, _) = db
        .admin_get()
        .map_err(|e| e.to_string())?
        .ok_or("no administrator exists yet; use the setup token")?;
    if let Some(wanted) = &opts.username {
        if wanted != &username {
            return Err(format!(
                "no account named {wanted:?}: this coordinator has exactly one administrator account, named {username:?}"
            ));
        }
    }
    let (password, temporary) = if opts.temporary {
        (temporary_password(), true)
    } else if input.interactive() {
        let first = input.read_secret("New password: ")?;
        let second = input.read_secret("Repeat new password: ")?;
        if first != second {
            return Err("the two passwords do not match; nothing was changed".into());
        }
        (first, false)
    } else {
        (input.read_secret("")?, false)
    };
    auth::valid_password(&password)?;
    let revoked = db
        .admin_reset(&auth::hash_password(&password), opts.keep_api_tokens, temporary)
        .map_err(|e| e.to_string())?;
    Ok(Report {
        temporary_password: temporary.then_some(password),
        revoked_tokens: revoked,
    })
}

impl Db {
    /// In one transaction: set the password, delete every session, revoke every API token unless
    /// `keep_api_tokens`, record the reset time, set (`must_change`) or clear the change-required
    /// flag, and write the audit entry. Returns the number of revoked tokens.
    pub fn admin_reset(&self, hash: &str, keep_api_tokens: bool, must_change: bool) -> rusqlite::Result<usize> {
        let mut c = self.c();
        let tx = c.transaction()?;
        tx.execute("UPDATE admin SET password_hash=? WHERE id=1", params![hash])?;
        tx.execute("DELETE FROM sessions", [])?;
        let revoked = if keep_api_tokens { 0 } else { tx.execute("DELETE FROM api_tokens", [])? };
        tx.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            params![KEY_RESET_AT, crate::now().to_string()],
        )?;
        if must_change {
            tx.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, '1')", params![KEY_REQUIRED])?;
        } else {
            tx.execute("DELETE FROM meta WHERE key=?", params![KEY_REQUIRED])?;
        }
        let tokens = if keep_api_tokens {
            "API tokens kept".to_string()
        } else {
            format!("{revoked} API token(s) revoked")
        };
        let change = if must_change {
            "; temporary password, change required at next sign-in"
        } else {
            ""
        };
        tx.execute(
            "INSERT INTO audit(ts, actor, action, detail, remote) VALUES(?, 'cli', 'password.reset', ?, 'local')",
            params![crate::now(), format!("all sessions deleted; {tokens}{change}")],
        )?;
        tx.commit()?;
        Ok(revoked)
    }

    /// True while a temporary password set by `admin reset-password --temporary` is in place.
    pub fn password_change_required(&self) -> rusqlite::Result<bool> {
        Ok(self.meta_value(KEY_REQUIRED)?.is_some())
    }

    /// UTC time of the last reset by the host, while the notice is still current: until the
    /// password is changed through the API or [`NOTICE_KEEP_SECS`] have passed.
    pub fn password_reset_notice(&self) -> rusqlite::Result<Option<i64>> {
        let at = self.meta_value(KEY_RESET_AT)?.and_then(|v| v.parse::<i64>().ok());
        Ok(at.filter(|at| crate::now() - at < NOTICE_KEEP_SECS))
    }

    fn meta_value(&self, key: &str) -> rusqlite::Result<Option<String>> {
        self.c().query_row("SELECT value FROM meta WHERE key=?", params![key], |r| r.get(0)).optional()
    }
}

/// Reads the password from the process's standard input: with a prompt and no echo on a
/// terminal, one line otherwise.
pub struct TerminalInput {
    tty: bool,
}

impl TerminalInput {
    pub fn new() -> TerminalInput {
        TerminalInput {
            tty: std::io::stdin().is_terminal(),
        }
    }
}

impl Default for TerminalInput {
    fn default() -> Self {
        Self::new()
    }
}

impl PasswordInput for TerminalInput {
    fn interactive(&self) -> bool {
        self.tty
    }

    fn read_secret(&mut self, prompt: &str) -> Result<String, String> {
        let restore = if self.tty { Some(EchoOff::new()?) } else { None };
        if self.tty {
            eprint!("{prompt}");
        }
        let mut line = String::new();
        let read = std::io::stdin().lock().read_line(&mut line);
        drop(restore);
        if self.tty {
            eprintln!();
        }
        read.map_err(|e| e.to_string())?;
        Ok(line.trim_end_matches(['\n', '\r']).to_string())
    }
}

/// Switches terminal echo off with `stty` (no extra dependency) and restores the saved settings
/// on drop.
struct EchoOff {
    saved: String,
}

impl EchoOff {
    fn new() -> Result<EchoOff, String> {
        use std::process::{Command, Stdio};
        let fail = |e: &dyn std::fmt::Display| format!("cannot turn terminal echo off ({e}); pipe the password on standard input instead");
        let out = Command::new("stty")
            .arg("-g")
            .stdin(Stdio::inherit())
            .stderr(Stdio::null())
            .output()
            .map_err(|e| fail(&e))?;
        if !out.status.success() {
            return Err(fail(&"stty -g failed"));
        }
        let saved = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let status = Command::new("stty").arg("-echo").stdin(Stdio::inherit()).status().map_err(|e| fail(&e))?;
        if !status.success() {
            return Err(fail(&"stty -echo failed"));
        }
        Ok(EchoOff { saved })
    }
}

impl Drop for EchoOff {
    fn drop(&mut self) {
        let _ = std::process::Command::new("stty")
            .arg(&self.saved)
            .stdin(std::process::Stdio::inherit())
            .status();
    }
}

/// Files of the database that `admin reset-password` may have created as another user (root, when
/// run through sudo) and that must belong to the owner of the data directory, which is the
/// service user. Empty when ownership already matches.
#[cfg(unix)]
pub fn files_needing_chown(dir: &Path) -> Vec<std::path::PathBuf> {
    use std::os::unix::fs::MetadataExt;
    use std::path::PathBuf;
    let Ok(owner) = std::fs::metadata(dir) else {
        return vec![];
    };
    let base = crate::db_path(dir);
    let names = [
        base.clone(),
        PathBuf::from(format!("{}-wal", base.display())),
        PathBuf::from(format!("{}-shm", base.display())),
    ];
    names
        .into_iter()
        .filter(|p| std::fs::metadata(p).is_ok_and(|m| m.uid() != owner.uid() || m.gid() != owner.gid()))
        .collect()
}

/// Give the database files back to the owner of the data directory. Best effort: when the command
/// runs as the service user nothing needs to change, and an unprivileged user cannot chown.
#[cfg(unix)]
pub fn restore_ownership(dir: &Path) {
    use std::os::unix::fs::MetadataExt;
    let Ok(owner) = std::fs::metadata(dir) else {
        return;
    };
    for p in files_needing_chown(dir) {
        let _ = std::os::unix::fs::chown(&p, Some(owner.uid()), Some(owner.gid()));
    }
}

#[cfg(not(unix))]
pub fn restore_ownership(_dir: &Path) {}
