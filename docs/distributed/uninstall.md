# Uninstalling the coordinator

This page covers each way of installing the coordinator described in
[install.md](install.md). For the Python tools (`llm-bridge`, workers, `llm-usage` and
the others) see [../llm-bridge/uninstall.md](../llm-bridge/uninstall.md).

Every method below ends with a check that no service, listening port or file remains
except what you chose to keep. The coordinator opens one TCP port (8443 by default) and
writes only to its data directory.

## Keep what you want first

Take a backup before removing anything that holds the data directory. A backup
restores on another host and another architecture.

```sh
# container
docker compose exec coordinator bridge-coordinator backup --output /data/final.sqlite3
docker compose cp coordinator:/data/final.sqlite3 .
# native
sudo -u bridge bridge-coordinator backup --data-dir /var/lib/bridge-coordinator --output /var/lib/bridge-coordinator/final.sqlite3
sudo cp /var/lib/bridge-coordinator/final.sqlite3 .
```

The CA (`ca.pem`, `ca.key`), the server key and the setup token are in the same data
directory. Copy the CA files too if workers or browsers should keep trusting the same
CA after a reinstall.

## Container deployment

Run these in the folder that holds `compose.yaml`.

```sh
docker compose down                      # stops and removes the container and its network; keeps the volume
docker compose down --volumes            # also removes the data volume (bridge-data): the database, CA and keys are gone
docker image rm llm-bridge-coordinator:latest    # or the image named in BRIDGE_IMAGE
rm -f .env                               # only if you created one
```

If you started the Tailscale profile, add the profile flags to `down` so its containers
and volumes are removed too:

```sh
docker compose --env-file tailscale.env --profile tailscale down --volumes
rm -f tailscale.env
```

Check:

```sh
docker ps -a --filter name=coordinator   # no coordinator container
docker volume ls | grep -E 'bridge-data|tailscale'   # nothing, unless you kept a volume
ss -ltn | grep :8443                     # nothing listening
```

## Native install with systemd

`deploy/install.sh` creates the `bridge` user, `/usr/local/bin/bridge-coordinator`,
`/usr/local/bin/llm-bridge-coordinator`, `/usr/share/bridge-coordinator/web`,
`/var/lib/bridge-coordinator` and the unit `bridge-coordinator.service`.

```sh
sudo systemctl disable --now bridge-coordinator
sudo rm -f /etc/systemd/system/bridge-coordinator.service
sudo systemctl daemon-reload
sudo rm -f /usr/local/bin/bridge-coordinator /usr/local/bin/llm-bridge-coordinator
sudo rm -rf /usr/share/bridge-coordinator
# Only when you no longer want the data (database, CA, keys, backups in /var/lib/bridge-coordinator):
sudo rm -rf /var/lib/bridge-coordinator
sudo userdel bridge
```

Check:

```sh
systemctl status bridge-coordinator      # Unit could not be found
ss -ltn | grep :8443                     # nothing listening
ls /usr/local/bin/bridge-coordinator /var/lib/bridge-coordinator   # No such file or directory
id bridge                                # no such user
```

If you used `deploy/install.sh --prefix DIR`, nothing was installed on the host: delete
`DIR`.

## Windows

The coordinator on Windows is a plain executable that you started yourself or wrapped
in a service with your own tool.

```powershell
# Stop it: close the PowerShell window running serve, or stop the service you created.
Stop-Service bridge-coordinator              # only if you registered a service under this name
sc.exe delete bridge-coordinator             # removes that service registration
Remove-Item .\bridge-coordinator.exe
# Only when you no longer want the data (database, CA, keys):
Remove-Item -Recurse -Force $env:LOCALAPPDATA\bridge-coordinator
```

Check: `Get-NetTCPConnection -LocalPort 8443` reports no listener, and
`Get-Service bridge-coordinator` reports that the service does not exist.

## After removal

Workers that were enrolled with this coordinator keep their enrolment file and try to
reconnect. On each worker run `llm-bridge worker unenroll`, and on each client machine
`llm-bridge remote logout` (see [../llm-bridge/uninstall.md](../llm-bridge/uninstall.md)).
Remove the coordinator CA from the browsers and operating systems where you imported it.
