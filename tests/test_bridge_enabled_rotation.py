"""A rotation naming a provider that [usage].providers disables is refused up front."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_rotation
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore

RESTRICTED = '[usage]\nproviders = ["claude", "codex"]\n'
ROUTES = RESTRICTED + '[routes.kilo-glm]\nprovider = "kilo"\nmodel = "zai/glm-5.2"\n[routes.codex-fast]\nprovider = "codex"\n'


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.delenv("LLM_USAGE_PROVIDERS", raising=False)
    return directory


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(path))
    return path


def _create(tmp_path: Path, capsys, *extra: str, code: int = 0) -> str:
    status = main(["mission", "create", "--prompt", "Do it", "--cwd", str(tmp_path), "--json", *extra])
    captured = capsys.readouterr()
    assert status == code, captured.err
    return captured.out if code == 0 else captured.err


def _mission(state: Path) -> dict:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return store.list_missions()[0]


def test_create_refuses_a_disabled_provider_with_an_actionable_message(tmp_path, state, monkeypatch, capsys) -> None:
    config = _config(tmp_path, monkeypatch, RESTRICTED)
    err = _create(tmp_path, capsys, "--providers", "copilot", code=2)
    assert f"copilot is disabled by [usage].providers in {config} · add it there or set LLM_USAGE_PROVIDERS" in err
    assert not (state / "bridge.sqlite3").exists() or BridgeStore(state / "bridge.sqlite3").list_missions() == []


def test_create_refuses_an_explicit_rotation_with_any_disabled_entry(tmp_path, state, monkeypatch, capsys) -> None:
    _config(tmp_path, monkeypatch, RESTRICTED)
    err = _create(tmp_path, capsys, "--providers", "claude,copilot", code=2)
    assert err.startswith("llm-bridge: copilot is disabled by")


def test_create_names_the_environment_variable_when_it_does_the_disabling(tmp_path, state, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "codex")
    err = _create(tmp_path, capsys, "--providers", "claude", code=2)
    assert "claude is disabled by LLM_USAGE_PROVIDERS · add it there" in err


def test_default_rotation_drops_disabled_entries_instead_of_failing(tmp_path, state, monkeypatch, capsys) -> None:
    _config(tmp_path, monkeypatch, RESTRICTED)
    _create(tmp_path, capsys)
    config = _mission(state)["config"]
    assert (config["providers"], config["routes"]) == ("claude,codex", "")


def test_default_rotation_with_nothing_enabled_is_refused(tmp_path, state, monkeypatch, capsys) -> None:
    _config(tmp_path, monkeypatch, '[usage]\nproviders = ["copilot"]\n')
    err = _create(tmp_path, capsys, code=2)
    assert "claude, codex, opencode are disabled by [usage].providers" in err


def test_routes_are_checked_by_their_launch_cli(tmp_path, state, monkeypatch, capsys) -> None:
    _config(tmp_path, monkeypatch, ROUTES)
    err = _create(tmp_path, capsys, "--routes", "codex-fast,kilo-glm", code=2)
    assert "kilo-glm (launch CLI kilo) is disabled by [usage].providers" in err
    _create(tmp_path, capsys, "--routes", "codex-fast")
    assert _mission(state)["config"]["routes"] == "codex-fast"


def test_enabled_rotation_is_accepted_and_unrestricted_config_allows_everything(tmp_path, state, monkeypatch, capsys) -> None:
    _config(tmp_path, monkeypatch, RESTRICTED)
    _create(tmp_path, capsys, "--providers", "codex,claude")
    assert _mission(state)["config"]["providers"] == "codex,claude"
    enabled = bridge_rotation.EnabledProviders(None, "config")
    assert bridge_rotation.check_rotation(enabled, {}, "copilot", "", config_label="x", explicit=True) is None
    assert bridge_rotation.usable_entries(enabled, {}, "copilot,codex", "") == ("copilot,codex", "")


def _frozen_mission(tmp_path: Path, state: Path, providers: str, frozen: str, status: str = "CREATED") -> str:
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission(
            "frozen", "objective", cwd=tmp_path, initial_prompt="p",
            config={"providers": providers, "routes": "", "headless": True, "frozen_tool_config": frozen},
        )["id"]
        if status == "STOPPED":
            store.set_mission_status(mission_id, "RUNNING")
            store.set_mission_status(mission_id, "STOPPING")
            store.set_mission_status(mission_id, "STOPPED")
    return mission_id


def test_start_uses_the_missions_frozen_config_and_refuses_before_launching(tmp_path, state, monkeypatch, capsys) -> None:
    mission_id = _frozen_mission(tmp_path, state, "copilot", RESTRICTED)
    launched: list[object] = []
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: launched.append(a) or 0)
    assert main(["start", "--mission", mission_id]) == 2
    err = capsys.readouterr().err
    assert "copilot is disabled by [usage].providers in this mission's saved config" in err
    assert launched == []
    # The refusal does not change the saved rotation.
    assert _mission(state)["config"]["providers"] == "copilot"


def test_start_with_an_explicit_disabled_provider_is_refused_but_a_saved_partial_rotation_runs(tmp_path, state, monkeypatch, capsys) -> None:
    mission_id = _frozen_mission(tmp_path, state, "claude,copilot", RESTRICTED)
    launched: list[object] = []
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: launched.append(a) or 0)
    assert main(["start", "--mission", mission_id]) == 0  # ralph-robin drops copilot and continues with claude
    assert len(launched) == 1
    assert main(["start", "--mission", mission_id, "--providers", "claude,copilot"]) == 2
    assert "copilot is disabled" in capsys.readouterr().err
    assert len(launched) == 1


def test_environment_can_enable_what_the_frozen_config_disables(tmp_path, state, monkeypatch, capsys) -> None:
    mission_id = _frozen_mission(tmp_path, state, "copilot", RESTRICTED)
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "copilot")
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: 0)
    assert main(["start", "--mission", mission_id]) == 0


def test_resume_refuses_a_rotation_that_can_never_launch(tmp_path, state, monkeypatch, capsys) -> None:
    mission_id = _frozen_mission(tmp_path, state, "copilot", RESTRICTED, status="STOPPED")
    monkeypatch.setattr(bridge, "start_mission", lambda *a, **k: pytest.fail("must not launch"))
    assert main(["resume", "--mission", mission_id]) == 2
    assert "copilot is disabled by [usage].providers" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "STOPPED"


def test_rotation_set_refuses_disabled_entries_and_keeps_the_saved_rotation(tmp_path, state, capsys) -> None:
    mission_id = _frozen_mission(tmp_path, state, "claude", RESTRICTED)
    assert main(["rotation", "--mission", mission_id, "--set", "claude,copilot"]) == 2
    assert "copilot is disabled by [usage].providers in this mission's saved config" in capsys.readouterr().err
    assert _mission(state)["config"]["providers"] == "claude"
    assert main(["rotation", "--mission", mission_id, "--set", "codex,claude"]) == 0
    capsys.readouterr()
    assert _mission(state)["config"]["providers"] == "codex,claude"


def test_mission_create_records_acceptance_on_the_first_task(tmp_path, state, capsys) -> None:
    out = _create(tmp_path, capsys, "--acceptance", "tests pass", "--acceptance", "docs updated")
    assert json.loads(out)["task"]["acceptance"] == ["tests pass", "docs updated"]
    assert main(["task", "show", "--mission", _mission(state)["id"], "--task", "1"]) == 0
    shown = capsys.readouterr().out
    assert "acceptance:" in shown and "  - tests pass" in shown and "  - docs updated" in shown


def test_legacy_prompt_form_accepts_acceptance(tmp_path, state, capsys) -> None:
    assert main(["--prompt", "Do it", "--cwd", str(tmp_path), "--acceptance", "it works", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["task"]["acceptance"] == ["it works"]


def test_task_list_shows_dependencies_in_an_after_column(tmp_path, state, capsys) -> None:
    _create(tmp_path, capsys)
    mission_id = _mission(state)["id"]
    for prompt, after in (("second", "1"), ("third", "1,2")):
        assert main(["task", "add", "--mission", mission_id, "--prompt", prompt, "--depends-on", after]) == 0
    capsys.readouterr()
    assert main(["task", "list", "--mission", mission_id]) == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert rows[0] == ["TASK", "STATUS", "PRIORITY", "AFTER", "TITLE"]
    assert rows[1][3:] == ["Do", "it"]  # no dependency: the column is blank
    assert rows[2][3] == "1" and rows[3][3] == "1,2"
    assert main(["task", "show", "--mission", mission_id, "--task", "3"]) == 0
    assert "depends on 1, 2" in capsys.readouterr().out
