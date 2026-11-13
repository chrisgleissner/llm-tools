"""Coordinator crash while a command is in flight (TEST-006).

The coordinator is killed with SIGKILL after it accepted a command and the
worker started executing it, so the result cannot be delivered. After the
coordinator restarts on the same data directory it redelivers the journalled
command. The worker must apply it at most once, and an outcome it cannot
confirm must be reported as uncertain, never as success.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import threading
from pathlib import Path

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_worker
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for
from tests.distributed_harness import requires_coordinator, restart, running_system
from tests.test_distributed_integration import _online

pytestmark = requires_coordinator
REQUEST_ID = "crash-add"


def _task_count(state: Path, mission_id: str) -> int:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return len(store.list_tasks(mission_id))


@pytest.mark.parametrize("storage_failures", [0, 2], ids=["effect-applied-once", "unconfirmed-effect-is-uncertain"])
def test_coordinator_sigkill_during_command_never_loses_or_repeats_the_effect(tmp_path, monkeypatch, capsys, storage_failures):
    entered, release = threading.Event(), threading.Event()
    executions: list[str] = []
    real_execute, real_finish = commands.execute, BridgeStore.finish_command
    ledger_hits: list[str] = []  # redeliveries the worker answered from its outcome ledger instead of executing
    real_stored = bridge_worker.WorkerSession._stored_outcome

    def counting_stored(self, request_id):
        outcome = real_stored(self, request_id)
        if outcome is not None and request_id == REQUEST_ID:
            ledger_hits.append(request_id)
        return outcome

    failures = iter([sqlite3.OperationalError("disk I/O error")] * storage_failures)

    def gated(request):
        if request.request_id != REQUEST_ID:
            return real_execute(request)
        executions.append(request.request_id)
        if len(executions) == 1:
            entered.set()  # the coordinator has accepted and delivered the command
            assert release.wait(scaled(60))
        return real_execute(request)

    def flaky_finish(self, *args, **kwargs):
        error = next(failures, None) if args and args[0] == REQUEST_ID else None
        if error is not None:
            raise error
        return real_finish(self, *args, **kwargs)

    monkeypatch.setattr(commands, "execute", gated)
    monkeypatch.setattr(bridge_worker.WorkerSession, "_stored_outcome", counting_stored)
    monkeypatch.setattr(BridgeStore, "finish_command", flaky_finish)
    with running_system(tmp_path, monkeypatch, capsys, allow_control=True) as system:
        coordinator = system.coordinator
        before = _task_count(system.state, system.mission_id)
        request = commands.CommandRequest("task.add", {"prompt": "added once"}, mission_id=system.mission_id, target="ws", request_id=REQUEST_ID)
        status, body = coordinator.request("POST", "/api/v1/commands", {"target": "ws", "request": request.to_json(), "wait_ms": 0})
        assert status in {200, 202}, body
        assert entered.wait(scaled(15)), "the worker never received the command"

        os.killpg(coordinator.process.pid, signal.SIGKILL)  # crash with the command accepted and no result recorded
        coordinator.process.wait(timeout=scaled(10))
        coordinator.process.stderr.close()
        release.set()
        restart(coordinator)
        wait_for(lambda: _online(coordinator), timeout=scaled(30), interval=0.1, message="worker did not reconnect")

        def settled() -> dict | None:
            code, row = coordinator.request("GET", f"/api/v1/commands/{REQUEST_ID}")
            return row if code == 200 and row.get("state") == "done" else None

        row = wait_for(settled, timeout=scaled(30), interval=0.1, message="coordinator never recorded an outcome after the restart")
        result = row["result"]
        # Reported once: a repeat lookup and a repeated submission return the identical outcome.
        assert coordinator.request("GET", f"/api/v1/commands/{REQUEST_ID}")[1]["result"] == result
        status, again = coordinator.request("POST", "/api/v1/commands", {"target": "ws", "request": request.to_json(), "wait_ms": 0})
        assert status == 200 and again["result"] == result

        tasks = _task_count(system.state, system.mission_id) - before
        if storage_failures == 0:
            assert result["outcome"] == "applied"
            assert tasks == 1, "the redelivered command must not be applied twice"
            # The coordinator redelivered the command after the restart: the worker saw the id again and answered it from
            # its outcome ledger, so the operation itself ran once.
            assert ledger_hits, "the coordinator did not redeliver the command after the restart"
            assert len(executions) == 1, "the redelivered command was executed again"
        else:
            assert result["outcome"] == "uncertain", result
            assert result["error"]["code"] == "internal"
            assert tasks == 0, "an effect reported as uncertain must not have been committed"
        with BridgeStore(system.state / "bridge.sqlite3") as store:
            rows = [row[0] for row in store.db.execute("SELECT status FROM commands WHERE id=?", (REQUEST_ID,))]
        assert rows.count("APPLIED") == (1 if storage_failures == 0 else 0)
