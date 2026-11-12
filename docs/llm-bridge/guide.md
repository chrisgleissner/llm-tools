# LLM Bridge user guide

This guide explains the ideas behind Bridge and walks through the five things
every operator does. For every option of every command see
[operations](operations.md). When a command fails, its last line names an entry
in [troubleshooting](troubleshooting.md).

The same words are available offline: `llm-bridge help terms`, `llm-bridge help
quickstart` and `llm-bridge help` (the list of topics).

## Concepts

| Word | Meaning |
| --- | --- |
| Mission | One piece of work in one folder: an objective and an ordered list of tasks. Missions are saved, so closing the terminal does not lose them. |
| Task | One step of a mission. Each task has a number (1, 2, ...) and a state: waiting, running, in REVIEW (waiting for you), completed or cancelled. |
| Provider | The AI command-line tool that does the work, for example Claude Code, Codex or Copilot. A route is a provider pinned to one model. |
| Rotation | The ordered list of providers a mission may use. When one runs out of capacity, Bridge moves on to the next. Change it with `llm-bridge rotation`. |
| Controller | The background process that runs a mission: it starts the provider, watches capacity and moves from task to task. `llm-bridge start` starts it and `llm-bridge stop` ends it. |
| Station | The terminal window (a private tmux session) where you watch or use a provider. `llm-bridge attach` opens it. Leaving it does not stop the mission. |
| Review | The state of a finished task that waits for your decision. Read its output and diff (and the verifier result, if the mission has a verifier), then approve or reject it. A provider that exits cleanly has not necessarily done the work. |
| Capacity | How much of a provider's usage allowance is left: the 5-hour, weekly and monthly windows that `llm-bridge usage` shows. |

Three facts explain most of Bridge's behaviour:

- State lives in a private SQLite database in your state directory, not in a
  terminal. Exiting Bridge, detaching or losing an SSH connection never stops
  a mission.
- Only `start`, `resume` and the home screen's "Start and open provider" start
  a provider and use paid capacity. Creating a mission, opening the home screen
  and every read-only command do not.
- Bridge never marks work completed without you, or a verifier you configured.

## 1. Create and start a mission

1. Check the machine: `llm-bridge doctor`. Its `Next` line says what to do
   (install a provider, sign in, install tmux).
2. In your project folder, save the mission. This starts nothing.

   ```bash
   llm-bridge mission create --file task.md --name demo --mode native
   ```

   Add `--providers claude,codex` to choose the rotation, or `--verifier
   'python -m pytest -q'` to check each finished task.
3. Start work. This uses paid capacity.

   ```bash
   llm-bridge start
   ```

4. Check what it is doing with `llm-bridge status`. `llm-bridge missions` lists
   every mission, live ones first.

To reuse a mission's shape, `llm-bridge mission export ID --template rel.toml` saves it
and `llm-bridge mission create --template rel.toml` creates a new one from it; see
[Mission templates](./templates.md).

Without a terminal on hand, `--mode background` (the command-line default) runs
the provider under a controller, and `--mode headless` needs no tmux at all.
On a terminal, `llm-bridge` with no command opens the home screen, where `N`
creates a mission.

## 2. Watch and steer a mission

1. Open the provider's terminal: `llm-bridge attach`. Detach with Ctrl-] then
   `d`; the mission keeps running. `llm-bridge control` opens the full control
   view.
2. Read progress without a terminal:

   ```bash
   llm-bridge status
   llm-bridge events --limit 20
   llm-bridge events --follow
   llm-bridge task list
   ```

3. Add work or change it:

   ```bash
   llm-bridge task add --file next-task.md
   llm-bridge task amend --task 2 --file better-task-2.md
   llm-bridge task cancel --task 3
   ```

   What the provider sees depends on the mission mode. A new or amended task is
   stored in the queue and runs in its turn. In a native mission with a running
   Claude Code, Codex or Copilot, Bridge also types one short notice into that
   provider's terminal at its next idle prompt (the pane has not changed for a few
   seconds, the composer is empty and no approval dialog is open). Bridge never
   submits your unsent text: text you have typed in the provider's prompt (anything
   that is not the faint placeholder) holds the notice back until the prompt is
   empty, and a notice held back for ten minutes is recorded once in the activity
   log. The notice
   names the task number and title and the command to read it
   (`llm-bridge task show --mission ID --task N`), and tells the provider not to
   start the task: Bridge still starts it after the current one. Several changes
   made before the provider is idle become one notice, and a task is announced
   once. The receipt says which case applies: `delivered to the running provider
   at its next prompt`, or `starts after task N`. Headless and background missions
   have no provider terminal to type into and keep the plain queue. To turn the
   notices off, set `task_notices = false` under `[bridge]` in
   `~/.config/llm-tools/config.toml`; the receipt then says `starts after task N`.

4. Change who works, at the next safe point:

   ```bash
   llm-bridge rotation --set claude,codex
   llm-bridge steer --kind handoff --to codex
   ```

5. Hold the mission without losing anything: `llm-bridge pause` waits for the
   provider's current step, and `llm-bridge resume` continues. Pause never
   suspends the computer.

## 3. Review, approve or reject work

A task whose provider exited moves to REVIEW. `llm-bridge status` then shows a
`Needs you` line with the command to run.

1. Read the evidence:

   ```bash
   llm-bridge task show --task 1
   llm-bridge task output --task 1
   llm-bridge task diff --task 1
   ```

2. In a native mission, if the provider is still open and waiting, ask it to
   exit so the task moves to review: `llm-bridge finish`.
3. Approve a good result, or send it back with a reason that the next prompt
   quotes:

   ```bash
   llm-bridge task approve --task 1 --evidence 'tests passed'
   llm-bridge task reject --task 1 --reason 'tests were not run'
   ```

4. To start the next task while one is still waiting in review, run
   `llm-bridge continue`.
5. Providers can suggest tasks. Review them with `llm-bridge proposal list`,
   then `llm-bridge proposal accept N` or `llm-bridge proposal reject N`. A
   proposal adds no work until you accept it. `llm-bridge proposal create`
   records one on behalf of a provider operation.

## 4. Handle a provider that ran out of capacity or asks a question

Out of capacity:

1. See the numbers: `llm-bridge usage`, or `llm-usage --watch 60` for the live
   table of all providers.
2. By default Bridge moves to the next provider in the rotation. If every
   provider is exhausted, the mission waits for the earliest reset
   ("waiting for capacity") and then continues by itself. Add a provider with
   `llm-bridge rotation --set claude,codex`.
3. To let the computer sleep until capacity returns, see `llm-bridge
   suspend-until-credits` and the mission setting `--suspend-when-rate-limited
   on` (see [operations](operations.md)). Both arm a wake timer first.

A provider asks a question, or a task is blocked:

1. `llm-bridge status` shows `Needs you` with the question and three ways
   forward.
2. Answer by putting the answer into the task text and retrying:

   ```bash
   llm-bridge task amend --task 2 --file task-with-answer.txt
   llm-bridge task retry --task 2
   ```

   Or run the task again unchanged with `llm-bridge task retry --task 2`, or
   drop it with `llm-bridge task cancel --task 2`.

See [troubleshooting](troubleshooting.md#task-blocked-on-a-question) and
[waiting for capacity](troubleshooting.md#waiting-for-capacity).

## 5. Stop, resume, back up and clean up

1. Stop the controller (the provider finishes its step first):
   `llm-bridge stop`. `llm-bridge stop --after-task` waits until the running
   task is completed, and `llm-bridge stop --force` ends the provider now.
   `llm-bridge resume` or `llm-bridge start` continues.
2. Cancel for good with `llm-bridge cancel --yes`. This cannot be undone.
3. Back up the database before an upgrade or a risky change, and restore it
   if needed. A backup never overwrites an existing file, and a restore keeps
   the current database beside it.

   ```bash
   llm-bridge backup --output bridge-backup.db
   llm-bridge restore --input bridge-backup.db
   ```

4. Close the idle stations of finished missions: `llm-bridge gc`.
5. If something is wrong and you need help, `llm-bridge doctor --bundle` writes
   a redacted diagnostic archive for a bug report.

## Other computers

A coordinator lets one terminal reach missions on other computers. These
commands are optional; see `llm-bridge help distributed` and
[distributed operations](../distributed/operations.md).

| Command | Does |
| --- | --- |
| `llm-bridge remote login` | Store the coordinator address and token on this computer. |
| `llm-bridge remote status` | Show the stored coordinator address. |
| `llm-bridge remote logout` | Remove the stored coordinator configuration. |
| `llm-bridge worker enroll` | Register this computer as a worker. |
| `llm-bridge worker rotate-token` | Install a coordinator-staged worker token without enrolling again. |
| `llm-bridge worker status` | Show this worker's enrolment and connection. |
| `llm-bridge worker run` | Run the worker in the foreground. |
| `llm-bridge worker install-service` | Install and start a systemd user unit for the worker. |
| `llm-bridge worker uninstall-service` | Remove that unit. |
| `llm-bridge worker audit` | Show the worker's security audit log. |
| `llm-bridge worker unenroll` | Remove the worker's enrolment. |
| `llm-bridge worker list` | List the workers registered with the coordinator. |

Run a command on a worker with `llm-bridge --target NAME COMMAND`.

## Exit status and error output

Scripts can rely on the exit status: 0 success, 1 runtime failure, 2 usage
error, 3 not found or ambiguous, 4 wrong state. An error prints its message
and a final line `see: URL#CODE` that points at the matching entry in
[troubleshooting](troubleshooting.md). With `--json` the error object carries
the same address in `help`.
