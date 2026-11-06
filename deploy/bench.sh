#!/bin/sh
# Run the coordinator measurement harness (release build) and print the Markdown table.
# Usage: deploy/bench.sh [OUTPUT_FILE]
# Starts the real binary on 127.0.0.1 with a temporary data directory; nothing else is touched.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
target=${CARGO_TARGET_DIR:-/tmp/llmtools-run/cargo-target}
[ $# -ge 1 ] && export BENCH_OUT="$1"
cd "$root/coordinator"
CARGO_TARGET_DIR=$target cargo test --release --locked --test bench -- --ignored bench --nocapture
