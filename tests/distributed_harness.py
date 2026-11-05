"""Shared harness: the real release coordinator, a real worker thread and a mission (TEST-005, 006, 012).

Builds on :class:`tests.test_distributed_integration.Coordinator`.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for
from tests.test_distributed_integration import BINARY, Coordinator, _online

requires_coordinator = pytest.mark.skipif(BINARY is None, reason="bridge-coordinator is not built")


@dataclass
class System:
    coordinator: Coordinator
    mission_id: str
    state: Path
    tmp: Path


def create_mission(tmp_path: Path, capsys) -> str:
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["id"]


def add_proposals(state: Path, mission_id: str, prompts: tuple[str, ...], capsys) -> None:
    """Record agent proposals the way a provider operation would (task numbers 2, 3, ...)."""
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        source = store.list_tasks(mission_id)[0]
        operation = store.begin_operation(source["id"], iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    for prompt in prompts:
        assert main(["proposal", "create", "--mission", mission_id, "--operation", operation, "--prompt", prompt, "--json"]) == 0
    capsys.readouterr()


def login_remote(coordinator: Coordinator, tmp: Path, capsys) -> None:
    token_file = tmp / "api-token"
    token_file.write_text(coordinator.api_token)
    assert main(["remote", "login", "--coordinator", f"https://localhost:{coordinator.port}", "--token-file", str(token_file),
                 "--ca-file", str(coordinator.data / "ca.pem")]) == 0
    capsys.readouterr()


def restart(coordinator: Coordinator) -> None:
    """Start the coordinator again on the same data directory and port."""
    config = coordinator.data / "config.toml"
    config.write_text(re.sub(r'bind = ".*"', f'bind = "127.0.0.1:{coordinator.port}"', config.read_text()))
    coordinator.process = subprocess.Popen([str(BINARY), "serve", "--data-dir", str(coordinator.data)], stderr=subprocess.PIPE, text=True,
                                           start_new_session=True)
    assert "https://" in coordinator.process.stderr.readline()


@contextmanager
def running_system(tmp_path: Path, monkeypatch, capsys, *, allow_control: bool, worker_name: str = "ws", prepare=None,
                   data_dir: Path | None = None) -> Iterator[System]:
    """Mission + coordinator + enrolled worker thread, torn down on exit. ``prepare(state, mission_id)`` runs before the worker starts.

    ``data_dir`` places the coordinator's data directory elsewhere than ``tmp_path`` (storage-write tests need a block device)."""
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id = create_mission(tmp_path, capsys)
    if prepare is not None:
        prepare(state, mission_id)
    coordinator = Coordinator(data_dir or tmp_path / "coordinator")
    token = coordinator.run("worker", "add", worker_name)
    worker.enroll(worker_name, f"wss://localhost:{coordinator.port}", token, ca_pem=(coordinator.data / "ca.pem").read_text(), allow_control=allow_control)
    stop = threading.Event()
    session = worker.default_session("test")
    thread = threading.Thread(target=worker.run, args=(session, worker.websocket_connector()), kwargs={"stop": stop, "idle": 0.05}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: _online(coordinator), timeout=scaled(15), interval=0.05, message="worker never came online")
        yield System(coordinator, mission_id, state, tmp_path)
    finally:
        stop.set()
        thread.join(scaled(15))
        coordinator.stop()
