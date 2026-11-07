"""A task added or amended while a native provider works is announced to that provider (DOG-017)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_tools import bridge, bridge_task_notice
from llm_tools.bridge import main
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend

IDLE_CODEX = "work output\n\n› Ask Codex to do something\n\n  gpt-5 · 100% context left\n"
IDLE_CLAUDE = "work output\n─────────────\n❯ \n─────────────\n  ⏵⏵ bypass permissions on\n"
WORKING_CLAUDE = "● Reading files\n✻ Cogitating… (12s · esc to interrupt)\n─────────────\n❯ \n─────────────\n"
APPROVAL = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
HANDLE = TerminalHandle(Path("/tmp/dog017.sock"), Path("/tmp/dog017.conf"), "m-dog017", "%1", "%2", 8)


class Pane:
    """The provider pane: what it shows and what was typed into it."""

    def __init__(self, screen: str = IDLE_CODEX) -> None:
        self.screen = screen
        self.sent: list[str] = []
        self.alive = True


@pytest.fixture()
def native(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A RUNNING native mission with one running operation; tmux is replaced by ``Pane``."""
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(BridgeMissionHooks, "USAGE_LIMIT_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(BridgeMissionHooks, "TASK_NOTICE_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(bridge_task_notice, "QUIET_SECONDS", 0.0)
    monkeypatch.setattr(bridge_task_notice, "KEY_PAUSE_SECONDS", 0.0)
    pane = Pane()

    monkeypatch.setattr(TmuxTerminalBackend, "capture", lambda _self, _handle, *, pane: pane_screen(pane))
    monkeypatch.setattr(TmuxTerminalBackend, "send_keys", lambda _self, _handle, *, pane, keys: pane_send(pane, keys))
    monkeypatch.setattr(TmuxTerminalBackend, "pane_activity_screen", lambda _self, _handle, *, pane="provider", seen_epoch=None: (3, None if seen_epoch == 3 else pane_screen(pane)))
    state = SimpleNamespace(pane=pane)

    def pane_screen(_name: str) -> str:
        return state.pane.screen

    def pane_send(_name: str, keys: str) -> None:
        state.pane.sent.append(keys)

    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")

    def start(provider: str = "codex", *, native_tui: bool = True, headless: bool = False) -> SimpleNamespace:
        folder = tmp_path / f"w-{provider}-{native_tui}-{headless}"
        folder.mkdir()
        created = store.create_mission("Long", "objective", cwd=folder, initial_prompt="first long task", config={"providers": provider, "native_tui": native_tui, "headless": headless})
        mission_id, task_id = str(created["id"]), int(created["initial_task_id"])
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station=provider)
        store.mark_operation_running(operation)
        hooks = BridgeMissionHooks(mission_id, handle=HANDLE, headless=headless, native_tui=native_tui, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        return SimpleNamespace(mission_id=mission_id, task_id=task_id, operation=operation, provider=provider, hooks=hooks)

    state.store, state.start, state.tmp_path = store, start, tmp_path
    yield state
    store.close()


def observe(run: SimpleNamespace) -> int | None:
    return run.hooks._observe_native_operation(run.operation, run.provider, "")


def notices(native, run) -> list[dict[str, Any]]:
    return [event for event in native.store.events(run.mission_id) if event["kind"] == "task-notice-delivered"]


def test_task_added_during_a_native_run_is_typed_into_the_provider_at_its_idle_prompt(native) -> None:
    run = native.start()
    observe(run)
    assert native.pane.sent == [], "nothing is announced while nothing was added"

    native.store.add_task(run.mission_id, "write the docs", title="Write docs")
    assert observe(run) is None

    assert len(native.pane.sent) == 2 and native.pane.sent[1] == "Enter"
    text = native.pane.sent[0]
    assert "\n" not in text
    assert 'task 2 "Write docs"' in text
    assert f"llm-bridge task show --mission {run.mission_id} --task 2" in text
    recorded = notices(native, run)
    assert len(recorded) == 1
    assert recorded[0]["data"]["tasks"][0]["ordinal"] == 2 and recorded[0]["data"]["operation_id"] == run.operation


def test_several_adds_and_an_amend_before_delivery_become_one_notice(native) -> None:
    run = native.start("claude")
    native.pane.screen = WORKING_CLAUDE
    native.store.add_task(run.mission_id, "docs", title="Docs")
    native.store.add_task(run.mission_id, "lint", title="Lint")
    native.store.amend_task(2, prompt="docs, with examples")
    observe(run)
    assert native.pane.sent == [], "the provider is busy, so nothing is typed yet"

    native.pane.screen = IDLE_CLAUDE
    observe(run)
    assert len(native.pane.sent) == 2
    text = native.pane.sent[0]
    assert text.count("task 2") >= 1 and "task 3" in text and 'task 2 "Docs"' in text and 'task 3 "Lint"' in text
    assert len(notices(native, run)) == 1
    assert [item["kind"] for item in notices(native, run)[0]["data"]["tasks"]] == ["added", "added"], "an amend of a task that was not announced yet stays one 'added' entry"


def test_a_delivered_task_is_never_delivered_twice_even_by_a_restarted_controller(native) -> None:
    run = native.start()
    native.store.add_task(run.mission_id, "docs", title="Docs")
    observe(run)
    observe(run)
    observe(run)
    assert len(native.pane.sent) == 2

    restarted = BridgeMissionHooks(run.mission_id, handle=HANDLE, headless=False, native_tui=True, controller_logs=native.tmp_path / "controller.log")
    restarted.bind(native.store)
    restarted._observe_native_operation(run.operation, "codex", "")
    assert len(native.pane.sent) == 2

    native.store.add_task(run.mission_id, "lint", title="Lint")
    observe(run)
    assert len(native.pane.sent) == 4
    assert "task 3" in native.pane.sent[2] and "task 2" not in native.pane.sent[2]
    native.store.amend_task(3, prompt="lint everything")
    observe(run)
    assert len(native.pane.sent) == 6 and "amended" in native.pane.sent[4]
    assert len(notices(native, run)) == 3


def test_nothing_is_typed_into_a_dialog_or_while_the_provider_is_working(native) -> None:
    run = native.start("claude")
    native.store.add_task(run.mission_id, "docs", title="Docs")
    for screen in (APPROVAL, WORKING_CLAUDE, ""):
        native.pane.screen = screen
        observe(run)
        assert native.pane.sent == [], screen
    assert notices(native, run) == []
    native.pane.screen = IDLE_CLAUDE
    observe(run)
    assert len(native.pane.sent) == 2


def test_tasks_from_before_the_run_and_finished_tasks_are_not_announced(native) -> None:
    folder = native.tmp_path / "w-early"
    folder.mkdir()
    created = native.store.create_mission("Early", "objective", cwd=folder, initial_prompt="one", config={"providers": "codex", "native_tui": True})
    early = native.store.add_task(created["id"], "queued before the provider started", title="Early")
    native.store.set_mission_status(created["id"], "RUNNING")
    operation = native.store.begin_operation(int(created["initial_task_id"]), iteration=1, station="codex")
    native.store.mark_operation_running(operation)
    hooks = BridgeMissionHooks(created["id"], handle=HANDLE, headless=False, native_tui=True, controller_logs=native.tmp_path / "controller.log")
    hooks.bind(native.store)
    cancelled = native.store.add_task(created["id"], "added then dropped", title="Dropped")
    native.store.set_task_status(cancelled, "CANCELLED")
    assert early and hooks._observe_native_operation(operation, "codex", "") is None
    assert native.pane.sent == []


@pytest.mark.parametrize(("provider", "native_tui", "headless"), [("codex", False, True), ("kilo", True, False)])
def test_headless_and_unverified_providers_keep_the_plain_queue(native, provider: str, native_tui: bool, headless: bool) -> None:
    run = native.start(provider, native_tui=native_tui, headless=headless)
    native.store.add_task(run.mission_id, "docs", title="Docs")
    observe(run)
    assert native.pane.sent == []
    assert notices(native, run) == []


def test_notice_text_is_one_bounded_line_without_control_characters() -> None:
    items = [bridge_task_notice.NoticeItem(task_id=n, ordinal=n, title=f"Title\x1b[31m {n}\nsecond line " + "x" * 200, kind="added") for n in range(2, 12)]
    text = bridge_task_notice.compose_notice("mid", items)
    assert "\n" not in text and "\x1b" not in text and len(text) < 700
    assert "and 5 more" in text and "llm-bridge task show --mission mid --task N" in text


def test_delivery_waits_for_a_screen_that_stays_unchanged() -> None:
    recorded: list[dict[str, Any]] = []
    store = SimpleNamespace(
        task_notice_cursor=lambda *_a: 0,
        task_notice_candidates=lambda _mission, after, **_k: [{"id": 5, "kind": "task-added", "task_id": 9, "ordinal": 2, "title": "Docs", "status": "QUEUED"}] if after < 5 else [],
        record_task_notice=lambda mission, operation, **kw: recorded.append(kw),
    )
    delivery = bridge_task_notice.TaskNoticeDelivery("mid", "op", store=store, quiet_seconds=3.0)
    sent: list[str] = []
    screens = [IDLE_CODEX + "a", IDLE_CODEX + "b", IDLE_CODEX + "b", IDLE_CODEX + "b", IDLE_CODEX + "b", IDLE_CODEX + "b"]
    delivered_at = []
    for second, screen in enumerate(screens):
        if delivery.poll(capture=lambda screen=screen: screen, send=sent.append, now=float(second)):
            delivered_at.append(second)
    assert delivered_at == [4], "the screen last changed at second 1 and must stay unchanged for 3 seconds"
    assert len(sent) == 1 and recorded[0]["through_event_id"] == 5


# ------------------------------------------------------------------ CLI and command results


def _json(capsys, *argv: str) -> dict[str, Any]:
    assert main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def _text(capsys, *argv: str) -> str:
    assert main(list(argv)) == 0
    return capsys.readouterr().out.strip()


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    created = _json(capsys, "mission", "create", "--prompt", "first", "--name", "n", "--cwd", str(tmp_path), "--mode", "native")
    mission_id = created["mission"]["id"]
    return SimpleNamespace(mission_id=mission_id, store_path=tmp_path / "state" / "bridge.sqlite3", monkeypatch=monkeypatch)


def _run_first_task(cli, provider: str = "codex") -> None:
    with BridgeStore(cli.store_path) as store:
        task = store.list_tasks(cli.mission_id)[0]
        store.set_mission_status(cli.mission_id, "RUNNING")
        operation = store.begin_operation(int(task["id"]), iteration=1, station=provider)
        store.mark_operation_running(operation)


def test_task_add_to_a_running_native_mission_says_the_provider_is_told_at_its_next_prompt(cli, capsys) -> None:
    _run_first_task(cli)
    cli.monkeypatch.setattr(bridge, "controller_running", lambda _mission: True)
    out = _text(capsys, "task", "add", "--mission", cli.mission_id, "--prompt", "second", "--title", "Second")
    assert out == 'Added task 2 "Second" · QUEUED · priority 0 · delivered to the running provider at its next prompt'
    result = _json(capsys, "task", "add", "--mission", cli.mission_id, "--prompt", "third", "--title", "Third")
    assert result["delivery"] == {"mode": "provider-notice", "text": "delivered to the running provider at its next prompt", "after_task": None}
    amended = _text(capsys, "task", "amend", "--mission", cli.mission_id, "--task", "2", "--prompt", "second, changed")
    assert amended == "Amended · task 2 is now revision 2 · delivered to the running provider at its next prompt"


def test_task_add_elsewhere_says_which_task_it_starts_after(cli, capsys) -> None:
    out = _text(capsys, "task", "add", "--mission", cli.mission_id, "--prompt", "second", "--title", "Second")
    assert out == 'Added task 2 "Second" · QUEUED · priority 0 · starts after task 1'
    third = _json(capsys, "task", "add", "--mission", cli.mission_id, "--prompt", "third", "--title", "Third")
    assert third["delivery"] == {"mode": "queue", "text": "starts after task 2", "after_task": 2}


def test_a_native_mission_without_a_live_controller_or_with_headless_mode_does_not_promise_delivery(cli, capsys) -> None:
    _run_first_task(cli)
    cli.monkeypatch.setattr(bridge, "controller_running", lambda _mission: False)
    out = _text(capsys, "task", "add", "--mission", cli.mission_id, "--prompt", "second", "--title", "Second")
    assert out.endswith("starts after task 1") and "delivered" not in out


def test_the_typed_command_result_carries_the_delivery(cli) -> None:
    from llm_tools import bridge_commands as commands

    _run_first_task(cli)
    cli.monkeypatch.setattr(bridge, "controller_running", lambda _mission: True)
    result = commands.execute(commands.CommandRequest("task.add", {"prompt": "remote task", "title": "Remote"}, mission_id=cli.mission_id))
    assert result.outcome == "applied"
    assert result.result["result"]["delivery"]["mode"] == "provider-notice"


def test_the_console_acknowledgement_includes_the_delivery(cli, capsys) -> None:
    from llm_tools import bridge_commands as commands
    from llm_tools import bridge_tui

    _run_first_task(cli)
    cli.monkeypatch.setattr(bridge, "controller_running", lambda _mission: True)
    # The console shows the durable command envelope of `task add`; its result carries the delivery.
    envelope = commands.execute(commands.CommandRequest("task.add", {"prompt": "console task", "title": "From console"}, mission_id=cli.mission_id)).result
    feedback = bridge_tui._command_feedback("task add --prompt x", json.dumps(envelope))
    assert feedback == "Task 2 added \u2014 delivered to the running provider at its next prompt"


# ------------------------------------------------------------------ real tmux


FAKE_PROVIDER = """\
import sys
print("working on the first task")
log = open(sys.argv[1], "a", buffering=1)
while True:
    sys.stdout.write("\\n\\u203a ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        break
    log.write(line)
"""


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_notice_reaches_a_real_provider_pane_at_its_idle_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import wait_for

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(BridgeMissionHooks, "USAGE_LIMIT_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(BridgeMissionHooks, "TASK_NOTICE_LOOK_SECONDS", 0.0)
    monkeypatch.setattr(bridge_task_notice, "QUIET_SECONDS", 0.3)
    received = tmp_path / "received.txt"
    script = tmp_path / "provider.py"
    script.write_text(FAKE_PROVIDER, encoding="utf-8")
    backend = TmuxTerminalBackend()
    work = tmp_path / "work"
    work.mkdir()
    handle = backend.start(
        provider_argv=[sys.executable, str(script), str(received)], bridge_argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=work, socket_path=tmp_path / "n.sock", session_name="notice",
    )
    store = BridgeStore(tmp_path / "state" / "bridge.sqlite3")
    try:
        created = store.create_mission("Real", "objective", cwd=work, initial_prompt="first long task", config={"providers": "codex", "native_tui": True})
        mission_id = str(created["id"])
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(int(created["initial_task_id"]), iteration=1, station="codex")
        store.mark_operation_running(operation)
        hooks = BridgeMissionHooks(mission_id, handle=handle, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        assert wait_for(lambda: "›" in backend.capture(handle, pane="provider"), 6.0, 0.05)
        store.add_task(mission_id, "write the docs", title="Write docs")

        def delivered() -> bool:
            hooks._observe_native_operation(operation, "codex", "")
            return received.exists() and "Write docs" in received.read_text(encoding="utf-8")

        assert wait_for(delivered, 10.0, 0.1), backend.capture(handle, pane="provider")
        lines = received.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1 and f"llm-bridge task show --mission {mission_id} --task 2" in lines[0]
        for _ in range(5):
            hooks._observe_native_operation(operation, "codex", "")
        assert len(received.read_text(encoding="utf-8").splitlines()) == 1, "the same task is not typed twice"
    finally:
        store.close()
        backend.stop(handle)
