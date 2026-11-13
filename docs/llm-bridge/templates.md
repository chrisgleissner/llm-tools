# Mission templates

A template is a TOML file that describes a mission: its objective, tasks and
their dependencies, the verifier, the rotation and the execution policy. It lets
you create the same kind of mission again, or share one between folders and
computers. A complete example is in [example-template.toml](./example-template.toml).

```bash
llm-bridge mission create --template release            # NAME: release.toml in the templates folder
llm-bridge mission create --template ./plans/rel.toml   # PATH: contains '/' or ends in .toml
llm-bridge mission export ID --template ./rel.toml      # write a mission as a template
llm-bridge mission export ID --template -               # print it to standard output
llm-bridge mission export ID --template release         # write NAME.toml in the templates folder
```

Creating from a template saves the mission and its tasks and starts no provider,
like any `mission create`.

## Where names are looked up

A value without `/` that does not end in `.toml` is a template name. It resolves to
`templates/NAME.toml` in the folder that holds the llm-tools config file
(`$XDG_CONFIG_HOME/llm-tools/templates/`, or next to `LLM_TOOLS_CONFIG`). An unknown
name is an error (exit status 2) that lists the available names.

## Keys

| Key | Meaning |
| --- | --- |
| `template = 1` | Schema version. Required. |
| `name` | Mission name. Optional. |
| `objective` | Mission objective. Required unless the file has tasks or `--prompt`/`--file` is given. |
| `[[tasks]]` | One entry per task, in order. `id` (local to the file), `title`, optional `body` (default: the title), optional `depends_on` (list of ids), optional `acceptance` (list of strings). |
| `[verifier]` | `command` (split like `--verifier`) or `argv` (list), optional `files` (extra files to hash, relative to the mission folder), optional `timeout_seconds`. |
| `[rotation]` | `providers` or `routes`, lists of names, checked like `--providers` / `--routes`. |
| `[policy]` | `teams` (`"on"`/`"off"`), `token_spend`, `mode` (`native`, `background`, `headless`), `trust_folder`, `auto_continue`, `log_max_bytes`, `log_rotations`, `log_retained_runs`, `capacity_refresh_seconds`. |

An unknown key is refused with its name. An unknown dependency id or a dependency
cycle is refused before anything is saved. The mission folder is never part of a
template: it is `--cwd` (default: the current folder).

## Command-line options win

An option you type overrides the template value: `--name`, `--providers`,
`--routes`, `--teams`, `--token-spend`, `--mode`, `--log-*`,
`--capacity-refresh-seconds`, and `--verifier` / `--verifier-arg` / `--`
(replacing the whole template verifier). `--prompt` / `--file` replaces the
objective; the tasks still come from the template. `--verifier-file` adds to the
template's `files`. `--acceptance` is refused together with `--template`; put
acceptance criteria on the template's tasks.

## Verifier guard

A verifier from a template is treated exactly like one from the command line.
`mission create` hashes the verifier program, the script it interprets and the
listed `files` at creation. `files` entries must stay inside the mission folder
(no absolute path, no `..`). If one of those files changes later, a passing
verifier no longer completes a task without your approval.
Review a template's verifier before you create a mission from a file you did not
write: its command runs on your computer when a task is verified.

## Export

`mission export` writes the same schema. Creating a mission from the exported
file gives the same configuration and task graph. Task ids are written as `t1`,
`t2`, ... by task number; cancelled tasks and unaccepted proposals are left out.
The file contains the task prompts and the verifier command line, so treat it like
the mission itself and keep secrets out of both. It is written readable by you only
(mode 0600; a new templates folder is 0700). It holds no folder path and none of the
saved llm-tools config or `--command-template`. An existing file is not overwritten
unless you pass `--force`. `--template` always takes a value; `-` means standard
output. With `--json` the result is `{"path": ...}`, or `{"template": ...}` for `-`.
