# Coordinator capacity plan (PRD-009)

Status: measured on 2026-11-13 (see Results). Findings F1 to F10 have dispositions in the last
sections. The sections up to "Findings" are the original plan and describe the code before the
fixes; Results and Dispositions describe the code after them.

## Summary

The coordinator enforces caps on five of the resources PRD-009 names: worker
connections (64), terminal sessions (16 per worker, 64 in total), TCP connections
(256 in total, 32 per client address, 16 of them reserved for workers), outbound
messages per worker connection (256) and per-viewer terminal bytes (256 KiB). Commands
per worker are capped (1000 unfinished). Each of these caps fails with a defined
refusal, and the coordinator does not crash when one is reached.

Several resources are not capped, or are capped in units that do not bound memory:

- The `/stream` broadcast buffer holds 256 messages, and an events message can carry
  up to about 1 MiB, so the buffer can retain about 256 MiB.
- The per-connection outbound queue counts messages, not bytes.
- Each terminal session queues up to 64 input frames of up to about 87 KB each.
- Terminal viewers are not capped per session.
- Enrolled workers, total missions and events per second have no coordinator-wide cap.
- The unpaged `GET /api/v1/missions` returns every match.
- A browser refused by the total connection cap sees a closed socket with no HTTP
  error, which does not meet the "clear errors" requirement.

Section "Findings" lists each gap with a proposed limit. The proposed numbers are
provisional. Phase B sets the final values from measurements on both hardware targets.

## Scope and sources

Limits were read from `coordinator/src` (line numbers below are as of this branch),
`docs/distributed/coordinator.md` (limits table), `docs/distributed/protocol.md`
(bounds section), and the existing Rust benchmark `coordinator/tests/bench.rs`, which
measures single-client latency and ingest throughput and does not test any limit.
`scripts/bridge_store_benchmark.py` measures the local SQLite store on the worker, not
the coordinator, and is not used here.

Existing tests that exercise limits: `coordinator/tests/limits.rs`
(`connections_over_the_cap_are_closed_promptly`), `coordinator/tests/hardening.rs`
(`connections_per_address_are_capped`, `worker_reserve_admits_workers_when_general_slots_are_full`,
`one_pending_connection_per_worker_token`), and `coordinator/tests/terminal.rs`
(`session_caps`, which checks 16 per worker and 64 in total, and `lagging_viewer_is_closed_with_1013`).

## Hardware matrix

| Target | Identity in this plan | Role | Notes |
|---|---|---|---|
| x86-64 CI runner | GitHub-hosted `ubuntu-latest` | Reproducible reference; the phase B run is a `workflow_dispatch` job | Record vCPU count, RAM and kernel from `lscpu` and `/proc/meminfo` in the job log. Runners are shared, so repeat each run three times and report the median and the spread. |
| x86-64 developer host | Intel Core i7-6700K, 8 logical CPUs, 48 GiB RAM | Baseline only; earlier numbers are in `benchmarks.md` | Not a CI target. Report it separately from the runner. |
| Raspberry Pi Zero 2 W | host `llm-coordinator`; four Cortex-A53 cores, 462 MiB visible RAM, Debian 13 | Minimum deployment target | Run the coordinator as the shipped container with the 96 MiB limit (`container/compose.yaml`), and also natively for CPU figures. The earlier physical run used tmpfs for the data directory (`benchmarks.md`), so the phase B run must also use the microSD for the write-path measurement. |

## Measurement method

Load generator: the PRD-007 soak harness, `scripts/bridge_soak.py`, which is being
written in parallel and is not on this branch yet. The capacity runs need that harness
to provide these capabilities. Each item is a requirement on the harness, not a
property the plan assumes it has:

1. Fake workers that complete TLS, `hello`, `welcome`, event batches (`events`) and
   `command.ack` / `command.result`, with a per-worker event rate and a fixed event size.
2. Fake browsers: `/stream` WebSockets that answer pings, and `/terminal` viewers that
   record the sequence number of each `o` frame so per-viewer lag can be computed.
3. A fake terminal producer on the worker side that writes output at a fixed rate.
4. A fixed admin session and API token, created before the run.
5. Timestamps embedded in event payloads and command requests, so end-to-end latency is
   measured on the client side.

Metrics, collected for every step:

- Coordinator RSS (`VmRSS`, and `VmHWM` for the peak) from `/proc/<pid>/status`, and the
  container cgroup peak where the container is used.
- CPU use of the coordinator process, from `utime + stime` deltas per second.
- Command round trip p50, p95 and p99 (`POST /api/v1/commands` with `wait_ms` to the
  first `command.result`).
- Event visibility latency: time from the worker sending a batch to the cursor being
  visible through `GET /api/v1/events`, sampled every 250 ms.
- Browser fan-out latency: time from the publish to receipt by each `/stream` client,
  p99 across clients.
- Error rate: refused connections, HTTP 4xx and 5xx responses other than the designed
  refusals, `error` frames, nacks, and unexpected close codes.
- Queue depth: `command_queue_depths` from `/diagnostics` (already exposed), plus the
  new fields listed in finding F9 (not yet exposed).
- Database growth: main file and WAL size from `/diagnostics` `db_bytes`.

Runs last 10 minutes at each step, after a 60 second warm-up. A step passes only when
its metrics stay inside the thresholds for the whole window, not on average.

Provisional thresholds, to be confirmed in phase B: command round trip p99 at most
500 ms, browser fan-out p99 at most 500 ms, terminal relay p99 at most 200 ms, error
rate 0 apart from designed refusals, and RSS peak at most 80% of the 96 MiB container
limit on the Pi.

Phase B runs on the merged coordinator, after the findings in this document are
implemented or explicitly deferred. Measuring before the fixes would record the
unbounded behaviour as the limit.

## Dimensions

### 1. Workers

Current enforced limit:
- 64 concurrent worker WebSocket connections, counted from the upgrade and before
  `hello` (`coordinator/src/hub.rs:9`, `Hub::try_reserve` at `hub.rs:118-124`).
- One pending connection per worker token, waiting for `hello` (`hub.rs:107`).
- Worker connections may use the 16 reserved TCP slots (`coordinator/src/server.rs:141`,
  `config.rs:49`), so workers still connect when general slots are full.
- Enrolled workers: not capped (finding F5).

How to measure: N fake workers (N = 1, 8, 16, 32, 48, 64, 65). Each sends event batches
at 100 events/s and receives commands. Run POST /commands round trips to every worker
at the same time. The 65th connection is the designed refusal.

Stop criterion: the limit is the largest N for which command round trip p99 is within
the threshold, the error rate is 0 apart from the designed refusal at N+1, RSS and CPU
are stable over the window, and the outbound queue depth stays below 64 on every
connection.

Expected degradation: connections above 64 get HTTP 503 with the plain-text body
`too many worker connections` (`coordinator/src/ws_worker.rs:47-49`). Existing worker
connections are unaffected. The worker retries with backoff, and the command journal
redelivers after reconnect (`protocol.md`, command delivery).

Test that proves it at the limit: `coordinator/tests/worker.rs` has no test for the
64-connection cap. Add `worker_connection_cap_admits_64_and_refuses_65th`: open 64
worker sockets, check that each receives `welcome` after `hello`, check that the 65th
upgrade returns 503 with that body, close one socket, and check that the 65th is then
accepted. Reverting the cap (for example `MAX_WORKER_CONNECTIONS` to 1000) must fail the
test.

### 2. Missions

Current enforced limit:
- 5000 missions per `hello` (`coordinator/src/protocol.rs:8`, checked at `protocol.rs:301-302`).
  A `hello` above the limit gets an `error` frame with code `invalid-message` and the
  message `hello: too many missions`. The connection stays open until the hello deadline
  (`ws_worker.rs`, close 1008 `hello timeout`).
- `GET /api/v1/missions`: `limit` is 1 to 500 when given. Without `limit` every match is
  returned (`coordinator/src/http.rs:595-605`, `http.rs:660`).
- Total missions across workers: not capped (finding F6).

How to measure: W workers, each with M missions (M = 100, 1000, 5000), so total missions
go from 6400 to 320 000 at W = 64. At each step measure the `hello` duration, the paged
list (`limit=100`) p95, the unpaged list time and response size, RSS during the unpaged
request, and the database size.

Stop criterion: the limit is the largest total mission count for which the paged list p95
is at most 50 ms and the unpaged list response, when requested, stays below the memory
budget of the target (RSS peak below 80% of the limit on the Pi, from the phase B run).
The unpaged list is not part of the SLO once F6 is fixed by a default page size.

Expected degradation: above 5000 per `hello`, the worker gets `invalid-message` and must
send a smaller list. Above the total limit (once F6 adds one), `hello` is refused with a
new `too-many-missions` error code. Today the unpaged list has no degradation; it grows
with the data.

Test that proves it at the limit: no test exists for the 5000 cap or for the unpaged
list size (searched `coordinator/src` and `coordinator/tests` for `too many missions`
and `MAX_MISSIONS_PER_HELLO`; no match in tests). Add a protocol test with 5001 missions
and a test with 5000 that passes. Reverting `MAX_MISSIONS_PER_HELLO` must fail the 5001 case.

### 3. Events per second

Current enforced limit:
- 500 events per `events` batch (`protocol.rs:7`, checked at `protocol.rs:140-141`,
  refusal `invalid-message`).
- 1 MiB per WebSocket message (`protocol.rs:6`; `ws_worker.rs:94` closes with 1009).
- 50 000 stored events per worker, oldest deleted first (`config.rs:44`, deletion in
  `db.rs:538`).
- `GET /api/v1/events` `limit` is 1 to 1000 (`http.rs:677`).
- Rate: not limited (finding F7). Throughput has only been measured: about 40 000
  events/s on the x86-64 developer host and 5933 events/s on the Pi, both for 200-byte
  events in batches of 500 (`benchmarks.md`).

How to measure: W workers (W = 1, 8, 64), each sending batches of 500 events at a target
rate R (R = 500, 2000, 5000, 10 000 events/s per coordinator). Measure achieved events/s
(growth of the stored cursor over the window), event visibility latency, nacks, SQLite
write time per batch, WAL size, RSS and CPU. Also measure browser fan-out with 8 `/stream`
clients, because each accepted batch is published to all of them.

Stop criterion: the limit is the largest R at which achieved rate is within 1% of
offered, visibility p99 is within the threshold, nack count stays 0 after warm-up, and
WAL size returns to a bounded value after each checkpoint.

Expected degradation: none today. Above the limit the coordinator accepts every batch
and latency rises, while the worker's own store holds the backlog. After F7 is fixed,
the coordinator refuses over-rate batches with a retryable error (proposed in F7).

Test that proves it at the limit: `coordinator/tests/bench.rs` gives a throughput figure
for one worker, not a limit. Add a protocol test that a 501-event batch gets
`invalid-message` (no test found for `MAX_EVENTS_PER_BATCH`). Add the rate test with
the F7 fix.

### 4. Browser sessions

Current enforced limit:
- 256 TCP connections in total, all kinds (`config.rs:47`). The general tier has 240
  slots (`server.rs:140`, which is `max_connections - reserve`). The reserve tier has
  16 slots, usable only for worker requests with a valid token (`server.rs:141`,
  `reserve_gate` at `coordinator/src/http.rs:952`).
- 32 connections per client address, IPv6 by /64 (`config.rs:48`, `server.rs:153`).
  Behind a proxy or a published container port every browser shares one address, so
  the effective total is 32 unless the operator raises the setting.
- `/stream`: no separate session cap (finding F8). It shares the TCP caps above.
- Slow `/stream` client: the broadcast receiver lags after 256 buffered messages
  (`hub.rs:11`), and the socket is closed with 1013 `client too slow`
  (`coordinator/src/http.rs:829`).
- Idle: a `/stream` client is closed with 1001 after 90 s without a pong or message
  (`config.rs:53`, `ws_idle_timeout_secs`).

Because keep-alive HTTP connections also count against the slots, the number of browser
sessions that can be open at once is below 240 when tabs keep HTTP connections open
(`http_idle_timeout_secs` is 30 s by default).

How to measure: N browser sessions (N = 1, 16, 64, 128, 240), each with a session cookie,
one `/stream` WebSocket, answering pings. Publish one event per second from 8 workers
(each batch is fanned out to every session). Measure RSS and CPU per session, fan-out
latency p99, and the number of unexpected closes (1001 or 1013). Add a second step where
half the sessions stop reading, to measure the lag path.

Stop criterion: the limit is the largest N for which fan-out p99 is within the threshold,
no client is closed unexpectedly, and RSS growth per session stays within the budget
fixed in phase B. The lag path is a pass when the stopped clients get 1013 and the
active clients are not affected.

Expected degradation and error:
- Over the general tier (240 open connections), the TCP connection is dropped before TLS
  (`server.rs:153-166`, `continue`). The browser sees a reset or an unexpected end of
  stream and no HTTP status. This is not a clear error (finding F8).
- Over the reserve tier, a non-worker request gets HTTP 503 with error code `busy` and
  the message `the coordinator is at its connection limit; only workers are accepted`
  (`http.rs:955-968`).
- Over 32 per address: the socket is dropped, as above.
- A slow `/stream` client: close 1013 with reason `client too slow`.

Test that proves it at the limit: `limits.rs` (`connections_over_the_cap_are_closed_promptly`)
checks that the refused socket closes quickly. It does not check the refusal reason, and
the test uses `max_connections = 2`. Add `general_tier_admits_240_and_refuses_the_241st`
and `stream_client_that_stops_reading_gets_1013` (the latter has no existing test; the
existing 1013 test covers only terminal viewers). Reverting `worker_reserve_connections`
to 0 must fail the reserve-tier test.

### 5. Terminal viewers

Current enforced limit:
- 16 terminal sessions per worker and 64 in total (`coordinator/src/hub.rs:12-13`,
  `terminal_register` at `hub.rs:196-206`). A refused browser gets an `x` frame with
  the reason `too many terminal sessions for this worker` or `... on this coordinator`,
  then close 1013 (`coordinator/src/terminal.rs:128-142`).
- 256 KiB queued per viewer (`terminal.rs:30`, checked in `push` at `terminal.rs:298-302`).
  A viewer that exceeds it is closed with 1013 `viewer too slow` (`terminal.rs:353-354`);
  the close waits up to `LAG_GRACE` (30 s, `terminal.rs:38`) before the socket is dropped.
- Browser input: at most 64 KiB decoded per frame (`terminal.rs:32`, checked at
  `terminal.rs:209-213`), and at most 64 frames queued per session towards the worker
  (`terminal.rs:36`). A frame that does not fit is dropped silently (`terminal.rs:316`).
- Viewers per session: not capped (finding F4). The only bound is the TCP caps in
  dimension 4.
- Worker side: one writer per mission, and the viewer count there is not limited
  (`protocol.md`, terminal sessions).

How to measure: S sessions (1, 16, 64) with V viewers each (V = 1, 8, 32, 128, 240). The
fake producer writes output at 16, 64 and 256 KiB/s, and each frame carries a sequence
number. Measure per-viewer lag, the number of 1013 closes by reason, relay latency p99,
RSS and CPU. Add an input step: viewers in write mode send 64 KiB frames at a fixed rate
and the worker-side queue is checked.

Stop criterion: the limit is the largest V for which no viewer is closed for lag at the
producer rate, relay p99 is within the threshold, and RSS stays within the budget. The
viewer memory ceiling is V x 256 KiB per session, which is 60 MiB at V = 240, so this
ceiling must fit in the 96 MiB container with the base RSS.

Expected degradation: a viewer that falls behind gets close 1013 `viewer too slow`; the
other viewers continue. A session refused by the session caps gets the `x` frame and close
1013 described above. A viewer refused by the viewer cap (proposed in F4) gets an `x`
frame `too many viewers for this terminal` and close 1013.

Test that proves it at the limit: `terminal.rs` `session_caps` (16 per worker, 64 total)
and `lagging_viewer_is_closed_with_1013` exist. The viewer cap needs a new test when F4
is fixed: 32 viewers accepted, the 33rd refused, and the refused viewer's frame is `x`
with close 1013. Reverting the cap must fail the test.

## Findings: limits not enforced today

Each finding gives the current behaviour, the risk, and a proposed limit. The proposed
values are starting points for phase B.

F1. `/stream` broadcast retains large messages. `hub.rs:11` sets `STREAM_BUFFER` to 256
messages. The events branch in `ws_worker.rs` publishes the full accepted batch as JSON
(`ws_worker.rs`, `"events"` arm, `hub.publish`). A batch can be close to 1 MiB, so the
ring can hold about 256 MiB when a client stalls. Proposed: publish the cursor and the
event count only, and have the browser fetch the events it needs from
`GET /api/v1/events`. Before this change, check whether `web/` reads the event bodies
from the broadcast. If it does, use a byte budget for the broadcast instead (proposed
8 MiB) and drop the oldest messages beyond it.

F2. Outbound queue per worker counts messages, not bytes. `OUTBOUND_QUEUE` is 256
(`hub.rs:10`), and a command request can be up to 1 MiB (`http.rs:24`, `MAX_BODY`). A
stalled worker connection could hold about 256 MiB. Overflow already closes the
connection (`hub.rs:175-190`), and the journal redelivers, so this is a memory bound
problem, not a crash. Proposed: a byte budget of 8 MiB per connection, with overflow
handled as the existing message overflow is.

F3. Terminal input queue is large in bytes. Each session queues up to 64 frames towards
the worker (`terminal.rs:36`, `terminal.rs:257`). Each frame can be about 87 KB of base64
(`terminal.rs:209-213`), so one session can hold about 5.5 MiB and 64 sessions about
350 MiB. Proposed: a byte budget of 256 KiB per session. Frames beyond the budget are
dropped as they are now, and a count of dropped frames is added to the diagnostics.

F4. Terminal viewers are not capped per session. A session with 240 viewers can hold 60
MiB of viewer queues. Proposed: 32 viewers per session; the 33rd gets `x` with reason
`too many viewers for this terminal` and close 1013.

F5. Enrolled workers are not capped. Enrolment (`POST /api/v1/workers`) and the
`workers` table have no limit. The risk is low because enrolment needs an administrator
session or an API token, but the table and the per-worker mission and event data grow
with it. Proposed: 256 enrolled workers; enrolment beyond that returns 409
`too-many-workers`.

F6. Total missions are not capped, and the unpaged mission list returns every row.
`GET /api/v1/missions` without `limit` returns all matches (`http.rs:605`, `http.rs:660`).
The web interface may request without `limit`; check it before changing. Proposed: a
default page size of 500 when `limit` is absent, and a coordinator-wide mission cap of
50 000 that refuses `hello` with `too-many-missions` beyond it.

F7. Events per second are not limited per worker. A worker can send batches back to back
and the coordinator accepts each one. Proposed: a sustained limit of 2000 events/s per
worker with a burst of 5000 events. Over the limit, the coordinator sends an `error`
frame with code `rate-limited` and `details.retry_after_ms`, and the worker resends the
same batch after the delay. This adds one error code to `protocol.md`. The number is set
from the phase B measurement, not from the Pi figure alone.

F8. A browser refused by the TCP caps sees no HTTP error. Over the general tier the
socket is dropped before TLS (`server.rs:153-166`), so the browser cannot show a reason.
The requirement for clear errors is not met for browsers. Proposed: over the general
tier, accept the socket, complete TLS with a 1 s timeout, answer 503 `busy` with a
`Retry-After` header, and close. Limit the number of refusals in progress to 8, so the
refusal path cannot become a new resource problem. Also add an explicit `/stream`
session cap (proposed 128) with the same error.

F9. Diagnostics do not expose what the measurements need. `/diagnostics`
(`http.rs:784-793`) reports the command queue depths and the RSS, but not the outbound
queue size per connection, the number of `/stream` clients, the number of terminal
sessions, the viewer count, or the dropped input frame count. Add these fields before
phase B. They are cheap counters and do not change behaviour.

F10. Test gaps. No test covers: the 64-connection worker cap, the 5000-mission hello
cap, the 500-event batch cap, the `/stream` 1013 lag path (`http.rs:829`), or the
unpaged mission list size. Each is listed under its dimension above with the test to add.

## Results (phase B)

Load generator: `scripts/coordinator_capacity.py` (fake workers, browsers and terminal producers over
TLS; one Python process at nice 19). Raw summaries, one JSON line per run:
`docs/distributed/capacity-measurements/x86-64.jsonl`, `emulated-pi.jsonl`, `find-009-emulated-pi.jsonl`.

Deviations from the method above, all because of time and host constraints:

- Windows are 45 s after a 10 s warm-up instead of 10 min after 60 s. Rows marked "3 runs" were run three
  times and show the median; other rows are one run.
- There is no GitHub-hosted runner run. The x86-64 rows are from the developer host only.
- The Pi rows are from the emulated Pi (QEMU raspi3b TCG, 4 emulated Cortex-A53, 478 MB, DietPi, the
  aarch64-musl build, in a 96 MiB / MemoryHigh 80 MiB cgroup without swap, same limits as the shipped unit).
  TCG is far slower than the board. **Memory figures carry over; latency, CPU and throughput figures do
  not**, and on the emulated Pi the load generator and the coordinator compete for the same emulated
  CPUs, so steps with many TLS handshakes stalled (see notes). Physical Pi timing is not measured here.
- The fake clients are Python threads. Fan-out and relay latency above about 100 ms at 128 browser clients
  is the client's own scheduling; the coordinator-side figure is lower. The `closed` count in the events
  rows is a load generator artefact (a TLS socket shared by a reader and a writer thread reports EOF); the
  coordinator logged no error and no worker saw an `error` frame or a nack.
- The per-address cap (32) was raised to 1000 for all runs, because every fake client uses 127.0.0.1.

x86-64 developer host (Intel Core i7-6700K, glibc release build of this branch, 2026-11-13):

| Dimension | Step | RSS peak (MiB) | CPU (%) | p99 (ms) | Errors | Result and notes |
|---|---|---|---|---|---|---|
| Workers | 1 | 11.6 | 2.8 | 54 (command round trip) | 0 | pass |
| Workers | 16 | 16.0 | 13.2 | 62 | 0 | pass |
| Workers | 64 (3 runs) | 27.1 (26.7 to 29.6) | 31.7 | 60 (59 to 68) | 0 | pass; round trip floor is about 53 ms (the wait for a command result polls) |
| Workers | 65 | n/a | n/a | n/a | 1 designed | 503 `too many worker connections`; the 64 stay connected |
| Missions | 1 000 total | 15.7 | n/a | 14 (paged list p95) | 0 | pass |
| Missions | 5 000 total | 35.6 | n/a | 19 | 0 | pass |
| Missions | 10 000 total | 49.1 to 52.1 | n/a | 26 to 41 | 0 | pass; this is the cap |
| Missions | 20 000 and 50 000 offered | 65 | n/a | n/a | refused | 10 000 accepted, the rest got `too-many-missions` and close 1008 |
| Events/s | 500 | 20.7 | 4.3 | 75 (visibility) | 0 | achieved 501/s |
| Events/s | 2 000 | 25.1 | 9.4 | 85 | 0 | achieved 2 001/s |
| Events/s | 5 000 (3 runs) | 28.0 (26.0 to 28.0) | 14.3 | 41 (33 to 101) | 0 | achieved 5 002/s, database +95 MiB per 45 s before retention |
| Events/s | 10 000 | 37.6 | 18.8 | 91 | 0 | achieved 10 005/s; no limit reached |
| Events/s | 5 000 with 8 browsers | 31.4 | 16.9 | 37 | 0 | pass |
| Browser sessions | 1 | 12.7 | 3.9 | 3 (fan-out) | 0 | pass |
| Browser sessions | 64 | 27.5 | 19.6 | 125 | 0 | pass |
| Browser sessions | 128 | 39.5 | 26.8 | 478 (client-limited) | 0 | pass; this is the cap, about 0.2 MiB per socket |
| Browser sessions | 235 and 260 offered (3 runs) | 47.5 (43.4 to 50.8) | 27.3 | n/a | 107 to 132 designed | 128 admitted, the rest 503 `busy` with Retry-After |
| Browser sessions | 40, 20 not reading | 21.8 | 33.6 | 74 | 0 | all 20 stalled sockets closed with 1013 `client too slow`; none of the 20 active clients closed |
| Terminal sessions | 1 | 9.6 | 0.4 | 2.5 (relay) | 0 | 16 KiB/s |
| Terminal sessions | 16 | 14.1 | 5.4 | 4.2 | 0 | 16 KiB/s each |
| Terminal sessions | 64 (3 runs) | 28.9 (28.7 to 29.1) | 12.5 | 5.6 (5.2 to 6.9) | 0 | 16 KiB/s each |
| Terminal sessions | 65 | 28.7 | 12.3 | 5.3 | 1 designed | `x` frame `too many terminal sessions on this coordinator` |
| Terminal sessions | 64 at 64 KiB/s | 29.3 | 10.2 | 37 | 0 | pass |
| Terminal sessions | 16 at 256 KiB/s, 4 not reading | 16.7 | 5.3 | 45 | 0 | the 4 stalled viewers were disconnected by the coordinator; the other 12 were not affected |

Emulated Pi (memory figures only; 10 s sampling interval through SSH, 2026-11-13):

| Dimension | Step | RSS peak (MiB) | cgroup peak (MiB) | Notes |
|---|---|---|---|---|
| Workers | 16 | 26.9 | 36.7 | connected; command round trips timed out (emulated CPU saturated) |
| Workers | 64 | 21.2 | 28.8 | only 19 workers completed the handshake before the load generator's timeouts; not a coordinator limit |
| Missions | 5 000 total | 44.5 | 45.0 | hello p50 4.7 s, paged list p95 2.5 s (emulated) |
| Missions | 10 000 total | 60.8 | 66.4 | below the 76.8 MiB threshold; this is why the cap is 10 000 |
| Events/s | 500 offered, 8 workers | 16.7 | 22.1 | achieved 500/s |
| Events/s | 20 000 offered (prefill) | 32.1 to 58.7 | 79.7 to 80.4 | achieved 374/s (old binary) and 532/s (this branch); the cgroup reaches MemoryHigh because of page cache |
| Browser sessions | 128 | 34.0 | 36.6 | 71 opened before the emulated CPU stalled the handshakes; not a coordinator limit |
| Terminal sessions | 16 | 12.0 | 20.9 | relay latency not meaningful |
| Terminal sessions | 64 | 15.9 | 20.9 | 21 of 64 viewers timed out waiting for the worker; emulated CPU |

### Measured limits

| Resource | x86-64 | Emulated Pi (memory) | Limit now enforced |
|---|---|---|---|
| Worker connections | 64 within the thresholds, RSS 27 MiB | RSS 21 to 27 MiB | 64 (503) |
| Missions indexed | 10 000 at 49 to 52 MiB | 10 000 at 61 MiB | 10 000 across workers (`too-many-missions`); 5 000 per hello |
| Events per second | 10 000/s, RSS 38 MiB, not saturated | 500/s with 17 MiB; 374 to 532/s ingest while prefilling | not limited (F7 deferred) |
| Browser sessions | 128 (cap), RSS 40 MiB | RSS 34 MiB for 71 clients | 128 (503 `busy`) |
| Terminal sessions | 64 (cap), RSS 29 MiB | RSS 16 MiB | 16 per worker, 64 total |

Memory findings made during the measurement, all fixed:

- The mission list parsed every mission summary into a JSON tree on each request, about 10 KiB per mission.
  Only the fields that the filters and the sort read are parsed for every row now; the full summary is parsed
  for the returned page.
- The parsed `hello` (every mission summary plus the capability list) stayed allocated for as long as the
  worker's connection lived. It is dropped after the database write. For 20 000 missions over 10 connections
  the RSS after the hellos fell from 109 MiB to 37 MiB (musl build, same allocator as the Pi). Together with
  the list change, 10 000 missions went from 84 MiB to 42 MiB peak on the x86-64 musl build.
- An upgraded WebSocket no longer holds a TCP connection slot, so the 256-slot cap does not bound `/stream`
  clients (260 were accepted in the first run, before the cap). The explicit cap of 128 is therefore the
  only bound.

### Thresholds

The provisional thresholds are adopted unchanged:

| Threshold | Value | Evidence |
|---|---|---|
| Command round trip p99 | at most 500 ms | 54 to 68 ms on x86-64 up to 64 workers. Not verifiable on the emulated Pi; re-check on the board. |
| Browser fan-out p99 | at most 500 ms | Met up to 128 clients on x86-64 (478 ms, mostly client scheduling). |
| Terminal relay p99 | at most 200 ms | 2.5 to 45 ms on x86-64. Not verifiable on the emulated Pi. |
| Error rate | 0 apart from designed refusals | 0 in every run. |
| RSS peak on the Pi | at most 80 % of 96 MiB (76.8 MiB) | Highest emulated-Pi process RSS 66.4 MiB (event ingest with backup, pre-DOG-024 binary), 63.4 MiB (this branch); 60.8 MiB at 10 000 missions. |

## FIND-009: coordinator memory on the Pi

The earlier physical-Pi logs recorded a kernel out-of-memory kill of the coordinator at about 93 MiB against
the 96 MiB limit (`PI-SYS-005`, `PI-VERIFY-005`, `PI-VERIFY-011`). The cause was never identified.

Method. Emulated Pi, a throw-away coordinator in a cgroup with `MemoryMax=96M`, `MemoryHigh=80M` and no
swap. Phase 1: 8 fake workers offered 20 000 events/s for 240 s (the database reached 36 to 52 MiB, not the
220 MiB the 50 000-event cap allows, because the emulated board ingests only 374 to 532 events/s). Phase 2:
500 events/s from 8 workers and 8 `/stream` clients for 420 s, with an online `backup` started 90 s into it.
The pre-DOG-024 binary is the one installed on the emulated Pi (built 2026-11-06); the other is this branch.
The pre-DOG-024 binary's phase 2 could not start (see below), so for that binary the backup ran on an idle
coordinator and its phase 2 RSS is the figure after the whole run.

| | Pre-DOG-024 binary | This branch |
|---|---|---|
| Process peak RSS (VmHWM), prefill | 58.7 MiB | 32.1 MiB |
| Process peak RSS, whole run | 60.9 to 66.4 MiB (two runs) | 63.4 MiB (event load and backup) |
| cgroup peak (includes page cache) | 84.2 to 84.3 MiB | 84.6 MiB |
| `anon` / `file` at the end | 57.5 to 62.8 / 18.3 to 19.1 MiB | 59.8 / 20.9 MiB |
| `oom_kill` | 0 | 0 |
| `memory.high` events (throttling at 80 MiB) | 568 to 1702 | 33 to 894 |
| Online backup of the 35 to 57 MiB database | 52 to 123 s, 37 to 51 MiB written | 11 s, 0.2 MiB written (7.2 MiB in use) |

Result.

- The kernel did not kill either binary at 96 MiB under this load. Anonymous memory peaked at about 60 MiB, so
  the headroom to the limit is about 36 MiB. The cgroup total sits at the 80 MiB `memory.high` line because
  of page cache, which the kernel reclaims; this throttles the process but does not kill it.
- The DOG-024 change did not change the steady process RSS (60 to 66 MiB under load). It changed the backup:
  the new backup copies only pages in use, took 11 s instead of 52 to 123 s and did not move RSS.
- The 93 MiB kill was not reproduced, so this measurement does not prove its cause. The earlier physical run
  was 24 hours long with the pre-DOG-024 binary, whose backup ran concurrently with the coordinator and wrote
  GiBs (D29). The likeliest contributor is the page cache and dirty-page accounting of that backup in the
  same cgroup, which DOG-024 removed, but that is an inference from D29 and not a measurement. The emulated
  card is a file on a fast host disk, so slow-card writeback pressure is not reproduced here.
- After the prefill, the pre-DOG-024 binary did not complete a TLS handshake within 30 s in two of two runs,
  so its phase 2 could not enrol workers. The new binary did not show this. It was not investigated further.
- Recommendation: keep the 96 MiB limit. Anonymous memory is about 60 MiB with 8 workers, 8 browsers and
  a 500 events/s stream, and 61 MiB at the 10 000 mission cap. A physical-Pi run of at least 24 hours with
  the new binary and `memory.events` sampled is still needed to close the question; it is the only evidence
  that can show the kill gone on the board itself.

## Dispositions

| Finding | Disposition | Evidence |
|---|---|---|
| F1 | Fixed before this change: the stream ring holds 32 notices of at most 4 KiB, with no event or result bodies | `worker.rs` `hub_fanout_keeps_only_small_change_notifications_for_multiple_browsers`, `browser_stream_gets_change_notices_not_event_bodies_or_command_results` |
| F2 | Fixed by existing byte caps: 64 KiB per pending request, 512 KiB per worker, 8 MiB in total bound the outbound queue; measured depth was 0 in every step | `worker.rs` `command_journal_refuses_excess_pending_bytes`; `resources.outbound_queue_depth_*` in diagnostics |
| F3 | Fixed before this change: 128 KiB per session; this change adds the dropped-frame counter | `terminal.rs` `terminal_input_queue_refuses_bytes_above_its_per_session_budget` |
| F4 | Not applicable. A terminal session has exactly one browser viewer (the browser socket is the session), so the 64-session cap bounds viewers at 64 and their queues at 16 MiB. Dimension 5 is measured as sessions | measured above |
| F5 | Fixed: 256 enrolled workers, 409 `too-many-workers` | `capacity.rs` `enrolment_is_capped_at_256_workers_with_a_clear_error` |
| F6 | Fixed: the unpaged list already returned one page (default 100); this change adds the 10 000 total mission cap (`too-many-missions`, close 1008, old list kept), the list memory fix and the hello memory fix. The plan's 50 000 was not used because of the measured memory | `capacity.rs` `total_missions_are_capped_and_an_unpaged_list_is_one_page` |
| F7 | Deferred. No rate was found at which the coordinator fails: 10 000 events/s on x86-64 at 38 MiB, with no nacks; storage per worker is bounded by `max_events_per_worker`. A limit needs a protocol error code and worker retry behaviour. Revisit with a physical-Pi measurement, where ingest is CPU-bound at about 6 000 events/s | measured above |
| F8 | Partly fixed. `/stream` is capped at 128 with HTTP 503 `busy` and Retry-After. Deferred: the silent drop above the 256 TCP slots, which needs a bounded 503 path in the accept loop. Upgraded WebSockets release their TCP slot, so the remaining case is plain HTTP over 256 connections | `capacity.rs` `stream_sockets_are_capped_at_128_with_a_clear_error` |
| F9 | Fixed: `/diagnostics` has `enrolled_workers`, `missions_indexed` and `resources` (`worker_connections`, `outbound_queue_depth_max`, `outbound_queue_depth_total`, `stream_clients`, `terminal_sessions`, `terminal_input_frames_dropped`) | `capacity.rs` `diagnostics_expose_connection_and_queue_counters` |
| F10 | Fixed: tests for the 64-worker cap, the 5 000 mission hello, the 500 event batch, the `/stream` 1013 lag path, the unpaged list size, the 10 000 mission cap, the enrolment cap and the stream cap | `coordinator/tests/capacity.rs` |

The `hello` error for more than 5 000 missions now says `too many missions`; before it said
"capabilities and missions must be lists". With 300-byte summaries the 1 MiB message limit is reached at
about 3 000 missions, before the 5 000 limit.

## Open items

- Physical Raspberry Pi Zero 2 W run of `scripts/coordinator_capacity.py` for the time figures, and the
  24-hour run with `memory.events` sampling for FIND-009.
- GitHub-hosted runner repeat of the x86-64 rows (three runs of 10 minutes).
- F7 and the rest of F8.
