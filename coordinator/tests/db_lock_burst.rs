//! A burst of sign-in requests must not be refused because of contention for the database connection.
//!
//! Each task runs the database calls of one refused sign-in (`known_addr`, then the notification write
//! that the rate limiter triggers) or one verified-and-failed sign-in (`known_addr`, `admin_get`, the audit
//! insert), on a two-thread runtime like the coordinator's. The hash itself runs outside the database
//! connection (`verify_blocking`), so it is not part of the locked sections measured here.
//! The longest hold is printed for information; it is not asserted, because it depends on host load.
use bridge_coordinator::db::Db;
use std::sync::Arc;
use std::time::{Duration, Instant};

fn burst(requests: usize, verified: usize) -> (usize, Duration, bridge_coordinator::db::LockSnapshot, Duration) {
    let dir = std::env::temp_dir().join(format!("bridge-lock-burst-{}-{requests}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let db = Arc::new(Db::open(&dir.join("t.sqlite3")).unwrap());
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap();
    let started = Instant::now();
    let (failed, slowest) = rt.block_on(async {
        let mut tasks = Vec::new();
        for i in 0..requests {
            let db = db.clone();
            tasks.push(tokio::spawn(async move {
                let begun = Instant::now();
                let r = (|| {
                    db.known_addr("ip:127.0.0.1")?;
                    if i < verified {
                        db.admin_get()?;
                        db.audit("admin", "login.failure", "invalid credentials", "127.0.0.1");
                    } else {
                        db.security_notification_add("admin", "repeated_sign_in_failures")?;
                    }
                    Ok::<_, rusqlite::Error>(())
                })();
                (r.is_err(), begun.elapsed())
            }));
        }
        let mut failed = 0;
        let mut slowest = Duration::ZERO;
        for t in tasks {
            let (f, d) = t.await.unwrap();
            failed += f as usize;
            slowest = slowest.max(d);
        }
        (failed, slowest)
    });
    let total = started.elapsed();
    let stats = db.lock_stats();
    drop(rt);
    let _ = std::fs::remove_dir_all(dir);
    (failed, total, stats, slowest)
}

/// 60 is the size of the burst in `hardening.rs`; 256 is the default `max_connections`, the most callers
/// that can be in flight at once (every connection has one call at a time). On the development host the
/// 60-request burst queued 56 callers and the 256-request burst queued 247, with the connection held for
/// at most a few milliseconds at a time.
#[test]
fn a_burst_of_sign_in_requests_is_not_refused_for_database_contention() {
    for n in [60, 256] {
        let (failed, total, stats, slowest) = burst(n, 5);
        println!(
            "burst of {n}: {failed} refused, {:.0} ms in all, slowest request {:.0} ms, longest connection hold {:.2} ms, peak waiters {}",
            total.as_secs_f64() * 1000.0,
            slowest.as_secs_f64() * 1000.0,
            stats.max_hold.as_secs_f64() * 1000.0,
            stats.peak_waiters
        );
        assert_eq!(failed, 0, "{n} requests: some were refused for contention");
        // The bound is on work, not elapsed time: wall-clock holds vary with host load (a Windows CI run
        // measured 553 ms). Each request makes at most three calls that take the connection (the first
        // `known_addr` writes the salt, then either the notification write or the audit insert). A call
        // that retried, polled or took the connection twice would raise the count above this bound.
        assert!(stats.acquisitions >= n as u64, "{n} requests: only {} acquisitions", stats.acquisitions);
        assert!(
            stats.acquisitions <= 3 * n as u64,
            "{n} requests took the connection {} times, more than 3 per request",
            stats.acquisitions
        );
        assert!(stats.peak_waiters < n, "{n} requests: {} waited at once", stats.peak_waiters);
    }
}
