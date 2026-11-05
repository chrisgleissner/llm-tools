"""`mission create` and the bare `--prompt` form build the same mission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_tools.bridge import main, open_store


def _create(capsys, monkeypatch, tmp_path: Path, name: str, *argv: str) -> dict[str, Any]:
    state = tmp_path / name
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main([*argv, "--json", "--include-config"]) == 0
    return json.loads(capsys.readouterr().out)


def _comparable(result: dict[str, Any]) -> dict[str, Any]:
    mission = {key: value for key, value in result["mission"].items() if key not in {"id", "created_at", "updated_at", "initial_task_id"}}
    task = {key: value for key, value in result["task"].items() if key not in {"id", "mission_id", "created_at", "updated_at"}}
    return {"mission": mission, "task": task}


COMMON = ("--prompt", "Build it", "--name", "Named", "--acceptance", "first", "--acceptance", "second",
          "--providers", "codex", "--headless", "--command-template", "true", "--verifier-arg", "check",
          "--log-max-bytes", "2048", "--log-rotations", "2", "--log-retained-runs", "5")


def test_legacy_form_and_mission_create_store_the_same_mission_and_task(tmp_path, monkeypatch, capsys) -> None:
    cwd = ["--cwd", str(tmp_path)]
    legacy = _create(capsys, monkeypatch, tmp_path, "legacy", *COMMON, *cwd, "--no-start")
    explicit = _create(capsys, monkeypatch, tmp_path, "explicit", "mission", "create", *COMMON, *cwd)
    assert _comparable(legacy) == _comparable(explicit)
    config = explicit["mission"]["config"]
    assert config["headless"] is True and config["providers"] == "codex"
    assert config["command_template"] == "true" and config["verifier"]["argv"] == ["check"]
    assert explicit["task"]["acceptance"] == ["first", "second"]
    assert "auto_continue" not in config


def test_auto_continue_applies_to_mission_create_only(tmp_path, monkeypatch, capsys) -> None:
    cwd = ["--cwd", str(tmp_path)]
    explicit = _create(capsys, monkeypatch, tmp_path, "explicit", "mission", "create", *COMMON, *cwd, "--auto-continue")
    legacy = _create(capsys, monkeypatch, tmp_path, "legacy", *COMMON, *cwd, "--no-start", "--auto-continue")
    assert explicit["mission"]["config"]["auto_continue"] is True
    assert "auto_continue" not in legacy["mission"]["config"]


def test_team_policy_defaults_config_and_cli_precedence(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[bridge]\nteams = false\ntoken_spend = "high"\n', encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config))
    cwd = ["--cwd", str(tmp_path)]
    defaulted = _create(capsys, monkeypatch, tmp_path, "defaults", "mission", "create", "--prompt", "Keep exact", *cwd)
    assert defaulted["mission"]["config"]["team_policy_enabled"] is False
    assert defaulted["mission"]["config"]["token_spend"] == "high"
    explicit = _create(capsys, monkeypatch, tmp_path, "explicit", "mission", "create", "--prompt", "Keep exact", *cwd,
                       "--teams", "on", "--token-spend", "low")
    assert explicit["mission"]["config"]["team_policy_enabled"] is True
    assert explicit["mission"]["config"]["token_spend"] == "low"
    assert explicit["mission"]["objective"] == explicit["task"]["original_prompt"] == "Keep exact"


def test_mission_set_updates_team_policy_without_touching_task_text(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with open_store() as store:
        mission = store.create_mission("Keep", "Original\nobjective", cwd=tmp_path, initial_prompt="Original\ntask")
        mission_id = mission["id"]
        task_id = int(mission["initial_task_id"])
    assert main(["mission", "set", "--mission", mission_id, "--teams", "on", "--token-spend", "high", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["execution_policy"] == {"teams_enabled": True, "token_spend": "high", "legacy": False}
    with open_store(readonly=True) as store:
        saved = store.get_mission(mission_id)
        task = store.get_task(task_id)
    assert saved["objective"] == "Original\nobjective"
    assert task["original_prompt"] == task["prompt"] == "Original\ntask"


def test_rejected_options_create_no_mission(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    conflicting = ["--prompt", "x", "--cwd", str(tmp_path), "--headless", "--native"]
    assert main(["mission", "create", *conflicting]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert main([*conflicting, "--no-start"]) == 2
    assert "cannot be combined" in capsys.readouterr().err
    assert main(["mission", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["missions"] == []


def test_legacy_form_rejects_before_opening_the_store(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["--prompt", "x", "--cwd", str(tmp_path), "--headless", "--native", "--no-start"]) == 2
    capsys.readouterr()
    assert not (state / "bridge.sqlite3").exists()
