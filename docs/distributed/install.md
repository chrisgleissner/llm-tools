# Installing and operating the coordinator

The recommended deployment is the container image (next section). It runs on any
Linux host with Docker or another OCI runtime, on amd64 or arm64, and keeps the
host OS untouched. A native install with systemd on Debian-family Linux (including
DietPi) is a supported alternative. Both use the same runtime defaults
(`Config::default` in `coordinator/src/config.rs`, overridable by `config.toml` in
the data directory and by `BRIDGE_COORDINATOR_*` environment variables). Workers
always run natively on the workstations that hold the provider CLIs (see
`DECISIONS.md` D6). Limits and timeouts are listed in `coordinator.md`; the trust
boundaries are in `threat-model.md`.

## Container deployment (recommended)

The image contains only the static `bridge-coordinator` binary and the web files
(`FROM scratch`, about 6.5 MB). It runs as UID 65532 with a read-only root
filesystem, no capabilities and no privileged mode; all state lives in the `/data`
volume (SQLite database, CA and server keys, `config.toml`, setup token). Logs go to
stdout/stderr. `SIGTERM` drains connections and exits within 10 s. The image health
check runs `bridge-coordinator health`.

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

`BRIDGE_PUBLIC_NAMES` are the names browsers and workers use. They replace the
container-internal names in the certificate (`localhost`, `127.0.0.1` and `::1` are
always added) and in the URLs `info` prints. Publishing on the LAN is not
authentication; the login, the CA and the firewall guidance below still apply.

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
(`BRIDGE_MEMORY_LIMIT`), 64 processes and one CPU. The limit is about twice the
measured peak: 48 MiB peak RSS under 30 concurrent logins, 200 commands and 1500
replicated events in the container on x86-64 (`benchmarks.md`). Idle RSS is about
2 MiB before the first login and about 15-35 MiB afterwards, because the two
Argon2id verification slots (19 MiB each) and the allocator keep memory. Log files
are capped at 3 x 1 MB by the `json-file` options.

Client addresses: the login rate limits and the per-address connection cap
(`max_connections_per_ip`, default 32) key on the client address the coordinator
sees. Docker's userland proxy, used for connections to a port published on
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

`init` with no names puts into the certificate: `localhost`, `127.0.0.1`, `::1`, the
machine host name, `<host>.home.arpa` and the machine's global IPv4/IPv6 addresses.
If the address of the machine changes later, issue a new certificate (Recovery below).

After install, open the printed URL, import the CA (next section), and create the
administrator with the content of the setup-token file. The token is consumed by that
step.

The unit runs as `bridge` with `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, `PrivateTmp`, an empty capability set, address families limited to
IPv4, IPv6 and Unix sockets, `MemoryMax=96M`, `Restart=on-failure` and
`TimeoutStopSec=20` (the coordinator drains for up to 10 s on SIGTERM). Binding a port
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
(`BRIDGE_COORDINATOR_LAN_ENABLED`). **LAN placement is not authentication.** Anyone
who can reach the port can attempt a login and can open connections up to the
`max_connections` cap. Every request still needs the administrator session, an API
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
The CA is name-constrained: it carries a critical Name Constraints extension and
`pathLenConstraint=0`. Browsers and workers that trust it accept it only for the names
the server certificate had when `init` ran (host names, and each address as a
single-host range), `localhost`, `home.arpa` and its subdomains, `127.0.0.0/8` and
`::1`. A certificate for any other site, signed with a stolen `ca.key`, is rejected. It
is valid for 5 years. Check with
`openssl x509 -in ca.pem -noout -text | grep -A8 'Name Constraints'`.
A coordinator created before this change has an unconstrained CA; `info` says so, and
`cert new-ca` replaces it.

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

- Protocol: the worker sends the protocol versions it speaks in `hello`
  (`protocol_versions`); the coordinator answers with the version it selected in
  `welcome`, or closes the connection with 1002 `unsupported version` when there is no common version. Today only
  version 1 exists (`protocol.md`).
- Capabilities: each worker advertises its operations in `hello`. The coordinator
  rejects a command for an operation the worker did not advertise
  (`unsupported-operation`), so workers on different software versions can be connected
  at the same time; features a worker lacks are unavailable for that worker only.
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

## Target profile: Raspberry Pi Zero 2 W

Target hardware for the native install: Raspberry Pi Zero 2 W (512 MB RAM, ARM64),
DietPi (Debian 13). Nothing in the coordinator or the installer is specific to this
device; it is a sizing reference only. Measurements on this device are pending (no
hardware has been used yet); see `benchmarks.md` for the only measured platform.
