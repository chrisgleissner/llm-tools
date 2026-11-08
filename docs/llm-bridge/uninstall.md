# Uninstall and move llm-tools

This page removes llm-tools from a host so that no service, port or file is left behind
except what you choose to keep. It also says what to keep so that your missions restore on
another host, including one with a different CPU architecture.

It covers the Python tools: `llm-usage`, `llm-scheduler`, `ralph-robin`, `llm-sleep-soak`
and `llm-bridge` (including the optional worker service and remote CLI). The coordinator
(container or native) is removed as described in
[distributed install](../distributed/install.md).

`tests/test_uninstall_portability.py` fails when the code uses a path that this page does
not list, so the lists below match the installed version.

## What llm-tools can leave on a host

| Kind | Created by | Removed in step |
|---|---|---|
| Program files and the five commands `llm-usage`, `llm-scheduler`, `ralph-robin`, `llm-sleep-soak`, `llm-bridge` | the install method | 3 |
| systemd user unit `llm-usage.service` (macOS: LaunchAgent `com.llm-tools.llm-usage`) | `llm-usage --service-install` | 2 |
| systemd user unit `llm-bridge-worker.service` | `llm-bridge worker install-service` | 2 |
| Background processes: mission controllers, tmux stations, the sampler, the worker | `llm-bridge start`, `llm-usage --service-install`, `llm-bridge worker run` | 1 |
| Transient systemd user timers that wake or resume the machine | `llm-scheduler --wake` and `--suspend-until-ready`, `ralph-robin`, `llm-sleep-soak`, missions with `--suspend-when-rate-limited on` | 1 |
| An RTC wake alarm in the firmware clock | the same tools, through `rtcwake` | 1 |
| Unix sockets (no TCP port is ever opened) | the sampler and each mission station | 1 |
| Data: database, enrolment, caches, logs | all of the above | 4 |

llm-tools never opens a network port. The worker and the remote CLI only make outbound
connections to your coordinator. It never changes files that belong to the provider CLIs
(`~/.claude`, `~/.codex` and similar); those stay as they are.

## Install methods

| Method | Where the program lives | Step 3 command |
|---|---|---|
| pipx (`pipx install git+https://github.com/chrisgleissner/llm-tools.git`, a release wheel, or `pipx install .`) | `~/.local/share/pipx/venvs/llm-tools` or `~/.local/pipx/venvs/llm-tools`, with links in `~/.local/bin` | `pipx uninstall llm-tools` |
| pip (`python3 -m pip install --user llm-tools` or into a virtual environment) | the Python site-packages, with scripts in `~/.local/bin` or the environment's `bin` | `python3 -m pip uninstall llm-tools` |
| Direct run from a checkout (`./llm-bridge`, `./llm-usage` and the other three scripts) | the checkout folder only; nothing is installed | delete the checkout folder |

A service unit written by a checkout run points at that checkout through `PYTHONPATH`, so
remove the units (step 2) before deleting the folder.

## Step 0: keep what you want

Do this first if you may come back:

```bash
llm-bridge backup --output ~/llm-bridge-backup-$(date +%F).sqlite3
```

The backup is one SQLite file (mode 0600) holding every mission, task, event, operation
and verification record. It is the only thing you need to carry to a new host. See
[Moving to another host](#moving-to-another-host-and-another-architecture) for what it
does not contain.

## Step 1: stop everything that runs

1. Stop each mission controller and close finished stations:

   ```bash
   llm-bridge missions                      # lists missions; live ones first
   llm-bridge stop --mission ID             # once per live mission
   llm-bridge gc                            # closes the idle stations of finished missions
   ```

   `stop` ends the controller and its tmux station. A leftover station socket is listed in
   step 4; a station that still answers is ended with `tmux -S SOCKET kill-server`.
   Stopping does not discard task history.

2. Stop the worker and the sampler (step 2 also removes their units). A worker or sampler
   started in a terminal (`llm-bridge worker run`, `llm-usage --service-run`) stops with Ctrl-C.

   ```bash
   systemctl --user stop llm-bridge-worker.service
   llm-usage --service-stop
   ```

3. Remove transient timers. The tools name them `llm-scheduler-wake-<epoch>`,
   `llm-scheduler-resume-<provider>-<epoch>`, `ralph-robin-wake-<epoch>`,
   `llm-sleep-soak-wake-<epoch>` and `llm-bridge-capacity-wake-<target>-wake-<epoch>`. A
   resume timer would start `llm-scheduler` again at its time, so stop them before removing
   the program:

   ```bash
   systemctl --user list-timers --all | grep -E 'llm-|ralph-robin'
   systemctl --user stop 'llm-scheduler-*.timer' 'llm-bridge-capacity-wake-*.timer' \
       'ralph-robin-wake-*.timer' 'llm-sleep-soak-wake-*.timer'
   ```

   Transient units live only in `/run/user/$(id -u)/systemd/transient`, so a reboot also
   removes them.

4. Clear an RTC wake alarm. `rtcwake -m no` programs the firmware clock directly. Read the
   alarm and clear it if it is set and you do not want the wake:

   ```bash
   cat /sys/class/rtc/rtc0/wakealarm        # empty means no alarm
   echo 0 | sudo tee /sys/class/rtc/rtc0/wakealarm
   ```

   The default alarm file is `/sys/class/rtc/rtc0/wakealarm`; `LLM_TOOLS_RTC_WAKEALARM`
   overrides it for tests.

## Step 2: remove the services

```bash
llm-usage --service-uninstall            # disables llm-usage.service (macOS: boots out the LaunchAgent) and deletes the unit
llm-bridge worker uninstall-service      # disables llm-bridge-worker.service and deletes the unit
```

Both commands run `systemctl --user disable --now`, delete the unit file and run
`systemctl --user daemon-reload`. They are safe to repeat. They leave the data in step 4.

| Unit | File |
|---|---|
| `llm-usage.service` | `$HOME/.config/systemd/user/llm-usage.service` |
| macOS LaunchAgent `com.llm-tools.llm-usage` | `$HOME/Library/LaunchAgents/com.llm-tools.llm-usage.plist` |
| `llm-bridge-worker.service` | `$HOME/.config/systemd/user/llm-bridge-worker.service` |

If you ran `loginctl enable-linger` only for the worker, `loginctl disable-linger` undoes it.

Check that nothing is left:

```bash
systemctl --user list-unit-files 'llm-*'
systemctl --user list-units 'llm-*' --all
```

## Step 3: remove the program

Use the command for your install method from the table above. The five commands disappear
from `PATH`. Check with `command -v llm-usage llm-scheduler ralph-robin llm-sleep-soak llm-bridge`.

## Step 4: remove the data

Each row says what the path holds, whether you may want to keep it, and the exact command.
Paths use `${XDG_STATE_HOME:-$HOME/.local/state}`, `${XDG_CACHE_HOME:-$HOME/.cache}`,
`${XDG_CONFIG_HOME:-$HOME/.config}` and `${XDG_RUNTIME_DIR}`; replace them with your values
if you set them. `LLM_BRIDGE_STATE_DIR` replaces the state directory path.

### State: `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge`

| Path | Holds | Keep for a later restore? |
|---|---|---|
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/bridge.sqlite3` (and `-wal`, `-shm`, `bridge.sqlite3.before-restore-*`) | missions, tasks, events, verification evidence | Yes: take `llm-bridge backup` first |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/worker` | worker enrolment (`worker.json` with the token, `coordinator-ca.pem`, `audit.jsonl`) | No. Enrol again on the new host. `llm-bridge worker unenroll` deletes the token and CA but leaves `audit.jsonl` |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/worker/worker.json` | worker token, coordinator URL, opt-ins | No |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/worker/coordinator-ca.pem` | coordinator CA for the worker | Keep a copy of the coordinator's `ca.pem` instead |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/worker/audit.jsonl` | worker security audit log (and `audit.jsonl.1`) | Optional |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/remote` | remote CLI configuration | No. `llm-bridge remote logout` deletes the files in it |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/remote/remote.json` | coordinator URL and API token | No |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/remote/coordinator-ca.pem` | coordinator CA for the CLI | No |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/drafts` | unsent task drafts (pruned after 14 days) | Optional |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/locks` | per-mission controller lock files | No |
| `${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge/runtime` | per-mission controller files, provider run logs, verifier copies | No; logs only matter for an audit |

To delete all of it, after the controllers are stopped:

```bash
rm -rf "${LLM_BRIDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools/llm-bridge}"
rmdir "${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools" 2>/dev/null   # only if now empty
```

### Cache: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`

The cache holds nothing that cannot be recomputed except logs.

| Path | Holds |
|---|---|
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools` | the root of every cache path below |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage` | provider usage caches, the usage sample log, a copy of the Claude statusline input |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage/service` | the sampler's working folder |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage/service/latest.json` | the sampler's latest snapshot |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage/service/history.jsonl` | the sampler's append-only history, plus `service.log` and `service.err` |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-scheduler/logs` | `llm-scheduler` run directories (prompt copies, output) |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs` | `ralph-robin` run directories |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/state.json` | which provider `ralph-robin` used last |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-sleep-soak/logs` | `llm-sleep-soak` run directories |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/suspend-ledger.jsonl` | durable record of every machine suspend (a start with no end marks a failed resume); keep it if you are investigating suspend problems |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/shared-usage` | usage snapshots shared between tools for a few seconds |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/routes/blocks` | temporary blocks on routes (`LLM_TOOLS_LOCAL_BLOCK_DIR` overrides the folder) |
| `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-bridge/capacity-wakes` | the shared wake plan (`plan.json`, one request file per mission, `planner.lock`) |

Older releases kept three of these directly under the cache directory
(`${XDG_CACHE_HOME:-$HOME/.cache}/llm-usage`, `.../llm-scheduler`, `.../ralph-robin`). They are
moved into `llm-tools` the first time a tool runs, but a host that never ran the new version may
still have them.

```bash
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools"
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}"/{llm-usage,llm-scheduler,ralph-robin}   # old layout, if present
```

`${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/runtime` is a fallback location for tmux sockets
(used only when every `/tmp` name is taken); it is inside the cache directory removed above.

### Runtime: sockets

| Path | Holds |
|---|---|
| `${XDG_RUNTIME_DIR}/llm-tools` | the sampler's socket |
| `${XDG_RUNTIME_DIR}/llm-tools/llm-usage.sock` | the sampler's Unix socket |
| `${XDG_RUNTIME_DIR}/llm-tools/llm-usage.pid` | the sampler's process id |
| `${XDG_RUNTIME_DIR}/llm-bridge` | one tmux socket (`m-<hash>.sock`) and one `.conf` per mission station |
| `/tmp/lb-$UID`, `/tmp/lb-$UID-1` to `/tmp/lb-$UID-4` | the same tmux sockets when `XDG_RUNTIME_DIR` is not set |
| `/tmp/llm-tools-$UID` | the sampler's socket directory when `XDG_RUNTIME_DIR` is not set |

These live in tmpfs or `/tmp` and disappear at reboot. To remove them now, after step 1:

```bash
rm -rf "${XDG_RUNTIME_DIR:-/nonexistent}/llm-tools" "${XDG_RUNTIME_DIR:-/nonexistent}/llm-bridge"
rm -rf "/tmp/llm-tools-$(id -u)" /tmp/lb-"$(id -u)" /tmp/lb-"$(id -u)"-[1-4]
```

A tmux server can outlive its socket file. If `pgrep -af 'tmux.*(llm-bridge|/lb-)'` still
lists a server after step 1, end it with `tmux -S SOCKET kill-server` before removing the socket.

### Configuration

`${XDG_CONFIG_HOME:-$HOME/.config}/llm-tools/config.toml` is written by you, never by the tools.
Keep it if you may come back (it holds your provider list, routes and budget); otherwise delete it:

```bash
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/llm-tools"
```

## Check that nothing is left

```bash
command -v llm-usage llm-scheduler ralph-robin llm-sleep-soak llm-bridge     # prints nothing
systemctl --user list-unit-files 'llm-*'                                     # no entries
systemctl --user list-timers --all | grep -E 'llm-|ralph-robin'              # no output
cat /sys/class/rtc/rtc0/wakealarm                                            # empty, or a wake you chose
ls -d "${XDG_RUNTIME_DIR:-/nonexistent}"/llm-* /tmp/lb-"$(id -u)"* /tmp/llm-tools-"$(id -u)" 2>&1 | grep -v 'No such'
ls -d "${XDG_STATE_HOME:-$HOME/.local/state}/llm-tools" "${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools" 2>&1 | grep -v 'No such'
ss -xlp 2>/dev/null | grep -E 'llm-usage|llm-bridge|/lb-'                    # no listening socket
```

No TCP or UDP port is opened by llm-tools, so `ss -tulpn` shows none that belongs to it.

## Moving to another host and another architecture

On the old host:

```bash
llm-bridge stop --mission ID        # for each live mission; a backup of a running mission is consistent, but its controller state is not carried
llm-bridge backup --output llm-bridge-backup.sqlite3
```

Copy `llm-bridge-backup.sqlite3` to the new host (the file is mode 0600; use `scp` or another
private channel). On the new host, install llm-tools, then:

```bash
llm-bridge restore --input llm-bridge-backup.sqlite3
llm-bridge missions
```

`restore` refuses while a mission controller runs there, keeps any existing database beside the
new one as `bridge.sqlite3.before-restore-<time>`, migrates an older backup to the current schema, and
refuses a backup from a newer Bridge ("Upgrade Bridge first"). It raises the event id counter so a
coordinator that already holds newer events never sees an id reused.

What the backup contains:

- Missions, tasks, task text, amendments, events, operations and verification evidence.
- The absolute path of each mission's working folder (`missions.cwd`, the identity derived from it,
  and the `cwd` in the mission-created event). **No other absolute path of the old host is stored**
  (`tests/test_uninstall_portability.py::test_backup_holds_no_path_of_the_source_host_except_mission_folders`
  checks the state directory, home directory and runtime directory).

What it does not contain, so restore it or redo it on the new host:

- The working folders themselves. Missions keep their old folder path; to continue a mission, make
  the project available at the same path on the new host. History, tasks and events are readable
  without the folder. Bridge has no command to repoint a mission at another folder yet.
- Worker enrolment and remote login: run `llm-bridge worker enroll` and `llm-bridge remote login`
  again (the new host gets a new worker id).
- `config.toml`, provider sign-in (each provider CLI's own login), usage caches and run logs.
- A running mission's controller: start it again with `llm-bridge start --mission ID`.

### Why the file works on any architecture

The backup is a SQLite database file written with the SQLite online backup API and then closed in
rollback-journal mode, so it is a single self-contained file with no `-wal` or `-shm` companion.
The SQLite project states that "SQLite databases are portable across 32-bit and 64-bit machines and
between big-endian and little-endian architectures" and that a database created on one machine "can be
copied and used on a different machine with a different architecture"
([single-file format](https://www.sqlite.org/onefile.html)). It also states that database files "are
bit-for-bit identical on 32-bit, 64-bit, big-endian, and little-endian platforms", that the on-disk
format stays fully backwards compatible, and that the developers plan to support it through 2050
([long-term support](https://www.sqlite.org/lts.html)). The byte-level layout is defined in the
[file format specification](https://www.sqlite.org/fileformat2.html).
`tests/test_uninstall_portability.py::test_backup_file_format_does_not_depend_on_the_architecture`
checks the three properties this relies on in every backup: the `SQLite format 3` header, the
rollback-journal format bytes and the UTF-8 encoding marker.

Do not copy `bridge.sqlite3` itself while a controller runs: its `-wal` file holds committed data
that the main file does not. Always use `llm-bridge backup`.

## Not covered

- The coordinator: [distributed install](../distributed/install.md) (container and native removal, its
  own backup and the key files a coordinator backup does not contain).
- Windows hosts: the Bridge controller and worker need tmux and a POSIX terminal, so there is no
  Bridge service to remove there; `pipx uninstall llm-tools` removes the commands.
