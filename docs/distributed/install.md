# Installing and operating the coordinator

The recommended deployment is the container image (next section). It runs on any
Linux host with Docker or another OCI runtime, on amd64 or arm64, and keeps the
host OS untouched. A native install with systemd on Debian-family Linux (including
DietPi) is a supported alternative. Both use the same runtime defaults
(`Config::default` in `coordinator/src/config.rs`, overridable by `config.toml` in
the data directory and by `BRIDGE_COORDINATOR_*` environment variables). Workers
always run natively on the workstations that hold the provider CLIs (see
`DECISIONS.md` D6).

Where to find each topic: configuration keys, environment variables, limits and
timeouts are in `coordinator.md`; the wire format is in `protocol.md`; running a
worker, the audit log, day-to-day checks and troubleshooting are in
`operations.md`; trust boundaries and security reasoning are in `threat-model.md`;
measurements are in `benchmarks.md`. This file covers installation, certificate
trust, backup of the coordinator, recovery and upgrades.

## Container deployment (recommended)

The image contains only the static `bridge-coordinator` binary and the web files
(`FROM scratch`, about 6.5 MB). It runs as UID 65532 with a read-only root
filesystem, no capabilities and no privileged mode; all state lives in the `/data`
volume (SQLite database, CA and server keys, `config.toml`, setup token). Logs go to
stdout/stderr. `SIGTERM` drains connections and exits within 10 s. The image health
check runs `bridge-coordinator health`.

If the host's LAN or another Docker host network overlaps Docker's default bridge
`172.17.0.0/16`, set `bip` and `default-address-pools` in `/etc/docker/daemon.json`
(for example `"bip": "172.30.1.1/24"` and
`"default-address-pools": [{"base": "172.29.0.0/16", "size": 24}]`) before starting
Docker; otherwise starting the daemon cuts the host's network.

Build it from this repository (cross-compiles static musl binaries for amd64 and
arm64 on the build host with cargo-zigbuild; nothing is compiled inside an emulated
container):

```sh
container/build.sh --load                                   # this host's architecture, into Docker
container/build.sh --oci bridge-coordinator.oci.tar         # amd64 + arm64 archive
docker load < ...  /  skopeo copy oci-archive:... docker-daemon:...   # on the target host
```

Run it with `container/compose.yaml`. By default it publishes port 8443 on
`127.0.0.1` only. To reach it from the LAN, create `.env` next to `compose.yaml`:

```sh
BRIDGE_PUBLISH_ADDRESS=192.168.1.20        # host LAN address (or 0.0.0.0 for every interface)
BRIDGE_PUBLIC_NAMES=bridge.home.arpa,192.168.1.20
BRIDGE_PORT=8443                           # host port
```

`BRIDGE_PUBLIC_NAMES` are the names browsers and workers use. The compose file passes
them to the container as `BRIDGE_COORDINATOR_PUBLIC_NAMES` (see `coordinator.md`,
Environment variables). Publishing on the LAN is not authentication; the login, the
CA and the firewall guidance below still apply.

First run and day-to-day administration (no shell is needed or present in the image):

```sh
docker compose run --rm coordinator init                       # CA, certificate, config, setup token
docker compose up -d
docker compose cp coordinator:/data/ca.pem .                   # import into browsers (Trusting the certificate)
docker compose cp coordinator:/data/setup-token .              # create the administrator in the browser
docker compose exec coordinator bridge-coordinator info
docker compose exec coordinator bridge-coordinator worker add build-1     # prints the worker token once
docker compose exec coordinator bridge-coordinator token create laptop    # API token for llm-bridge remote login (expires in 90 days; --expires-days N)
docker compose exec -T coordinator bridge-coordinator admin reset-password < new-password.txt
docker compose exec coordinator bridge-coordinator cert renew             # new server certificate; names must be within the CA's name constraints (see "Trusting the certificate")
docker compose exec coordinator bridge-coordinator backup --output /data/backup-2026-11-05.sqlite3
docker compose stop && docker compose run --rm coordinator restore --input /data/backup-2026-11-05.sqlite3 && docker compose start
```

`init` on an initialised volume prints "already initialised" and changes nothing.
`restore` refuses while the server holds the data-directory lock, so stop it first.
Copy backups off the volume with `docker compose cp`; back up the whole volume (for
example `docker run --rm -v <project>_bridge-data:/data -v "$PWD":/out busybox tar
-C /data -czf /out/bridge-data.tgz .` with the server stopped) to include the CA and
server keys.

Resources: `compose.yaml` sets `mem_limit` and `memswap_limit` to 96 MiB
(`BRIDGE_MEMORY_LIMIT`), 64 processes (`pids_limit`) and one CPU (`BRIDGE_CPUS`). The
measurements behind the 96 MiB limit are in `benchmarks.md`. Log files are capped at
3 x 1 MB by the `json-file` options.

Client addresses: the login rate limits and the per-address connection cap
(`max_connections_per_ip`; defaults in `coordinator.md`, Limits and timeouts) key on
the client address the coordinator sees. Docker's userland proxy, used for connections to a port published on
loopback, and the port forwarder of rootless Docker replace the client address
with a gateway address, so all clients that arrive that way share one address and
one set of limits. Rootful Docker keeps the real address for LAN clients that are
forwarded by iptables. When several people or a reverse proxy share one address,
raise `max_connections_per_ip` in `/data/config.toml`.

Verified on x86-64 (rootless Docker 29.9): init, idempotent init, health check,
loopback-only publishing, administrator setup, worker add and token create through
`exec`, a native worker connecting with `--allow-control` and answering
`llm-bridge --target` commands, backup, restore refused while running and done while
stopped, password reset, certificate renewal, restart with data kept, and stop in
0.3 s.

## Windows

On Windows (x86-64 and ARM64) the coordinator and the remote client are supported;
workers and the standalone Bridge are not (decision D8 in `DECISIONS.md`). Build or
download `bridge-coordinator.exe`, then run it from PowerShell:

```powershell
.\bridge-coordinator.exe init --data-dir $env:LOCALAPPDATA\bridge-coordinator
.\bridge-coordinator.exe serve --data-dir $env:LOCALAPPDATA\bridge-coordinator
```

Windows has no permission bits, so the files are created normally; the access control
list inherited from the data directory protects `ca.key`, `server.key`, `setup-token`
and the database. Use a directory only your account can read. There is no systemd unit;
run the coordinator as a service with your usual tool, or use the container
deployment. `llm-bridge --target NAME COMMAND`, `llm-bridge remote login` and
`llm-bridge worker list` work on Windows; every other command prints
`not supported on Windows` and exits with status 2.

## Native install with systemd (alternative)

You need a prebuilt `bridge-coordinator` binary for the target architecture, and
optionally the built web interface directory.

```sh
sudo deploy/install.sh --binary ./bridge-coordinator --web-dir ./web --lan
```

`deploy/install.sh` (POSIX shell, safe to run again):

1. creates the system user `bridge` (no login shell),
2. installs the binary to `/usr/local/bin` and the web files to
   `/usr/share/bridge-coordinator/web`,
3. runs `bridge-coordinator init` as `bridge` in `/var/lib/bridge-coordinator`
   (an initialised directory is left unchanged),
4. installs and enables `bridge-coordinator.service`,
5. prints the `info` output: URL(s), CA path and fingerprint, setup-token path.

Options: `--bind HOST:PORT`, `--lan` (same as `--bind 0.0.0.0:8443`), repeatable
`--hostname NAME` and `--ip ADDR` (certificate names; they replace automatic
discovery), `--prefix DIR` (stage files below DIR, no user, no systemd), and
`--dry-run` (print every action, change nothing).

`init` with no names discovers the certificate names (`coordinator.md`, Process and
files). If the address of the machine changes later, issue a new certificate
(Recovery below).

After install, open the printed URL, import the CA (next section), and create the
administrator with the content of the setup-token file. The token is consumed by that
step.

The unit runs as `bridge` with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, an empty capability set, address families limited to
IPv4, IPv6 and Unix sockets, `MemoryMax=96M` (the same limit as the container, see `benchmarks.md`),
`Restart=on-failure` and `TimeoutStopSec=20` (the coordinator drains for up to 10 s on
SIGTERM). Binding a port
below 1024 needs a drop-in granting `CAP_NET_BIND_SERVICE`; the unit file shows the
two lines.

Health check: `bridge-coordinator health --data-dir /var/lib/bridge-coordinator`
exits 0 when `GET /healthz` over HTTPS answers `{"status":"ok"}` (verified against
the local CA, at most 3 s) and 1 otherwise. Use it from monitoring or a timer;
`/healthz` itself needs no authentication and returns nothing else.

## Network placement

`bind` in `config.toml` (or `BRIDGE_COORDINATOR_BIND`) selects the listen address:

- `127.0.0.1:8443` (default): only the machine itself. Use this behind a local reverse
  proxy or an SSH tunnel.
- A specific LAN address such as `192.168.1.20:8443`: only that interface.
- `0.0.0.0:8443` or `[::]:8443`: all interfaces.

A non-loopback bind is refused unless `lan_enabled = true`
(`BRIDGE_COORDINATOR_LAN_ENABLED`; in the container image the published port decides
exposure, see `coordinator.md`). **LAN placement is not authentication.** Anyone
who can reach the port can attempt a login and can open connections up to the
connection limits in `coordinator.md`. Every request still needs the administrator session, an API
token or a worker token, but you should also restrict who can reach the port.
Never expose the port to the internet.

Firewall examples (documentation only; the installer does not change firewall
settings). Replace `192.168.1.0/24` with your LAN subnet.

```sh
# ufw
sudo ufw allow from 192.168.1.0/24 to any port 8443 proto tcp
sudo ufw deny 8443/tcp
```

```nft
# /etc/nftables.conf fragment: inet table "filter", chain "input" with policy drop
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    ip saddr 192.168.1.0/24 tcp dport 8443 accept
    # ip6 saddr fd00:1234::/64 tcp dport 8443 accept
  }
}
```

With ufw, rules are evaluated in order, so keep the `allow` before the `deny`.

## Trusting the certificate

`init` creates a private CA. Browsers must trust that CA once. Import `ca.pem` as a
certification authority; never click through a "proceed anyway" warning, because that
trains you to accept a certificate from anyone on the network.

First verify the file. `bridge-coordinator info` prints the CA SHA-256 fingerprint.
On the device, compare it with the fingerprint of the file you copied:

```sh
openssl x509 -in ca.pem -noout -fingerprint -sha256
```

On Windows, after import, open the certificate and compare the "SHA-256" value on its
Details tab with the fingerprint printed by `info`. Copy `ca.pem` using a path you trust (`scp` from the
machine, a USB stick), not from the web page it protects.

Chrome and Chromium-based browsers:

- Linux: Settings > Privacy and security > Security > Manage certificates >
  Authorities > Import, choose `ca.pem`, tick "Trust this certificate for identifying
  websites". (Chrome uses the NSS store of the user; alternatively
  `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n bridge-ca -i ca.pem`.)
- Windows: Settings > Privacy and security > Security > Manage certificates opens the
  system store. Import into "Trusted Root Certification Authorities" (rename `ca.pem`
  to `ca.crt` first so the wizard accepts it).
- macOS: open `ca.pem` in Keychain Access (login keychain), double-click the
  certificate, expand Trust and set "Secure Sockets Layer (SSL)" to "Always Trust".

Firefox (all systems; Firefox keeps its own store): Settings > Privacy & Security >
Certificates > View Certificates > Authorities > Import, choose `ca.pem`, tick
"Trust this CA to identify websites".

Android: copy `ca.pem` (or `ca.crt`) to the phone, then Settings > Security >
Encryption & credentials > Install a certificate > CA certificate, and confirm the
warning. The exact menu names vary by vendor (search settings for "CA certificate").
The CA is name-constrained and valid for 5 years. Browsers and workers accept it only
for the names listed in `coordinator.md` (Process and files, CA constraints). Check
the extension with
`openssl x509 -in ca.pem -noout -text | grep -A8 'Name Constraints'`.
A coordinator created before name constraints existed has an unconstrained CA; `info`
says so, and `cert new-ca` replaces it.

Chrome on Android uses the user store. Some apps ignore user CAs; the web interface
in Chrome works.

The address in the browser must be one of the certificate's names. If it is not, the
browser reports a name mismatch; fix it with `cert renew` (Recovery), not by
bypassing the warning. If the new name is outside the CA's name constraints, `cert
renew` refuses and names it; run `cert new-ca` with the full list of names, then import
the new `ca.pem` in every browser and re-enrol every worker. Workers trust the same
`ca.pem` through their enrolment.

## Backup and restore

`bridge-coordinator backup --data-dir DIR --output FILE` copies the database with
the SQLite online backup API while the server runs. The file is mode 0600, an
existing file is never overwritten, and `integrity_check` must pass before the file
appears. It contains workers (token hashes), the administrator password hash,
sessions, API token hashes, the event index and the audit log.

The backup does not contain these files. Back them up separately, readable only by
you, and treat them as secrets (except `ca.pem` and `server.pem`):

| File in the data directory | Why |
|---|---|
| `ca.key` | Signs server certificates. Without it a new CA is needed and every browser and worker must trust it again. |
| `ca.pem` | The CA certificate that clients trust. |
| `server.key`, `server.pem` | The current server identity (can be reissued from `ca.key`). |
| `config.toml` | Bind address, limits, allowed origins. |

For example, as the `bridge` user:

```sh
umask 077
bridge-coordinator backup --data-dir /var/lib/bridge-coordinator --output /backup/coordinator-$(date +%F).sqlite3
tar -C /var/lib/bridge-coordinator -cf /backup/coordinator-keys-$(date +%F).tar ca.key ca.pem server.key server.pem config.toml
```

Encrypt the archive if the backup medium is shared.

Restore: stop the service, then run
`bridge-coordinator restore --data-dir DIR --input FILE` as the `bridge` user and start
the service. `restore` refuses to run while a server holds `DIR/coordinator.lock`,
checks integrity and schema version of the input (a file from a newer coordinator is
refused), and keeps the replaced database as
`coordinator.sqlite3.before-restore-<UTC time>`. Browser sessions are deleted by the
restore. Workers and API tokens that were revoked after the backup was taken are
active again: `restore` prints every worker and API token now active, each with the
command that revokes it (`bridge-coordinator worker revoke NAME`,
`bridge-coordinator token revoke NAME`). Go through that list. If the administrator
password changed after the backup, the old one is back: run `admin reset-password`.
Schema version 2 added the expiry of API tokens; tokens created before it never expire,
so revoke and recreate them with `token create` to get the 90-day default.

## Recovery

All recovery commands run on the machine, as the service user, with the same data
directory (`--data-dir` or `BRIDGE_COORDINATOR_DATA_DIR`).

- Lost administrator password: `bridge-coordinator admin reset-password` reads the new
  password from standard input (at least the minimum length), deletes every
  session and revokes every API token (it prints how many; `--keep-api-tokens` keeps
  them). Example: `printf '%s\n' "$NEW" | bridge-coordinator admin reset-password`.
- Expired certificate, or the machine's address or name changed: run
  `bridge-coordinator cert renew --hostname NAME --ip ADDR` (or without options to
  rediscover this machine's names) and restart the service. The CA is unchanged, so
  browsers keep their trust, but only while every name is within the CA's name
  constraints. A new name outside them (for example a new public address) needs
  `bridge-coordinator cert new-ca --hostname NAME --ip ADDR` instead: it creates a new
  constrained CA and server certificate, keeps the old CA as `ca.pem.old-<UTC>` (and the
  old key as `ca.key.old-<UTC>`; delete it once nothing needs the old CA), and prints the
  new fingerprint. Import the new `ca.pem` in every browser and re-enrol every worker. `bridge-coordinator info` flags a URL that the current
  certificate does not cover. To use a certificate from your own CA instead, run
  `bridge-coordinator cert install --cert FILE --key FILE`; the pair is checked
  before it replaces the current one.
- Lost setup token before the administrator exists: `bridge-coordinator setup-token`
  writes a new token file (0600) and prints its path. It refuses once an administrator
  exists; use `admin reset-password` then.
- Lost `ca.key`: run `bridge-coordinator cert new-ca` (with `--hostname`/`--ip` for the
  names to serve), then import the new CA in every browser and re-enrol every worker.
  The database is unaffected.
- Database damaged: restore the latest backup (previous section).

## Upgrades and compatibility

- Protocol and capabilities: version negotiation and capability advertisement are
  specified in `protocol.md`. Workers on different software versions can be connected
  at the same time; an operation a worker lacks is unavailable for that worker only.
- Database: the coordinator database carries `PRAGMA user_version`. Starting a newer
  coordinator migrates the database forward when it opens it. Migrations are
  forward-only: an older binary refuses a database with a newer version and does not
  modify it. To roll back, stop the service, install the old binary and restore the
  backup taken before the upgrade.
- Order: take a backup (database and key files), upgrade the coordinator first, check
  `health` and the diagnostics page, then upgrade workers one at a time. A worker that
  reconnects after the coordinator upgrade replays its event cursor, so no events are
  lost while workers still run the old version.
- Replacing the binary: `deploy/install.sh --binary NEW` again, or copy the file to
  `/usr/local/bin/bridge-coordinator` and `systemctl restart bridge-coordinator`.

## Low-write host

This section applies to any host whose storage wears with writes: SD and microSD
cards, eMMC and USB sticks. Nothing in it is specific to one board.

What writes to storage while the coordinator runs:

- **The coordinator database.** Every commit is flushed (`synchronous=FULL`), which is
  what keeps acknowledged commands and replicated events across a power loss. Leave it
  that way and keep the data directory on persistent storage, not on a RAM disk. The
  measured volumes are in `benchmarks.md` (Storage writes): about 545 bytes per
  replicated event, about 68 KiB per command and about 104 KiB per idle hour per
  connected worker. An idle coordinator writes nothing per worker heartbeat. A connected worker's `last_seen` is stored at most every ten
  minutes, and an open browser session or API token is touched at most every five
  minutes.
- **Docker's container log.** `compose.yaml` uses the `json-file` driver with up to
  three 1 MB files.
- **Docker's health checks.** Docker records every health-check result in its state
  directory. The default interval of 30 seconds is 2880 writes a day.
- **The system journal**, if it is stored on disk.

For a container deployment, add the override file `container/compose.low-write.yaml`.
It sends the container log to journald instead of `json-file` and checks health every
five minutes. Docker still checks every 5 seconds during the 20-second start period, so
a fresh start is reported healthy as quickly as before.

```sh
cd container
docker compose -f compose.yaml -f compose.low-write.yaml up -d
```

Two variables in `.env` adjust it:

| Variable | Default with the override | Default without it | Meaning |
|---|---|---|---|
| `BRIDGE_LOG_DRIVER` | `journald` | `json-file` (fixed) | Docker log driver. Use a driver without size options: `journald`, `syslog` or `none`. |
| `BRIDGE_HEALTHCHECK_INTERVAL` | `5m` | `30s` | Interval between health checks. `compose.yaml` reads it too, so it can be set without the override. |

Keep the journal in memory so that the journald driver, and the native service's log,
do not reach storage. In `/etc/systemd/journald.conf` set:

```ini
[Journal]
Storage=volatile
RuntimeMaxUse=16M
```

Then run `sudo systemctl restart systemd-journald`. Some distributions already keep
logs in RAM; check `journalctl --header | grep 'File path'`, which shows a path under
`/run` for a volatile journal. A volatile journal loses the coordinator's log at
reboot. The audit trail is not affected: it is stored in the coordinator database.

A native install has no Docker log or health check, and its log goes to the journal,
so the journal setting above is the only change it needs.

To see what the coordinator itself writes, read its `write_bytes` counter twice, an
hour apart, while the system is in normal use:

```sh
pid=$(systemctl show -p MainPID --value bridge-coordinator)   # native install
pid=$(docker inspect -f '{{.State.Pid}}' $(docker compose ps -q coordinator))  # container, run in container/
sudo grep write_bytes /proc/$pid/io
```

## Raspberry Pi Zero 2 W target profile

This is the only device-specific section in the distributed documentation. The rest
of the documentation, the coordinator and the installer are hardware-neutral.

Target: Raspberry Pi Zero 2 W (512 MB RAM, ARM64) running DietPi (Debian 13).

**Status: measured on an emulated Pi, not yet on the device.** The figures marked
"emulated" come from pi-sim: QEMU `raspi3b` in TCG mode with `mem=512M` (478 MB
visible), an unmodified DietPi v10.8.2 image (Debian 13, arm64, kernel 6.18.60). TCG
runs Cortex-A53 code far slower than the real device, so times are pessimistic; memory
and bytes written do not depend on the CPU speed. The physical Pi Zero 2 W measurement
is scheduled for 2026-11-06 and repeats the commands below.

Emulated results (2026-11-06, details in `benchmarks.md`, "ARM64, emulated"):

- Native install with `install.sh --lan`: 9.8 s; rerunning it with a newer binary
  upgraded the service in 11.7 s and kept the database, keys and administrator. The
  service starts at boot, uses 3.4-4.6 MiB RSS idle and peaked at 22.2 MiB.
- Container (Docker 29.9 and Compose 5.7 from `dietpi-software install 162 134`): the
  arm64 image loads in 7 s; with `compose.low-write.yaml` and LAN publishing the
  container used 2.4 MiB idle and peaked at 21.7 MiB (cgroup `memory.peak`, including
  administrator setup and login) under its 96 MiB limit. Host memory in use stayed
  below 150 MB of 478 MB.
- Both paths: administrator setup over HTTPS from another machine, a worker on another
  machine with remote commands, and all state kept across `systemctl reboot`.

DietPi reported a pending kernel upgrade for CVE-2026-98432 in its login message on the
emulated image. On the physical device run `apt update && apt full-upgrade`, then
reboot, before installing the coordinator.

Memory budget (derived from x86-64 container measurements; the emulated figures above are lower):

- The container peak was 48.1 MiB RSS (`benchmarks.md`), measured with 30 concurrent
  logins, 200 commands and 1500 replicated events. The shipped limit is 96 MiB, which
  is under a fifth of 512 MB.
- The limit covers the coordinator container only. The kernel, DietPi, the Docker
  daemon (container deployment) and any other containers use the remaining memory. Their
  size on this device is not known; check it with `free -m` before adding services.
- The native install avoids the Docker daemon. It uses the same 96 MiB limit
  (`MemoryMax=96M`).

Recommended settings:

- Start with the shipped defaults: `BRIDGE_MEMORY_LIMIT=96m`, one CPU, loopback
  publishing unless LAN access is needed (Container deployment).
- Keep `memswap_limit` equal to `mem_limit` (the default in `compose.yaml`). The
  coordinator then never swaps. If it needs more memory than the limit, the kernel stops
  the container and `restart: unless-stopped` starts it again. This avoids heavy swap
  writes on a microSD card. No swap or zram configuration is needed for the coordinator.
- The card wears with writes. Follow Low-write host above: the low-write compose
  override and a volatile journal.
- The database uses `synchronous=FULL`, so each commit is flushed to the card. The
  x86-64 measurement is 557 bytes per event including the write-ahead log; with the
  default `max_events_per_worker` of 50 000 that is about 27 MB per worker (derived,
  not measured on the card). Keep the data volume on a card or disk with free space of
  at least several times that, and lower `max_events_per_worker` in `config.toml` if
  space is short.
- The Pi Zero 2 W has no battery-backed clock. TLS certificates and login sessions
  depend on the system time, so keep a time-synchronisation service enabled on the
  device and check `date` after each boot.
- Build the arm64 image on another machine (`container/build.sh --oci`) and load it on
  the Pi; building on the device is not intended.

Verification after deployment (run these on the device and record the results; they
replace the derived figures above):

```sh
docker compose exec coordinator bridge-coordinator health && echo healthy
docker compose exec coordinator bridge-coordinator info
docker stats --no-stream            # MEM USAGE / LIMIT of the coordinator container
free -m                             # host memory with the coordinator running
```

For a native install run `bridge-coordinator health --data-dir
/var/lib/bridge-coordinator` and `systemctl status bridge-coordinator` (it shows the
memory in use). The diagnostics page (`GET /api/v1/diagnostics`) reports the process
RSS. Take the reading after a few logins, because the Argon2id verification memory is
allocated on first use. If the container uses more than about 80 percent of its limit,
raise `BRIDGE_MEMORY_LIMIT` (for example to `128m`) and record the measured value in
`benchmarks.md`.
