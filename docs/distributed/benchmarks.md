# Coordinator measurements

Harness: `coordinator/tests/bench.rs` (ignored test), run with `deploy/bench.sh [FILE]`
or `cargo test --release --test bench -- --ignored bench --nocapture`. It starts the
release `bridge-coordinator` binary on 127.0.0.1 with a temporary data directory and a
fake worker in the test process. RSS is read from `/proc/<pid>/status` of the
coordinator process only (`VmRSS`; peak is `VmHWM`).

## x86-64 host measurement

Host: Intel Core i7-6700K, 8 logical CPUs, 48 GiB RAM, Linux 7.0, release build
(`opt-level = "z"`, LTO, `panic = "abort"`), loopback network. Single run on 2026-11-05.

| Measurement | Result |
|---|---|
| Startup to `/healthz` (process spawn to first healthy answer) | 47 ms |
| Idle RSS (2 s after healthy) | 6.2 MiB |
| RSS after administrator setup and one login (Argon2id, 19 MiB working memory) | 25.4 MiB |
| RSS after 1000 events | 26.4 MiB |
| Peak RSS (`VmHWM`) up to and including the 1000-event load | 26.4 MiB |
| RSS after 10 000 events | 29.9 MiB |
| `GET /api/v1/missions` p50 / p95 (100 sequential requests) | 0.29 / 0.36 ms |
| `POST /api/v1/commands` p50 / p95 (100 sequential requests) | 0.52 / 0.84 ms |
| Event ingest throughput (9000 events, batches of 500, 200-byte payload) | about 40 000 events/s |
| Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20 reconnects) | 5.2 / 10.3 ms |
| Database growth for 10 000 events (main file + WAL) | 5.32 MiB (557 bytes/event) |

Notes:

- The API latency figures are for one client on loopback with a warm connection pool
  and no TLS session set-up per request. They do not include network time.
- The step from 6.2 MiB to 25.4 MiB is the Argon2id password hash (19 MiB per hash,
  at most two at once). A container or unit memory limit should cover idle RSS plus
  that working memory twice, plus event and connection growth. Under this load the
  measured peak was 26.4 MiB. The container measurement below drives both Argon2id
  slots at once and is the basis of the shipped limits.
- Each run uses a fresh database, so these figures do not show long-term growth
  beyond `max_events_per_worker` (default 50 000 events per worker).

## x86-64 host measurement, container image

Same host, image `llm-bridge-coordinator` built by `container/build.sh` (static musl
binary, `FROM scratch`), rootless Docker 29.9, `container/compose.yaml` limits
(96 MiB, one CPU). RSS read from `/proc/<pid>/status` and the container cgroup on
the host. One run on 2026-11-05.

| Measurement | Result |
|---|---|
| Image size | 6.44 MB |
| RSS before the first login | 1.5 MiB |
| Load: 30 concurrent logins (Argon2id, 2 slots), 200 concurrent `POST /api/v1/commands` with `wait_ms` through a native worker, 1500 replicated events | 200 commands answered in 1.4 s |
| Peak RSS (`VmHWM`) | 48.1 MiB |
| Peak cgroup memory (`memory.peak`, includes page cache) | 48.3 MiB |
| RSS after the load | 32.8 MiB |
| `docker compose stop` (SIGTERM to exit) | 0.3 s |

Basis of the shipped limit: 96 MiB (`mem_limit`/`memswap_limit` in `compose.yaml`,
`MemoryMax=96M` in the systemd unit), about twice the measured peak. Idle RSS is
about 2 MiB before the first login and 15-35 MiB afterwards, because the two Argon2id
slots and the allocator keep memory. How to apply the limit is described in
`install.md`.

## x86-64 container under memory pressure

Same host and image, one run per limit on 2026-11-05. For each limit the
compose file's `mem_limit`/`memswap_limit` were set through
`BRIDGE_MEMORY_LIMIT`, the volume was initialised, the administrator was
created, and then 30 concurrent logins with the correct password were sent,
followed by 200 concurrent `GET /api/v1/missions` with the session. The
login limiter admits 5 of the 30 logins per minute from one address (the
other 25 get 429), so at most two Argon2id hashes (19 MiB each) run at once.
Peak is the container cgroup's `memory.peak`.

| Limit | Logins | Peak | Result |
|---|---|---|---|
| 96 MiB | 5 x 200, 25 x 429 | 41 MiB | 199 of 200 GETs answered, 1 closed during the TLS handshake. |
| 64 MiB | 5 x 200, 25 x 429 | 42 MiB | 200 of 200 GETs answered. |
| 48 MiB | 5 x 200, 25 x 429 | 42 MiB | 164 of 200 GETs answered, 36 closed during the TLS handshake; no restart. |
| 40 MiB, 32 MiB, 24 MiB | connections dropped | - | An allocation failed during the concurrent Argon2id hashes, the process aborted and Docker restarted it (`restart: unless-stopped`, not reported as an OOM kill). The restarted process was healthy and kept its data. |

Docker's userland proxy makes all 200 parallel GET connections come from one
address, so the per-address connection cap (32, see `coordinator.md`) can close
connections above it during the TLS handshake. The number closed varied
between runs (0 to 36). This run does not separate that effect from memory
at 48 MiB; the process did not restart there.

The coordinator needs about 42 MiB while two password hashes run. Below
about 48 MiB a login burst ends the process. The shipped limit of 96 MiB
keeps about twice that peak as headroom.

## Raspberry Pi Zero 2 W

Not measured (no hardware available). Do not read any figure above as a measurement
for that device. The sizing that `install.md` gives for it (Raspberry Pi Zero 2 W
target profile) is derived from the x86-64 figures and is labelled as such there.
