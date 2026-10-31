"""Final dogfooding defects: explicit provider selection, None text, confirm hint, objective preview, managed prompt."""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_tools import bridge_draw, bridge, bridge_logs, bridge_tui
from llm_tools.bridge import main, resolve_rotation
from llm_tools.bridge_runtime import (
    NATIVE_COMPLETION_NOTE,
    BridgeMissionHooks,
    _format_acceptance,
    _format_checkpoint,
    launch_controller,
)
from llm_tools.bridge_store import BridgeStore


@pytest.fixture()
def configured(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    config = tmp_path / "config.toml"
    config.write_text('[ralph]\nroutes = ["claude", "codex"]\n', encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    return tmp_path


def _create(tmp_path: Path, capsys, *extra: str) -> dict:
    assert main(["mission", "create", "--prompt", "Do it", "--cwd", str(tmp_path), *extra, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["config"]


# --- 1. explicit --providers is not overridden by configured routes ---------------------


def test_explicit_providers_clear_the_configured_routes(configured, capsys) -> None:
    config = _create(configured, capsys, "--providers", "codex", "--native")
    assert (config["providers"], config["routes"]) == ("codex", "")


def test_explicit_routes_win_and_defaults_apply_when_nothing_is_given(configured, capsys) -> None:
    config = _create(configured, capsys, "--providers", "codex", "--routes", "claude")
    assert (config["providers"], config["routes"]) == ("codex", "claude")
    config = _create(configured, capsys, "--routes", "codex")
    assert config["routes"] == "codex" and config["providers"] == bridge.default_rotation()[0]
    config = _create(configured, capsys)
    assert config["routes"] == "claude,codex"


def test_legacy_prompt_create_follows_the_same_rule(configured, capsys) -> None:
    assert main(["--prompt", "Build", "--cwd", str(configured), "--no-start", "--providers", "codex"]) == 0
    assert capsys.readouterr().out.startswith("Created mission ")
    with BridgeStore(configured / "state" / "bridge.sqlite3") as store:
        config = store.list_missions()[0]["config"]
    assert (config["providers"], config["routes"]) == ("codex", "")


def test_resolve_rotation_cases(configured) -> None:
    assert resolve_rotation(None, None) == (bridge.default_rotation()[0], "claude,codex")
    assert resolve_rotation("codex", None) == ("codex", "")
    assert resolve_rotation(None, "a") == (bridge.default_rotation()[0], "a")
    assert resolve_rotation("x", "") == ("x", "")


def test_create_help_shows_effective_defaults(configured, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["mission", "create", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "(default: claude,codex)" in text and "disables configured routes" in text


def test_start_with_explicit_providers_clears_saved_routes(configured, monkeypatch) -> None:
    with BridgeStore(configured / "state" / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=configured, config={"headless": True, "providers": "claude", "routes": "claude,codex"})
    calls: list[dict] = []
    monkeypatch.setattr("llm_tools.bridge.start_mission", lambda *a, **k: calls.append(k) or 0)
    mid = mission["id"]
    assert main(["start", "--mission", mid, "--providers", "codex"]) == 0
    assert (calls[-1]["providers"], calls[-1]["routes"]) == ("codex", "")
    # An override is saved for the mission, so later starts build on it.
    assert main(["start", "--mission", mid, "--routes", "codex"]) == 0
    assert (calls[-1]["providers"], calls[-1]["routes"]) == ("codex", "codex")
    assert main(["start", "--mission", mid]) == 0
    assert (calls[-1]["providers"], calls[-1]["routes"]) == ("codex", "codex")


def test_launch_controller_passes_an_explicit_empty_routes_to_the_controller(configured, monkeypatch, capsys) -> None:
    with BridgeStore(configured / "state" / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=configured, config={"headless": True, "routes": "claude,codex"})
    runtime = configured / "state" / "runtime" / f"mission-{mission['id']}"
    runtime.mkdir(parents=True, mode=0o700)
    observed: dict[str, list[str]] = {}

    class Proc:
        pid = 4547
        returncode = None

        def poll(self):
            return None

    def popen(command, **_kwargs):
        observed["command"] = command
        token = command[command.index("--startup-token") + 1]
        (runtime / "controller-ready.json").write_text(json.dumps({"pid": 4547, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return Proc()

    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess", SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    assert launch_controller(mission["id"], headless=True, providers="codex", routes="") == 0
    command = observed["command"]
    assert command[command.index("--providers") + 1] == "codex" and command[command.index("--routes") + 1] == ""
    capsys.readouterr()


def test_controller_parser_does_not_inherit_configured_routes(configured, monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr("llm_tools.bridge_runtime.run_controller", lambda mid, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(bridge_logs, "capture_process_output", lambda *a, **k: nullcontext())
    with BridgeStore(configured / "state" / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=configured, config={})
    assert main(["_controller", "--mission", mission["id"], "--providers", "codex", "--routes", "", "--startup-token", "t"]) == 0
    assert seen["providers"] == "codex" and seen["routes"] == ""


# --- 2. missing model is never rendered as text "None" ----------------------------------


def test_missing_selected_model_is_not_rendered_as_none() -> None:
    snapshot = bridge_tui._bounded_snapshot({"capacity": {"selection": {
        "route": "claude", "selected_model": None,
        "decisions": [{"route": "claude", "selected_model": None, "usable": True, "windows": []}],
    }}})
    assert snapshot["capacity"]["selection"]["selected_model"] == ""
    text = bridge_tui._capacity_summaries(snapshot)[0]
    assert text.startswith("claude (next)") and "None" not in text
    legacy = {"capacity": {"selection": {"route": "claude", "decisions": [{"route": "claude", "selected_model": "None", "windows": []}]}}}
    assert bridge_tui._capacity_summaries(legacy)[0].startswith("claude (next)")


# --- 3. confirmation hint appears once ---------------------------------------------------


class _Screen:
    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.lines = [""] * height

    def getmaxyx(self):
        return self.height, self.width

    def erase(self) -> None:
        self.lines = [""] * self.height

    def addnstr(self, y, x, text, count, attr=0) -> None:
        line = self.lines[y].ljust(x)
        self.lines[y] = line[:x] + text[:count] + line[x + len(text[:count]):]

    def noutrefresh(self) -> None:
        pass

    def move(self, *_args) -> None:
        pass


def test_confirmation_overlay_shows_its_hint_once_after_the_question(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState("0123456789abcdef0123456789abcdef", lambda *_: "{}", lambda *_: {}, full=True)
    state.snapshot = {"mission": {"status": "RUNNING"}, "tasks": [{"id": 3, "ordinal": 3, "status": "VERIFYING"}]}
    assert state._confirm_or_dispatch("/approve")
    screen = _Screen(10, 70)
    bridge_tui._draw_response_overlay(screen, state)
    text = "\n".join(screen.lines)
    assert text.count("Y approve · N or Esc cancel") == 1
    lines = [line for line in screen.lines if line.strip()]
    # The question, then what is being approved, then the single choice line.
    assert lines[0].strip() == "Confirm" and "mission 01234567" in lines[1] and lines[2].startswith("Task 3:")
    assert lines[-1].startswith("Y approve")
    state.output.clear()
    assert state._confirm_or_dispatch("/cancel")
    plain = "\n".join(line for line in screen.lines)
    bridge_tui._draw_response_overlay(screen, state)
    assert "\n".join(screen.lines).count("Y confirm · N or Esc cancel") == 1


# --- 4. objective preview ----------------------------------------------------------------


def test_objective_is_not_repeated_on_line_one_and_stays_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_draw, "role_attr", lambda _role: 0)
    objective = "## Acceptance demo\n\nBuild the thing"
    state = bridge_tui._ConsoleState("M1", lambda *_: "ok", lambda *_: {}, full=True)
    state.snapshot = bridge_tui._bounded_snapshot({"mission": {"id": "M1", "objective": objective, "status": "RUNNING"}})
    screen = _Screen(20, 100)
    bridge_tui._draw_full(screen, state)
    assert not any("Acceptance demo" in line for line in screen.lines)  # line 1 is the status strip; `o` opens the objective
    assert state.snapshot["mission"]["objective"].startswith("## ")


# --- 5/6. managed prompt -----------------------------------------------------------------


def _prompt(tmp_path: Path, monkeypatch, *, headless: bool, native: bool, acceptance=None) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "the objective", cwd=tmp_path, initial_prompt="the task")
    task_id = int(mission["initial_task_id"])
    if acceptance is not None:
        store.db.execute("UPDATE tasks SET acceptance_json=? WHERE id=?", (json.dumps(acceptance), task_id))
        store.db.commit()
    with store._transaction():
        store._event(mission["id"], "checkpoint", {"summary": "prior work", "step": 2}, task_id=task_id)
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=headless, native_tui=native, controller_logs=tmp_path / "c.log")
    hooks.bind(store)
    return hooks.before_iteration()["prompt"]


def test_managed_prompt_is_readable_text_without_raw_json(tmp_path, monkeypatch) -> None:
    prompt = _prompt(tmp_path, monkeypatch, headless=True, native=False)
    assert "Acceptance criteria:\nnone recorded" in prompt
    assert "restoration: RECONSTRUCTED" in prompt and "task revision: 1" in prompt
    assert "evidence: none recorded" in prompt
    assert "- checkpoint: prior work (step=2) (event " in prompt
    assert '{"' not in prompt and "[]" not in prompt


def test_acceptance_criteria_are_a_numbered_list() -> None:
    assert _format_acceptance(["tests pass", {"check": "lint"}, ""]) == '1. tests pass\n2. {"check": "lint"}'
    assert _format_acceptance([]) == "none recorded" and _format_acceptance(None) == "none recorded"
    assert _format_acceptance("single") == "1. single"


def test_checkpoint_formatting_is_deterministic_bounded_and_keeps_details() -> None:
    checkpoint = {
        "restoration": "RECONSTRUCTED", "task_revision": 3, "evidence": ["ran tests", {"a": 1}],
        "operations": [{"id": "op1", "station": "claude", "route_id": "r", "model": "m", "status": "FAILED", "exit_code": 75, "logs_path": "/x"}, {"id": "op2"}],
        "recent_events": [{"id": i, "kind": "k", "data": {"detail": "x" * 1000} if i else "text", "at": "t"} for i in range(40)],
    }
    first = _format_checkpoint(checkpoint)
    assert first == _format_checkpoint(checkpoint) and len(first) <= 12000
    assert "- ran tests" in first and '- {"a": 1}' in first
    assert "- operation op1: station claude, route r, model m, status FAILED, exit 75, logs /x" in first
    assert "- operation op2: no details" in first
    assert "- k: text (event 0, t)" in first
    assert "evidence: none recorded" in _format_checkpoint({"evidence": {}})
    assert "evidence:\n- single" in _format_checkpoint({"evidence": "single"})
    empty = _format_checkpoint({"restoration": "R", "task_revision": 1, "evidence": None, "recent_events": [{"id": 1, "kind": "k", "data": {}, "at": "t"}]})
    assert "operations: none" in empty and "- k: no details (event 1, t)" in empty
    assert "recent events: none" in _format_checkpoint({})


def test_numbered_acceptance_in_the_prompt(tmp_path, monkeypatch) -> None:
    prompt = _prompt(tmp_path, monkeypatch, headless=True, native=False, acceptance=["tests pass", "docs updated"])
    assert "Acceptance criteria:\n1. tests pass\n2. docs updated\n" in prompt


def test_native_prompt_asks_the_operator_to_exit_but_headless_does_not(tmp_path, monkeypatch) -> None:
    native = _prompt(tmp_path / "a", monkeypatch, headless=False, native=True)
    headless = _prompt(tmp_path / "b", monkeypatch, headless=True, native=False)
    assert native.endswith(NATIVE_COMPLETION_NOTE) and "ask the operator to exit this CLI (for example /exit)" in native
    assert NATIVE_COMPLETION_NOTE not in headless
