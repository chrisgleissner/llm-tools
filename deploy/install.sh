#!/bin/sh
# Install bridge-coordinator as a systemd service on Debian/DietPi (amd64 or arm64).
#
#   deploy/install.sh --binary PATH [--web-dir DIR] [--lan | --bind HOST:PORT]
#                     [--hostname NAME]... [--ip ADDR]... [--prefix DIR] [--dry-run]
#
# Copies a prebuilt binary (no compiler is needed on the target), creates the system user
# "bridge", runs `init` as that user, installs and enables the unit and prints `info`.
# Safe to run again: existing users, keys, certificates and the database are left alone, and the
# binary and unit are replaced by the given versions. Needs root unless --dry-run or --prefix is
# used. With --prefix DIR the files are placed below DIR and neither the user nor systemd is
# touched (staging, tests). --dry-run prints every action and changes nothing.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
user=bridge
binary=
webdir=
bind=
prefix=
dry=0
init_args=

die() { echo "install.sh: $*" >&2; exit 1; }
usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case $1 in
        --binary) [ $# -ge 2 ] || die "--binary needs a value"; binary=$2; shift 2 ;;
        --web-dir) [ $# -ge 2 ] || die "--web-dir needs a value"; webdir=$2; shift 2 ;;
        --bind) [ $# -ge 2 ] || die "--bind needs a value"; bind=$2; shift 2 ;;
        --lan) bind=0.0.0.0:8443; shift ;;
        --prefix) [ $# -ge 2 ] || die "--prefix needs a value"; prefix=$2; shift 2 ;;
        --hostname|--ip)
            [ $# -ge 2 ] || die "$1 needs a value"
            case $2 in *[!A-Za-z0-9.:_-]*|'') die "invalid value for $1: $2" ;; esac
            init_args="$init_args $1 $2"; shift 2 ;;
        --dry-run) dry=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
[ -n "$binary" ] || die "--binary PATH is required (a prebuilt bridge-coordinator for this architecture)"
[ -n "$bind" ] && init_args="$init_args --bind $bind"

run() {
    if [ "$dry" = 1 ]; then echo "+ $*"; else "$@"; fi
}
note() { echo "$*"; }

staged=0
[ -n "$prefix" ] && staged=1
if [ "$dry" = 0 ] && [ "$staged" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "run as root (or use --dry-run / --prefix)"
fi

arch=$(uname -m)
case $arch in
    x86_64|amd64|aarch64|arm64) note "architecture: $arch" ;;
    *) die "unsupported architecture $arch (amd64 and arm64 are supported)" ;;
esac
[ "$dry" = 1 ] || [ -x "$binary" ] || die "$binary is not an executable file"
[ -f "$here/bridge-coordinator.service" ] || die "$here/bridge-coordinator.service not found"

bin_dir=$prefix/usr/local/bin
unit_dir=$prefix/etc/systemd/system
data_dir=$prefix/var/lib/bridge-coordinator
web_target=$prefix/usr/share/bridge-coordinator/web

if [ "$staged" = 0 ]; then
    if [ "$dry" = 1 ] || ! id "$user" >/dev/null 2>&1; then
        run useradd --system --home-dir /var/lib/bridge-coordinator --no-create-home \
            --shell /usr/sbin/nologin --user-group "$user"
    else
        note "user $user exists"
    fi
fi

run install -d -m 0755 "$bin_dir"
run install -m 0755 "$binary" "$bin_dir/bridge-coordinator"
# Host-level administration wrapper (same name as for a container install); it calls the binary
# with sudo, as `llm-bridge-coordinator reset-password`.
if [ -f "$here/../container/llm-bridge-coordinator" ]; then
    run install -m 0755 "$here/../container/llm-bridge-coordinator" "$bin_dir/llm-bridge-coordinator"
else
    note "container/llm-bridge-coordinator not found next to deploy/: the wrapper is not installed (use sudo bridge-coordinator admin reset-password)"
fi
if [ -n "$webdir" ]; then
    run install -d -m 0755 "$web_target"
    run cp -R "$webdir"/. "$web_target"/
fi
if [ "$staged" = 0 ]; then
    run install -d -o "$user" -g "$user" -m 0700 "$data_dir"
else
    run install -d -m 0700 "$data_dir"
fi

# init is idempotent: on an initialised directory it prints a message and changes nothing.
# shellcheck disable=SC2086
if [ "$staged" = 0 ]; then
    if command -v runuser >/dev/null 2>&1; then
        run runuser -u "$user" -- "$bin_dir/bridge-coordinator" init --data-dir "$data_dir" $init_args
    else
        run su -s /bin/sh "$user" -c "$bin_dir/bridge-coordinator init --data-dir $data_dir $init_args"
    fi
else
    note "staged install: not running init (it runs as $user on the target)"
fi

run install -d -m 0755 "$unit_dir"
run install -m 0644 "$here/bridge-coordinator.service" "$unit_dir/bridge-coordinator.service"
if [ "$staged" = 0 ]; then
    run systemctl daemon-reload
    run systemctl enable bridge-coordinator.service
    run systemctl restart bridge-coordinator.service
    if [ "$dry" = 0 ]; then
        if command -v runuser >/dev/null 2>&1; then
            runuser -u "$user" -- "$bin_dir/bridge-coordinator" info --data-dir "$data_dir"
        else
            su -s /bin/sh "$user" -c "$bin_dir/bridge-coordinator info --data-dir $data_dir"
        fi
    else
        echo "+ $bin_dir/bridge-coordinator info --data-dir $data_dir   (as $user)"
    fi
fi
note "done. Create the administrator in a browser using the setup token file shown above,"
note "then see docs/distributed/install.md (firewall, certificate trust, recovery)."
