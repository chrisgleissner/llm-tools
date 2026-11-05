"""Provider-neutral team and token spend guidance."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_tools import bridge_policy, scheduler
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def test_policy_prefix_is_bounded_and_token_preferences_are_distinct() -> None:
    prefixes = [
        bridge_policy.prompt_prefix({"team_policy_enabled": True, "token_spend": preference})
        for preference in bridge_policy.TOKEN_SPEND_PREFERENCES
    ]
    assert len({prefix for prefix in prefixes}) == len(bridge_policy.TOKEN_SPEND_PREFERENCES)
    assert max(len(prefix.split()) for prefix in prefixes) <= 200
    assert all("mandatory validation" in prefix for prefix in prefixes)
    assert "Minimize extra passes" in prefixes[0]
    assert "in proportion" in prefixes[1]
    assert "skeptical checks" in prefixes[2]


@pytest.mark.parametrize("same_content", (True, False))
def test_iteration_emits_identical_objective_once_and_preserves_distinct_task(tmp_path, same_content) -> None:
    objective = "Original objective\nwith  exact spacing."
    task_prompt = objective if same_content else "Distinct task\nwith  exact spacing."
    with BridgeStore(tmp_path / "prompt.sqlite3") as store:
        mission = store.create_mission("mission", objective, cwd=tmp_path, initial_prompt=task_prompt)
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)
        context = hook.before_iteration()
        prompt = context["prompt"]
        assert prompt.count(objective) == 1
        if not same_content:
            assert prompt.count(task_prompt) == 1
        assert store.get_task(mission["initial_task_id"])["original_prompt"] == task_prompt
        assert store.get_mission(mission["id"])["objective"] == objective
        assert context["source_content_bytes"] == len(objective.encode("utf-8")) + (
            0 if same_content else len(task_prompt.encode("utf-8"))
        )


@pytest.mark.parametrize("preference", bridge_policy.TOKEN_SPEND_PREFERENCES)
def test_disabled_teams_omits_delegation_guidance_but_keeps_spend_policy(preference: str) -> None:
    prefix = bridge_policy.prompt_prefix({"team_policy_enabled": False, "token_spend": preference})
    assert "subagent" not in prefix and "delegat" not in prefix
    assert "Token spend preference" in prefix and "mandatory validation" in prefix


@pytest.mark.parametrize("mode", ("native", "background", "headless"))
@pytest.mark.parametrize("preference", bridge_policy.TOKEN_SPEND_PREFERENCES)
@pytest.mark.parametrize("teams_enabled", (True, False))
def test_launch_assembly_adds_policy_and_preserves_user_text(tmp_path, mode: str, preference: str, teams_enabled: bool) -> None:
    store = BridgeStore(tmp_path / f"{mode}-{preference}-{teams_enabled}.sqlite3")
    original = "mission\nobjective with  two spaces"
    task_prompt = "task\ntext with  exact spacing"
    mission = store.create_mission(
        "mission", original, cwd=tmp_path, initial_prompt=task_prompt,
        config={
            "team_policy_enabled": teams_enabled,
            "token_spend": preference,
            "native_tui": mode == "native",
            "headless": mode == "headless",
        },
    )
    task_id = int(mission["initial_task_id"])
    store.set_mission_status(mission["id"], "RUNNING")
    context = {"task_id": task_id}
    handle = SimpleNamespace(session_name="mission-x", socket_path=tmp_path / "station.sock", provider_pane="%1") if mode == "native" else None
    hook = BridgeMissionHooks(
        mission["id"], handle=handle, headless=mode == "headless", native_tui=mode == "native",
        controller_logs=tmp_path / "controller.log",
    )
    hook.bind(store)
    user_text = "user task prompt\nwith  spacing"
    config = scheduler.SchedulerConfig(provider="codex", prompt_text=user_text, cwd=str(tmp_path), ralph_robin_active=True)

    assert hook.before_launch(context, {"provider": "codex"}, config, 1)
    assert config.prompt_text.count(bridge_policy.POLICY_MARKER) == 1
    metrics = next(event["data"] for event in store.events(mission["id"]) if event["kind"] == "prompt-assembled")
    assert metrics["incoming_bytes"] == len(user_text.encode("utf-8"))
    assert metrics["assembled_bytes"] == len(config.prompt_text.encode("utf-8"))
    assert metrics["bridge_added_bytes"] == metrics["assembled_bytes"] - metrics["incoming_bytes"]
    assert metrics["token_count"] is None
    if not teams_enabled:
        assert "subagent" not in config.prompt_text and "delegat" not in config.prompt_text
    assert user_text in config.prompt_text
    assert original == store.get_mission(mission["id"])["objective"]
    assert store.get_task(task_id)["original_prompt"] == task_prompt
    store.close()


def test_legacy_mission_has_no_policy_prefix_and_explicitly_set_policy_does(tmp_path) -> None:
    store = BridgeStore(tmp_path / "legacy.sqlite3")
    mission = store.create_mission("legacy", "objective", cwd=tmp_path, config={})
    assert bridge_policy.effective_settings(mission["config"]) == {
        "teams_enabled": False, "token_spend": "legacy", "legacy": True,
    }
    assert bridge_policy.prompt_prefix(mission["config"]) == ""
    store.close()


def test_status_json_and_overview_show_effective_policy(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge, bridge_format, bridge_tui

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission(
            "mission", "objective", cwd=tmp_path,
            config={"team_policy_enabled": True, "token_spend": "high"},
        )
        mission_id = mission["id"]
    snapshot = bridge.status_snapshot(mission_id)
    assert snapshot["mission"]["execution_policy"] == {
        "teams_enabled": True, "token_spend": "high", "legacy": False,
    }
    assert "Policy      teams on · token spend high" in bridge_format.format_status(snapshot)
    bounded = bridge_tui._bounded_snapshot(snapshot)
    state = bridge_tui._ConsoleState(mission_id, lambda *_: "", lambda _id: {}, full=True, snapshot=bounded)
    assert any("Policy" in row and "teams on · token spend high" in row for row in state.rows())


def test_settings_validation_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="true or false"):
        bridge_policy.validate_settings("on", "medium")
    with pytest.raises(ValueError, match="low, medium, or high"):
        bridge_policy.validate_settings(True, "fast")


def test_new_mission_defaults_and_config_overrides() -> None:
    assert bridge_policy.defaults_from_tool_config({}) == {
        "team_policy_enabled": True, "token_spend": "medium",
    }
    assert bridge_policy.defaults_from_tool_config({"bridge": {"teams": False, "token_spend": "high"}}) == {
        "team_policy_enabled": False, "token_spend": "high",
    }


def test_user_text_containing_policy_marker_does_not_suppress_configured_policy(tmp_path) -> None:
    store = BridgeStore(tmp_path / "quoted-marker.sqlite3")
    mission = store.create_mission(
        "mission", "objective", cwd=tmp_path,
        config={"team_policy_enabled": True, "token_spend": "medium"},
    )
    task_id = store.add_task(mission["id"], "task")
    store.set_mission_status(mission["id"], "RUNNING")
    user_text = f'Please quote "{bridge_policy.POLICY_MARKER}" exactly.'
    config = scheduler.SchedulerConfig(provider="codex", prompt_text=user_text, cwd=str(tmp_path))
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    assert hook.before_launch({"task_id": task_id}, {"provider": "codex"}, config, 1)
    assert config.prompt_text.count(bridge_policy.POLICY_MARKER) == 2  # configured prefix and literal user text
    assert user_text in config.prompt_text
    first = config.prompt_text
    without_duplicate, applied = bridge_policy.apply_prefix_once(
        first, mission["config"], already_applied=config.bridge_policy_applied,
    )
    assert applied and without_duplicate == first
    store.close()
