"""INT-019: warning, traceback and quoted lines are not questions; a real question blocks visibly."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

import llm_tools.bridge_runtime as runtime
from llm_tools import bridge_format as fmt
from llm_tools import bridge_tui, common, ralph_robin, scheduler
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled

PY_WARNING = "<string>:1: SyntaxWarning: \"is\" with 'int' literal. Did you mean \"==\"?"
NODE_WARNING = "(node:4242) Warning: Did you mean to await the promise here?"

# --------------------------------------------------------------------------- the shared classifier

NOT_QUESTIONS = {
    "python-syntax-warning": PY_WARNING + "\n",
    "python-warning-with-source-line": PY_WARNING + "\n  if x is 1:\n",
    "python-deprecation-warning": "app.py:12: DeprecationWarning: Which parser should I use?\n",
    "python-user-warning": "lib/x.py:3: UserWarning: Should I retry?\n",
    "python-future-warning": "FutureWarning: How do I silence this?\n",
    "python-runtime-warning": "m.py:9: RuntimeWarning: Why is this so slow?\n",
    "python-resource-warning": "ResourceWarning: Which socket should be closed?\n",
    "node-warning": NODE_WARNING + "\n",
    "node-deprecation": "(node:99) [DEP0005] DeprecationWarning: What should I use instead?\n",
    "traceback-last-frame": "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n    Should I use ==?\n",
    "traceback-exception-line": "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n    f()\nValueError: Which one?\n",
    "blockquote": "> Which approach should I take?\n",
    "double-quoted": "\"Which approach should I take?\"\n",
    "indented-code": "    Should I continue with the migration?\n",
    "code-fence": "```\nWhich approach should I take?\n```\n",
    "inside-code-fence": "```python\nWhich approach should I take?\n",
}

QUESTIONS = {
    "plain": "Which approach should I take?\n",
    "after-text": "Working on it.\nShould I continue with the migration?\n",
    "list-item": "- Should I continue with the migration?\n",
    "numbered": "1. Which database should I migrate first?\n",
    "after-a-finished-traceback": "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\nValueError: bad\nWhich approach should I take?\n",
    "after-a-warning": PY_WARNING + "\nWhich approach should I take?\n",
}


@pytest.mark.parametrize("name", sorted(NOT_QUESTIONS))
def test_warning_traceback_and_quoted_lines_are_not_questions(name: str) -> None:
    assert common.ends_with_question(NOT_QUESTIONS[name]) is False
    assert common.pending_question(NOT_QUESTIONS[name]) is None


@pytest.mark.parametrize("name", sorted(QUESTIONS))
def test_genuine_questions_are_still_recognised(name: str) -> None:
    text = QUESTIONS[name]
    assert common.ends_with_question(text) is True
    assert common.pending_question(text) == text.rstrip().splitlines()[-1].strip()


def test_progress_guard_uses_the_shared_classifier() -> None:
    guard = scheduler.ProgressGuard()
    guard.note_output("> Should I continue with the migration?\n")
    assert guard.question_seen_at is None
    guard.note_output(PY_WARNING + "\n")
    assert guard.question_seen_at is None
    guard.note_output("Should I continue with the migration?\n")
    assert guard.question_seen_at is not None


def test_a_long_traceback_cut_to_the_scan_tail_still_does_not_arm_the_guard() -> None:
    frames = "".join(f"  File \"m{index}.py\", line {index}, in f\n    call()\n" for index in range(12))
    guard = scheduler.ProgressGuard()
    guard.note_output("Traceback (most recent call last):\n" + frames + "    Should I retry?\n")
    assert guard.question_seen_at is None


# --------------------------------------------------------------------------- headless and PTY runs

@pytest.fixture
def watchdog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "1")
    monkeypatch.setenv("LLM_SCHEDULER_IDLE_TIMEOUT", "30")
    monkeypatch.setenv("LLM_SCHEDULER_NO_STREAM", "1")
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON", json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}))


# A provider that prints these lines last, stays silent for longer than the question timeout, then finishes.
_LAST_LINES = {
    "syntax-warning": PY_WARNING,
    "warning-then-source-line": PY_WARNING + "\n  if x is 1:",
    "quoted-diff": "> Should I continue with the migration?",
    "indented-code": "    Which value should I compare?",
    "node-warning": NODE_WARNING,
    "traceback-frame": "Traceback (most recent call last):\n  File \"x.py\", line 3, in <module>\n    Should I use ==?",
}


def _line_for(name: str) -> str:
    return _LAST_LINES[name]


@pytest.mark.parametrize("name", sorted(_LAST_LINES))
def test_headless_run_ending_on_a_warning_or_quoted_line_is_not_aborted(tmp_path: Path, watchdog_env, name: str) -> None:
    code = f"import time; print({_line_for(name)!r}, flush=True); time.sleep({scaled(2.5)}); print('done')"
    cfg = scheduler.SchedulerConfig(provider="codex", cwd=str(tmp_path), exact_stdout=True, prompt_text="x")
    status = scheduler.run_fresh_exact_stdout(cfg, [sys.executable, "-c", code], tmp_path / "out.txt", tmp_path / "status.txt")
    output = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert status == 0, output
    assert "done" in output and "question required" not in output


@pytest.mark.parametrize("name", ["syntax-warning", "quoted-diff", "traceback-frame"])
def test_pty_run_ending_on_a_warning_or_quoted_line_is_not_aborted(tmp_path: Path, name: str) -> None:
    code = f"import time; print({_line_for(name)!r}, flush=True); time.sleep({scaled(2.5)}); print('done')"
    status, text = common.run_pty_capture(
        [sys.executable, "-c", code], tmp_path, 30, stream=False, auto_confirm=False,
        idle_timeout=30, question_idle_timeout=1,
    )
    assert status == 0, text
    assert "done" in text


# --------------------------------------------------------------------------- Bridge: a real question blocks visibly

def _fake_codex(tmp_path: Path, body: str) -> str:
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    binary.chmod(0o755)
    return shlex.join([str(binary), "exec", "--json", "{prompt_file}"])


def _agent_message(text: str) -> str:
    """One line of ``codex exec --json`` output carrying model text."""
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


def _mission(tmp_path: Path) -> tuple[BridgeStore, str, int, BridgeMissionHooks]:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("question", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "migrate the database")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    return store, mission["id"], task_id, hooks


def _attempt(tmp_path: Path, hooks: BridgeMissionHooks, template: str, attempt: int) -> int:
    context = hooks.before_iteration()
    cfg = scheduler.SchedulerConfig(
        provider="codex", prompt_text=context["prompt"], cwd=str(tmp_path), log_dir=tmp_path / f"logs-{attempt}",
        command_template=template, headless=True, exact_stdout=True, ralph_robin_active=True,
    )
    selection = {"provider": "codex"}
    assert hooks.before_launch(context, selection, cfg, attempt)
    status = ralph_robin.run_scheduler_inline(cfg)
    hooks.after_iteration(context, selection, cfg, attempt, status, 0.1)
    return status


@pytest.mark.parametrize("last_line", [PY_WARNING, "> Should I continue with the migration?"], ids=["syntax-warning", "quoted-text"])
def test_provider_that_prints_a_warning_last_and_pauses_completes_the_task(tmp_path: Path, watchdog_env, last_line: str) -> None:
    body = (
        "import time\n"
        f"print({_agent_message(last_line)!r}, flush=True)\n"
        f"time.sleep({scaled(2.5)})\n"
        f"print({_agent_message('finished the work')!r}, flush=True)\n"
    )
    store, mission_id, task_id, hooks = _mission(tmp_path)
    try:
        assert _attempt(tmp_path, hooks, _fake_codex(tmp_path, body), 1) == 0
        assert store.get_task(task_id)["status"] == "VERIFYING"
        kinds = [event["kind"] for event in store.events(mission_id)]
        assert "task-awaiting-answer" not in kinds
    finally:
        store.close()


def test_provider_that_asks_a_question_and_stalls_blocks_with_the_question_and_its_actions(tmp_path: Path, watchdog_env, monkeypatch) -> None:
    question = "Which database should I migrate first, users or orders?"
    body = f"import time\nprint({_agent_message(question)!r}, flush=True)\ntime.sleep(30)\n"
    template = _fake_codex(tmp_path, body)
    store, mission_id, task_id, hooks = _mission(tmp_path)
    try:
        assert _attempt(tmp_path, hooks, template, 1) == common.AUTONOMY_ABORT_STATUS
        assert store.get_task(task_id)["status"] == "READY"  # the first stop goes to the next rotation entry
        assert _attempt(tmp_path, hooks, template, 2) == common.AUTONOMY_ABORT_STATUS
        assert store.get_task(task_id)["status"] == "BLOCKED"

        asked = [event for event in store.events(mission_id) if event["kind"] == "task-awaiting-answer"]
        assert asked and asked[-1]["data"]["question"] == question
        assert asked[-1]["data"]["task_id"] == task_id

        snapshot = project_snapshot(store, mission_id)
        needs = snapshot["needs_you"]
        short = mission_id[:8]
        assert needs is not None and needs["kind"] == "question"
        assert question in needs["message"]
        assert needs["question"] == question
        commands = [action["command"] for action in needs["actions"]]
        assert [action["name"] for action in needs["actions"]] == ["answer", "continue", "reject"]
        assert commands[0].startswith(f"llm-bridge task amend --mission {short} --task 1")
        assert commands[1] == f"llm-bridge task retry --mission {short} --task 1"
        assert commands[2] == f"llm-bridge task cancel --mission {short} --task 1"
        assert needs["command"] == commands[1]
        for command in commands:
            assert command in needs["message"]

        status_text = fmt.format_status(snapshot)
        assert question in status_text and "task amend" in status_text and "task retry" in status_text and "task cancel" in status_text

        rows = bridge_tui._needs_you_rows(dict(snapshot))
        assert any(question in row for row in rows), rows
        assert any("task amend" in row or "amend" in row for row in rows), rows

        # The controller's wait for the operator is visible, backs off, and carries the question.
        delays: list[float] = []

        def sleep(seconds: float) -> None:
            delays.append(seconds)
            if len(delays) >= 4:
                store.set_mission_status(mission_id, "STOPPING")

        monkeypatch.setattr(runtime.time, "sleep", sleep)
        assert hooks.before_iteration()["action"] == "stop"
        waiting = [event for event in store.events(mission_id) if event["kind"] == "mission-waiting-for-operator"]
        assert len(waiting) == 1
        assert waiting[0]["data"]["question"] == question
        assert question in waiting[0]["data"]["summary"]
        assert delays[-1] > delays[0]
    finally:
        store.close()


def test_answering_the_question_by_amending_and_retrying_runs_the_task_again(tmp_path: Path, watchdog_env) -> None:
    body = f"import time\nprint({_agent_message('Which database should I migrate first?')!r}, flush=True)\ntime.sleep(30)\n"
    template = _fake_codex(tmp_path, body)
    store, mission_id, task_id, hooks = _mission(tmp_path)
    try:
        _attempt(tmp_path, hooks, template, 1)
        _attempt(tmp_path, hooks, template, 2)
        assert project_snapshot(store, mission_id)["needs_you"]["kind"] == "question"
        store.amend_task(task_id, prompt="migrate the database, users first")
        store.set_task_status(task_id, "QUEUED")  # what `task retry` does
        snapshot = project_snapshot(store, mission_id)
        assert (snapshot["needs_you"] or {}).get("kind") != "question"
    finally:
        store.close()
