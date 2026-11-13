# LLM Bridge troubleshooting

Every error that `llm-bridge` prints ends with a line like

```text
see: https://github.com/chrisgleissner/llm-tools/blob/main/docs/llm-bridge/troubleshooting.md#mission-not-found
```

and with `--json` the error object has the same address in `help`. The part
after `#` is the error code, and each code below is a heading with that exact
name. The [user guide](guide.md) explains the concepts; [operations](operations.md)
lists every option.

Each entry says what you see, why it happens, and what to do. Start with
`llm-bridge doctor`; it checks this computer and prints a `Next` line.

## Symptoms

### Controller not running (INTERRUPTED)

What you see: `llm-bridge missions` or `status` shows `INTERRUPTED (was
RUNNING)`, or the home screen shows INTERRUPTED next to a mission.

Why: the mission's controller process is gone. The computer restarted, the
controller was killed, or it crashed. The mission and its tasks are safe in the
database; nothing runs.

What to do:

1. Read the last events: `llm-bridge events --limit 20`.
2. Run `llm-bridge doctor --mission ID` if the events show an error.
3. Continue with `llm-bridge resume` (or `llm-bridge start`). This starts a
   provider and uses paid capacity. Do not start a second controller by hand.

### Provider not signed in

What you see: `llm-bridge doctor` says `claude: not signed in` (or another
provider), or finishes with `no provider CLI is installed and signed in`. The
provider's terminal may show its own login prompt.

Why: the provider CLI is installed but has no credentials, or is not installed.
Bridge checks the provider's own sign-in files offline and never signs in for
you.

What to do: run the provider CLI once yourself and sign in (for example
`claude`, then `/login`), then run `llm-bridge doctor` again. To use another
provider, change the rotation: `llm-bridge rotation --set codex`.

### Waiting for capacity

What you see: status or the Activity view says `Waiting for capacity, resumes
in 2h 10m`, or a provider shows `rate-limited`.

Why: every provider in the rotation has used up a 5-hour, weekly or monthly
window. This is not an error. Bridge waits for the earliest real reset and then
continues by itself. The computer stays awake unless the mission opted into
sleep with `--suspend-when-rate-limited on`.

What to do:

1. Look at the numbers: `llm-bridge usage`, or `llm-usage --watch 60`.
2. To work sooner, add a provider that has capacity:
   `llm-bridge rotation --set claude,codex`.
3. To stop waiting, `llm-bridge pause` holds the mission and `llm-bridge resume`
   continues it later.

### Task blocked on a question

What you see: `status` shows a `Needs you` line such as `task 2 asked: "Which
database should I use?"` with three commands, and the task is BLOCKED.

Why: a provider ended its run with a question and nobody answered. Bridge does
not guess an answer.

What to do, either:

- put the answer into the task text and retry:
  `llm-bridge task amend --task 2 --file task-with-answer.txt`, then
  `llm-bridge task retry --task 2`;
- run it again unchanged with `llm-bridge task retry --task 2`;
- drop it with `llm-bridge task cancel --task 2`.

A Claude Code folder-trust prompt that you declined (the first option is `No,
exit`) also holds the task BLOCKED. Run `llm-bridge start` again and choose
`Yes, I trust this folder`.

### Usage limit reached in native mode

What you see: the provider's own terminal in the station shows its usage limit
notice (`Claude usage limit reached`, `You've hit your usage limit`), and the
Activity view shows `usage limit reached, resets in ...` or `reset time
unknown`.

Why: in native mode the provider talks to you directly. Bridge reads the
station screen, recognises the notice, records when the provider is usable
again and moves to the next provider in the rotation at a safe point. A notice
without a reset time is retried after a short backoff.

What to do: nothing, unless every provider is limited (see [Waiting for
capacity](#waiting-for-capacity)). To switch at once, run
`llm-bridge steer --kind handoff --to codex`. `llm-bridge usage` shows the
capacity Bridge currently believes each provider has.

### Station gone

What you see: `llm-bridge attach` says there is no station, or the tmux window
closed and the mission shows no terminal.

Why: the private tmux session ended (the computer restarted, the tmux server was
killed, or the mission is headless and never had one). A dead socket can also be
left behind. The mission is not affected, but a native provider's open
conversation is lost with its terminal.

What to do:

1. `llm-bridge status` shows whether the controller is alive.
2. Run `llm-bridge start` (the command `attach` names) or `llm-bridge resume`.
   A later resume creates or reconnects the station as needed. This starts a
   provider and uses paid capacity.
3. A headless mission has no station at all; read it with `llm-bridge events
   --follow` and `llm-bridge task output`.
4. `llm-bridge gc` removes the idle stations of finished missions.

### State directory must be private

What you see: `llm-bridge: Bridge state directory must be owned by this user and
private: /path/to/llm-bridge`, usually with `fix with: chmod 700 /path`.

Why: Bridge keeps prompts and provider logs in its state directory, so it
refuses to run when another user could read it. The directory is owned by a
different user, or its permissions allow group or other access.

What to do: run the `chmod 700 PATH` command from the message. If the
directory belongs to another user (for example after using `sudo`), set
`LLM_BRIDGE_STATE_DIR` to a directory you own, or fix the owner with
`chown`. Never loosen the permissions to make the message go away.

### Locked out of the coordinator web interface

What you see: the coordinator's sign-in page rejects the administrator password
(`invalid username or password`), or the password is forgotten. `llm-bridge
remote login` with an API token may still work.

Why: the password is only stored as a hash and cannot be recovered. It can be
replaced from a shell on the machine that runs the coordinator, which proves
that you control the host.

What to do: in an SSH session on the coordinator host run

```sh
llm-bridge-coordinator reset-password
```

It asks for a new password twice (without echo). Use `--temporary` to
generate a one-time password that the web interface forces you to change at the
next sign-in, and `--keep-api-tokens` to keep API tokens (they are revoked by
default, as are all web sessions). The command uses docker through `sudo` when
your user is not in the docker group, or `sudo bridge-coordinator admin
reset-password` on a native install. The wrapper is installed with the compose
files (`sudo container/install-cli.sh`) or by `deploy/install.sh`. The exact
sessions and output are in
[install](../distributed/install.md#locked-out-or-forgot-the-password).

## Error codes

The status is the process exit status. A `--json` error is printed on standard
output as `{"error": {"code", "message", "hint", "help"}}`.

### usage

Exit 2. The command line is not valid: an unknown command or option, a missing
value, an option in the wrong place, `--target` without a name, or a command
that needs an interactive terminal when there is none.

The message usually suggests the closest valid spelling and names the help to
read, for example `llm-bridge task --help`. Place `--target NAME` before the
command: `llm-bridge --target NAME status`.

### invalid-argument

Exit 2. The command is valid but a value is not: a number out of range, an
unknown provider or route, a verifier timeout outside 1-3600 seconds, a task
number that is not a number.

Read the message for the accepted range, and `llm-bridge COMMAND --help` for the
option.

### mission-not-found

Exit 3. No mission matches the ID prefix you gave, or there is no mission for
the current folder.

List missions with `llm-bridge mission list` and pass `--mission ID` (three or
more characters of the ID), or run the command inside the mission's folder.
A mission created in another folder is not found from here unless you give its
ID. If you use a different state directory (`LLM_BRIDGE_STATE_DIR`), check
that the variable is the same as when you created the mission.

### task-not-found

Exit 3. The mission has no task with that number.

Run `llm-bridge task list --mission ID` to see the numbers. Task numbers start
at 1 in each mission.

### not-found

Exit 3. A resource other than a mission or task is missing, or there is nothing
to show: `task output` or `task diff` for a task that has not run, a log that
was pruned, or a backup file that does not exist.

Read the message. For `task output` and `task diff`, the task needs to have run
first: see `llm-bridge task list`.

### mission-ambiguous

Exit 3. The ID prefix matches more than one mission.

Use more characters of the ID, copied from `llm-bridge mission list`.

### wrong-state

Exit 4. The mission or task is not in a state that allows the command: `start`
on a cancelled mission, `task approve` on a task that is not in REVIEW, `finish`
without a native provider, `attach` without a station, or `mission delete` on a
mission that is running, paused or stopping (stop it first, or cancel it) or that
still has a controller or an open provider station (`llm-bridge gc` closes the
idle stations of finished missions).

The message names the state. `llm-bridge status` shows what the mission is doing
now, and `llm-bridge help states` explains each state. A repeated `pause` or
`stop` is reported as already done and exits 0.

### conflict

Exit 4. The task changed between the time you read it and the time your command
was applied, for example you amended a task that a provider is already working
on, or two operators changed it at once. Nothing was applied.

Run `llm-bridge task show --task N` to read the current text, then repeat the
command if it still makes sense.

### confirmation-required

A request to delete a mission (`mission.delete`, or `DELETE /api/v1/missions/ID`) did
not contain `confirm: true`, so nothing was deleted. The command line never sends this:
`llm-bridge mission delete` asks first, or takes `--yes`. A script that calls the REST
resource must send the body `{"confirm": true}`.

### schema-too-new

Exit 4. The database was written by a newer `llm-bridge` than the one you are
running (for example after trying a newer version and going back).

Upgrade llm-tools, or restore the backup that was made before the upgrade: the
message names it, and `llm-bridge restore --input FILE` or copying it over the
database restores it. Bridge never downgrades a database.

### refused-in-managed-session

Exit 2. The command ran inside a provider session that Bridge manages
(`LLM_BRIDGE_MISSION_ID` is set), and a managed provider may only read its own
mission and record proposals. Commands that start, stop, approve or edit work
need the operator.

Run the command from your own terminal, outside the provider. To suggest work
from inside a provider use `llm-bridge proposal create`; you accept it later.

### failed

Exit 1. A runtime failure that has no more specific code: a provider, the
controller, the disk, tmux or the network failed.

Run `llm-bridge doctor` and read `llm-bridge events --limit 20`. If the message
mentions the database being busy, wait for the other command to finish and
repeat it. `llm-bridge doctor --bundle` writes a redacted archive you can
attach to a bug report.

### invalid-request

Exit 1 (as a command error). A remote or scripted request was not a valid
command envelope: not a JSON object, an unknown field, a bad type, or a
`request_id` that is not 1-128 letters, digits, `.`, `_` or `-`.

This comes from a client of the typed command layer, not from typing a command.
Fix the client; see [distributed operations](../distributed/operations.md).

### invalid-target

Exit 2. A `--target` name is not valid: use lowercase letters, digits and `-`,
or `local` or `all`. The message can also say that a local dispatch was given a
target.

List the registered names with `llm-bridge worker list`.

### unsupported-operation

Exit 1 (as a command error). The worker or this version does not know the
operation in the request.

The message lists the supported operations. Upgrade the older side so that
both run the same llm-tools version.

### unsupported-remote-option

Exit 1 (as a command error). `mission create --target WORKER` was given an
option that a worker cannot apply: `--command-template`, `--verifier`,
`--verifier-arg`, `--verifier-file` or `-- COMMAND`. These would let the caller
choose a command or file on the worker. The message names each option.

Declare the verifier in the worker's `config.toml` as `[bridge.verifiers.NAME]`
and give `--verifier-name NAME`, or create the mission on the worker itself.

### unknown-verifier

Exit 1 (as a command error). `mission create --verifier-name NAME` named a
verifier that the machine running the mission does not declare, or the name is
not lowercase letters, digits, `-` and `_`.

Add `[bridge.verifiers.NAME]` with `argv` (and optionally `files` and
`timeout_seconds`) to that machine's llm-tools `config.toml`, or use a name it
declares.

### unsupported-version

Exit 1 (as a command error). The request uses a command schema version this
side does not support. The message lists the supported versions.

Upgrade the older side.

### unsupported-platform

Exit 2. The command needs Linux or macOS with tmux or POSIX locks, and you ran
it on Windows. Windows supports the remote client only.

Use `llm-bridge --target NAME COMMAND`, `llm-bridge remote login|status|logout`
or `llm-bridge worker list`, or run the command on a Linux or macOS computer.

### forbidden

A worker refused a remote operation because it was enrolled read-only. Enrol
the worker with `--allow-control` (`llm-bridge worker enroll --allow-control`)
if the coordinator should be able to start and control missions on it.

### internal

A command crashed in a way Bridge did not expect. The message names the
exception type only; details stay in the local logs.

Repeat the command once. If it fails again, run `llm-bridge doctor --bundle`
and report the bundle.

### rejected

A command was accepted for processing but its effect was refused when it was
applied, for example because the task changed in between. The message carries
the reason. Read it, check `llm-bridge status` and decide whether to repeat the
command. `llm-bridge events` shows a `command-rejected` event with the reason.

### offline

A worker is registered but not connected right now, so `--target all` skipped it.
Check that `llm-bridge worker run` (or its service) runs on that computer, and
look at `llm-bridge worker list`.

### unreachable

The coordinator or the worker did not answer, so the request may still be
queued. Exit 3 for `--target`.

Check the coordinator address with `llm-bridge remote status`, that the
coordinator is running, and the network. Do not repeat a command that changes
work until you have looked at `llm-bridge --target NAME events`: a request that
was queued runs when the worker returns.

### stale-generation

A worker refused a coordinator connection or command that is older than one it
has already accepted. This protects against a replayed or restarted
coordinator. Reconnect: restart `llm-bridge worker run`, or restart the
coordinator so that it advertises a newer generation.

### not-welcomed

The worker received a message before the coordinator's welcome. Reconnect the
worker; if it repeats, the coordinator and worker versions or clocks differ.

### unsupported-message

A worker received a message type it does not handle. The coordinator is a newer
version than the worker. Upgrade the worker.
