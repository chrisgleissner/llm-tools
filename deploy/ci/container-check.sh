#!/bin/bash
# Container checks against an already built image, driven through container/compose.yaml on loopback.
#   deploy/ci/container-check.sh smoke      init, health, /healthz, 401, info, stop time, restart persistence
#   deploy/ci/container-check.sh pressure   login burst + concurrent GETs under BRIDGE_MEMORY_LIMIT
# Environment: BRIDGE_IMAGE (default llm-bridge-coordinator:latest), BRIDGE_PORT (default 18443),
# COMPOSE_PROJECT_NAME (default bridgeci), BRIDGE_MEMORY_LIMIT (compose default 96m),
# SUMMARY_FILE (Markdown lines are appended there as well as printed).
# Needs docker compose and curl. The project volume is removed at the start and the end.
set -eu
mode=${1:?usage: container-check.sh smoke|pressure}
root=$(cd "$(dirname "$0")/../.." && pwd)
export BRIDGE_PORT=${BRIDGE_PORT:-18443}
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-bridgeci}
export BRIDGE_IMAGE=${BRIDGE_IMAGE:-llm-bridge-coordinator:latest}
SUMMARY_FILE=${SUMMARY_FILE:-/dev/null}
PASSWORD="correct horse battery"
work=$(mktemp -d)
dc() { docker compose -f "$root/container/compose.yaml" "$@"; }
now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.2f", b - a }'; }
say() { echo "$*"; echo "$*" >>"$SUMMARY_FILE"; }
fail() { echo "FAIL: $*" >&2; dc logs --tail 40 coordinator >&2 || true; exit 1; }
cleanup() { dc down -v --timeout 20 >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
base=https://localhost:$BRIDGE_PORT
curlc() { curl -sS --max-time 20 --cacert "$work/ca.pem" "$@"; }

container_id() { dc ps -q coordinator; }
check_web_permissions() {
    id=$(container_id)
    [ -n "$id" ] || fail "coordinator container is missing before web permission check"
    docker export --output "$work/rootfs.tar" "$id" || fail "could not export the coordinator root filesystem"
    python3 - "$work/rootfs.tar" <<'PY' || fail "web asset directories are not traversable or files are not readable by UID 65532"
import sys
import tarfile

root = "usr/share/bridge-coordinator/web"
required_dirs = {
    "usr",
    "usr/share",
    "usr/share/bridge-coordinator",
    root,
}
with tarfile.open(sys.argv[1], "r:") as archive:
    members = {member.name.rstrip("/"): member for member in archive.getmembers()}
    missing = sorted(path for path in required_dirs if path not in members or not members[path].isdir())
    bad_dirs = sorted(
        path for path, member in members.items()
        if path == root or path.startswith(root + "/")
        if member.isdir() and member.mode & 0o005 != 0o005
    )
    web_files = [
        member for path, member in members.items()
        if path.startswith(root + "/") and member.isfile()
    ]
    unreadable = sorted(member.name for member in web_files if member.mode & 0o004 == 0)
    if missing or bad_dirs or unreadable or not web_files:
        print(f"missing_dirs={missing}; bad_dirs={bad_dirs}; unreadable={unreadable}; files={len(web_files)}", file=sys.stderr)
        raise SystemExit(1)
PY
    say "| Web asset directories traversable and files readable by UID 65532 | yes |"
}
wait_healthy() { # prints seconds to healthy
    start=$(now)
    for _ in $(seq 1 240); do
        id=$(container_id)
        status=$([ -n "$id" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" || echo none)
        [ "$status" = healthy ] && { elapsed "$start" "$(now)"; return 0; }
        sleep 1
    done
    fail "container did not become healthy within 240 s"
}
wait_https() { # the health check interval is 30 s; poll /healthz directly for the first answer
    start=$(now)
    for _ in $(seq 1 120); do
        [ "$(curlc -o /dev/null -w '%{http_code}' "$base/healthz" 2>/dev/null || true)" = 200 ] && { elapsed "$start" "$(now)"; return 0; }
        sleep 0.5
    done
    fail "/healthz did not answer 200 within 60 s"
}
setup_admin() {
    dc cp coordinator:/data/setup-token "$work/setup-token"
    code=$(curlc -o /dev/null -w '%{http_code}' -H 'content-type: application/json' \
        -d "{\"setup_token\":\"$(tr -d '\n' <"$work/setup-token")\",\"username\":\"admin\",\"password\":\"$PASSWORD\"}" \
        "$base/api/v1/auth/setup")
    [ "$code" = 200 ] || [ "$code" = 201 ] || fail "administrator setup returned $code"
}
login_code() { curlc -o /dev/null -w '%{http_code}' -c "$1" -H 'content-type: application/json' \
    -d "{\"username\":\"admin\",\"password\":\"$PASSWORD\"}" "$base/api/v1/auth/login"; }
memory_peak_mib() {
    id=$(container_id)
    full=$(docker inspect -f '{{.Id}}' "$id")
    cg=$(find /sys/fs/cgroup -maxdepth 7 -type d -name "*$full*" 2>/dev/null | head -1)
    if [ -n "$cg" ] && [ -r "$cg/memory.peak" ]; then
        awk '{ printf "%.1f", $1 / 1048576 }' "$cg/memory.peak"
    else
        echo "unavailable"
    fi
}

dc down -v --timeout 20 >/dev/null 2>&1 || true
dc run --rm coordinator init >"$work/init.out"
t0=$(now)
dc up -d
check_web_permissions
dc cp coordinator:/data/ca.pem "$work/ca.pem"
https_s=$(wait_https)

case "$mode" in
smoke)
    healthy_s=$(wait_healthy)
    say "| Container started to first \`/healthz\` 200 | ${https_s} s |"
    say "| Container started to Docker health \`healthy\` (interval 30 s) | ${healthy_s} s |"
    [ "$(curlc -o /dev/null -w '%{http_code}' "$base/healthz")" = 200 ] || fail "/healthz is not 200"
    code=$(curlc -o /dev/null -w '%{http_code}' "$base/api/v1/missions")
    [ "$code" = 401 ] || fail "unauthenticated /api/v1/missions returned $code, expected 401"
    say "| Unauthenticated \`GET /api/v1/missions\` | $code |"
    dc exec -T coordinator bridge-coordinator info >"$work/info.out" || fail "info failed"
    grep -qi "fingerprint\|https://" "$work/info.out" || fail "info output has no URL or fingerprint"
    say "| \`docker compose exec coordinator bridge-coordinator info\` | ok |"
    setup_admin
    [ "$(login_code "$work/jar")" = 200 ] || fail "login before restart failed"
    before=$(sha256sum "$work/ca.pem" | cut -d' ' -f1)
    s=$(now); dc stop >/dev/null; e=$(now)
    stop_s=$(elapsed "$s" "$e")
    say "| \`docker compose stop\` (SIGTERM to exit) | ${stop_s} s |"
    awk -v v="$stop_s" 'BEGIN { exit !(v < 10) }' || fail "stop took ${stop_s} s (limit 10 s)"
    dc up -d
    dc cp coordinator:/data/ca.pem "$work/ca2.pem"
    after=$(sha256sum "$work/ca2.pem" | cut -d' ' -f1)
    [ "$before" = "$after" ] || fail "CA changed across restart"
    restart_s=$(wait_https)
    [ "$(login_code "$work/jar2")" = 200 ] || fail "administrator login after restart failed (state not persisted)"
    say "| Restart to \`/healthz\` 200 | ${restart_s} s |"
    say "| State after restart (same CA, administrator login works) | persisted |"
    ;;
pressure)
    limit=${BRIDGE_MEMORY_LIMIT:-96m}
    setup_admin
    for i in $(seq 1 30); do echo "$i"; done | xargs -P 30 -I{} sh -c \
        "curl -sS --max-time 60 -o /dev/null -w '%{http_code}\n' --cacert '$work/ca.pem' -c '$work/jar.{}' \
        -H 'content-type: application/json' -d '{\"username\":\"admin\",\"password\":\"$PASSWORD\"}' '$base/api/v1/auth/login' || echo 000" \
        >"$work/login.codes" 2>/dev/null
    ok=$(grep -c '^200$' "$work/login.codes" || true)
    limited=$(grep -c '^429$' "$work/login.codes" || true)
    other=$((30 - ok - limited))
    jar=$(grep -l -i 'session\|token' "$work"/jar.* 2>/dev/null | head -1 || true)
    [ -n "$jar" ] || fail "no login succeeded, cannot continue"
    for i in $(seq 1 200); do echo "$i"; done | xargs -P 200 -I{} curl -sS --max-time 60 -o /dev/null \
        -w '%{http_code}\n' --cacert "$work/ca.pem" -b "$jar" "$base/api/v1/missions" >"$work/get.codes" 2>/dev/null || true
    answered=$(grep -c '^200$' "$work/get.codes" || true)
    id=$(container_id)
    restarts=$(docker inspect -f '{{.RestartCount}}' "$id")
    oom=$(docker inspect -f '{{.State.OOMKilled}}' "$id")
    peak=$(memory_peak_mib)
    say "| Limit $limit (memswap equal) | logins: ${ok} x 200, ${limited} x 429, ${other} other; GETs: ${answered} of 200 answered; memory.peak ${peak} MiB; restarts ${restarts}; OOMKilled ${oom} |"
    [ "$ok" -ge 1 ] || fail "no login succeeded"
    ;;
*) echo "unknown mode $mode" >&2; exit 2 ;;
esac
echo "total $(elapsed "$t0" "$(now)") s"
