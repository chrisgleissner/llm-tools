"""Approvals through the coordinator and the standalone first run (TEST-005).

A proposal on a worker is listed, accepted and rejected by the remote CLI
(``llm-bridge --target``) through the real coordinator. A worker enrolled
without ``--allow-control`` refuses the same decision.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from llm_tools.bridge import main
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled
from tests.distributed_harness import add_proposals, login_remote, requires_coordinator, running_system

ROOT = Path(__file__).resolve().parents[1]
pytestmark = requires_coordinator


def _remote(capsys, *argv: str) -> tuple[int, dict | str, str]:
    code = main(["--target", "ws", *argv])
    captured = capsys.readouterr()
    try:
        return code, json.loads(captured.out), captured.err
    except ValueError:
        return code, captured.out, captured.err


def _statuses(state: Path, mission_id: str) -> dict[str, str]:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return {str(row["original_prompt"]): str(row["proposal_status"]) for row in store.list_proposals(mission_id, status="ALL")}


def test_proposals_are_listed_accepted_and_rejected_through_the_coordinator(tmp_path, monkeypatch, capsys):
    prepare = lambda state, mission_id: add_proposals(state, mission_id, ("Keep", "Drop"), capsys)  # noqa: E731
    with running_system(tmp_path, monkeypatch, capsys, allow_control=True, prepare=prepare) as system:
        login_remote(system.coordinator, tmp_path, capsys)
        code, listed, err = _remote(capsys, "proposal", "list", "--mission", system.mission_id, "--json")
        assert code == 0, err
        assert [item["original_prompt"] for item in listed["proposals"]] == ["Keep", "Drop"]

        code, accepted, err = _remote(capsys, "proposal", "accept", "--mission", system.mission_id, "--task", "2", "--json")
        assert code == 0, err
        assert accepted["result"]["proposal_status"] == "ACCEPTED"
        code, rejected, err = _remote(capsys, "proposal", "reject", "--mission", system.mission_id, "--task", "3", "--reason", "not now", "--json")
        assert code == 0, err
        assert rejected["result"]["proposal_status"] == "REJECTED"
        # The decisions happened in the worker's own database, not in the coordinator.
        assert _statuses(system.state, system.mission_id) == {"Keep": "ACCEPTED", "Drop": "REJECTED"}
        code, pending, _ = _remote(capsys, "proposal", "list", "--mission", system.mission_id, "--json")
        assert code == 0 and pending["proposals"] == []
        # A second decision on a settled proposal is refused, not applied twice.
        code, _, err = _remote(capsys, "proposal", "accept", "--mission", system.mission_id, "--task", "3", "--json")
        assert code != 0
        assert _statuses(system.state, system.mission_id)["Drop"] == "REJECTED"


def test_read_only_worker_lists_proposals_but_refuses_decisions(tmp_path, monkeypatch, capsys):
    prepare = lambda state, mission_id: add_proposals(state, mission_id, ("Keep",), capsys)  # noqa: E731
    with running_system(tmp_path, monkeypatch, capsys, allow_control=False, prepare=prepare) as system:
        login_remote(system.coordinator, tmp_path, capsys)
        code, listed, err = _remote(capsys, "proposal", "list", "--mission", system.mission_id, "--json")
        assert code == 0, err
        assert [item["original_prompt"] for item in listed["proposals"]] == ["Keep"]
        for action in (("accept",), ("reject", "--reason", "no")):
            code, _, err = _remote(capsys, "proposal", *action[:1], "--mission", system.mission_id, "--task", "2", *action[1:])
            assert code != 0
            assert "forbidden" in err or "read-only" in err or "not-supported" in err or "unsupported" in err, err
        assert _statuses(system.state, system.mission_id) == {"Keep": "PENDING"}
        with BridgeStore(system.state / "bridge.sqlite3") as store:
            assert store.db.execute("SELECT COUNT(*) FROM commands WHERE payload_json LIKE '%proposal%'").fetchone()[0] == 0


FIRST_RUN = textwrap.dedent(
    """
    import json, socket, sys
    from llm_tools.bridge import main

    attempts = []
    for name in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, name, lambda self, *a, _n=name, **k: attempts.append(_n))
    socket.getaddrinfo = lambda *a, **k: attempts.append("getaddrinfo")
    socket.create_connection = lambda *a, **k: attempts.append("create_connection")

    assert main(["mission", "create", "--prompt", "Objective\\n", "--cwd", sys.argv[1], "--providers", "codex", "--json"]) == 0
    assert main(["mission", "list", "--json"]) == 0
    assert main(["status", "--json"]) in (0, 2)
    distributed = sorted(name for name in sys.modules if name.startswith(("llm_tools.bridge_worker", "llm_tools.bridge_ws", "llm_tools.bridge_remote")))
    print(json.dumps({"attempts": attempts, "distributed": distributed}))
    """
)


def test_standalone_first_run_needs_no_coordinator_module_or_socket(tmp_path):
    state = tmp_path / "fresh-state"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("LLM_", "TMUX", "BRIDGE_"))}
    env.update(PYTHONPATH=str(ROOT), LLM_BRIDGE_STATE_DIR=str(state), HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "config"))
    proc = subprocess.run([sys.executable, "-c", FIRST_RUN, str(tmp_path)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=scaled(60))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["distributed"] == [], "standalone Bridge imported worker or remote modules"
    assert report["attempts"] == [], f"standalone Bridge touched the network: {report['attempts']}"
    assert not (state / "worker").exists(), "no worker enrolment is created without enrolling"
    assert (state / "bridge.sqlite3").exists()
