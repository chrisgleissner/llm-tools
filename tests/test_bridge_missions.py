"""The mission service resolves references through named store methods and holds no SQL itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_tools import bridge, bridge_missions
from llm_tools.bridge_store import BridgeStore


def _store(tmp_path: Path) -> tuple[BridgeStore, list[str]]:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    ids = [store.create_mission(f"mission {index}", "objective", cwd=tmp_path)["id"] for index in range(3)]
    return store, ids


def test_store_methods_find_a_mission_by_id_prefix_and_recency(tmp_path: Path) -> None:
    store, ids = _store(tmp_path)
    with store:
        assert store.mission_id_if_exists(ids[0]) == ids[0]
        assert store.mission_id_if_exists("not-a-mission") is None
        assert [str(row["id"]) for row in store.missions_with_id_prefix(ids[1][:12])] == [ids[1]]
        assert store.missions_with_id_prefix("zzzz") == []
        newest_first = [str(row["id"]) for row in store.missions_newest_first()]
        assert sorted(newest_first) == sorted(ids)


def test_resolve_mission_ref_uses_the_store_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, ids = _store(tmp_path)
    with store:
        assert bridge_missions.resolve_mission_ref(store, ids[2]) == ids[2]
        assert bridge_missions.resolve_mission_ref(store, ids[1][:10]) == ids[1]
        with pytest.raises(bridge_missions.MissionNotFound):
            bridge_missions.resolve_mission_ref(store, "zzzz9999")
        with pytest.raises(ValueError, match="too short"):
            bridge_missions.resolve_mission_ref(store, "ab")
        monkeypatch.setattr(bridge_missions, "station_live", lambda _mission: False)
        monkeypatch.setattr(bridge_missions, "controller_running", lambda _mission: False)
        with pytest.raises(ValueError, match="several missions match folder"):
            bridge_missions.resolve_mission_ref(store, None, cwd=tmp_path)


def test_bridge_re_exports_the_service_names() -> None:
    assert bridge.resolve_mission_ref is bridge_missions.resolve_mission_ref
    assert bridge.order_missions is bridge_missions.order_missions
    assert bridge.MissionNotFound is bridge_missions.MissionNotFound
    assert bridge.MIN_MISSION_PREFIX == bridge_missions.MIN_MISSION_PREFIX


def test_service_module_contains_no_sql() -> None:
    text = Path(bridge_missions.__file__).read_text(encoding="utf-8")
    assert ".db.execute" not in text and "SELECT" not in text
