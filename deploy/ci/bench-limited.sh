#!/bin/bash
# Run the coordinator measurement harness (coordinator/tests/bench.rs, release build) inside one
# container limited to Pi-like resources: the harness client and the coordinator it starts share
# the limit, so the figure covers the whole stack. Writes the Markdown table to $1 (default
# bench-limited.md) and prints the container's peak memory.
# Environment: BENCH_MEMORY (default 512m), BENCH_CPUS (default 1), BENCH_IMAGE (default ubuntu:24.04,
# the same glibc as the ubuntu-24.04 runners, because the test binary is dynamically linked).
set -eu
root=$(cd "$(dirname "$0")/../.." && pwd)
out=$(realpath -m "${1:-bench-limited.md}")
mem=${BENCH_MEMORY:-512m}
cpus=${BENCH_CPUS:-1}
image=${BENCH_IMAGE:-ubuntu:24.04}
target=${CARGO_TARGET_DIR:-$root/coordinator/target}
cd "$root/coordinator"
exe=$(CARGO_TARGET_DIR=$target cargo test --release --locked --test bench --no-run --message-format=json |
    jq -r 'select(.executable != null and .target.name == "bench") | .executable' | tail -1)
[ -x "$exe" ] || { echo "bench test executable not found" >&2; exit 1; }
work=$(mktemp -d)   # on disk, so the database does not count as RAM disk memory
chmod 777 "$work"
name=bench-limited-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
# The binary embeds the absolute path of the coordinator it starts, so the target directory is
# mounted read-only at the same path.
docker run -d --name "$name" --memory "$mem" --memory-swap "$mem" --cpus "$cpus" \
    -v "$target:$target:ro" -v "$work:/work" -e TMPDIR=/work -e BENCH_OUT=/work/table.md \
    -e RUST_BACKTRACE=1 "$image" "$exe" --ignored bench --nocapture >/dev/null
id=$(docker inspect -f '{{.Id}}' "$name")
cg=
peak=0
while [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ]; do
    [ -n "$cg" ] || cg=$(find /sys/fs/cgroup -maxdepth 7 -type d -name "*$id*" 2>/dev/null | head -1)
    if [ -n "$cg" ] && [ -r "$cg/memory.peak" ]; then
        v=$(cat "$cg/memory.peak" 2>/dev/null || echo 0)
        [ "$v" -gt "$peak" ] && peak=$v
    fi
    sleep 0.5
done
docker logs "$name" 2>&1 | tail -40
code=$(docker inspect -f '{{.State.ExitCode}}' "$name")
oom=$(docker inspect -f '{{.State.OOMKilled}}' "$name")
[ "$code" = 0 ] || { echo "bench exited with $code (OOMKilled=$oom)" >&2; exit 1; }
{
    echo "Limits: --memory $mem (swap equal), --cpus $cpus, harness client and coordinator in one container."
    echo
    cat "$work/table.md"
    echo "| Container peak memory (\`memory.peak\`, includes page cache) | $(awk -v p="$peak" 'BEGIN { if (p > 0) printf "%.1f MiB", p / 1048576; else print "unavailable" }') |"
    echo "| OOM killed | $oom |"
} >"$out"
cat "$out"
