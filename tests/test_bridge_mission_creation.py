"""`mission create` and the bare `--prompt` form build the same mission."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from llm_tools.bridge import main


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
