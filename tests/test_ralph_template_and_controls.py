"""A saved command template belongs to its providers; operator controls end capacity waits."""

from __future__ import annotations

from pathlib import Path

from llm_tools import common, ralph_robin
from llm_tools.bridge_runtime import BridgeMissionHooks, ralph_argv
from llm_tools.bridge_store import BridgeStore

TEMPLATE = "/opt/bin/codex exec --json --model gpt-6.1-sol {prompt_file}"


def _logs(tmp_path: Path) -> common.RunLogs:
    return common.setup_run_logs(tmp_path / "logs", "test")


def _config(tmp_path: Path, *args: str) -> ralph_robin.RalphConfig:
    return ralph_robin.parse_args(["--prompt", "x", "--cwd", str(tmp_path), "--command-template", TEMPLATE, *args])


def test_template_saved_for_codex_is_not_applied_to_claude(tmp_path: Path) -> None:
    cfg = _config(tmp_path, "--command-template-providers", "codex")
    claude = ralph_robin.scheduler_config_for(cfg, "claude", _logs(tmp_path), "p", 1)
    codex = ralph_robin.scheduler_config_for(cfg, "codex", _logs(tmp_path), "p", 1)

    assert claude.command_template == ""
    assert claude.claude_stream_json is True, "claude runs its own launch command, so its output is parsed as Claude output"
    assert codex.command_template == TEMPLATE
    assert codex.claude_stream_json is False


def test_legacy_template_without_saved_providers_follows_its_executable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert ralph_robin.scheduler_config_for(cfg, "claude", _logs(tmp_path), "p", 1).command_template == ""
    assert ralph_robin.scheduler_config_for(cfg, "codex", _logs(tmp_path), "p", 1).command_template == TEMPLATE


def test_generic_template_still_applies_to_every_provider(tmp_path: Path) -> None:
    cfg = ralph_robin.parse_args(["--prompt", "x", "--cwd", str(tmp_path), "--command-template", "true"])
    assert ralph_robin.scheduler_config_for(cfg, "claude", _logs(tmp_path), "p", 1).command_template == "true"


def test_template_with_provider_placeholder_applies_to_every_provider(tmp_path: Path) -> None:
    cfg = ralph_robin.parse_args(
        ["--prompt", "x", "--cwd", str(tmp_path), "--command-template", "/opt/bin/codex run {provider} {prompt_file}",
         "--command-template-providers", "codex"],
    )
    assert ralph_robin.scheduler_config_for(cfg, "claude", _logs(tmp_path), "p", 1).command_template == cfg.command_template


def test_bridge_passes_the_providers_the_template_was_saved_for(tmp_path: Path) -> None:
    mission = {"objective": "o", "cwd": str(tmp_path), "config": {"command_template": TEMPLATE, "command_template_providers": "codex"}}
    argv = ralph_argv(mission, tmp_path, providers="claude", routes="")
    assert argv[argv.index("--command-template-providers") + 1] == "codex"


def _hooks(tmp_path: Path) -> tuple[BridgeMissionHooks, BridgeStore, dict]:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("m", "objective", cwd=tmp_path, config={"providers": "codex"})
    store.add_task(mission["id"], "first")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    hooks.applied_rotation = ("codex", "")
    return hooks, store, mission


def test_capacity_wait_ends_for_a_rotation_change_and_a_new_ready_task(tmp_path: Path) -> None:
    hooks, store, mission = _hooks(tmp_path)
    hooks.on_wait({"decisions": []}, None)
    assert hooks.wait_interrupted() is False, "nothing changed since the wait began"

    store.add_task(mission["id"], "second")
    assert hooks.wait_interrupted() is True, "a task added during the wait must be seen"

    hooks.on_wait({"decisions": []}, None)
    assert hooks.wait_interrupted() is False
    with store.db:
        store.db.execute("UPDATE missions SET config_json=json_set(config_json,'$.providers','claude') WHERE id=?", (mission["id"],))
    assert hooks.wait_interrupted() is True, "a rotation change must be seen"
    store.close()
