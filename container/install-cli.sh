#!/bin/sh
# Install the llm-bridge-coordinator command for a container deployment.
#
#   sudo ./install-cli.sh [--compose-dir DIR] [--prefix DIR]
#
# Copies llm-bridge-coordinator to /usr/local/bin and records the compose folder (default: the
# folder of this script, which is where compose.yaml is) in
# /etc/llm-bridge-coordinator/compose-dir. A copy and a record in world-readable places let a
# user who cannot read the compose folder (for example mode 0700 below /opt) still run
# `llm-bridge-coordinator reset-password` over SSH; the command uses sudo for docker when the
# user is not in the docker group. --prefix DIR places both below DIR (staging, tests).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
compose_dir=$here
prefix=

die() { echo "install-cli.sh: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --compose-dir) [ $# -ge 2 ] || die "--compose-dir needs a value"; compose_dir=$2; shift 2 ;;
        --prefix) [ $# -ge 2 ] || die "--prefix needs a value"; prefix=$2; shift 2 ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1" ;;
    esac
done

compose_dir=$(cd "$compose_dir" && pwd) || die "$compose_dir is not a folder"
[ -f "$compose_dir/compose.yaml" ] || die "$compose_dir/compose.yaml not found"
[ -f "$here/llm-bridge-coordinator" ] || die "$here/llm-bridge-coordinator not found"
if [ -z "$prefix" ] && [ "$(id -u)" != 0 ]; then
    die "run as root (sudo ./install-cli.sh), or use --prefix"
fi

install -d -m 0755 "$prefix/usr/local/bin" "$prefix/etc/llm-bridge-coordinator"
install -m 0755 "$here/llm-bridge-coordinator" "$prefix/usr/local/bin/llm-bridge-coordinator"
printf '%s\n' "$compose_dir" >"$prefix/etc/llm-bridge-coordinator/compose-dir"
chmod 0644 "$prefix/etc/llm-bridge-coordinator/compose-dir"
echo "installed $prefix/usr/local/bin/llm-bridge-coordinator (compose folder $compose_dir)"
echo "try: llm-bridge-coordinator reset-password --help"
