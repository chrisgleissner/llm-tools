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
| Event ingest throughput (9000 events, batches of 500, 200-byte payload) | 1506 events/s |
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

## GitHub-hosted ARM64 runner (not a Pi)

Figures to be filled from the `arm64-container` job of the `test` workflow (artifact
`bench-arm64`, file `bench-arm64.md`, and the job summary). Not yet recorded.

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

| Measurement | Result |
|---|---|
| (to be filled from the CI run) | - |

Limits of this measurement: the runner's CPU cores are faster than the Cortex-A53 of
a Pi Zero 2 W, and `--cpus 1` limits CPU time but not cache, memory bandwidth or
storage. The data directory is on the runner's disk, not a microSD card. Time figures are
therefore not a prediction for the Pi. Memory figures use the same architecture and the
same static binary and are the meaningful result; they cover the process and cgroup,
not a full Raspberry Pi OS. The runner is shared, so timings vary between runs.

## Storage writes (microSD wear)

Platform: x86-64 host (ext4 on NVMe), release coordinator, one run per figure on
2026-11-05, repeated three times with the same results to within 1 %. The figures
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
| 1000 replicated events (about 100-byte payload, batches of up to 200) | 0.52 MiB written, 545 bytes/event | 1.5 MiB |
| Database file growth for those events (main file + WAL) | 527 bytes/event | 1024 bytes/event |
| `db_bytes` from `GET /api/v1/diagnostics` for the same events | 896 bytes/event | 2048 bytes/event |
| 1000 `mission.list` commands round-tripped through a worker (16 in flight) | 66.6 MiB written, 68 KiB/command | 150 MiB |

Reading the figures:

- Events are written one transaction per batch, so the bytes written are about
  the bytes stored (545 written against 527 stored per event). The bound is about
  2.9 times the measurement. A change that adds a write per event fails it.
- The 557-558 bytes/event above is for 10 000 events. The 527 bytes/event here is for
  1000 events: the write-ahead log (WAL) file is counted at its current size, and
  SQLite checkpoints it into the main file after 1000 pages (about 4 MiB) and then
  reuses it, so the file size stops growing by the WAL's share beyond that.
  `db_bytes` is larger because it adds the WAL file size to `page_count * page_size`,
  which already includes pages that are still only in the WAL.
- A command costs much more than an event: about 17 database pages (68 KiB). The
  coordinator commits five separate transactions per command (insert, audit row,
  `sent`, `acked`, `done`), each with `synchronous=FULL` and therefore an fsync
  of the WAL, and each rewrites the `commands` table page and its two indexes.
  This is a property of `coordinator/src/db.rs`; it was not changed.
- Is a change justified? At the rate an operator issues commands, no. Ten commands
  per minute, around the clock, would write about 0.9 GB per day. A card rated for
  tens of terabytes written (the usual figure for a 32 GB card, which
  assumes ideal wear levelling) would take decades to wear out at that rate. Merging the
  audit row into the insert transaction and `sent` with `acked` would cut the
  writes by about 40 % if the command rate were ever to become much higher. That
  would not change durability, but it is not needed at the measured rates and is
  left alone. Do not lower `synchronous` to save writes: it is what keeps
  acknowledged commands and replicated events across a power loss.

## Raspberry Pi Zero 2 W

Not measured on the device (hardware arrives 2026-11-06). The emulated ARM64
figures above show the memory behaviour on the same CPU architecture; their time
figures must not be read as Pi Zero 2 W performance. The sizing that `install.md` gives for it (Raspberry Pi Zero 2 W
target profile) is derived from the x86-64 figures and is labelled as such there.
