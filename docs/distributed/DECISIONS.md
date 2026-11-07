# Distributed Bridge decisions

## PK-003 WebAuthn core

Use `webauthn-rs` 0.6.2 (Kanidm, MPL-2.0) for server-side WebAuthn verification. It pulls `rsa 0.10.0-rc.19` through `crypto-glue 0.2.2`, which cargo audit and cargo deny flag for RUSTSEC-2023-0071 (Marvin timing attack); the advisory has no fixed RSA version. The 0.6.2 stack has no feature to remove RSA while preserving its verifier. Its 0.5.5 maintenance line removes `rsa`, but replaces it with system OpenSSL crypto and a dynamic OpenSSL runtime dependency, breaking the coordinator's static `FROM scratch` image and cross-target build. Keep 0.6.2 and narrowly ignore the advisory because the coordinator uses only RSA public-key signature verification for authenticator assertions; it creates, stores, signs with and decrypts with no RSA private key. The exact ignore is applied in both audit tools and must be revisited if that call path or dependency changes.

The library exposes typed registration/authentication state and credential types, verifies origins/RP IDs and authenticator assertions, and documents that ceremony state must be persisted. The coordinator serializes that state only in its private SQLite database, with a single-use session/setup-bound challenge row capped at five minutes. The `danger-allow-state-serialisation` feature is required because the library deliberately gates persistence of in-flight ceremony state behind an explicit opt-in; the state remains private and is never returned to a client.

The coordinator now wires registration and assertion start/finish routes through the library and persists the returned passkeys and verifier state. This is still partial: `start_passkey_registration` hardcodes `require_resident_key(false)` and the wrapper exposes no general resident-key option. The alternate resident-key helper requires direct attestation and rejects synchronized authenticators, so it does not satisfy the requested profile. Do not mutate the generated options as a workaround: the verifier state would not match what it can actually validate. Expose a configurable no-attestation/required-resident-key method through a maintained upstream version or a narrow vendored wrapper before claiming this requirement. Canonical-origin configuration, AAGUID extraction, and fixed-vector ceremony verification plus explicit ES256/Ed25519/RS256 policy verification also remain. The exact RUSTSEC-2023-0071 exception is limited to this reviewed public-key-only call path; do not broaden it to other advisories.

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
| Initial JavaScript, raw / gzip | 81869 / 31595 | 91000 / 31750 (unchanged) |
| Initial CSS, raw / gzip | 29105 / 6583 | 34000 / 9000 (unchanged) |
| Terminal chunk, raw / gzip | 354858 / 92148 | 360000 / 93000 (unchanged) |
| Route JavaScript (all lazy chunks but the terminal), raw / gzip | 89710 / 36952 | 94000 / 38700 |
| Route CSS (`fleet`, `auth`, `mission`), raw / gzip | 17613 / 4682 | 18400 / 4900 |

The initial gzip figure is 155 bytes below its cap, so any further first-load code needs a matching removal.

Per-chunk figures (raw / gzip) and the package that added each one:

| Chunk | Raw / gzip | Added by |
|---|---:|---|
| `views/mission` (header, tasks, evidence, activity, about, page) | 31329 / 11554 | WP-3 |
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
| four re-export shims | 1000 / 740 | esbuild |

The session pane itself is part of the terminal chunk (xterm.js plus `session.ts`, `composer.ts`); the page reserves its box
with `sessionReserve()` (`session-box.ts`, a few bytes in the first load) until that chunk arrives. Route CSS: `auth`
3313 / 982, `fleet` 3639 / 1003, `mission` (session.css and mission.css) 10458 / 2658 at the first measurement.

Dead code removed at integration: the `statusBadge` shim, `sortHeader`, the legacy table CSS (`table`, `table.rt`,
`table.kv`, `.table-wrap`, `.sort`), the `.grid-auto`, `.pane-split` and `.grid-2` helpers and `@xterm/addon-fit`.
