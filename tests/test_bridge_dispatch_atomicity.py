"""Operator command effects and their acknowledgement commit together."""
from __future__ import annotations

from pathlib import Path

import pytest

from llm_tools.bridge import _apply_operator_command
from llm_tools.bridge_store import BridgeStore


def test_command_interruption_cannot_leave_applied_task_without_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "bridge.sqlite3"
    with BridgeStore(path) as store:
        mission_id = store.create_mission("commands", "objective", cwd=tmp_path)["id"]

        def interrupted_ack(*args, **kwargs):
            raise KeyboardInterrupt("interrupted before acknowledgement commit")

        with monkeypatch.context() as patch:
            patch.setattr(store, "finish_command", interrupted_ack)
            with pytest.raises(KeyboardInterrupt):
                _apply_operator_command(store, mission_id,
                                        {"action": "task.add", "prompt": "exactly one durable task"})
        assert store.list_tasks(mission_id) == []
        assert not store.db.in_transaction
        pending = store.pending_commands(mission_id)
        assert len(pending) == 1
        assert pending[0]["payload"]["prompt"] == "exactly one durable task"
        assert not any(event["kind"] == "task-added" for event in store.events(mission_id))
    with BridgeStore(path) as reopened:
        assert reopened.list_tasks(mission_id) == []
        assert len(reopened.pending_commands(mission_id)) == 1
