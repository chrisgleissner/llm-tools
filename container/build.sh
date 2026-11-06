#!/bin/sh
# Build the coordinator OCI image for linux/amd64 and linux/arm64.
#
# The Rust binary is cross-compiled on this host as a static musl executable
# with cargo-zigbuild, and the web files are built with npm; the Dockerfile
# only copies them (FROM scratch), so no step runs inside an emulated
# container. Sources come from `git archive` of a commit, timestamps from that
# commit (SOURCE_DATE_EPOCH), so a rebuild of the same commit gives the same
# binaries and image layers.
#
# Usage: container/build.sh [--ref COMMIT] [--tag NAME] [--load | --oci FILE | --push] [--metadata FILE]
#   --load       load the image for this host's architecture into Docker (default)
#   --oci FILE   write a multi-architecture OCI archive (amd64 + arm64) to FILE
#   --push       push the multi-architecture image to the registry in --tag
# Needs: git, cargo with cargo-zigbuild and zig, rustup targets
# x86_64-unknown-linux-musl and aarch64-unknown-linux-musl, node/npm, docker buildx.
set -eu

ref=HEAD
tag=llm-bridge-coordinator:latest
mode=load
oci=
metadata=
while [ $# -gt 0 ]; do
    case "$1" in
        --ref) ref=$2; shift 2 ;;
        --tag) tag=$2; shift 2 ;;
        --load) mode=load; shift ;;
        --oci) mode=oci; oci=$2; shift 2 ;;
        --push) mode=push; shift ;;
        --metadata) metadata=$2; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
commit=$(git -C "$repo" rev-parse --verify "$ref^{commit}")
SOURCE_DATE_EPOCH=$(git -C "$repo" log -1 --format=%ct "$commit")
export SOURCE_DATE_EPOCH
work=${BRIDGE_BUILD_DIR:-${TMPDIR:-/tmp}/bridge-image-build}
rm -rf "$work/src" "$work/context"
mkdir -p "$work/src" "$work/context/bin/amd64" "$work/context/bin/arm64" "$work/context/data"
git -C "$repo" archive "$commit" rust-toolchain.toml coordinator web container | tar -x -C "$work/src"

target_dir=${CARGO_TARGET_DIR:-$work/cargo-target}
for pair in amd64:x86_64-unknown-linux-musl arm64:aarch64-unknown-linux-musl; do
    arch=${pair%%:*}
    triple=${pair#*:}
    (cd "$work/src/coordinator" && CARGO_TARGET_DIR=$target_dir \
        RUSTFLAGS="--remap-path-prefix=$work/src=/src --remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo" \
        cargo zigbuild --release --locked --target "$triple")
    cp "$target_dir/$triple/release/bridge-coordinator" "$work/context/bin/$arch/"
done

(cd "$work/src/web" && npm ci --no-audit --no-fund && npm run build)
cp -R "$work/src/web/dist" "$work/context/web"
cp "$work/src/container/Dockerfile" "$work/context/Dockerfile"

set -- docker buildx build --provenance=false --build-arg SOURCE_DATE_EPOCH \
    --label "org.opencontainers.image.revision=$commit" -t "$tag"
case "$mode" in
    load) set -- "$@" --load ;;
    oci) set -- "$@" --platform linux/amd64,linux/arm64 --output "type=oci,dest=$oci,rewrite-timestamp=true" ;;
    push)
        set -- "$@" --platform linux/amd64,linux/arm64 --output "type=image,push=true,rewrite-timestamp=true"
        if [ -n "$metadata" ]; then set -- "$@" --metadata-file "$metadata"; fi
        ;;
esac
"$@" "$work/context"
echo "built $tag from $commit"
