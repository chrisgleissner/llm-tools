# Coordinator measurements

Harness: `coordinator/tests/bench.rs` (ignored test), run with `deploy/bench.sh [FILE]`
or `cargo test --release --test bench -- --ignored bench --nocapture`. It starts the
release `bridge-coordinator` binary on 127.0.0.1 with a temporary data directory and a
fake worker in the test process. RSS is read from `/proc/<pid>/status` of the
coordinator process only (`VmRSS`; peak is `VmHWM`).

## x86-64 host measurement

Host: Intel Core i7-6700K, 8 logical CPUs, 48 GiB RAM, Linux 7.0, release build
(`opt-level = "z"`, LTO, `panic = "abort"`), loopback network. Single run on 2026-11-07.

| Measurement | Result |
|---|---|
| Startup to `/healthz` (process spawn to first healthy answer) | 53 ms |
| Idle RSS (2 s after healthy) | 8.1 MiB |
| RSS after administrator setup and one login (Argon2id, 19 MiB working memory) | 27.2 MiB |
| RSS after 1000 events | 28.3 MiB |
| Peak RSS (`VmHWM`) up to and including the 1000-event load | 28.3 MiB |
| RSS after 10 000 events | 30.3 MiB |
| `GET /api/v1/missions` p50 / p95 (100 sequential requests) | 0.22 / 0.28 ms |
| `POST /api/v1/commands` p50 / p95 (100 sequential requests) | 0.44 / 1.08 ms |
| Event ingest throughput (9000 events, batches of 100, 200-byte payload) | 52 232 events/s |
| Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20 reconnects) | 49.8 / 59.0 ms |
| Database growth for 10 000 events (main file + WAL) | 6.59 MiB (691 bytes/event) |

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

## ARM64, emulated (QEMU raspi3b TCG, mem=512M)

Guest: DietPi v10.8.2, Debian 13 arm64, kernel 6.18.60+rpt-rpi-v8, 4 emulated
Cortex-A53 cores, 478 MB usable RAM, root file system on an emulated SD card
(ext4). QEMU emulates every instruction (TCG) on the x86-64 host above, so
**time figures are far slower than real hardware and are not a prediction for
the Pi Zero 2 W**. Memory and database figures do not depend on emulation speed.
Binary: the static `aarch64-unknown-linux-musl` release build that the image
uses. One run on 2026-11-05.

The measurement harness (`coordinator/tests/bench.rs`) was cross-compiled for
aarch64-musl and run inside the guest, so client and server share the guest as
they share the host in the x86-64 run. The database was on the SD card
(`TMPDIR=/var/tmp`; `/tmp` is a RAM disk in DietPi).

| Measurement | Result |
|---|---|
| Startup to `/healthz` | 767 ms |
| Idle RSS (2 s after healthy) | 3.2 MiB |
| RSS after administrator setup and one login | 3.4 MiB |
| RSS after 1000 events | 6.2 MiB |
| Peak RSS (`VmHWM`) up to and including the 1000-event load | 22.1 MiB |
| RSS after 10 000 events | 15.1 MiB |
| `GET /api/v1/missions` p50 / p95 (100 sequential requests) | 7.75 / 9.03 ms |
| `POST /api/v1/commands` p50 / p95 (100 sequential requests) | 51.37 / 72.60 ms |
| Event ingest throughput (9000 events, batches of 500, 200-byte payload; 2026-11-05, before the 128 KiB batch cap) | 1506 events/s |
| Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20 reconnects) | 174.2 / 220.3 ms |
| Database growth for 10 000 events (main file + WAL) | 5.32 MiB (558 bytes/event) |

RSS falls back after a login here but not in the x86-64 host run. The musl
build's allocator returns the Argon2id working memory to the system, while the
glibc host build keeps it. The peak is what a memory limit must cover.

Native systemd service installed with `deploy/install.sh --lan` (inside the
guest; the guest port is forwarded only to the host's loopback):

| Measurement | Result |
|---|---|
| `install.sh` run (user, `init`, unit, `info`) | 9.8 s |
| Service RSS / peak (`VmRSS` / `VmHWM`) after administrator setup, a worker, remote commands and a restart | 4.6 MiB / 22.2 MiB |
| Unit `MemoryPeak` (`MemoryMax=96M`) | 20.5 MiB |
| `systemctl restart` to healthy | 1.45 s |
| `systemctl stop` | 0.21 s |
| Guest memory used with the service running | 80 MB of 478 MB |
| `install.sh` rerun with a newer binary (upgrade; database, keys and administrator kept) | 11.7 s |
| After `systemctl reboot`: service active at boot, RSS / peak | 3.4 MiB / 21.8 MiB |

Container, 2026-11-06, image built from 311538b (`container/build.sh --oci`),
Docker 29.9.3 and Compose 5.7.0 installed with `dietpi-software install 162 134`,
`compose.yaml` plus `compose.low-write.yaml` (journald, five-minute health checks),
published on all guest interfaces and reached through the forwarded port:

| Measurement | Result |
|---|---|
| `docker load` of the multi-architecture OCI archive (4.4 MB; arm64 image 8.4 MB) | 7.3 s |
| `docker compose run --rm coordinator init` | 12.9 s |
| `docker compose up -d` | 7.0 s |
| Container memory idle (`docker stats`) | 2.4 MiB of 96 MiB |
| Container peak (cgroup `memory.peak`) after administrator setup, a login, a worker and remote commands | 21.7 MiB |
| Guest memory used with Docker and the container running | 146 MB of 478 MB |
| After `systemctl reboot`: container started by Docker, healthy, state kept | yes (worker reconnected, administrator login and tasks unchanged) |

Starting Docker in this guest first cut its network: Docker's default bridge
`172.17.0.0/16` overlapped the emulator's own uplink on the outer Docker bridge.
Setting `bip` and `default-address-pools` in the guest's `/etc/docker/daemon.json`
fixed it (install.md, Container deployment). This is a property of the emulator setup,
not of the coordinator.

## GitHub-hosted ARM64 runner (not a Pi)

Figures from the `arm64-container` job of CI run 42170022995 on b967d89 (pull request
merge commit f24750b), artifact `bench-arm64`, file `bench-arm64.md`. Runner: aarch64,
4 CPUs, 15.6 GiB RAM, Ubuntu 24.04.5 LTS, kernel 7.0.0-1017-azure.

Method: the job runs natively on a GitHub-hosted `ubuntu-24.04-arm` runner (a
cloud arm64 virtual machine, not a Raspberry Pi). It builds the linux/arm64 image with
`container/build.sh --load` (static `aarch64-unknown-linux-musl` binary, the same
artifact as the Pi image), then:

- `deploy/ci/container-check.sh smoke`: `init`, `/healthz` 200, an unauthenticated
  API call answered 401, `info` through `docker compose exec`, `docker compose stop`
  time, and persistence across a restart (same CA, administrator login works).
- `deploy/ci/container-check.sh pressure` at 96 MiB, 64 MiB and 48 MiB: 30 concurrent
  logins and 200 concurrent `GET /api/v1/missions`, with the container's `memory.peak`,
  restart count and OOM flag (the same procedure as the x86-64 memory-pressure run).
- `deploy/ci/bench-limited.sh`: the release `coordinator/tests/bench.rs` harness inside one
  container limited to 512 MB (swap equal) and one CPU, so the harness client and the
  coordinator share the limit.

Measurement harness, 512 MB and 1 CPU:

| Measurement | Result |
|---|---|
| Startup to `/healthz` | 32 ms |
| Idle RSS (2 s after healthy) | 5.0 MiB |
| RSS after administrator setup and one login | 24.1 MiB |
| RSS after 1000 events | 25.3 MiB |
| Peak RSS (`VmHWM`) up to and including the 1000-event load | 25.3 MiB |
| RSS after 10 000 events | 28.4 MiB |
| `GET /api/v1/missions` p50 / p95 (100 sequential requests) | 0.17 / 0.19 ms |
| `POST /api/v1/commands` p50 / p95 (100 sequential requests) | 0.93 / 1.24 ms |
| Event ingest throughput (9000 events, batches of 100, 200-byte payload) | pending rerun after the 128 KiB cap |
| Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20 reconnects) | 1.3 / 2.8 ms |
| Database growth for 10 000 events (main file + WAL) | 5.32 MiB (558 bytes/event) |
| Container peak memory (`memory.peak`, includes page cache) | 21.7 MiB |
| OOM killed | no |

Container under memory limits (30 concurrent logins, 200 concurrent `GET /api/v1/missions`):

| Limit | Logins (200 / 429) | GETs answered | `memory.peak` | Restarts | OOM killed |
|---|---|---|---|---|---|
| 96 MiB | 9 / 21 | 200 of 200 | 40.9 MiB | 0 | no |
| 64 MiB | 9 / 21 | 200 of 200 | 41.0 MiB | 0 | no |
| 48 MiB | 7 / 23 | 200 of 200 | 41.1 MiB | 0 | no |

The 429 answers come from the per-address login limiter, as in the x86-64 run.

Limits of this measurement: the runner's CPU cores are faster than the Cortex-A53 of
a Pi Zero 2 W, and `--cpus 1` limits CPU time but not cache, memory bandwidth or
storage. The data directory is on the runner's disk, not a microSD card. Time figures are
therefore not a prediction for the Pi. Memory figures use the same architecture and the
same static binary and are the meaningful result; they cover the process and cgroup,
not a full Raspberry Pi OS. The runner is shared, so timings vary between runs.

## Storage writes (microSD wear)

Platform: x86-64 host (ext4 on NVMe), release coordinator. Event, command and daily-budget
measurements are from the 2026-11-07 run; the one-hour idle figure is from 2026-11-05.
The figures
are not from a microSD card. They are bytes, not time, so they do not depend on
the medium, but the Pi's file system (ext4 on the card) adds its own journal
writes that are not counted here.

Test: `tests/test_distributed_storage_writes.py` (Linux only; it reads
`write_bytes` from `/proc/<pid>/io` of the coordinator process and is skipped
where that file does not exist). `write_bytes` counts bytes sent to a block
device, so the test puts the coordinator's data directory on a directory where a
probe write moves the counter. `/tmp` is a RAM disk on some hosts and reports
zero, so the test looks next to it (`BRIDGE_STORAGE_TEST_DIR`, the system
temporary directory, then `~/.cache`) and skips with that reason when none works.

| Workload | Measured | Bound asserted |
|---|---|---|
| 1000 replicated events (about 100-byte payload, committed at once, sent in the worker's production batches: 40 batches of 25) | 1.61 MiB written, 1688 bytes/event, 42 189 bytes/batch (identical in 3 runs) | 4 MiB |
| Transactions and log frames for those events | 40 commits, 369 frames (9.2 frames of 4 KiB per batch) | 40 commits, 12 frames per commit |
| Database file growth for those events (main file, after a checkpoint) | 377 bytes/event | 940 bytes/event |
| `db_bytes` from `GET /api/v1/diagnostics` for the same events (after the checkpoint) | 377 bytes/event | 940 bytes/event |
| 1000 `mission.list` commands round-tripped through a worker (16 in flight) | 66.6 MiB written, 68 KiB/command | 150 MiB |
| (Earlier run on main, 2026-11-07, test sending five batches of 200 events) 1000 replicated events (about 100-byte payload, committed at once, five batches of 200) | 0.60-0.64 MiB written, 631-668 bytes/event | 1.5 MiB |
| (Earlier run on main, 2026-11-07) 1000 `mission.list` command round-trips through a worker (16 in flight) | 86.6 MiB written, 88.7 KiB/round-trip | 150 MiB |
| Coordinator flood harness: six accepted 25-event batches (400-byte event payloads, with two browsers subscribed) | 258,048 bytes total, 43,008 bytes/batch | Reported for comparison |
| Flood harness: one command round-trip with a deliberately large 200 KiB result | 454,514 bytes written/round-trip | Reported for comparison |
| Idle for 50 s: one connected worker (pinged every 20 s) and an API client reading the worker list every 5 s | 0 bytes | 8 KiB |
| Idle for one hour, one connected worker, no clients | 104 KiB (16-20 KiB per 10 minutes) | not asserted (one-hour run) |
| Budget extrapolation: 3 running missions at 1 accepted event/s each, 3 connected workers, 2 browser sessions (one event/page measurement above; conservative 4 KiB session touch every 5 minutes) | 447,440,486 bytes/day (426.7 MiB/day), computed by `tests/test_distributed_storage_writes.py` from the 1688 bytes/event measured with the worker's production batches (183,081,370 bytes/day, 174.6 MiB/day, from the earlier 668 bytes/event measurement with batches of 200) | 1 GiB/day |

The 426.7 MiB/day figure is an extrapolation from this local per-event measurement, not a 24-hour Pi measurement. The browser does not issue a full `mission.status` command on its overview, missions list, or mission-detail refresh: compact task counts and current-work labels arrive with the replicated mission summary, while activity details use the coordinator's local read-only events endpoint. A worker that predates the replicated summary is still asked for one bounded `mission.status` per change of its `updated_at`. Two sessions therefore add no command-result writes during ordinary view refresh; the budget model still reserves one 4 KiB database page every five minutes per session. The Pi's own filesystem and its real event rate still need a fresh 24-hour measurement after deployment.

Reading the figures:

- Events are written one transaction per accepted batch, with no checkpoint of its own (the
  automatic checkpoint runs after 1000 log frames). The cost of a batch is a fixed part plus a part
  proportional to the events. Measured without any API polling (release build, 2026-11-07, 1000
  events, bytes written per batch): 25 events per batch 42.2 KB; 100 events 73.7 KB; 200 events
  109.8 KB. That is about 0.39 KB per event plus about 32 KB per batch. The fixed part is the pages
  every commit rewrites: the first page of the database file, the `workers` row page (cursors),
  the tail of the `events` index, the interior page above it, the extra pages the index rebalances
  when its last leaf fills, and the partly filled 4 KiB page at the end of the log that each
  commit's fsync writes again (frames are 4120 bytes). At 25 events per batch the fixed part is
  about three quarters of the bytes written. The proportional part is about what the events occupy.
- The figure of about 115 KB per batch reported after PR #53 was mostly the test, not the
  coordinator. The test polled `GET /api/v1/workers` every 50 ms while waiting for the events.
  Every API call with a bearer token is stored as an audit row, which is a commit of its own
  (the `audit` page and the `sqlite_sequence` page, about 12 KB written including the log's tail page).
  With production batches the wait is longer (rate cap), so the number of polls, and with it the
  bytes, grew. Measured with the previous coordinator and the same 250 events in batches of 25:
  114 KB per batch with polling (61 audit rows in 10 batches) and 44 KB per batch without. The test
  now reads the replicated cursor from the database file with a read-only connection, so the
  figure no longer depends on the duration of the wait.
- Changes to the coordinator (`Db::events_store`): the mission summaries that a batch carries are
  written only when they differ from the stored row (the previous `INSERT OR REPLACE` rewrote
  the row and its page for every batch); the two `UPDATE`s of the worker row are one statement;
  retention no longer deletes the oldest few events in every batch once a worker is at its cap.
  Without the change each batch at the cap ran a query that walks `max_events_per_worker` index
  entries and deleted one batch worth of rows at the left edge of the index, which rewrote about
  seven extra pages per batch. Now the coordinator keeps an estimate of the stored count per worker
  and, when it exceeds the cap, deletes down to 90 % of the cap in one pass and counts the rows
  again. The stored count still never exceeds `max_events_per_worker`; it is between 90 % and
  100 % of it once the cap has been reached.
- Before and after, same host, 1000 events in batches of 25 with the worker's production batching
  and no API polling. Below the cap: 46 285 bytes/batch (1851 bytes/event, 409 log frames) before,
  42 189 bytes/batch (1688 bytes/event, 369 frames) after. At the cap (cap 3000, 3200 events
  stored first): 76 698 bytes/batch (3068 bytes/event, 700 frames) before, 60 621 bytes/batch
  (2425 bytes/event, 533 frames) after. In batches of 200: 569 and 549 bytes/event.
- What was not changed. The per-batch fixed cost remains because every accepted batch is committed
  before the worker's next ping is answered, and a commit rewrites those pages. The `events` table is
  `WITHOUT ROWID`, so SQLite does not use its right-edge append shortcut for it and redistributes
  the last leaf pages every time one fills; a rowid table with a unique index on `(worker, id)` would
  save about two pages per batch in a standalone measurement (8.5 against 10.3 frames per batch), but
  it needs a schema migration and a changed events query and was not done here. Writing several batches in one
  transaction would remove most of the fixed cost, but the coordinator would then acknowledge
  events that are not yet durable, which the ping fence does not allow today.
- Before each size reading the test runs `PRAGMA wal_checkpoint(TRUNCATE)` on its own
  connection, so the WAL file is empty and the main file alone is measured: the number
  of pages the events occupy. Without this, an automatic checkpoint (after 1000 WAL
  frames) landing inside the window would count the same pages once in the main file and
  once in the WAL file, because the WAL file keeps its size after a checkpoint.
  `db_bytes` (`page_count * page_size` plus the WAL file size) has the same dependency
  and is read after the checkpoint as well.
- The 557-558 bytes/event for 10 000 events in the load tables above counts the main file
  plus WAL and was not re-measured with this method.
- A command costs much more than an event: 88.7 KiB per `mission.list` round-trip in the 2026-11-07 run, about 22 four-KiB pages. The
  coordinator commits five separate transactions per command (insert, audit row,
  `sent`, `acked`, `done`), each with `synchronous=FULL` and therefore an fsync
  of the WAL, and each rewrites the `commands` table page and its two indexes.
  This is a property of `coordinator/src/db.rs`; it was not changed.
- Idle writes. Until 2026-11-05 every 20-second worker ping committed an update of
  the worker's `last_seen`: 24-32 KiB in 50 seconds, about 2.2 MiB per connected
  worker per hour, with nothing else happening. The value is now stored at most every
  ten minutes while the worker is connected (`LAST_SEEN_EVERY_SECS` in
  `coordinator/src/ws_worker.rs`) and at once when it disconnects. Its age after a
  coordinator crash is therefore at most ten minutes. A session's `last_seen` and an
  API token's `last_used` are touched at most every five minutes (`TOUCH_EVERY_SECS`
  in `coordinator/src/db.rs`; previously every minute). The shortest idle timeout is
  two hours, so expiry moves by at most 4 %. Measured over one idle hour with one
  connected worker (2026-11-05, same host): 104 KiB written in total, 16-20 KiB per
  ten minutes, which is the one `last_seen` commit per ten minutes. Before the
  change the same hour cost about 2.2 MiB. The hourly maintenance pass deletes nothing on an idle system and
  writes nothing. Reverting the throttle (setting `LAST_SEEN_EVERY_SECS` to 0) makes
  the idle test fail with 24576 bytes.
- Is a change to commands justified? At the rate an operator issues commands, no. Ten commands
  per minute, around the clock, would write about 0.9 GB per day. A card rated for
  tens of terabytes written (the usual figure for a 32 GB card, which
  assumes ideal wear levelling) would take decades to wear out at that rate. Merging the
  audit row into the insert transaction and `sent` with `acked` would cut the
  writes by about 40 % if the command rate were ever to become much higher. That
  would not change durability, but it is not needed at the measured rates and is
  left alone. Do not lower `synchronous` to save writes: it is what keeps
  acknowledged commands and replicated events across a power loss.

## Raspberry Pi Zero 2 W (physical)

Hardware: Raspberry Pi Zero 2 W Rev 1.0, Debian 13.7 (DietPi v10.8.2), ARM64,
`6.18.60+rpt-rpi-v8`, four Cortex-A53 cores, 462 MiB visible RAM. Measurements were
collected on 2026-11-06 on the physical board.

Method: the release benchmark test in `coordinator/tests/bench.rs` was cross-compiled
on the desktop for `aarch64-unknown-linux-musl`, copied to `/tmp` on the Pi, and run
natively there. The test's data directory was under `/tmp` (tmpfs), so its benchmark
writes did not reach the microSD. This is a physical ARM64 CPU measurement of the
coordinator binary; the isolated harness process itself was outside Docker. A second
attempt to run the harness in a 96 MiB temporary container did not become healthy in
30 seconds and produced no measurements, so no container cgroup peak is claimed from
that attempt.

| Measurement | Result |
|---|---|
| Startup to `/healthz` | 339 ms |
| Idle RSS (2 s after healthy) | 3.3 MiB |
| RSS after administrator setup and one login | 3.5 MiB |
| RSS after 1000 events | 5.8 MiB |
| Peak RSS (`VmHWM`) up to and including the 1000-event load | 22.3 MiB |
| RSS after 10 000 events | 20.8 MiB |
| `GET /api/v1/missions` p50 / p95 (100 sequential requests) | 2.15 / 2.25 ms |
| `POST /api/v1/commands` p50 / p95 (100 sequential requests) | 3.11 / 3.67 ms |
| Event ingest throughput (9000 events, batches of 500, 200-byte payload; 2026-11-06, before the 128 KiB batch cap) | 5933 events/s |
| Reconnect (TLS + WebSocket + hello to welcome) p50 / p95 (20 reconnects) | 16.4 / 42.1 ms |
| Database growth for 10 000 events (main file + WAL) | 5.32 MiB (558 bytes/event) |

Physical host baseline before starting containers: 462 MiB RAM visible, 325 MiB
available, load averages 0.08 / 0.07 / 0.05, CPU 91.2% idle and 1.8% I/O wait in
one `top -bn1` sample; `dockerd` RSS 85 MiB and `containerd` RSS 44 MiB. The running
coordinator deployment later reported 19.02 MiB / 96 MiB in `docker stats` after
browser login and a diagnostics RSS of 20 572 KiB. These are different measurements
and are reported separately. The 96 MiB limit remains appropriate: the physical
harness peak was 23.3% of that limit and the deployed container sample was below it.

The production container used a one-CPU limit, `mem_limit=memswap_limit=96m`,
`journald` logging, a 60-second health interval and `restart: unless-stopped`. Its
persistent Docker volume remained on the card. The physical board's 24-hour card-write
measurement is still pending; do not infer it from the harness, which ran from tmpfs.

## Web interface: bundle size and initial load (Pixel 4 emulation, x86-64 host)

Platform: x86-64 desktop (Intel Core i7-6700K, 4.0 GHz, 8 threads), release coordinator serving `web/dist` over
HTTPS on loopback, Chromium driven by Playwright with the built-in `Pixel 4` profile (353x745 CSS px, device scale
factor 3, touch). This is **emulation**: the CPU is throttled with the DevTools protocol
(`Emulation.setCPUThrottlingRate`, 4x), which slows script and layout work but is not a Pixel 4 and not a measured
phone. The network is loopback without throttling and the cache is disabled, so these figures show the cost of
parsing, executing and rendering the application, not of a slow radio. Run on 2026-11-05 by
`web/e2e/budget.spec.ts`; five loads per mode, median reported. The first-run page (setup form) is the page
measured, because a fresh coordinator always serves it first.

| Measurement | No throttling | 4x CPU throttling |
|---|---|---|
| First contentful paint (median) | 56 ms | 160 ms |
| `DOMContentLoaded` / `load` (median) | 25 ms / 25 ms | 69 ms / 70 ms |
| Setup form heading visible after `goto` (median) | 72 ms | 222 ms |
| Requests / bytes transferred (HTML, JS, CSS, as served, no compression) | 5 / 97.9 kB | 5 / 97.9 kB |

Bundle sizes (bytes, raw / gzip level 9). The coordinator serves files uncompressed today, so the raw figures are
what crosses the network; the gzip column shows what a compressing reverse proxy would send.

| Part | Raw | gzip |
|---|---|---|
| Initial JavaScript (`app-*.js`) | 58 304 | 20 174 |
| Initial CSS (`app-*.css`, includes the xterm.js stylesheet) | 28 141 | 7 001 |
| Terminal chunks (xterm.js and terminal panel), loaded only on a mission page | 362 116 | 95 120 |

Before the terminal code was split into a lazily loaded chunk, the initial JavaScript was 378 531 bytes.

**Budget** (`web/budget.json`, enforced by `web/e2e/budget.spec.ts`, which fails the build when exceeded):
initial JavaScript at most 70 000 bytes raw and 25 000 gzip; initial CSS at most 34 000 raw and 9 000 gzip; both
together at most 100 000 raw and 32 000 gzip; lazy chunks at most 400 000 raw and 100 000 gzip. The test also fails
when the entry imports the terminal chunk statically. The load test fails when the median first contentful paint
under 4x CPU throttling exceeds 4 000 ms or the form takes more than 5 000 ms to appear. These time limits are
deliberately loose (about 25 times the measured values) so that a slower or busier CI runner does not fail the test;
they catch order-of-magnitude regressions, not small ones. The measured values are in the table above and are also
written to `web/test-results/load-metrics.json` and uploaded by the CI `web` job.
