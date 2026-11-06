"""Cheap, offline checks of the provider CLIs a mission would launch.

``llm-bridge doctor`` uses these to say, for each provider in the rotation,
whether its command is on PATH and whether its sign-in files look present. No
provider is started and no request is sent, so the check cannot consume paid
capacity. "Logged in" means the credential files the provider's own CLI writes
exist; a token that has expired is only discovered when the provider runs.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

# Command each provider name launches. Names not listed launch a command of the same name.
_BINARY = {"minimax": "mmx"}
_CAPACITY_ONLY = frozenset({"zai"})


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or str(Path.home()))


def _data_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("XDG_DATA_HOME") or (_home(env) / ".local" / "share"))


def _codex_login(env: Mapping[str, str]) -> tuple[bool | None, str]:
    data = _read_json(_home(env) / ".codex" / "auth.json")
    if isinstance(data, dict) and (str(data.get("OPENAI_API_KEY") or "").strip() or data.get("tokens")):
        return True, ""
    return False, "run: codex login"


def _claude_login(env: Mapping[str, str]) -> tuple[bool | None, str]:
    if str(env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return True, ""
    data = _read_json(_home(env) / ".claude" / ".credentials.json")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and (str(oauth.get("accessToken") or "").strip() or str(oauth.get("refreshToken") or "").strip()):
        return True, ""
    return False, "run: claude, then /login"


def _copilot_login(env: Mapping[str, str]) -> tuple[bool | None, str]:
    for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if str(env.get(key) or "").strip():
            return True, ""
    hosts = _home(env) / ".config" / "gh" / "hosts.yml"
    try:
        text = hosts.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "oauth_token" in text or "user:" in text:
        return True, ""
    return False, "run: copilot login"


def _agent_store_login(app: str):
    def check(env: Mapping[str, str]) -> tuple[bool | None, str]:
        data = _read_json(_data_home(env) / app / "auth.json")
        if isinstance(data, dict) and data:
            return True, ""
        return False, f"run: {app} auth login"

    return check


_LOGIN_CHECKS = {
    "codex": _codex_login,
    "claude": _claude_login,
    "copilot": _copilot_login,
    "kilo": _agent_store_login("kilo"),
    "opencode": _agent_store_login("opencode"),
}


def signin_state(cli: str, env: Mapping[str, str] | None = None) -> tuple[bool | None, str]:
    """Whether ``cli`` looks signed in, from the same files ``provider_check`` reads; ``None`` when it cannot be told.

    The second item is the command that signs in (``codex login``), or empty. It does not look at PATH.
    """
    check = _LOGIN_CHECKS.get(cli)
    if check is None:
        return None, ""
    logged_in, detail = check(os.environ if env is None else env)
    return logged_in, detail.removeprefix("run: ")


def provider_check(name: str, launch_cli: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whether the command behind ``name`` is installed and appears signed in."""
    environment = os.environ if env is None else env
    cli = launch_cli or name
    if cli in _CAPACITY_ONLY:
        return {"name": name, "launch_cli": cli, "installed": None, "path": None, "logged_in": None, "detail": "capacity only; launched through a route"}
    binary = _BINARY.get(cli, cli)
    path = shutil.which(binary) if env is None else shutil.which(binary, path=environment.get("PATH"))
    if path is None:
        return {"name": name, "launch_cli": cli, "installed": False, "path": None, "logged_in": None, "detail": f"{binary} is not on PATH"}
    check = _LOGIN_CHECKS.get(cli)
    logged_in, detail = check(environment) if check else (None, "")
    return {"name": name, "launch_cli": cli, "installed": True, "path": path, "logged_in": logged_in, "detail": detail}


def provider_checks(entries: Sequence[tuple[str, str | None]], env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """One check per distinct ``(name, launch_cli)`` rotation entry, in rotation order."""
    seen: set[tuple[str, str | None]] = set()
    checks = []
    for name, launch_cli in entries:
        if (name, launch_cli) in seen:
            continue
        seen.add((name, launch_cli))
        checks.append(provider_check(name, launch_cli, env))
    return checks


_TRUST_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _claude_projects(env: Mapping[str, str]) -> dict[str, Any] | None:
    """The ``projects`` table of Claude Code's state file, or ``None`` when it cannot be read.

    A missing file is an empty table (a new install). The parsed file is cached
    by size and modification time because the launcher asks on every redraw.
    """
    config_dir = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
    path = Path(config_dir) / ".claude.json" if config_dir else _home(env) / ".claude.json"
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    key = (info.st_size, info.st_mtime_ns)
    cached = _TRUST_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    data = _read_json(path)
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return None if data is None else {}
    _TRUST_CACHE[str(path)] = (key, projects)
    return projects


def claude_folder_trusted(folder: str | Path, env: Mapping[str, str] | None = None) -> bool | None:
    """Whether Claude Code already trusts ``folder`` (or a parent), read from its state file.

    ``False`` means Claude Code will ask "Is this a project you trust?" the
    first time it starts there. ``None`` means the state file could not be read.
    Nothing is written.
    """
    projects = _claude_projects(os.environ if env is None else env)
    if projects is None:
        return None
    try:
        path = Path(folder).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for candidate in (path, *path.parents):
        entry = projects.get(str(candidate))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


TRUST_NOTE = "Claude Code asks once to trust a new folder: choose \"Yes, I trust this folder\"."


PREFERRED_ORDER = ("claude", "codex", "opencode", "copilot", "kilo")


def preferred_first_provider(installed: Sequence[str], env: Mapping[str, str] | None = None) -> str | None:
    """The first installed provider that is also signed in, in Bridge's built-in order, or ``None``."""
    for name in PREFERRED_ORDER:
        if name in installed and provider_check(name, env=env).get("logged_in") is True:
            return name
    return None


def _offered_names() -> str:
    """The provider CLIs the new-mission form can offer, from the launcher's registry."""
    from .bridge_launcher import PROVIDER_ORDER

    return ", ".join(PROVIDER_ORDER)


def readiness(providers: Sequence[Mapping[str, Any]], *, integrity: object = "ok") -> dict[str, Any]:
    """The overall verdict of ``doctor``: whether a mission could start a provider at all.

    ``blocking`` lists what stops it. A missing tmux is not blocking, because
    headless missions do not need it.
    """
    blocking: list[str] = []
    if str(integrity) != "ok":
        blocking.append(f"database integrity check reported {integrity}")
    if not any(check.get("installed") and check.get("logged_in") is True for check in providers):
        problems = []
        for check in providers:
            if check.get("installed") is False:
                problems.append(f"{check.get('name')}: not on PATH")
            elif check.get("installed") and check.get("logged_in") is False:
                problems.append(f"{check.get('name')}: not signed in")
        blocking += problems or ["no provider CLI is installed and signed in"]
    return {"ready": not blocking, "blocking": blocking}


def next_steps(*, tmux_available: bool, providers: Sequence[Mapping[str, Any]], folder: str | Path,
               env: Mapping[str, str] | None = None, needs_you: str | None = None) -> list[str]:
    """What to do next, from the same facts ``doctor`` just reported.

    ``needs_you`` is the mission's own next step (what ``status`` shows as "Needs you"); with
    ``doctor --mission`` it replaces the generic first-mission advice.
    """
    steps: list[str] = []
    if not tmux_available:
        steps.append("Install tmux (sudo apt install tmux, or brew install tmux); native stations need it. Without tmux, create missions with --mode headless.")
    ready = [check for check in providers if check.get("installed") and check.get("logged_in") is True]
    if not ready:
        waiting = [check for check in providers if check.get("installed") and check.get("logged_in") is False]
        if waiting:
            steps.append(f"Sign in to a provider first: {waiting[0].get('name')} · {waiting[0].get('detail') or 'sign in with its own CLI'}")
        else:
            steps.append(f"Install and sign in to one provider CLI (install one of: {_offered_names()}), then run llm-bridge doctor again.")
        return steps
    steps.append(needs_you or "Ready. In your project folder run llm-bridge, press N, type the task, then choose Start.")
    first = ready[0]
    if (first.get("launch_cli") or first.get("name")) == "claude" and claude_folder_trusted(folder, env) is False:
        steps.append(TRUST_NOTE)
    return steps
