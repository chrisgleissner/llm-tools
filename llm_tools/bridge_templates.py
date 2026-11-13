"""Mission templates: ``mission create --template`` reads one, ``mission export --template`` writes one.

A template is a TOML file (schema version ``template = 1``) holding a mission's objective, its tasks and
their dependencies, the verifier, the rotation and the execution policy. Creating from a template goes
through the same validation as the command-line options: the loader only fills ``args`` attributes that the
operator did not give on the command line, and ``mission create`` then builds the config as usual, so the
verifier fingerprints (``bridge_verifier_guard``) and the rotation checks apply unchanged.

Standard library only. Reading uses ``tomllib``; writing uses a small escaper (``toml_string``).
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from pathlib import Path
from typing import Any

TEMPLATE_VERSION = 1
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]+")

_TOP_KEYS = {"template", "name", "objective", "tasks", "verifier", "rotation", "policy"}
_TASK_KEYS = {"id", "title", "body", "depends_on", "acceptance"}
_VERIFIER_KEYS = {"command", "argv", "files", "timeout_seconds"}
_ROTATION_KEYS = {"providers", "routes"}
_POLICY_KEYS = {"teams", "token_spend", "mode", "isolate", "trust_folder", "auto_continue",
                "log_max_bytes", "log_rotations", "log_retained_runs", "capacity_refresh_seconds"}
_MODES = ("native", "background", "headless")
_INT_POLICY = ("log_max_bytes", "log_rotations", "log_retained_runs", "capacity_refresh_seconds")

# Command-line spellings that count as "the operator chose this" for each ``args`` attribute.
_OPTION_DEST = {
    "--name": "name", "--providers": "providers", "--routes": "routes", "--teams": "teams",
    "--token-spend": "token_spend", "--mode": "mode", "--isolate": "isolate", "--headless": "mode", "--native": "mode",
    "--trust-folder": "trust_folder", "--auto-continue": "auto_continue",
    "--log-max-bytes": "log_max_bytes", "--log-rotations": "log_rotations",
    "--log-retained-runs": "log_retained_runs", "--capacity-refresh-seconds": "capacity_refresh_seconds",
    "--verifier": "verifier_command", "--verifier-arg": "verifier_argv", "--verifier-file": "verifier_files",
    "--verifier-timeout": "verifier_timeout", "--acceptance": "acceptance",
    "--prompt": "prompt", "--file": "prompt", "--prompt-file": "prompt",
}


def explicit_options(arguments: list[str]) -> frozenset[str]:
    """The ``args`` attributes the command line set, so a template never overrides them."""
    found: set[str] = set()
    for word in arguments:
        if word == "--":
            found.add("verifier_command")
            break
        dest = _OPTION_DEST.get(word.split("=", 1)[0]) if word.startswith("--") else None
        if dest:
            found.add(dest)
    return frozenset(found)


# -- locating -----------------------------------------------------------------------------------

def templates_dir() -> Path:
    """``<config dir>/llm-tools/templates``, next to the llm-tools config file."""
    from . import config as toolconfig

    return toolconfig.config_path().parent / "templates"


def is_path_reference(ref: str) -> bool:
    return "/" in ref or ref.endswith(".toml")


def available_names() -> list[str]:
    try:
        return sorted(p.stem for p in templates_dir().glob("*.toml") if p.is_file() and _NAME.fullmatch(p.stem))
    except OSError:
        return []


def resolve_reference(ref: str) -> Path:
    """The file a ``--template`` value names; an unknown name lists the available ones."""
    if is_path_reference(ref):
        path = Path(ref).expanduser()
        if not path.is_file():
            raise ValueError(f"template file not found: {ref}")
        return path
    if not _NAME.fullmatch(ref):
        raise ValueError(f"invalid template name {ref!r}: use letters, digits, '_', '.' and '-', or give a path containing '/' or ending .toml")
    path = templates_dir() / f"{ref}.toml"
    if not path.is_file():
        names = available_names()
        listing = ", ".join(names) if names else f"none; put NAME.toml files in {templates_dir()}"
        raise ValueError(f"unknown template {ref!r}; available templates: {listing}")
    return path


# -- reading ------------------------------------------------------------------------------------

def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            raise ValueError(f"unknown key {key!r} in {where}; allowed: {', '.join(sorted(allowed))}")


def _str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be a list of strings")
    return list(value)


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a table")
    return value


def parse_template(text: str, label: str = "template") -> dict[str, Any]:
    """Validate TOML text and return the normalised template; ``ValueError`` names the problem."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{label} is not valid TOML: {exc}") from None
    try:
        return _normalise(raw)
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    _check_keys(raw, _TOP_KEYS, "the template")
    if raw.get("template") != TEMPLATE_VERSION:
        raise ValueError(f"missing or unsupported version: start the file with template = {TEMPLATE_VERSION}")
    result: dict[str, Any] = {}
    for key in ("name", "objective"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
            result[key] = raw[key]
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        raise ValueError("tasks must be an array of tables: [[tasks]]")
    result["tasks"] = _normalise_tasks(tasks)
    if "objective" not in result and not result["tasks"]:
        raise ValueError("give an objective or at least one [[tasks]] entry")
    if "verifier" in raw:
        result["verifier"] = _normalise_verifier(_table(raw["verifier"], "verifier"))
    if "rotation" in raw:
        rotation = _table(raw["rotation"], "rotation")
        _check_keys(rotation, _ROTATION_KEYS, "[rotation]")
        result["rotation"] = {key: _str_list(value, f"rotation.{key}") for key, value in rotation.items()}
    if "policy" in raw:
        result["policy"] = _normalise_policy(_table(raw["policy"], "policy"))
    return result


def _normalise_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks, 1):
        _check_keys(task, _TASK_KEYS, f"[[tasks]] entry {index}")
        ident = task.get("id")
        if not isinstance(ident, str) or not _TASK_ID.fullmatch(ident):
            raise ValueError(f"[[tasks]] entry {index} needs an id of letters, digits, '_', '.' and '-'")
        if ident in seen:
            raise ValueError(f"task id {ident!r} is used twice")
        seen.add(ident)
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"task {ident!r} needs a non-empty title")
        body = task.get("body", title)
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"task {ident!r}: body must be a non-empty string")
        out.append({
            "id": ident, "title": title, "body": body,
            "depends_on": _str_list(task.get("depends_on", []), f"task {ident!r} depends_on"),
            "acceptance": _str_list(task.get("acceptance", []), f"task {ident!r} acceptance"),
        })
    for task in out:
        for dep in task["depends_on"]:
            if dep == task["id"]:
                raise ValueError("a task cannot depend on itself: " + repr(dep))
            if dep not in seen:
                raise ValueError(f"task {task['id']!r} depends on unknown task {dep!r}")
    _reject_cycle(out)
    return out


def _reject_cycle(tasks: list[dict[str, Any]]) -> None:
    graph = {t["id"]: t["depends_on"] for t in tasks}
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = path[path.index(node):] + [node]
            raise ValueError("that dependency would create a cycle: " + " -> ".join(cycle))
        state[node] = 1
        for dep in graph[node]:
            visit(dep, path + [node])
        state[node] = 2

    for node in graph:
        visit(node, [])


def _normalise_verifier(table: dict[str, Any]) -> dict[str, Any]:
    _check_keys(table, _VERIFIER_KEYS, "[verifier]")
    if "command" in table and "argv" in table:
        raise ValueError("[verifier] takes command or argv, not both")
    if "command" not in table and "argv" not in table:
        raise ValueError("[verifier] needs command or argv")
    result: dict[str, Any] = {}
    if "command" in table:
        if not isinstance(table["command"], str) or not table["command"].strip():
            raise ValueError("verifier.command must be a non-empty string")
        result["command"] = table["command"]
    else:
        result["argv"] = _str_list(table["argv"], "verifier.argv")
        if not result["argv"]:
            raise ValueError("verifier.argv must not be empty")
    if "files" in table:
        result["files"] = _str_list(table["files"], "verifier.files")
        for entry in result["files"]:
            if os.path.isabs(entry) or ".." in Path(entry).parts:
                raise ValueError(f"verifier.files entry {entry!r} must be a path inside the mission folder: no absolute path and no '..'")
    if "timeout_seconds" in table:
        value = table["timeout_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("verifier.timeout_seconds must be a number")
        result["timeout_seconds"] = float(value)
    return result


def _normalise_policy(table: dict[str, Any]) -> dict[str, Any]:
    _check_keys(table, _POLICY_KEYS, "[policy]")
    result: dict[str, Any] = {}
    for key, value in table.items():
        if key == "teams":
            value = ("on" if value else "off") if isinstance(value, bool) else value
            if value not in ("on", "off"):
                raise ValueError("policy.teams must be \"on\" or \"off\"")
        elif key == "token_spend":
            if value not in ("low", "medium", "high"):
                raise ValueError("policy.token_spend must be low, medium or high")
        elif key == "mode":
            if value not in _MODES:
                raise ValueError("policy.mode must be one of " + ", ".join(_MODES))
        elif key == "isolate":
            if value not in ("none", "worktree"):
                raise ValueError("policy.isolate must be \"none\" or \"worktree\"")
        elif key in ("trust_folder", "auto_continue"):
            if not isinstance(value, bool):
                raise ValueError(f"policy.{key} must be true or false")
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"policy.{key} must be a whole number")
        result[key] = value
    return result


def load_template(ref: str) -> dict[str, Any]:
    path = resolve_reference(ref)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read template {path}: {exc}") from None
    return parse_template(text, f"template {path.name}")


# -- applying to the create arguments -----------------------------------------------------------

def apply_to_args(args: argparse.Namespace, template: dict[str, Any], explicit: frozenset[str]) -> None:
    """Fill the ``mission create`` arguments the command line left unset.

    Explicit command-line options win. The mission is then built by the usual code path, which
    validates rotation, policy and verifier and records the verifier fingerprints.
    """
    if "acceptance" in explicit:
        raise ValueError("--acceptance cannot be combined with --template; put acceptance in the template's [[tasks]]")
    if "prompt" not in explicit and "objective" in template:
        args.prompt, args.prompt_file = template["objective"], None
    elif "prompt" not in explicit:
        args.prompt, args.prompt_file = template["tasks"][0]["body"], None
    if "name" not in explicit and "name" in template:
        args.name = template["name"]
    args.template_tasks = template["tasks"]
    policy = template.get("policy", {})
    for key in ("teams", "token_spend", "mode", "isolate", *_INT_POLICY):
        if key in policy and key not in explicit:
            setattr(args, key, policy[key])
    if "mode" in policy and not ({"mode"} & explicit):
        args.headless = args.native_tui = False
    for key in ("trust_folder", "auto_continue"):
        if policy.get(key) and key not in explicit:
            setattr(args, key, True)
    rotation = template.get("rotation", {})
    if not ({"providers", "routes"} & explicit):
        if rotation.get("providers"):
            args.providers = ",".join(rotation["providers"])
        if rotation.get("routes"):
            args.routes = ",".join(rotation["routes"])
    verifier = template.get("verifier")
    if verifier and "verifier_command" not in explicit and "verifier_argv" not in explicit:
        args.verifier_command = verifier.get("command")
        args.verifier_argv = list(verifier.get("argv", []))
        args.verifier_files = list(verifier.get("files", [])) + list(getattr(args, "verifier_files", []) or [])
        if "timeout_seconds" in verifier and "verifier_timeout" not in explicit:
            args.verifier_timeout = verifier["timeout_seconds"]


def prepare_args(args: argparse.Namespace, auto_continue: bool) -> bool:
    """Apply ``--template`` to the create arguments, if given; returns the effective ``auto_continue``."""
    if not getattr(args, "template", None):
        return auto_continue
    apply_to_args(args, load_template(args.template), getattr(args, "explicit_options", frozenset()))
    return auto_continue or bool(getattr(args, "auto_continue", False))


def create_template_mission(store: Any, args: argparse.Namespace, name: str, prompt: str, cwd: str, config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Create the mission and the template's tasks and dependencies; returns the mission with ``initial_task_id``.

    ``extra`` (``mission_id``, ``isolation``) comes from the shared worktree setup in ``bridge``.
    The store creates everything in one transaction, so a failure leaves no mission and no tombstone.
    """
    return store.create_mission_with_tasks(name, prompt, args.template_tasks, cwd=cwd, config=config, **extra)


# -- writing ------------------------------------------------------------------------------------

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(text: str, *, multiline: bool = False) -> str:
    """A TOML basic string. With ``multiline`` real newlines stay as line breaks."""
    out: list[str] = []
    for char in text:
        if char == "\n" and multiline:
            out.append("\n")
        elif char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    body = "".join(out)
    return f'"""\n{body}"""' if multiline and "\n" in text else f'"{body}"'


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(toml_string(item) for item in items) + "]"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return toml_string(value)
    return repr(value)


def render_template(mission: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """The template text for a stored mission and its operator tasks (ordinal order)."""
    config = mission.get("config") or {}
    lines = [f"template = {TEMPLATE_VERSION}", f"name = {toml_string(str(mission['name']))}",
             f"objective = {toml_string(str(mission['objective']), multiline=True)}", ""]
    rotation = []
    if config.get("routes"):
        rotation.append(f"routes = {_toml_list(_split(config['routes']))}")
    elif config.get("providers"):
        rotation.append(f"providers = {_toml_list(_split(config['providers']))}")
    if rotation:
        lines += ["[rotation]", *rotation, ""]
    policy: dict[str, Any] = {}
    if isinstance(config.get("team_policy_enabled"), bool):
        policy["teams"] = "on" if config["team_policy_enabled"] else "off"
    if config.get("token_spend") in ("low", "medium", "high"):
        policy["token_spend"] = config["token_spend"]
    policy["mode"] = "headless" if config.get("headless") else "native" if config.get("native_tui") else "background"
    policy["isolate"] = "worktree" if mission.get("isolation") == "worktree" else "none"
    for key in ("trust_folder", "auto_continue"):
        if config.get(key):
            policy[key] = True
    for key in _INT_POLICY:
        if isinstance(config.get(key), int) and not isinstance(config.get(key), bool):
            policy[key] = config[key]
    lines += ["[policy]", *(f"{key} = {_toml_value(value)}" for key, value in policy.items()), ""]
    verifier = config.get("verifier")
    if isinstance(verifier, dict) and isinstance(verifier.get("argv"), list) and verifier["argv"]:
        lines += ["[verifier]", f"argv = {_toml_list([str(a) for a in verifier['argv']])}"]
        files = sorted(f for f in (verifier.get("fingerprints") or {}) if not os.path.isabs(f))
        if files:
            lines.append(f"files = {_toml_list(files)}")
        if "timeout_seconds" in verifier:
            lines.append(f"timeout_seconds = {float(verifier['timeout_seconds'])!r}")
        lines.append("")
    by_ordinal = {int(t["ordinal"]): f"t{int(t['ordinal'])}" for t in tasks}
    for task in tasks:
        lines += ["[[tasks]]", f"id = {toml_string(by_ordinal[int(task['ordinal'])])}", f"title = {toml_string(str(task['title']))}",
                  f"body = {toml_string(str(task['prompt']), multiline=True)}"]
        deps = [by_ordinal[o] for o in task.get("depends_on_ordinals", []) if o in by_ordinal]
        if deps:
            lines.append(f"depends_on = {_toml_list(deps)}")
        if task.get("acceptance"):
            lines.append(f"acceptance = {_toml_list([str(a) for a in task['acceptance']])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _split(raw: Any) -> list[str]:
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def export_target(value: str | None) -> Path | None:
    """The file ``--template VALUE`` writes: ``-`` is standard output, a bare name goes in the templates folder."""
    if value is None or value == "-":
        return None
    if is_path_reference(value):
        return Path(value).expanduser()
    if not _NAME.fullmatch(value):
        raise ValueError(f"invalid template name {value!r}: use letters, digits, '_', '.' and '-', or give a path")
    return templates_dir() / f"{value}.toml"


def write_export(text: str, target: Path, *, force: bool) -> None:
    """Write ``text`` to ``target``; an existing file is refused unless ``force``."""
    if not target.parent.exists():
        target.parent.mkdir(parents=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC | os.O_NOFOLLOW if force else os.O_EXCL)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        raise ValueError(f"{target} already exists; pass --force to overwrite it") from None
    except OSError as exc:
        raise ValueError(f"cannot write template {target}: {exc.strerror or exc}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def export_mission(store: Any, mission_id: str) -> str:
    mission = store.get_mission(mission_id)
    tasks = [t for t in store.list_tasks(mission_id)
             if (t.get("origin") == "operator" or t.get("proposal_status") == "ACCEPTED") and t["status"] != "CANCELLED"]
    return render_template(mission, sorted(tasks, key=lambda t: int(t["ordinal"])))


def export_command(args: argparse.Namespace) -> int:
    """`mission export --template PATH|NAME|-`: write the mission as a template, or print it for ``-``."""
    import sys

    from . import bridge_format as fmt
    from .bridge import _json_print, open_store

    target = export_target(args.template)
    with open_store(readonly=True) as store:
        text = export_mission(store, args.mission)
    if target is None:
        if args.json:
            _json_print({"template": text})
        else:
            sys.stdout.write(text)
        return 0
    write_export(text, target, force=args.force)
    if args.json:
        _json_print({"path": str(target), "mission": args.mission})
        return 0
    print(f"Wrote template {target} from mission {fmt.short_id(args.mission)} \u00b7 create from it with: llm-bridge mission create --template {target}")
    return 0
