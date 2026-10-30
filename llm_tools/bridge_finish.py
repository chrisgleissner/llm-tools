"""Finish a native task from Bridge by asking the provider to exit.

In native mode a task leaves RUNNING only when the provider process exits.
``finish_mission`` types the provider's own graceful exit command into the
provider pane, as an operator would, and waits for the process to end. It never
signals or kills anything.

Exit commands (each verified in the installed CLI without starting a session):

* Claude Code 2.1: built-in command ``exit`` with alias ``quit``.
* Codex CLI 0.165: built-in commands ``quit`` and ``exit``.
* GitHub Copilot CLI 1.0: built-in commands ``/exit`` and ``/quit``.

Each provider's composer shows a slash-command popup, so the first Enter can
only accept the popup selection. The plan therefore sends the command, Enter,
a short pause and a second Enter only if the pane still shows the popup with
the exit command as its first entry. The plan is data (``EXIT_STEPS``), and the
command text can be replaced per provider with ``LLM_BRIDGE_FINISH_<PROVIDER>``
(for example ``LLM_BRIDGE_FINISH_CODEX=/exit``).

Nothing is typed blind. The provider pane is read first, and keys are sent only
when its composer looks idle. A pane that shows an approval or trust dialog is
left untouched, because the keystrokes would answer that dialog.
``LLM_BRIDGE_FINISH_SKIP_CHECK=1`` disables the check for a provider whose
screen these heuristics do not recognise.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .bridge_store import BridgeStore, store_path
from .bridge_states import ACTIVE_OPERATION_STATUSES

DEFAULT_EXIT_COMMANDS = {"claude": "/exit", "codex": "/quit", "copilot": "/exit"}
SETTLE_SECONDS = 0.3
EXIT_TIMEOUT_SECONDS = 10.0
TASK_SETTLE_SECONDS = 5.0
POLL_SECONDS = 0.2
NOT_EXITED_MESSAGE = "Provider did not exit · switch to it with Ctrl-] then P"
ASKING_MESSAGE = "Provider is asking something — switch to it with Ctrl-] then P"

# Steps: ("text", literal keys), ("key", tmux key name), ("pause", seconds),
# ("key-if-popup", tmux key name; sent only while the pane is alive and still
# shows the slash-command popup with the exit command as its first entry).
EXIT_STEPS: tuple[tuple[str, object], ...] = (
    ("text", "{command}"),
    ("key", "Enter"),
    ("pause", SETTLE_SECONDS),
    ("key-if-popup", "Enter"),
)

# Screen text that means the provider is waiting for an answer, not for a prompt.
APPROVAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do you want to",
        r"do you trust",
        r"\ballow\b",
        r"\[?\(?y/n\)?\]?",
        r"\b1\.\s*yes\b",
        r"yes, and (?:don't|do not) ask",
        r"esc to cancel",
        r"enter to confirm",
        r"\bapprove\b",
    )
)
# A composer line: optional box-drawing border, then a prompt glyph.
COMPOSER_LINE = re.compile(r"^[\s│┃|]*[›❯>](?:\s|$)")
SCREEN_TAIL_LINES = 12
SKIP_CHECK_ENV = "LLM_BRIDGE_FINISH_SKIP_CHECK"


def _tail(screen: str) -> list[str]:
    lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
    return lines[-SCREEN_TAIL_LINES:]


def composer_is_idle(screen: str) -> bool:
    """Whether the provider screen shows a prompt and no approval or trust dialog."""
    tail = _tail(screen)
    if not tail:
        return False
    if any(pattern.search(line) for line in tail for pattern in APPROVAL_PATTERNS):
        return False
    return any(COMPOSER_LINE.match(line) for line in tail)


def exit_popup_open(screen: str, command: str) -> bool:
    """Whether the slash-command popup is still open with ``command`` as its first entry.

    After the first Enter accepted the popup selection, the composer still holds
    the command. The popup is the list of ``/name  description`` lines below or
    above it; the composer line itself starts with a prompt glyph and does not count.
    """
    for line in _tail(screen):
        if COMPOSER_LINE.match(line):
            continue
        entry = line.strip(" \t│┃|▶▸►›❯>")
        if entry.startswith("/"):
            return entry.split()[0] == command
    return False


class FinishError(ValueError):
    """The provider could not be finished; the message tells the operator why."""


class FinishBackend(Protocol):
    def capture(self, handle: Any, *, pane: str) -> str: ...
    def pane_is_alive(self, handle: Any, *, pane: str) -> bool: ...
    def send_keys(self, handle: Any, *, pane: str, keys: str) -> None: ...
    def session_exists(self, handle: Any) -> bool: ...


@dataclass(frozen=True)
class FinishResult:
    mission_id: str
    provider: str
    task_id: int
    command: str
    task_status: str
    detail: str

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "provider": self.provider,
            "task_id": self.task_id,
            "exit_command": self.command,
            "outcome": "exited",
            "task_status": self.task_status,
            "detail": self.detail,
        }


def exit_command(provider: str, env: dict[str, str] | None = None) -> str | None:
    """Exit command for ``provider``: an environment override, else the default."""
    source = os.environ if env is None else env
    override = source.get("LLM_BRIDGE_FINISH_" + "".join(c if c.isalnum() else "_" for c in provider.upper()), "").strip()
    return override or DEFAULT_EXIT_COMMANDS.get(provider)


def _active_operation(store: BridgeStore, mission_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT id,task_id,station,route_id,status FROM operations WHERE mission_id=? "
        "AND status IN ('INTENT','PREPARED','RUNNING') ORDER BY rowid DESC LIMIT 1",
        (mission_id,),
    ).fetchone()
    return dict(row) if row else None


def _operation_active(store: BridgeStore, operation_id: str) -> bool:
    row = store.db.execute("SELECT status FROM operations WHERE id=?", (operation_id,)).fetchone()
    return bool(row) and str(row["status"]) in ACTIVE_OPERATION_STATUSES


def finish_mission(
    mission_id: str,
    *,
    backend: FinishBackend | None = None,
    handle: Any | None = None,
    store: BridgeStore | None = None,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    env: dict[str, str] | None = None,
) -> FinishResult:
    """Ask the native provider of ``mission_id`` to exit and wait for it."""
    own_store = store is None
    store = store or BridgeStore(store_path())
    try:
        mission = store.get_mission(mission_id)
        config = mission["config"]
        if config.get("headless") or not config.get("native_tui"):
            raise FinishError("Finish applies to native providers only · this mission runs headless provider work")
        operation = _active_operation(store, mission_id)
        if operation is None:
            raise FinishError("No provider is running for this mission · nothing to finish")
        from .bridge_switcher import provider_of_station

        provider = provider_of_station(config, str(operation["route_id"] or operation["station"] or ""))
        command = exit_command(provider, env)
        if command is None:
            raise FinishError(
                f"No exit command is known for {provider} · set LLM_BRIDGE_FINISH_{provider.upper()} "
                "or switch to it with Ctrl-] then P and exit it there"
            )
        if backend is None:
            from .terminal import TmuxTerminalBackend

            backend = TmuxTerminalBackend()
        if handle is None:
            from .bridge import _load_handle

            try:
                handle = _load_handle(mission_id)
            except RuntimeError as exc:
                raise FinishError(str(exc)) from exc
        if not backend.pane_is_alive(handle, pane="provider"):
            raise FinishError("The provider pane is not running · nothing to finish")

        source = os.environ if env is None else env
        if source.get(SKIP_CHECK_ENV, "") != "1":
            try:
                screen = backend.capture(handle, pane="provider")
            except (OSError, RuntimeError, PermissionError):
                screen = ""
            if not composer_is_idle(screen):
                raise FinishError(ASKING_MESSAGE)

        operation_id = str(operation["id"])
        for kind, value in EXIT_STEPS:
            try:
                if kind == "text":
                    backend.send_keys(handle, pane="provider", keys=str(value).replace("{command}", command))
                elif kind == "key":
                    backend.send_keys(handle, pane="provider", keys=str(value))
                elif kind == "pause":
                    sleep(float(value))  # type: ignore[arg-type]
                elif kind == "key-if-popup" and backend.pane_is_alive(handle, pane="provider"):
                    if exit_popup_open(backend.capture(handle, pane="provider"), command):
                        backend.send_keys(handle, pane="provider", keys=str(value))
            except (OSError, RuntimeError, PermissionError):
                # The pane or station vanished while typing: the provider is gone.
                break

        deadline = clock() + (EXIT_TIMEOUT_SECONDS if timeout is None else timeout)
        while True:
            gone = not _operation_active(store, operation_id)
            if not gone:
                try:
                    gone = not backend.session_exists(handle) or not backend.pane_is_alive(handle, pane="provider")
                except (OSError, RuntimeError, PermissionError):
                    gone = True
            if gone:
                break
            if clock() >= deadline:
                raise FinishError(NOT_EXITED_MESSAGE)
            sleep(POLL_SECONDS)

        task_id = int(operation["task_id"])
        settle = clock() + TASK_SETTLE_SECONDS
        status = str(store.get_task(task_id)["status"])
        while status == "RUNNING" and clock() < settle:
            sleep(POLL_SECONDS)
            status = str(store.get_task(task_id)["status"])
        detail = f"Provider exited · task {store.get_task(task_id)['ordinal']} is now {status}"
        return FinishResult(mission_id, provider, task_id, command, status, detail)
    finally:
        if own_store:
            store.close()

