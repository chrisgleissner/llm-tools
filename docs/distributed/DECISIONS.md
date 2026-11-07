# Distributed Bridge decisions

## PK-003 WebAuthn core

Use `webauthn-rs` 0.6.2 (Kanidm, MPL-2.0) for server-side WebAuthn verification. It pulls `rsa 0.10.0-rc.19` through `crypto-glue 0.2.2`, which cargo audit and cargo deny flag for RUSTSEC-2023-0071 (Marvin timing attack); the advisory has no fixed RSA version. The 0.6.2 stack has no feature to remove RSA while preserving its verifier. Its 0.5.5 maintenance line removes `rsa`, but replaces it with system OpenSSL crypto and a dynamic OpenSSL runtime dependency, breaking the coordinator's static `FROM scratch` image and cross-target build. Keep 0.6.2 and narrowly ignore the advisory because the coordinator uses only RSA public-key signature verification for authenticator assertions; it creates, stores, signs with and decrypts with no RSA private key. The exact ignore is applied in both audit tools and must be revisited if that call path or dependency changes.

The library exposes typed registration/authentication state and credential types, verifies origins/RP IDs and authenticator assertions, and documents that ceremony state must be persisted. The coordinator serializes that state only in its private SQLite database, with a single-use session/setup-bound challenge row capped at five minutes. The `danger-allow-state-serialisation` feature is required because the library deliberately gates persistence of in-flight ceremony state behind an explicit opt-in; the state remains private and is never returned to a client.

The coordinator wires registration and assertion start/finish routes through the library and persists the returned passkeys and verifier state. A narrow MPL-2.0 vendor patch makes normal no-attestation registration require resident credentials and exposes the library's configured credential algorithm list. The coordinator explicitly advertises ES256, EdDSA (Ed25519), and RS256; a live API contract test checks COSE identifiers `-7`, `-8`, and `-257`. Discoverable sign-in requires the administrator's user handle, matches the returned credential ID and requires user verification. The coordinator serializes ceremony state only into its private database and binds each challenge to a session or short-lived browser ticket. Remaining PK-003 work includes AAGUID extraction and fixed-vector valid and user-verification-negative ceremony tests. The exact RUSTSEC-2023-0071 exception is limited to this reviewed public-key-only call path; do not broaden it to other advisories.

Only consequential decisions. Architecture contracts for standalone Bridge stay in
`docs/llm-bridge/architecture.md`.

## D1 Authoritative owners (REPO-002, QUAL-001)

Provider selection, rotation and retries stay in `ralph_robin`/`scheduler`;
capacity in `capacity`/`routes`/`usage`; mission and task transitions, durable
commands, events and evidence in `bridge_store`; controller, verification,
process ownership and recovery in `bridge_runtime`; tmux station ownership in
`terminal`. The coordinator and worker service add transport and projection only
and call these through the command layer. Consequence: no Rust re-implementation
of any of these rules.

## D2 Typed command layer (ARCH-005, CLI-001, WORK-006)

`llm_tools/bridge_commands.py` declares each shared operation once
(`OperationSpec`: stable ID such as `mission.pause`, typed `Param`s, handler).
Handlers call the existing implementations in `bridge.py` and the store.
`dispatch()` raises domain exceptions so the local CLI keeps its messages and
exit codes; `execute()` returns a `CommandResult` envelope with outcome
`ok|accepted|applied|rejected|failed|uncertain` and a stable error code for
transports. A mutating request's `request_id` becomes the durable
`commands.id`, so a duplicate delivery returns the stored outcome. The apply
step re-reads the command status after taking the SQLite write lock, so two
concurrent deliveries of one ID cannot both apply the effect. An unexpected
exception during a mutation is reported as `uncertain`, never as `failed`.

## D3 Target selection (CLI-003, CLI-005)

`--target NAME` is a leading global option removed before argparse, so every
existing command line is unchanged. `local` (or no option) runs in-process.
Any other name fails with exit 2 until it resolves through a configured
coordinator; there is no fallback to local execution.

## D4 Worker transport (WORK-002, PROTO-001, PROTO-009, SEC-002)

Workers open one outbound WebSocket over TLS to the coordinator and send JSON
messages (docs/distributed/protocol.md). Workers therefore need no listening
port and no firewall change. The Python side uses a small RFC 6455 client on
`ssl` (standard library only) instead of adding a dependency; HTTPS long-polling
was rejected because terminal streaming needs low-latency traffic in both
directions. Workers authenticate with a per-worker bearer token over verified
TLS; the coordinator stores only its hash, so revocation is deleting the record.

## D5 Remote control is opt-in per worker (SEC-001, AUTH-008)

An enrolled worker accepts only read operations unless it was enrolled with
`--allow-control`. The worker enforces this itself and advertises only the
allowed operations in `hello.capabilities`, so a compromised coordinator
cannot mutate a read-only worker.

## D6 Coordinator in a container, workers native (operator addendum A2, A3)

The coordinator is distributed as a `FROM scratch` OCI image holding one static
musl binary and the web files, for linux/amd64 and linux/arm64. Binaries are
cross-compiled on the build host with cargo-zigbuild, so building an arm64 image
needs no emulation (ARM64 user-mode emulation does not work under the rootless
Docker used here). The image runs as UID 65532 with a read-only root filesystem; all
state is in the `/data` volume. Inside the container the server binds 0.0.0.0 with
`lan_enabled = true`; host exposure is decided by the published port, which
`compose.yaml` binds to 127.0.0.1 unless the operator sets
`BRIDGE_PUBLISH_ADDRESS`. `BRIDGE_COORDINATOR_PUBLIC_NAMES` replaces discovery of
names because names discovered inside a container (random host name, bridge-network
address) are useless to browsers. The systemd unit stays a supported alternative and
shares `Config::default`.

No worker image is provided. A worker executes Bridge commands against the local
mission database, starts controllers that launch provider CLIs with the operator's
credentials and environment, and attaches to the operator's tmux stations. A
container would need the home directory, provider credentials, the tmux socket and
the mission folders mounted in, which removes the isolation that motivates a
container and puts provider credentials into a container configuration (SEC-005).
The native `llm-bridge worker install-service` unit is the supported deployment.

## D7 Security architecture review (SEC-009)

One focused review of the coordinator, worker, protocol, terminal relay and remote
CLI was done on 2026-11-05 with Opus before network control was treated as ready.
It reported no critical findings. Every finding below is fixed, with a test that
fails when the fix is reverted, except M4, which is a property of control mode and
is documented as an accepted risk.

| Finding | Disposition |
|---|---|
| H1 Concurrent logins passed the rate check before any failure was recorded, so parallel guesses were not limited. | Fixed: an attempt is reserved under the limiter lock before Argon2 runs; attempts in progress count against the limit; a dropped attempt counts as a failure. IPv6 is keyed by /64. A full key table refuses new keys. |
| H2 The local CA was unconstrained, so a browser that trusts it would accept a certificate for any site signed with `ca.key`. | Fixed: `pathLenConstraint=0`, critical name constraints (server names at `init`, `localhost`, `home.arpa`, loopback), 5-year validity. `cert renew` refuses names outside the constraints; `cert new-ca` replaces the CA; `info` reports an old unconstrained CA. |
| M1 Slow bodies and many sockets could hold all connection slots. | Fixed: body timeout, per-address connection cap, connection slots reserved for workers with a valid token. |
| M2 Unauthenticated routes buffered large bodies. | Fixed: 4 KiB limit on `/api/v1/auth/*`, rate check before the body is read, at most 8 buffered bodies above 64 KiB, only after authentication. |
| M3 A worker answered `error` messages with `error` messages, and a stale welcome left a connected worker that refused everything. Re-adding a worker restarted its generations. | Fixed on both sides: `error` is never answered; a stale welcome closes the connection; generation high-water marks survive revoke on the coordinator and reset only with a new token on the worker. |
| M4 Control mode lets whoever controls the coordinator, an administrator session or an API token make a provider agent run arbitrary commands through prompts or terminal input. | Accepted and documented (threat model). Control remains opt-in per worker (D5). |
| M5 API tokens never expired and survived a password change. | Fixed: 90-day default expiry, revoked by password change or reset unless kept explicitly. The threat model states that a token has full administrator power. |
| L1 Guessing `admin` from elsewhere could lock the administrator out. | Fixed: the per-user limit does not apply to an address with a successful login in the last 7 days. |
| L2 Restoring a backup silently reinstated revoked credentials. | Fixed: `restore` deletes browser sessions and lists every active worker and API token with its revoke command. |
| L3 Unlimited connections could wait before `hello`. | Fixed: one pre-hello connection per worker token. |
| L4 Read-only workers shared terminal screens with the coordinator. | Fixed: terminal sessions need `--allow-terminal-view` or `--allow-control`. |
| L5 Input could reach the pane after a local tmux client attached, until the next role check. | Fixed: the role is checked before every input write. |
| L6 An over-long integer in a message raised an unhandled exception in the worker. | Fixed: it is a protocol error. |
| L7 The session cookie had no `__Host-` prefix, the systemd unit lacked system call filtering, and the remote CLI followed redirects with the bearer token. | Fixed: `__Host-bridge_session`, `SystemCallFilter=@system-service`, `ProtectProc=invisible`, `ProcSubset=pid`; redirects are refused. |


## D8 Platform support and Windows (operator addendum 4, TEST-013, TEST-016)

CI runs on six GitHub-hosted platforms: Linux x86-64 (`ubuntu-latest`), Linux ARM64
(`ubuntu-24.04-arm`), macOS ARM64 (`macos-latest`), macOS x86-64 (`macos-15-intel`; GitHub
still offers an Intel image, `macos-26-intel` is the other label), Windows x86-64
(`windows-latest`) and Windows ARM64 (`windows-11-arm`, available to public repositories).

Decision, from the facilities each component needs:

| Component | Linux | macOS | Windows |
|---|---|---|---|
| Coordinator (`bridge-coordinator`), web interface | supported | supported | supported |
| Remote client: `llm-bridge --target NAME COMMAND`, `remote login\|status\|logout`, `worker list` | supported | supported | supported |
| Standalone Bridge: tmux station, console | supported | not supported (CI run 42159627299: 41 real-terminal and tmux tests time out or render differently on macOS; not ported) | not supported |
| `llm-usage`, `llm-scheduler`, `ralph-robin` | supported | supported; no systemd suspend or RTC wake (the tools wait awake) | not supported |
| Mission controller, verifier supervision, force-stop, `worker run` | supported | not supported: process identity uses Linux procfs | not supported |

Windows lacks tmux, POSIX pseudo-terminals, `curses`, `termios`, `fcntl` file locks and POSIX
signals, which the station, console, controller and worker use. Porting them would mean a
second terminal and locking implementation with no operator asking for it, so the Windows
scope is the coordinator and the remote client.

On Windows every other `llm-bridge` command exits with status 2 and one line,
`llm-bridge: '<command>' is not supported on Windows. Windows supports the remote client only: ...`.
With `--json` it prints `{"schema_version": 1, "outcome": "failed", "error": {"code":
"unsupported-platform", "message": ..., "details": {"platform": "windows", "command": ...}}}`
on standard output. There is no traceback (`llm_tools.bridge.unsupported_on_windows`,
`tests/test_windows_policy.py`).

Files the coordinator creates are mode 0600 (directory 0700) on Unix. Windows has no
permission bits: the files are created normally and the access control list of the data
directory (by default the user profile) is the protection. Keep the data directory in a
location only the service account can read.

On Windows, CI runs only the tests listed once in `WINDOWS_TESTS` in
`.github/workflows/test.yml`: the Windows policy, the remote CLI, the worker protocol parser,
the WebSocket client, the store-independent tests of the command layer and the coordinator
integration tests. The step fails when an entry names a missing file, and
`tests/test_workflow_hardening.py` checks every entry on every platform. The rest of the suite
tests components this table marks unsupported on Windows. Other platform skips are decided
centrally in `tests/conftest.py`: on any platform without `/proc` (macOS and Windows) it
skips the modules in `PROCFS_ONLY_MODULES` and `PROCFS_ONLY_TESTS`, each with its reason. On macOS it also
skips tests marked `tmux` or `pty` and the console tests in `MACOS_SKIPPED_TESTS`, because the
console and station are not supported there. Nothing is skipped on Linux by that code. The evidence for each skip is the CI run listed in
`SPEC_STATUS.md`.

## D9 API token scopes (API-005)

New coordinator API tokens default to `read`; the operator grants `operate`, `terminal:view`,
`terminal:write` and `admin` explicitly with repeatable `--scope` flags. Tokens already in the
database migrate to `admin`, preserving their former full access. Each authenticated route carries
its scope policy in `http::ROUTES`, and the `Authed` extractor enforces the policy for bearer tokens;
browser sessions retain their existing CSRF and Origin checks and full operator access. The route
integration test exercises every authenticated route against each token scope.

## D10 OpenAPI generation (API-009)

Use `utoipa` 6 for compile-time schemas of the coordinator's Rust response types,
then generate the operation list from `http::ROUTES`, the same route policy table
used by authorization tests. This keeps the published route and scope list tied
to server policy without introducing a separate handwritten endpoint inventory.
The coordinator serves the JSON and a small script-free HTML reference; no CDN
or renderer bundle is needed. `coordinator/src/bin/openapi.rs` regenerates the
checked-in snapshot, and a Rust test compares the generated value to it.

## D11 OpenAPI v1 compatibility baseline (API-011)

Because the coordinator had no released OpenAPI document before this follow-up, the first
published v1 contract is `docs/distributed/openapi-v1-baseline.json`. A standard-library
compatibility checker compares the generated snapshot with that baseline and rejects removed
paths, operations or fields and newly required fields. Breaking changes require a new API
version; the checker treats a version change as a new compatibility line. This gives future
changes a stable gate without inventing a pre-existing published contract.

## D12 Bounded mutation budgets (SEC2-010)

Authenticated mutation budgets default to 30 requests per token and 120 per client address in a
rolling 60-second window. Keep those defaults configurable within 1–10,000. This lets deployments
and stress tests tune the address budget without changing enforcement logic. The offline queue
stress harness raises only its configured address budget and uses one portable loopback address;
the coordinator hardening test still checks the production defaults. This avoids relying on
Linux's implicit `127/8` routing, which macOS runners do not provide.

## D13 JSON nesting limit and scanner compatibility (SEC2-003, SEC2-016)

Keep arbitrary JSON bodies to four nested levels, reflected by the runtime validator and the
generated OpenAPI schema. ZAP 2.17.0's Swagger/OpenAPI model deserializer recursively expands the
bounded JSON `anyOf` references. Sixteen levels exhausted its 4 GiB CI heap; a later local scan of
the full API document showed that eight levels still grew the ZAP JVM past 12 GiB before report
generation. An earlier eight-level test against a static fixture only proved that ZAP could import
the schema; it did not exercise the full API document. Four levels sharply bounds that expansion
while retaining the documented per-level string, array and object bounds and allowing the typed
operation payloads. Keep the runtime and OpenAPI limits generated from the same constant; do not
mask schema growth by raising scanner heap size. Validate the full document in the pinned scanner
before increasing this limit.

The runner proxy also intercepted ZAP's own Python client's calls to its localhost control API;
the CI log showed `ProxyError` for `http://localhost:<port>`. Both scanner containers now set
upper- and lower-case `NO_PROXY` entries for localhost and 127.0.0.1 so that control traffic stays
inside the container/network namespace.

## D14 Reviewed xterm.js inline-style CSP exception (SEC2-002, SEC2-016)

Keep `style-src 'unsafe-inline'` on the console while xterm.js requires runtime-injected styles.
Do not weaken `script-src 'self'` or the API CSP. ZAP reports this as plugin 10055, medium, on the
console pages. `scripts/check_zap_reports.py` accepts only that exact alert in the console JSON and
only when each instance shows the expected policy (`default-src 'self'`, `script-src 'self'`, and
the xterm inline-style directive). `docs/distributed/review-findings.md` records the evidence and
residual risk. Other CSP findings and all other medium-or-higher alerts remain failures.

## D15 Read-only, resource-bounded Schemathesis run (API-012, SEC2-016)

Repeated hosted scan runs stopped during Schemathesis about three minutes after its 47-operation
coverage phase, with no runner system log and no coordinator log artifact because cancellation
skipped finalization. The OpenAPI contract includes mutations that can start paid work, change
credentials or settings, create/revoke tokens and workers, restore backups, or suspend a worker.
Those state changes are exercised by authored coordinator/worker integration tests; keep the
automated property-based run on GET operations and WebSocket upgrades out of band. Run one worker,
cap its address space at 4 GiB, bound the fuzz phase to 120 seconds and wrap it in a 180-second
timeout. After a successful run, verify that coordinator health still responds. This keeps the
CI check useful for read-path status/schema/5xx defects without allowing fuzzed writes to change
deployment state or exceed the runner budget. A local pinned run selected 23 read operations,
passed 4,201 generated cases across coverage, fuzzing and stateful phases in 30 seconds, and left
the coordinator healthy. Revisit the scan scope if authored mutation coverage changes.

## D16 Memory gate of the soak and the controller leak (PRD-007)

PRD-007 forbids unbounded growth. The first 30-minute soak showed the mission controllers growing by what looked like
20 to 56 KB per completed task, and a first response raised the controller bound to 6 MiB per hour. That was
rejected: a bound that large (144 MiB per day) hides a leak. The decision rests on measurements, listed in
`soak-and-chaos.md` (Memory over time) with plots in `soak-and-chaos/`.

- **One leak, removed.** CPython 3.12 makes interned strings immortal and `pathlib` interns every path
  component. The controller parsed five new names per provider run (iteration directory, two verifier files,
  two pids), 5.00 new interned strings per task, 0.33 KiB per task on the traced heap. `common.RunPath` (a
  `PosixPath` whose parser does not intern; the plain class on other Python versions or if the internals
  differ) and `fileio.read_proc_text` remove them: 0.00 strings per task over 300 tasks, 0.02 KiB per task on
  the traced heap over 455 tasks. `tests/test_bridge_controller_memory.py` fails before the change (90 new
  strings after 18 runs) and passes after (at most 4).
- **No other accumulation.** The remaining growth of a controller is heap fragmentation (`malloc_trim(0)`
  returns 1.5 to 3.4 MiB) and a bounded SQLite page cache (1.63 to 2.38 MiB); live glibc heap rose 0.13 MiB in 35
  minutes. It flattens: 50 controllers rose for about 20 minutes, a single controller moved by 0.32 MiB per
  hour for 74 minutes after that (0.51 after the fix, same spread).
- **Gate.** The soak ignores a warm-up of 1200 s (at most two thirds of the run) instead of 25% of the run,
  judges controllers per process on the median of each controller's own post-warm-up slope (bound 1.5 MiB per
  hour, measured 0.88 to 1.12), and fails any single controller or worker above 56 MiB and the coordinator
  above 96 MiB (measured peaks 40.9 to 42.8, 34.1 and 34.3 MiB). The coordinator's growth is allowed 256 bytes
  per new row (measured 81 to 96). The 6 MiB per hour bound is reverted.
- **Not decided here.** The plateau is shown for 75 minutes after warm-up. The 4.5-hour nightly and the 24-hour
  runs are the evidence for longer, and the ceilings fail them if it does not hold. Python 3.13 or later should
  not need `RunPath`; this was not run. A periodic `malloc_trim` was rejected for now: it is glibc-specific and
  recovers about 1.5 to 3 MiB.

## D17 Web initial JavaScript budget (WEB-010, PRD-020, PRD-036)

The initial JavaScript caps in `web/budget.json` rose from 90000 raw / 31000 gzip to 91000 / 31750
bytes. Bytes are the app entry plus the static chunks it imports, measured the way
`e2e/budget.spec.ts` measures them (gzip level 9), by building each state in a clean tree. The
starting point is the tree after PR #31 was merged, because that merge had already grown the first
load from 88811 / 30944 (the follow-up branch before the merge) to 88984 / 31005, which is over the
old gzip cap by 5 bytes.

| State | Initial JS raw | gzip | Change raw | Change gzip |
|---|---:|---:|---:|---:|
| Merged main plus release readiness, before the PRD-036 web half | 88984 | 31005 | | |
| + `api.ts`: 403 `password-change-required` handler, auth/state fields | 89053 | 31042 | +69 | +37 |
| + `login.ts`: reset hint and link on the sign-in page | 89440 | 31215 | +387 | +173 |
| + `account.ts`: `passwordForm` takes a page title and intro paragraphs | 89594 | 31291 | +154 | +76 |
| + `main.ts` and `reset.ts` (forced screen and notice, static import) | 90997 | 31720 | +1403 | +429 |
| Total | | | +2013 | +715 |

The forced-change screen and the notice (`reset.ts`) are imported statically. Loading `reset.ts` with a
dynamic import was built and rejected. Measured on the follow-up branch before the merge, it gave
90055 / 32267 in 4 files against 90910 / 31692 in 2 files for the static import: 855 fewer raw bytes
but 575 more gzip bytes. The hint, the form change and the 403 handler have to be in the first load
either way, and `reset.ts` imports `passwordForm` from code that the account page also uses. esbuild
therefore moved shared code into two extra small static chunks, and each chunk is compressed on its
own (+1028 gzip bytes for +620 raw bytes). Gzip size is what crosses the network, and the lazy variant
also costs two more requests on the first load.

The messages of the forced screen and the hint were shortened by about 100 bytes so that the first
load fits the raw cap of 91000, which is the ceiling the operator accepted. It now leaves 3 raw and 30
gzip bytes of headroom, so the next change to the first load has to raise the caps or remove code. The
total initial figure (JavaScript plus CSS) is 121034 raw / 39254 gzip against caps of 124000 / 40000
and is unchanged in `budget.json`.

The numbers come from builds in clean trees. `e2e/budget.spec.ts` on a working tree can differ by a
byte of gzip because the chunk file names, which depend on the path, compress slightly differently.

Addendum after merging the passkey, token-restriction and listener work (PR #50) into this branch.
The initial caps stay at 91000 raw / 31750 gzip (JavaScript) and 40000 gzip (JavaScript plus CSS).
Same measurement method as above:

| State | Initial JS raw | gzip | Initial total gzip | Lazy raw | gzip |
|---|---:|---:|---:|---:|---:|
| Merge base | 88506 | 30848 | 38382 | | |
| `main` with PR #50 (account page and passkey code load lazily) | 87052 | 31408 | 38992 | 365652 | 96411 |
| This branch before the merge (already above its 91000 / 31750 caps) | 92390 | 32176 | 39710 | 352470 | 91000 |
| Merge, `passwordForm` still exported from `account.ts` | 102090 | 36331 | 43915 | | |
| Merge, `passwordForm` in `views/password-form.ts` | 92638 | 33163 | 40747 | 363929 | 95857 |
| Merge, plus workers, sessions and usage views loaded on demand | 84299 | 30855 | 38439 | 373424 | 100498 |

What moved:
- `passwordForm` lives in `views/password-form.ts`. The forced password-change screen (`reset.ts`) is part
  of the first load and imported it from `account.ts`, which pulled the whole lazy account page (API token
  restrictions, passkey list and re-enrolment, about 8 KB) into the first load: +9.5 KB raw.
- The router loads the workers, sessions, usage and account views with `import()` (`lazyView` in
  `main.ts`). While a chunk loads the main area shows the existing `status-skeleton` block with
  `aria-busy`; a route change during the load discards the result. The overview, missions list, mission
  detail and diagnostics views stay in the first load. The mission detail view alone is 3.9 KB gzip
  of the first load, but moving it, or the missions list, costs more lazy bytes than moving the three
  smaller views and would delay the pages used most.

Why the lazy caps rose (`jsRaw` 366000 to 374000, `jsGzip` 97000 to 101000): the merged code was
already larger than the two old budgets together (initial 31750 plus lazy 97000 is 128750 gzip bytes;
the merged build was 129020 before any split). Any code taken out of the first load therefore
counts against the lazy caps. Code that leaves the first-load bundle also compresses worse as separate
chunks: the three views take 8339 raw / 2308 gzip bytes out of the first load and add 9495 raw / 4641 gzip bytes to the lazy total (the difference is the chunk boundaries and the shared code each chunk now carries). The new lazy caps leave 576 raw / 502 gzip bytes of headroom.
The initial caps leave 6701 raw / 895 gzip / 1561 total-gzip bytes.

## D19 Worker memory: no leak, per-process gate (PRD-007)

The soaks showed `llm-bridge worker run` processes growing by 2.5 and then 1.9 MiB per hour after warm-up (about 48 MiB per day). The
investigation followed D16. Measurements are in `soak-and-chaos.md` (Worker memory), plots in `soak-and-chaos/worker-*.svg`.

- **No accumulating object.** `tracemalloc` with 25 frames inside a real worker moved from 7917 to 7964 KiB in 40 minutes (a deferred import
  on the first status command, not a per-command allocation); live objects, Python allocated blocks and SQLite memory in use did not trend; the
  controller pattern (CPython 3.12 interned path components) does not occur in the worker. A longer reading is the same: in a 3-hour
  single-worker run the bytes in use in the glibc heap rose by 0.2 MiB, all within the first 50 minutes.
- **What was measured as growth.** Free space inside the glibc heap (0.9 MiB at minute 1, 2.8 MiB from minute 55, then flat) and the steps down
  when the top of the heap is freed (up to 2.6 MiB). The 30- and 45-minute runs sampled the rising part. After minute 20 the resident size of the
  3-hour run moved by -0.06 to +0.22 MiB per hour depending on the start of the window.
- **No code change in the worker.** Holding one read-only SQLite connection instead of opening one every 0.5 s was tried and reverted: the
  heap grew by the same amount. A periodic `malloc_trim` was not added for the reason given in D16; at minute 24 it returned 1.0 MiB and the free
  space was back 200 s later.
- **Gate.** The workers are judged per process like the controllers (median over the processes of each one's slope after `--warmup-seconds`).
  The bound is 512 KiB per hour per worker (12 MiB per day, 2.3 times the largest post-warm-up slope measured), replacing 3 MiB per hour summed
  over the fleet. The floor (1024 KiB rise per process) and the ceiling (56 MiB, measured peak 34.2 MiB) are unchanged. In the 30-minute run the
  10-minute window measured a median of 2.81 MiB per hour (settling) with a rise of 474 KiB, so that run passes on the floor; the slope can only
  fail in runs where the window is two hours or longer (the 4.5-hour nightly run and the 24-hour run).
- **Tests.** `tests/test_bridge_worker_memory.py` (interned strings and live objects after 240 commands and event polls; it fails with a
  per-command `Path` or dictionary entry added) guards the property that there is no per-command growth. It cannot fail on the current
  code because there is nothing to fix. `test_workers_are_judged_per_process_with_a_bound_below_the_old_three_mib_per_hour` fails with the
  previous bound and the summed check.
- **Not decided here.** Whether a 24-hour worker stays inside 0.5 MiB per hour is unmeasured; the longest run is 3 hours.

## D20 Canonical passkey origin (PK-002)

Bind the administrator's passkeys to one HTTPS DNS origin. The first successful
registration binds the origin and credential in one SQLite transaction; an operator may
preconfigure the same value as `public_origin`. The value is persisted in coordinator
configuration after registration and remains database-authoritative if that write fails.
Startup rejects a changed configured origin while credentials remain. Older credentials
without a recorded origin require the operator to set `public_origin` explicitly before
startup, so an upgrade cannot silently choose a new RP ID from whichever address is visited
first. IP literals are rejected because they cannot be WebAuthn RP IDs. Explicit, confirmed
re-enrolment UX remains future work.

In the code: `coordinator/src/passkeys.rs::canonical_origin`, `coordinator/src/config.rs`,
`coordinator/src/db.rs::passkey_put_at_origin`, and `coordinator/src/server.rs`; regressions
are covered by `coordinator/tests/passkeys.rs` and `coordinator/tests/web_api.rs`.

## D21 Require discoverable passkeys (PK-003, PK-004)

The maintained `webauthn-rs` 0.6.2 no-attestation registration method hardcodes
`require_resident_key(false)`. Its separate resident-key API requires attestation roots and
rejects synced authenticators, which conflicts with the product's no-attestation and synced
passkey requirements. Keep upstream's normal passkey API and make a narrow, licensed local
vendor patch that sets the builder to require resident keys. Enable its conditional UI API
and start a discoverable authentication ceremony with an empty allow-credentials list and
user verification required. Before finishing, match the credential ID and verify the fixed
user handle against the administrator, then use the library's discoverable authentication
verification. Do not enable synced passkeys through the attestation-only API.

In the code: `coordinator/vendor/webauthn-rs/src/lib.rs` contains the one-line builder patch;
`coordinator/src/passkeys.rs` starts and finishes the conditional ceremony. The regression
tests assert required resident keys, conditional mediation and an empty allow list. Completion
checks the fixed user handle, registered credential ID and user-verification flag; valid
cryptographic vectors for that path remain required under PK-011.

## D22 Configurable API token lifetime ceiling (SEC2-009)

Keep the existing 90-day default and allow a deployment to lower or raise the explicit
expiry ceiling through `max_token_lifetime_days`, bounded to 1–3650 days. Preserve
`expires_days = 0` for non-expiring LAN tokens for compatibility. When Internet exposure is
implemented, its startup precondition must reject that value and enforce the 90-day ceiling.

In the code: `coordinator/src/config.rs` defines and validates the setting;
`coordinator/src/http.rs::create_token` enforces it. Boundary tests live in
`coordinator/src/config.rs::tests::api_token_lifetime_default_is_ten_years_and_config_is_bounded`
and `coordinator/tests/web_api.rs::token_expiry_respects_the_configured_maximum_but_allows_lan_nonexpiry`.

## D23 Identifiable API token strings (SEC2-009)

New API tokens use `lbr_pat_<id>_<secret>`. The random 16-character lowercase-hex ID makes
the credential recognizable to scanners; the independently generated 32-byte secret is
encoded as 64 lowercase-hex characters and provides 256 bits of entropy. Store the ID and
only the SHA-256 hash of the secret. Look up by public ID, then compare the secret hash in
constant time. Continue accepting existing unprefixed tokens through their legacy full-token
hash lookup until operators rotate them. The generated OpenAPI schema constrains the new
format. Do not log either component.

In the code: token minting is in `coordinator/src/http.rs::create_token`, and the response
schema is in `coordinator/src/openapi.rs::TokenCreated`; the format regression is in
`coordinator/tests/web_api.rs::token_management_lists_metadata_and_shows_secret_only_at_creation`.

## D24 Certificate provisioning boundary (SEC2-019)

Keep certificate source selection behind `CertificateProvider`, with implementations for the
internal coordinator CA and operator-supplied certificate files. File reload keeps the last
valid certificate when replacement files are incomplete. ACME remains an unimplemented future
provider; the project does not operate certificate or DNS infrastructure.

In the code: `coordinator/src/certs.rs` owns the trait and both current providers;
`coordinator/src/server.rs` selects a provider from the validated config. Existing file-reload
and TLS tests cover the provider implementations.

## D25 Stable principal ownership and sender binding extension points (SEC2-019)

Use a durable `principals` table with stable IDs (`admin` and `system` today) and role metadata.
Sessions, API tokens and audit rows point to the principal ID rather than deriving ownership from
the current administrator display name. Keep DPoP or mutual-TLS token sender constraints as two
nullable typed fields until a sender-constrained credential is actually implemented; do not imply
that bearer tokens are sender-constrained today.

In the code: schema migration 17 adds the principal registry and ownership fields in
`coordinator/src/db.rs`; `coordinator/tests/principals.rs` verifies new records, and
`coordinator/tests/hardening.rs::version_1_database_is_migrated_in_place` verifies legacy rows.

## D26 Allow the bundled webpki trust data license (SEC2-015)

Allow `CDLA-Permissive-2.0` in the Rust license audit because `reqwest` reaches `webpki-roots`,
which bundles root-certificate data. The official license permits use, modification and sharing;
if the data is redistributed, the license text must accompany it. Keep this explicit allow narrow
to the license identifier and retain `cargo deny`'s default deny for all other unknown licenses.

In the code: `coordinator/deny.toml` adds the exact identifier. The license terms are published by
[the Community Data License Agreement project](https://cdla.dev/permissive-2-0/).

