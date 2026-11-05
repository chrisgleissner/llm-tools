# Distributed Bridge decisions

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

