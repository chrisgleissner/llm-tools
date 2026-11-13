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

## D18 Web route chunks and the separate routes budget (PRD-037 WP-0, UXR-039)

The redesign adds a state model, shared components and several new pages, so the first load needs room.
WP-0 moves the seven routes that are not needed on the first page out of the entry chunk and gives
them their own cap. The initial JavaScript caps stay as D17 set them (91000 raw / 31750 gzip) and the
terminal chunk cap stays at 360000 raw / 93000 gzip. Sizes are bytes from `e2e/budget.spec.ts` (gzip
level 9); per-module numbers are the esbuild metafile `bytesInOutput` of the same build settings.

| Measure | Before (D17 tree) | After WP-0 | Change |
|---|---:|---:|---:|
| Initial JavaScript, raw | 90997 | 70785 | -20212 |
| Initial JavaScript, gzip | 31721 | 26199 | -5522 |
| Initial CSS, raw / gzip | 30037 / 7534 | 31706 / 6947 | +1669 / -587 |
| Terminal chunk, raw / gzip | 352470 / 91000 (as `lazy`) | 352588 / 91040 | +118 / +40 |
| Route chunks (new cap), raw / gzip | 0 | 30997 / 13725 | |

Modules moved to lazy chunks (raw, minified): `login` 10343, `account` 6185, `workers` 4737,
`remote` 1919 (shared by sessions, usage and diagnostics), `diagnostics` 1531, `password` 1368,
`usage` 1250, `reset` 1086, `sessions` 830; 29249 in total. The new foundation adds `state.ts` (3196),
a larger `dom.ts` and `ui.ts`, and the toast policy; the menu component is not in the first load until a page imports it.

Two further changes keep the first load inside the unchanged caps:

- The xterm.js stylesheet (5175 raw / 1908 gzip) was part of `app.css`. It is now a separate hashed
  file (`xterm-css.ts`) that the mission route starts fetching in parallel with the terminal chunk and that the
  terminal module awaits, so only a mission page pays for it. The first-load CSS therefore fits the 34000 / 9000 cap with 2294 bytes of headroom even
  though the new design tokens and components were added next to the legacy rules that the page
  packages still use.
- `reset.ts`, rejected as a lazy import in D17, is lazy now. D17 rejected it because the shared code of
  `reset.ts` and `account.ts` moved into extra static chunks of the first load. Here `login.ts`,
  `account.ts`, `password.ts` and `reset.ts` are all lazy, so the code they share is in lazy chunks as
  well. The first load is the entry plus three shared chunks (api, icons, state and dom; the store;
  the toast code with the event humanizer and its phrase table) where it was the entry plus one.

A route chunk costs one more request the first time a route is opened. The first-run page now waits
for the `login` chunk before the form renders. The load test (`setupFormMsMax` 5000 ms at 4x CPU) still
applies and is run after this change.

The new `routes` cap is interim: 40000 raw / 17000 gzip, which is the measured 30997 / 13725 plus about
30 percent for the growth of the lazy pages (the palette, the fleet pages and the account pages are
rebuilt in WP-1 to WP-5). It is replaced by the value measured after WP-5. If the first load exceeds a
cap before then, `missions` and `overview` may move behind `import()` before any cap is raised.

### D18 update: route stylesheets

The page packages add CSS that only lazy routes use (WP-5 fleet and auth about 4.9 KB, WP-3 mission about 6.1 KB),
which would push the first-load CSS over the 34000 / 9000 cap. Page CSS now loads with its route, the way the
xterm stylesheet does. `build.mjs` builds `css/fleet.css` (workers, sessions, usage, diagnostics), `css/auth.css`
(sign-in, setup, reset, account) and `css/route-mission.css` (imports `session.css` and `mission.css`) as separate
hashed files and defines their URLs. `withCss(name, loader)` in `src/route-css.ts` starts the stylesheet fetch together
with the route chunk's `import()` and resolves when both are done, so the route renders styled. The mission route awaits
`ensureRouteCss("mission")` before it mounts. The sign-in page waits for its stylesheet in parallel with the `login`
chunk (one more request, no extra round trip); the load test (`setupFormMsMax` 5000 ms at 4x CPU) measured a median form
time of 1007 ms. `app.css` imports only tokens, base, components, shell, inbox and missions.

How lazy CSS is counted: `routes` in `budget.json` now has `jsRaw` / `jsGzip` (route chunks, as before) and
`cssRaw` / `cssGzip` (every stylesheet in `dist` except the one `index.html` references and the xterm stylesheet).
`e2e/budget.spec.ts` checks both pairs. The initial CSS cap stays 34000 / 9000.

| Measure | Before | After |
|---|---:|---:|
| Initial CSS, raw / gzip | 31854 / 6988 | 27888 / 6285 |
| Route CSS (fleet empty, auth, mission), raw / gzip | in the first load | 4626 / 1631 |
| Initial JavaScript, raw / gzip | 70785 / 26198 | 71388 / 26439 |

The interim route CSS cap is 13000 / 3800, sized from the page packages' measured additions (about 11.8 KB raw together
with the current files). It is replaced by the value measured after WP-5 together with the JS routes cap.

Rules that a first-load page and a route stylesheet both use stay in `components.css` (`table.kv`, `.steps`,
`.transition-summary`, `.event-controls`); the landscape header rule moved from `session.css` to `shell.css`. The
page packages remove these with their legacy pages. The Coordinator-unreachable and load-error pages in `main.ts` use
the auth page styles and load that stylesheet before they render.

### D18 update: final routes caps after the last page package (integration)

All page packages are merged. The caps that were fixed before (initial JavaScript 91000 raw / 31750 gzip, initial CSS
34000 / 9000, terminal chunk 360000 / 93000) were not raised. The `routes` cap is now the measured figure plus less than
5% (`web/budget.json`, bytes, gzip level 9, `e2e/budget.spec.ts`):

| Measure | Measured | Cap |
|---|---:|---:|
| Initial JavaScript, raw / gzip | 81867 / 31590 | 91000 / 31750 (unchanged) |
| Initial CSS, raw / gzip | 29107 / 6584 | 34000 / 9000 (unchanged) |
| Terminal chunk, raw / gzip | 354858 / 92149 | 360000 / 93000 (unchanged) |
| Route JavaScript (all lazy chunks but the terminal), raw / gzip | 90026 / 37084 | 94000 / 38700 |
| Route CSS (`fleet`, `auth`, `mission`), raw / gzip | 17937 / 4739 | 18400 / 4900 |

The initial gzip figure is 160 bytes below its cap, so any further first-load code needs a matching removal.

Per-chunk figures (raw / gzip) and the package that added each one:

| Chunk | Raw / gzip | Added by |
|---|---:|---|
| `views/mission` (header, tasks, evidence, activity, about, page) | 31645 / 11692 | WP-3 |
| `views/login` (sign-in, setup) | 12397 / 4822 | WP-0 moved it; WP-5 restyled it |
| `views/account` and the tokens dialog | 7229 / 2901 | WP-0 moved it; WP-5 |
| `views/usage` | 6780 / 2982 | WP-0 moved it; WP-5 (meters) |
| `views/workers` | 6683 / 2886 | WP-0 moved it; WP-5 (enrolment warning) |
| session extras (steer dialogs, key bar) | 6272 / 2868 | WP-2 |
| `remote.ts` shared by sessions, usage and diagnostics | 4630 / 2166 | WP-0; WP-5 |
| `views/sessions` | 4889 / 2109 | WP-0 moved it; WP-5 |
| command palette | 4496 / 1992 | WP-1 |
| `views/diagnostics` | 2891 / 1401 | WP-0 moved it; WP-5 |
| `views/reset` | 1389 / 734 | WP-0 |
| three re-export shims | 725 / 537 | esbuild |

The session pane itself is part of the terminal chunk (xterm.js plus `session.ts`, `composer.ts`); the page reserves its box
with `sessionReserve()` (`session-box.ts`, a few bytes in the first load) until that chunk arrives. Route CSS: `auth`
3313 / 982, `fleet` 3695 / 1014, `mission` (session.css and mission.css) 10929 / 2743.

Dead code removed at integration: the `statusBadge` shim, `sortHeader`, the legacy table CSS (`table`, `table.rt`,
`table.kv`, `.table-wrap`, `.sort`), the `.grid-auto`, `.pane-split` and `.grid-2` helpers and `@xterm/addon-fit`.

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

## D27 Release decisions delegated to the lead (PRD-007, PRD-037, DOG-025)

The operator delegated the remaining open questions: "All other questions answer them like a principal engineer and product owner would". The lead decided:

- PRD-007 soak duration: PR CI keeps the 5-minute soak (job `soak-chaos`), `.github/workflows/soak-nightly.yml` runs 30 minutes, and the 24-hour run is a pre-release step. A 30-minute soak on every PR would add about 25 minutes to each CI run without finding defects the nightly run misses; the nightly run and the pre-release run cover slow growth.
- Lazy web JavaScript cap: the raise from 97,000 to 101,000 bytes gzip recorded in D17 is accepted. Its cause (the lazily loaded workers, sessions, usage and account views) was measured. Initial caps stay unchanged.
- PRD-037 spec section 14: the routes cap is approved at the values measured in D18; free-text steering through the web console is not part of 1.5.0; mission creation and snooze stay out of PRD-037; the plain `task.output` tail is accepted as the Inbox preview; the Inbox backend fields were delivered by WP-7 and DOG-021.
- DOG-025 (final provider line lost when a native provider exits at once on tmux 3.4) is deferred after 1.5.0 and listed as a known limitation in the release notes. A fix has to wrap the provider command, which changes how the station detects a clean exit, and that is too large a change for the release.

## D28 Operator decisions on the redesign and the overseer's findings R1 to R6 (PRD-037)

The numbers D19 to D27 belong to the release branch (PR #35); this entry is D28 so that both branches merge without a clash.

The fresh-eyes critique (`watch/prd037-critique-outcome.md`, "Needs operator decision") left six questions. The operator
answered: "I agree with all your suggestions and for the redesign decisions." The overseer then reviewed screenshots of
the redesign at 72930c5 (`watch/prd037-overseer-review.md`, 2026-11-08 00:20), accepted decisions 1, 2, 3, 5 and 6,
replaced decision 4 and added the findings R1 to R6. The decisions are implemented as follows. `web/e2e/redesign/uxr-decisions.spec.ts`
(T48) guards each of them; the revert column says what was undone to see the guarding test fail (each check was run in the Playwright
image with that one change made and then restored).

| # | Decision | What changed | Guarding test (T48 unless stated) | Revert that fails it |
|---|---|---|---|---|
| 1 | Inbox order | Failures from the last 24 hours rank first, then approval, then input. Older failures are one row after the input rows, "N older failures", with a Show button (and Hide) that lists them below that row. The heading count includes them. | "D28.1: failures of the last 24 hours come first..." and "D28.1: failures older than 24 hours are one row..." | the older-failures group removed; approval and input ranks swapped; the preview not following a visible row |
| 2 | Capacity chip | Neutral while the provider is ready. While `usage.snapshot` says the provider is not ready, the chip has the attention tone and "resets 12:25" in its text. At 320 px the scope, the meter and the visible percentage give way ("Claude resets 12:25", 12 px, 6 px padding); the percentage stays in the accessible text and the title. | "D28.2: a chip is neutral..." and "D28.2: the waiting chip... fits the header at 320 px"; T35 and T22 still pass | `waiting: false` in `chipsFrom` |
| 3 | Stop beside Reject, Undo for Pause | Stop has no danger outline while a Reject button is shown in the same header (from 30 rem; below it Reject is in the menu and Stop keeps its outline) and stays visible. Pause answers with "Mission paused." and an Undo button; the toast expires after 8 s (spec 6.4 has the exception) and Undo sends `mission.resume`. | "D28.3: Stop has no danger outline..." and "D28.3: Pause answers with an Undo toast..." | Stop always `danger`; the toast with an action sticky again |
| 4 | Phone action bar | Replaced by the overseer, because UXR-007 needs Pause and Stop always visible. The primary label is "Approve" (the accessible name keeps "Approve task 2"). Pause and Stop show icon and label in 44 px buttons; padding is 8 px so Take control, Pause, Stop and the menu fit one row at 353 and 360 px. At 320 px (below 21 rem) Pause and Stop are icon only with their names for screen readers, because Take control, Pause, Stop and the menu need 315 px and 296 px are available. Stop is never moved into the menu. | "D28.4: the primary action reads 'Approve'..." and "D28.4: on a phone Pause and Stop are 44 px buttons..." | the label back to "Approve task 2"; the icon-only rule at every phone width |
| 5 | Key bar | On a phone it is hidden until the client holds control, at every height (it was hidden only up to 640 px). T38 changes with it: hidden before control, six keys plus "..." after. | "D28.5: on a phone the key bar is hidden until control..." and T38 | the old height condition in `phone.css` |
| 6 | Reconnect banner age | The banner says "Showing data as of 12:00" and adds "(15s ago)" once the data is 10 s old. `shell.spec.ts` and T28 wait for it; T28 uses the fake clock because the fixed clock never reaches 10 s. | `shell.spec.ts` "a reconnect shorter than the debounce..." and T28 | `AGE_AFTER_MS = 0` |

The overseer's findings are registered as UXR-047 to UXR-052 in `SPEC_STATUS.md` (the register has the UXR group since PR #35 merged):

| Row | Finding | Change | Guarding test | Revert that fails it |
|---|---|---|---|---|
| UXR-047 | R1: a phone opens the live session also while a task awaits approval | The product already opened on Session (`let tab = ... "session"`); the Tasks tab in the screenshots came from the screen definitions. A test now asserts the selected tab, the live pane and 14 rows with Approve in reach. | "R1: a mission that awaits approval opens on the Session tab on a phone..." | default tab "tasks" |
| UXR-048 | R2: one place for the primary action | The header offers Approve, Reject and Retry for the task it is about; that task's row and the evidence block have no copy. A row of another task keeps its inline action and its menu; with several approvals the header says "Review N tasks" and each row keeps Approve and Reject. The evidence block has no buttons. | "R2: Approve and Reject are offered once..." and "R2: with several tasks awaiting approval..." | `subject` always undefined |
| UXR-049 | R3: one number everywhere | The heading "Needs you (n)" uses the same figure as the badge, the tab title and the answer line; "+" appears only while some active mission is unread. | "R3: ... one number" and "R3: when every mission has been read..." | the heading using the row count |
| UXR-050 | R4: the summary is not an instruction | The third part of the Inbox answer line names the kinds: "1 failed, 1 awaits approval". On phones taller than 30 rem the line keeps room for two lines from the start, so a late status does not move the rows (CLS); a landscape phone keeps one line so the first row stays above the fold (T18). | "R4: the Inbox summary names the kinds..."; T47 F2 and T36 | the text "Retry or open" |
| UXR-051 | R5: the pill column | The column is 8.5 rem in Needs you, 6.5 rem in Running and 11 rem elsewhere, so the title starts 40 px closer at 1440 px in Needs you and no pill reaches a title. | "R5: at 1440 px an Inbox title starts close to its pill..." | 11 rem in Needs you |
| UXR-052 | R6: the phone search button | No fill and no border at rest, like the account button. | "R6: on a phone the search button has no fill and no border..." | the old border colour |

UXR-043 to UXR-046 are registered too; their guarding tests are `uxr-paths.spec.ts` (UXR-043, UXR-044), `uxr-keyboard.spec.ts` and
`uxr-keyboard-only.spec.ts` (UXR-045) and `uxr-reach.spec.ts` (UXR-046).

Answers to the questions of section 14 of `web-design-spec.md` (the section now records them):

1. Backend fields for the Inbox: done by WP-7 and the DOG-021 digest. The Inbox rows switch to the digest after PR #35 merges.
2. Routes cap: approved at the measured values of D18.
3. Free-text steering without taking control: not in 1.5.0; a follow-up.
4. Mission creation and snooze (DEV-10, DEV-11): out of PRD-037.
5. Inbox preview content: the plain `task.output` tail is accepted.

Existing tests changed only as far as the decisions require: T17 (the Pause toast has an Undo button, so its dismiss button is chosen by name,
and the last queued toast of a phone ends after 24 s), T28 (fake clock, no age before 10 s, age after), T38 (key bar hidden before control),
T47 F2 (the summary text) and F4 (the toast text includes Undo), `shell.spec.ts` (banner age), and the Reject screen (opens the
dialog from the header, or from the header menu below 30 rem).

Budget (bytes, raw / gzip, caps in `web/budget.json` not raised):

| Measure | Before (72930c5) | Now | Cap |
|---|---:|---:|---:|
| Initial JavaScript | 79509 / 30905 | 80464 / 31326 | 91000 / 31750 |
| Initial CSS | 33525 / 7451 | 33949 / 7550 | 34000 / 9000 |
| Initial total | 113034 / 38356 | 114413 / 38876 | 124000 / 40000 |
| Terminal chunk | 355631 / 92442 | 355631 / 92441 | 360000 / 93000 |
| Route JavaScript | 93987 / 35975 | 93984 / 36138 | 94000 / 38700 |
| Route CSS | 18296 / 4848 | 18358 / 4869 | 18400 / 4900 |

The route JavaScript had 13 raw bytes left. The additions (the Undo option of a toast, the header's subject, the Approve label) were paid for by
folding the Stop and Cancel confirmations into one function, merging the two Retry branches of the header, one helper for the task
Retry, Resume and Pause calls, and dropping the evidence block's second pair of Approve and Reject buttons. The route JavaScript has 16 raw bytes left and the initial CSS 51.

Baselines: regenerated in `mcr.microsoft.com/playwright:v1.64.1-noble` with `--update-snapshots=all` for `screenshots.spec.ts`; images that were
pixel-identical or differed by fewer than 50 pixels were restored, which leaves 233 changed files (light and dark, desktop and phone) plus the new
`inbox-older-failures` screen. 47 of them differ only in the header (the waiting chip). The login and setup screens are among the rest: the committed
images still showed "What does this do?", which the source replaced with "What does remembering do?" earlier, and the 0.2% comparison tolerance had hidden it.
Looked at in light and dark: contact sheets of the Inbox, mission, session, Sessions and Missions screens on phone and desktop, header crops of the images that differ only in the chip, and the login screens.
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

## D29 Coordinator backups and database access (DOG-024)

Cause. On the Pi, `backup` ran for more than 35 minutes and the coordinator answered
neither HTTPS nor worker TLS handshakes. The old backup opened the database from a second
process and used the SQLite backup API in steps of 256 pages. SQLite restarts such a
backup from the first page when another connection commits between two steps. A
coordinator that receives events commits several times a second, so the copy could not
finish: on the test fixture (174 MiB, one worker sending an event batch every 50 ms) it
wrote 8.7 GiB in 60 seconds and never completed. Each step also committed the copy's
rollback journal with fsyncs, so the restarted copies kept the card busy. The likely
mechanism of the TLS stall, which was not observed directly: on ext4 a commit by the
coordinator (`synchronous=FULL`) waits for the dirty data of the same journal
transaction, and the coordinator runs database calls on two runtime threads behind one
connection mutex, so a commit that waits on the card stops both threads and with them
the TLS accept loop. That stall cannot be reproduced on an NVMe host (the slowest
health request there was 177 ms during the old backup); the livelock and the write
volume are measured, and the test fails on the old binary because the backup never
finishes.

Decision. The backup is `VACUUM INTO` from a read snapshot (cannot be restarted, copies
only pages in use), paced to a write rate cap with a flush every MiB, at nice 19 and idle
I/O, with a preflight estimate, a budget and progress. Retention: result bodies of
finished commands are dropped after an hour (read-only) or a day (mutating), finished
command rows are capped at 5 000, and the hourly pass runs on a blocking thread in
50-row statements. New databases use incremental auto-vacuum; each hourly pass returns at
most 8 MiB of free pages; `compact` converts an old database offline. The Python worker
already does not replicate controller heartbeats or superseded capacity refreshes
(DF-019/020), so the coordinator stores no plumbing it would never read.

Rejected: continuous WAL shipping (Litestream style) for the pre-upgrade step. It needs a
long-running replica process and a second storage target on a host with 512 MB of RAM,
and it holds a read snapshot open that stops WAL checkpoints. The design, if the recovery
point has to shrink to seconds: a sidecar tails `coordinator.sqlite3-wal` frames after
`PRAGMA wal_autocheckpoint=0` is replaced by a checkpoint that the sidecar runs, ships
each new frame range (salt and frame counters identify the generation) to the
off-device store, and takes a fresh `backup` snapshot per day; restore is the newest
snapshot plus the frames of its generation. Because the workers re-send replicated
events, the missing piece is only coordinator-local state, so a recent snapshot with an
age check (`backup-check`) is what ships now.

Database access off the runtime threads. The coordinator called SQLite synchronously from
request handlers on its two runtime threads, behind one connection mutex. One slow commit
(or one holder of SQLite's write lock) therefore stopped both threads: the accept loop,
TLS handshakes and `/healthz` stopped with them, and a pile of handlers waiting on the
mutex had no limit. Reproduced with a second connection holding the write lock for 6 s:
the old code did not run a 300 ms timer for 4.7 s. Every public `Db` method now runs
through `db::blocking`, which uses `tokio::task::block_in_place` on a multi-thread runtime,
so the worker hands its core to another thread while the call waits or commits (a
`macro_rules!` wraps the whole `impl Db`, so no call site changed). The connection
is handed out first come first served through a queue (`db::Gate`; each waiter sleeps on
its own condition variable and the releasing caller wakes only the head of the queue). A
caller waits at most 3 s (`db::set_lock_wait` for tests) and at most `max_connections` + 32
callers may wait (every connection has one call in flight; 288 by default); beyond either
the caller gets `SQLITE_BUSY`, which HTTP and the worker
upgrade report as 503 `database-busy` with `Retry-After`, and the worker channel closes
with 1011 and reconnects. `/healthz` never touches the database. Rejected: a dedicated
database thread with a queue (every call site would have to become async, and the
authentication helpers called from synchronous extractors would cascade). A poisoned
connection mutex is recovered, and the waiter count is released on every exit path, so
serving continues when the slow commit ends; `coordinator/tests/db_stall.rs` checks that,
and the unit tests in `db.rs` (`lock_wait_tests`) check the bounds.

The first version of this bound polled `try_lock` with a short sleep and allowed 48 waiters.
That was wrong in two ways, found when the login-burst test in `hardening.rs` (60 concurrent
sign-ins) answered 503 on the Windows runners and `coordinator/tests/db_lock_burst.rs` showed
the same on Linux: 2 of 60 requests refused with `too many requests are waiting`, peak 49
waiters, while the longest hold of the connection was 0.23 ms. Nothing slow runs under the
connection on the login path (the password hash runs in `verify_blocking`, outside it; the
locked sections are one read, one small write and the audit insert). A burst of N requests
queues about N callers at once because they all arrive within a millisecond, and polling made
the queue drain slowly and unfairly (on Windows a sleep lasts about 15 ms; with a 15 ms floor on
Linux 7 of 60 were refused). Measured after the change: bursts of 60, 256 and 288 requests
queue a peak of 56, 247 and 260 callers, none is refused, longest hold 0.8 to 3.6 ms. The limit
now follows `max_connections` (the number of calls that can be in flight), clamped to 480 so the
waiting threads stay below the 512 of tokio's blocking pool.

Reads on their own connections. A control run of the serving test without any backup failed on the
hosted Windows runners (event batch commits of 0.8 to 3 s, `/api/v1/auth/state` answering 503), so the
single connection was the bottleneck even without a backup: a read queued behind a committing write.
Reads now use up to four read-only connections (`PRAGMA query_only`) to the same WAL file, opened on
demand (`Db::r`). A read runs from the snapshot committed when its statement began and does not wait for the
writer or for a backup. Each `Db` method is one or the other: the lists, `admin_get`, `events_list`,
`worker_get` and the like read only; a method that reads and then maybe writes does its read on a reader and
takes the writer only for the write (`session_get` deletes an expired session or touches `last_seen`,
`token_lookup`, `cursor_key`, `known_addr` writes the salt once). Writes keep one connection with the
first-come queue and the 3 s bound. A read waits (at most 3 s) only when all four read connections are in
use by other reads, which take microseconds. Measured with `a_slow_commit_...` in `db_stall.rs`, a second
connection holding SQLite's write lock for 6 s and one write waiting in SQLite's busy handler: before,
`/api/v1/auth/state` answered 503 after 3.0 s and the workers list waited 1.5 s; after, the auth state,
workers, missions and events lists answer 200 in 1 to 2 ms. A worker connect, which only checks the token,
is admitted at once instead of getting 503; its hello (a write) waits. Test expectations that said
"queued reads get 503" were changed accordingly. Calls that take 500 ms or more, and retention steps that
hold the connection that long, are written to stderr (`slow database call <name>`); that log stays because
it names the call that held the writer.

Reads after writes. With reads on their own connections, a read no longer queues behind a write that
is already under way, so a client could see state older than a write that the coordinator had just made.
Found on the Windows runners: `unanswered_reads_fail_when_the_worker_disconnects_and_are_not_replayed`
polled a read-only command after the worker was seen offline (the in-memory hub changes first) and got
`sent` because the disconnect's commit was still running; with a 400 ms delay in that commit the test fails
every time on a plain reader and passes with the change below. Two changes: the disconnect writes the
worker row and finishes the worker's unanswered reads in one transaction (`Db::worker_gone`), and the
command read (`command_get`) uses `Db::r_after_writes`, which waits (at most 3 s, 503 `database-busy`
after that) until the writes that had asked for the writer connection when the read began have finished
or given up. Other reads, including the worker, mission, event and auth lists, never wait for a writer, so
they can lag a commit that is still running; the commits that these reads must follow (the worker hello)
finish before the coordinator answers the worker. Regression test:
`a_read_command_polled_while_the_disconnect_is_committing_is_seen_finished` in `tests/worker.rs` (fails on a
plain reader), plus a unit test in `db.rs`.

Backup priority outside Linux. On Linux the backup runs at nice 19 and in the idle I/O class. Windows uses
background mode (`PROCESS_MODE_BACKGROUND_BEGIN`; the copy then ran at 1.4 to 2 MiB/s). macOS uses nice 19
only: with the Darwin background class (`PRIO_DARWIN_BG`), which also throttles disk I/O, the copy and its
integrity check on the macOS ARM64 runner made no progress for over a minute (run 42457434409: copy done in
27 s, `integrity_check` unfinished after 63 s more) while the coordinator kept writing. The write cap limits
the copy's I/O there.

The copy's flush interval stays 1 MiB on every platform: a 32 MiB interval on macOS and Windows made the
copy stop at 60 of 98 MiB on the macOS ARM64 runner (no progress for 80 s).

Remaining limit: a commit that holds the writer connection for longer than the wait still makes
requests that write answer 503 for its duration (reads are not affected). Page-cache write stalls on the
card itself are outside what the process can control; the backup no longer causes them.

## D30 Merging main into the web redesign: digest and attention, notices, load time and the lazy JavaScript cap (PRD-037, PRD-038, UXR-032, UXR-039)

The redesign branch was merged with main after PR #35 (the release, with the DOG-021 mission digest and the coordinator's change notices) and PR #68 (mission delete).
This entry records what the merge decided.

| # | Decision | Change | Guarded by |
|---|---|---|---|
| 1 | One list answers what a row needs | A worker sends both `attention` (PRD-037 WP-7, for every mission that is not completed or cancelled, with the bounded message) and the digest (`task_counts`, `current_task`, `station`, `model`, `needs_kind`, `needs_tasks`, and `needs_you` for a live mission). `store.listedNeeds` reads `attention` first, then `needs_you` or `needs_kind`; only a mission for which the list says neither (a worker that predates both) is read with `mission.status`, at most 12 at a time. | `web/e2e/link-traffic.spec.ts`, `web/e2e/worker.spec.ts`, `tests/test_bridge_mission_summary.py`, `tests/test_bridge_worker.py` |
| 2 | The mission page asks a worker only for tasks | Its status is drawn from the digest of the list entry, its events come from `GET /api/v1/events` of the coordinator, and a change notice that names the mission reads the new events; the tasks are read again only for a notice with the mission in `task_missions` and when the digest of the mission changed. A notice with `truncated` reads the events as well. A worker without the digest is asked for the status as before. | `web/e2e/link-traffic.spec.ts` (the count of status reads is 0), `uxr-fresh-eyes.spec.ts` F9 for both kinds of worker |
| 3 | The activity line of a running row | `mission.activity_line` (spec DEV-5) is still asked for each running row, once, when the row's update time or current task changed, and for every row on the 30 s refresh of the page; a change notice alone does not ask again. | `web/e2e/link-traffic.spec.ts` |
| 4 | Delete mission is in the overflow menu | `data-action="delete-mission"`, danger style, only for CREATED, COMPLETED, FAILED, CANCELLED and STOPPED, disabled with the reason when the worker is unreachable. The confirmation says "Delete mission?", names the mission and the worker and says that it cannot be undone. After the delete the console shows the missions list with a toast that survives the route change. | `web/e2e/mission-delete.spec.ts` |
| 5 | Security notices | They are behind a header button with their count (a banner above the page moved the content after load: CLS 0.17 in Lighthouse). The code is a separate chunk, fetched after the first paint. | `web/e2e/auth-ux.spec.ts`, `web/e2e/passkey-login.spec.ts` |
| 6 | Load time (T37) | A late sentence about the verifier or a late terminal chunk set the largest contentful paint. The sentence has its line from the first paint; the verifier text of an Inbox row is a box of its own; the terminal chunk is fetched 100 ms after the second frame and its evaluation and the building of the pane are two tasks; the mission route imports its page module directly; `index.html` preloads the first-load chunks and the sign-in check; the build writes a gzip copy of every text asset and the coordinator sends it to a client that accepts gzip (`coordinator/src/web.rs`, `tests/web.rs`). | `web/e2e/redesign/uxr-lighthouse.spec.ts`, `coordinator/tests/web.rs`, `uxr-text.spec.ts` (T08) |

Lighthouse (mobile preset, simulated throttling, Playwright image, one run per page; each run varies by about 100 ms):

| Page | Before the fixes (merge head) | Now |
|---|---:|---:|
| Inbox | LCP 2944 ms, performance 85, CLS 0.169 | LCP 2131 and 2137 ms, performance 98, CLS 0.004 |
| Missions | LCP 2858 ms, performance 86 | LCP 1962 and 1976 ms, performance 98 and 99 |
| Mission page | LCP 6098 ms, performance 72, TBT 230 ms | LCP 2332 and 2347 ms, performance 96 and 97, TBT 93 and 100 ms, CLS 0.007 |

Where the browser tests run (first CI run on this branch, which had never run in CI): the layout, accessibility and timing specs of PRD-037 measure text and boxes, so they only
hold with the fonts of the Playwright image. On the Ubuntu runner the top bar, the capacity chip, the phone action row and the short fields of the mission page measured differently
(T35, T48 D28.2 and D28.4, T08), and the terminal and worker specs, which need tmux and Python, had been skipped in the image without anyone noticing: they failed against the
redesigned session pane (the reason a viewer cannot type is in the composer, not in the status line). The job `web` now runs the four standard projects, the matrix and the
click-path projects inside the image (tmux and a venv are installed in the container), the macOS and Windows jobs leave out `e2e/redesign` and the screenshots (a project's own
`testIgnore` had replaced the config's, so they were not left out before), T36 allows 150 ms off Linux (88 ms measured on the macOS runner, 50 ms on Linux), T37 has its own
project (`--project=lighthouse`) because it measures, and two terminal measurements of the mission page that depend on the row height of xterm.js in Firefox (T29 rows above
the fold, T32 text spacing) are not made there; they fail the same way on 3f3105b.

The lazy JavaScript cap is met without raising it (UXR-039). Main's passkey code and this branch's security notices, delete action and digest handling had taken the route
JavaScript to 107324 raw and 41438 gzip bytes against the caps of 94000 and 38700. The caps in `web/budget.json` are unchanged. The bytes were cut as follows:

| Measure | Before the merge (D28) | Merge, before the cut | Now | Cap |
|---|---:|---:|---:|---:|
| Initial JavaScript raw / gzip | 80464 / 31326 | 81701 / 31392 | 82357 / 31710 | 91000 / 31750 |
| Initial CSS raw / gzip | 33949 / 7550 | 33997 / 7576 | 33997 / 7576 | 34000 / 9000 |
| Terminal chunk raw / gzip | 355631 / 92441 | 355660 / 92450 | 355580 / 92423 | 360000 / 93000 |
| Route JavaScript raw / gzip | 93984 / 36138 | 107324 / 41438 | 93884 / 37826 | 94000 / 38700 |
| Route CSS raw / gzip | 18358 / 4869 | 18358 / 4869 | 18358 / 4869 | 18400 / 4900 |

- The sources of the lazy pages were reduced by about 12000 raw bytes: shared helpers for error messages, dialogs, field validation, fact lists and button rows; tables instead of
  repeated branches; the same function for near-identical code. No text, element, attribute or request changed. Gzip fell by only about 1200 bytes, because most of the removed bytes
  were repetition that gzip had already compressed.
- The gzip cap was the harder one. Most of its saving came from fewer chunks (every chunk is compressed on its own and starts with its imports): sign-in, setup, forced password change,
  account, workers, sessions, usage and diagnostics are one lazy chunk (`views/routes.ts`; each route still loads only its own stylesheet), and `passkeys.ts` is part of it instead of a chunk of
  its own. A visitor who is not signed in now fetches the fleet pages with the sign-in page (about 8 KB gzip more, on a page that no performance budget measures); the mission page and the
  two pages that T37 measures are unchanged.
- The mission page and the steer chips pass new events through a document event instead of a tiny shared module, and the mission page awaits the terminal stylesheet next to the terminal
  chunk import instead of a top-level await in the terminal module. Both removed a chunk that held a few hundred bytes.
- The event phrase table is generated as one string and leaves out the kinds that the browser derives from the kind name (26 of 84).
- The initial JavaScript gained 340 gzip bytes from the in-place update of the live lists (D31) and has 40 bytes of room left; the raw route JavaScript has 116 bytes of room left.

Merging a later main again needs no change here as long as it adds no web code.

## D31 Live lists update rows in place (DOG-026)

PR #70 found that the mission page redrew the tasks table several times in the half second after each task action, replacing the buttons, so that a click whose pointer-down and
pointer-up straddled a redraw never fired (seen on Firefox, 1 run in 20 in `worker.spec.ts`, which then retried the click). An operator loses the same click, so this is a defect
of the product and not of the test.

Before: the Inbox and the Missions list kept a row whose signature was unchanged but replaced a changed row; the tasks panel, Workers and Sessions rebuilt their whole content on
each redraw.

Now: `morph(old, fresh)` and `patch(parent, children)` in `web/src/dom.ts` make an element equal to a freshly built one without replacing it. A child is kept when its identity is the same
(`data-row-key`, `data-entity`, `data-action`, `id`, else its class); its attributes, text and listeners are patched. `h()` and `button()` register listeners through `on()`, which keeps
one dispatcher per event type on the element and lets `morph` hand it the function built for the new data, so a kept button acts on the latest revision of its task and not on the one
it was built with. A handler must not close over an element of the row it was built for (the Retry button and the row's details toggle take their element from the event). Things the user
changes by using the control are not overwritten: an open menu popover (`data-keep`), `open` and `aria-expanded`, and the open state of a row's details (`data-own`, `data-leaf`).
`reconcile` skips a row whose signature is unchanged and patches a changed one; the mission tasks panel, Workers, Sessions and the fleet pages call `patch`.

Why a guard that postpones the redraw while the pointer is pressed was not used: it would leave stale text on a row the pointer rests on (the pill after Pause), and the focused
element would still be replaced by a redraw that follows.

Not covered: the buttons of the mission header are rebuilt when the header data changes; they are not a list, and the header changes only on a change of the mission state.

Guarded by `web/e2e/live-lists.spec.ts` (Tasks, Inbox, Missions, Workers, Sessions; chromium-desktop and firefox-desktop) and by `web/e2e/link-traffic.spec.ts` (the tasks panel is redrawn
once for a change of its data). With the identity of every node replaced by a new value, nine of the ten tests of `live-lists.spec.ts` fail; the Missions test on firefox-desktop still
passes on revert and is the weakest of the set.

## D40 Release split (TRACK-013)

Release 1.5.0 ships the distributed Bridge, the web redesign (PRD-037) and the release gates. Release 1.6.0 ships provider parity (`PROV`) and orchestration (`ORCH`), together with `LB-004`. Reason: about 560 commits are unreleased since 1.4.0, and the 1.6.0 work changes the store schema and the controller loop. `scripts/spec_open.py` encodes this membership.

Numbering: D30 to D39 are left for the decisions that pull requests #57 and #64 add on their branches; they are renumbered when those pull requests merge (`TRACK-017`). D18 and D28 do not exist.

## D41 Status vocabulary (TRACK-013)

The register gains `[w]` (waived by a recorded decision; counts as closed) and `[d]` (deferred to a named target release by a recorded decision; closed for earlier releases and open for the target). A `[w]` row cites its decision; a `[d]` row cites its decision and `target <version>`. `[x]` is used only for verified work.

## D42 Deferred beyond 1.6.0 (ORCH-007, ORCH-010)

Parallel tasks inside one mission (`ORCH-007`) and mission migration between workers (`ORCH-010`) are deferred to 1.7.0. Parallel work is provided by several isolated missions in one repository (`ORCH-001`, `ORCH-002`), which needs no change to the one-task-at-a-time controller.

## D43 Multi-user access rejected for now (ORCH-011)

Multi-user role-based access and OIDC are not planned. Bridge is a single-operator product; `AUTH-009` stays as it is.

## D44 Last provider line on a fast exit (DOG-025)

`DOG-025` stays deferred for 1.5.0 and is listed as a known limitation (as D27 decided), but it is a 1.6.0 requirement because the handoff package of `ORCH-004` depends on the previous provider's last message.

## D45 Independent review (PRD-023, REV-004)

`PRD-023` and `REV-004` require a reviewer that is not the implementing model. One Codex review session per release, together with the existing ZAP scan, satisfies this. Findings and their dispositions are recorded in `docs/distributed/review-findings.md`.

## D46 Waivers and deferrals of existing rows

- `PRD-022` (internationalisation readiness of the web console) is deferred to 1.7.0. No translation is planned for 1.5.0 or 1.6.0.
- `PI-UPD-004`, `PI-UPD-009`, `PI-DEPLOY-007`, `PI-VERIFY-007`, `PI-VERIFY-008` and `PI-FLASH-007` are waived. Each row states the hardware reason: no newer kernel package to trigger a real automatic reboot, no phone with both browsers, a Wi-Fi interruption test that removes the only management link, an unplug test that needs a person at the device, and an optional USB gadget fallback that was not needed.
- `PK-012`, `PI-VERIFY-013` and `DOG-023` change from `[x]` to `[w]`, because they were closed by the operator decisions of 2026-11-07, not by verification.
- Mouse clicks inside native provider panes (`LB-003`) are waived; the mouse wheel is supported.

Acceptance: on 2026-11-12 the operator delegated all remaining decisions to the lead ("Make all decisions like a principal engineer and product owner would"). D40 to D47 are accepted under that delegation, without a veto.

## D47 Remote verifiers (ORCH-008)

A mission created through the coordinator may name only a verifier that the worker's own configuration declares. A verifier command or verifier file is never accepted over the network, because it would let any holder of a control token run arbitrary commands on the worker.

## D48 Faults of the UXR-040 critique are registered as UXR-053 to UXR-057 (UXR-040)

The fresh-eyes critique of every console page at 1440 and 360 pixels, in both themes, found five faults: an incomplete quota pill at 360, a clipped worker filter at 360, an approval reason cut mid-word and a wrapping pill in the Inbox at 1440, a blank provider cell on failed mission rows, and no sideways-scroll cue on the phone terminal. UXR-040 requires every finding to be filed as a UXR row and closed with proof, so they become UXR-053 to UXR-057. They are fixed with the web console work (W4), which can regenerate the Pixel 4 screenshot baselines in the Playwright image. UXR-040 stays open until they are closed.

## D49 Rich is the only runtime dependency (FIND-017)

`AGENTS.md` asks for standard-library-first code, and 1.2.0 added Rich. Rich stays, as the only runtime dependency, because writing a Markdown renderer for the document viewer would be more code to maintain than the dependency costs. It is imported only inside the Markdown and ANSI renderers of `llm_tools/bridge_documents.py`, never at module level, and both renderers fall back to plain text when it is missing, so every other command works without it. Tests in `tests/test_release_hygiene.py` hold all three rules.

## D50 Menus replaced by a live refresh are a registered defect (DOG-028)

The `terminal.spec.ts` failure on firefox-desktop in the CI of pull request #57 was a product defect, not a flaky test: a live refresh replaced the open session menu and the mission header menu. It is registered as `DOG-028` and fixed in the redesign branch, so the redesign does not merge with a known menu defect.

## D51 Soak memory gate and remaining phase-B soak items (FIND-001, FIND-005)

This replaces the 1200 s warm-up recorded in D16. The controller memory slope is fitted after a 1800 s warm-up, and the nightly soak runs 2700 s so that the fit window lies between two 900 s retention passes. The recorded nightly series show no leak: the failures came from a fit window that ended on a retention peak. A 900 s window cannot resolve a controller leak below about 2 MiB/h; the long nightly run covers that range.

The soak items `coordinator-task-count`, `clock-jump-native` and `stale-journal-after-restore` are deferred to 1.6.0. Each needs a coordinator change (a task count in diagnostics, a native clock source, or a query of unfinished commands per worker). Until then the thread count, the `LD_PRELOAD` clock shim and the `db-corrupt-page` restore fault cover them in part.

## D52 Lost frames and lost audit writes in the coordinator (FIND-003, FIND-004)

- A lost command or result frame is repaired by re-sending, not by a protocol change. The coordinator re-sends a mutating command that has been idle for 10 s, checked every 5 s. The worker is responsible for running each mutating request id at most once: it records every mutating result in a private outcome ledger before it answers, and answers a duplicate from the ledger. A crash after the effect and before the record can still run a command once more after a restart; `protocol.md` states this window.
- A failed audit write fails the action where the action and its audit row can share one transaction (`command.submit`). Where they cannot, because the action has already taken effect, the failure is reported as a health fault (`/healthz` 503 `audit-write-failed`) until an audit write succeeds again.

## D53 Old mode names are normalised on read, not migrated (FIND-018)

Controllers before 1.5.0 recorded Background mode as `autonomous` in `controller-mode` events. The rename is applied when events are read, in one store-level function, instead of rewriting stored rows in a schema 11 migration. A migration would make every older `llm-bridge` refuse the database for a cosmetic change, force a full backup copy and a scan of the events table under the write lock, and leave local rows different from the copies the coordinator already holds. Schema 11 stays free for the structural change of `ORCH-002`.

## D54 The FINAL_REPORT count table is checked at a release, not on every pull request

Every pull request that changed a register status also had to change the same lines of the `FINAL_REPORT.md` count table, so each merge made every other open pull request conflict and restarted its CI. The count table is now updated in the release commit, and the release workflow runs the count checks with `SPEC_RELEASE_CHECK=1` before a tag is built. Pull requests change only `SPEC_STATUS.md`; `scripts/spec_open.py` reports the live open IDs at any time.
