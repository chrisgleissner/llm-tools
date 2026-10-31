"""Station names come from the mission's own rotation."""

from __future__ import annotations

from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore


def test_stations_follow_the_mission_rotation_not_every_config_table(tmp_path) -> None:
    frozen = (
        '[providers.codex]\nmodel = "gpt-5"\neffort = "high"\n'
        '[providers.opencode]\nmodel = "x"\n'
        '[routes.kilo-minimax-m3]\nmodel = "minimax-m3"\n'
        '[routes.kilo-zai-glm-52]\nmodel = "zai/glm-5.2"\n'
    )
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("one", "objective", cwd=tmp_path, config={
        "providers": "codex", "routes": "", "frozen_tool_config": frozen,
    })
    snapshot = project_snapshot(store, mission["id"])
    assert [(row["name"], row["status"]) for row in snapshot["stations"]] == [("codex", "configured")]
    assert snapshot["stations"][0]["model"] == "gpt-5"
    assert snapshot["stations"][0]["effort"] == "high"
    assert snapshot["station_config"]["providers"] == ["codex"]
    assert snapshot["station_config"]["routes"] == []

    routed = store.create_mission("routed", "objective", cwd=tmp_path, config={
        "providers": "claude,codex,opencode", "routes": "kilo-zai-glm-52", "frozen_tool_config": frozen,
    })
    snapshot = project_snapshot(store, routed["id"])
    assert [row["name"] for row in snapshot["stations"]] == ["kilo-zai-glm-52"]
    assert snapshot["stations"][0]["model"] == "zai/glm-5.2"


def test_stations_fall_back_to_frozen_defaults_only_without_a_mission_rotation(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    frozen = '[defaults]\nproviders = ["claude", "codex"]\n[providers.kilo]\nmodel = "m"\n'
    mission = store.create_mission("defaults", "objective", cwd=tmp_path, config={"frozen_tool_config": frozen})
    snapshot = project_snapshot(store, mission["id"])
    assert [row["name"] for row in snapshot["stations"]] == ["claude", "codex"]
    ralph = '[ralph]\nroutes = ["a", "b"]\n[defaults]\nproviders = "claude"\n'
    mission = store.create_mission("ralph", "objective", cwd=tmp_path, config={"frozen_tool_config": ralph})
    assert [row["name"] for row in project_snapshot(store, mission["id"])["stations"]] == ["a", "b"]
    broken = store.create_mission("broken", "objective", cwd=tmp_path, config={"frozen_tool_config": "not = [toml"})
    assert project_snapshot(store, broken["id"])["stations"] == []
