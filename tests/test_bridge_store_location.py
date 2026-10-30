"""One definition of the database path and of "is a controller running"."""

from __future__ import annotations

import pytest

from llm_tools import bridge, bridge_launcher, bridge_store, bridge_switcher
from llm_tools.bridge_store import MissionLock, controller_running, default_state_dir, store_path


def test_store_path_is_the_database_inside_the_state_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert store_path() == tmp_path / "state" / "bridge.sqlite3"
    assert store_path({"LLM_BRIDGE_STATE_DIR": str(tmp_path / "other")}) == tmp_path / "other" / "bridge.sqlite3"
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert store_path() == tmp_path / "xdg" / "llm-tools" / "llm-bridge" / "bridge.sqlite3"
    assert default_state_dir() / "bridge.sqlite3" == store_path()


def test_a_store_opened_without_a_path_uses_store_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge_store.BridgeStore() as store:
        assert store.path == store_path()
    assert store_path().exists()
    assert bridge.open_store().path == store_path()


def test_every_caller_sees_a_held_lock_and_a_free_lock_alike(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    probes = (
        controller_running,
        bridge.controller_running,
        bridge_launcher.controller_running,
        bridge_switcher.default_controller_probe,
    )
    assert [probe("mission-1") for probe in probes] == [False, False, False, False]
    with MissionLock(default_state_dir() / "locks", "mission-1"):
        assert [probe("mission-1") for probe in probes] == [True, True, True, True]
    assert [probe("mission-1") for probe in probes] == [False, False, False, False]


def test_an_unreadable_lock_is_reported_the_way_each_caller_always_did(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))

    def broken(_directory, _mission_id):
        raise OSError("lock directory unreadable")

    monkeypatch.setattr(MissionLock, "is_held", staticmethod(broken))
    with pytest.raises(OSError):
        controller_running("mission-1")
    with pytest.raises(OSError):
        bridge.controller_running("mission-1")
    assert controller_running("mission-1", on_error=None) is None
    assert controller_running("mission-1", on_error=False) is False
    assert bridge_launcher.controller_running("mission-1") is None
    assert bridge_switcher.default_controller_probe("mission-1") is False
