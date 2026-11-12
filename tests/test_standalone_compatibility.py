"""REV-007: the five tools work without any coordinator, worker or remote configuration.

Each test runs a representative offline command in a subprocess with an empty HOME and
private XDG, state and runtime directories. A ``sitecustomize`` hook on PYTHONPATH records
every non-Unix socket connect, every name lookup and every coordinator-related module that
the process imported. The tests assert that nothing was recorded and that no worker or
coordinator file exists under the state directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import ROOT, scaled

HOOK = '''
import atexit, os, socket, sys, importlib.util

_log = os.environ["STANDALONE_AUDIT_LOG"]

def _note(kind, detail):
    with open(_log, "a", encoding="utf-8") as handle:
        handle.write(f"{kind} {detail}\\n")

_connect = socket.socket.connect
def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _note("connect", address)
    return _connect(self, address)
socket.socket.connect = connect
_connect_ex = socket.socket.connect_ex
def connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _note("connect", address)
    return _connect_ex(self, address)
socket.socket.connect_ex = connect_ex
_getaddrinfo = socket.getaddrinfo
def getaddrinfo(*args, **kwargs):
    _note("lookup", args[:2])
    return _getaddrinfo(*args, **kwargs)
socket.getaddrinfo = getaddrinfo

def _modules():
    for name in sorted(sys.modules):
        tail = name.rsplit(".", 1)[-1]
        if tail in {"bridge_worker", "bridge_remote", "bridge_ws", "bridge_protocol", "bridge_terminal_stream"}:
            _note("import", name)
atexit.register(_modules)

_spec = importlib.util.spec_from_file_location("_repo_sitecustomize", os.path.join(os.environ["STANDALONE_REPO"], "sitecustomize.py"))
if _spec and _spec.loader:
    try:
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
    except Exception:
        pass
'''

COORDINATOR_NAMES = {"worker", "coordinator", "remote.json", "remote"}


class Sandbox:
    def __init__(self, base: Path, env: dict[str, str]) -> None:
        self.state = base / "state"
        self.log = base / "audit.log"
        hook = base / "hook"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(HOOK, encoding="utf-8")
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o700)
        self.env = dict(env)
        self.env.update({
            "LLM_BRIDGE_STATE_DIR": str(self.state),
            "XDG_STATE_HOME": str(base / "xdg-state"),
            "XDG_CONFIG_HOME": str(base / "xdg-config"),
            "XDG_DATA_HOME": str(base / "xdg-data"),
            "LLM_BRIDGE_NO_SIGNIN_CHECK": "1",
            "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1",
            "LLM_TOOLS_NO_INHIBIT": "1",
            "STANDALONE_AUDIT_LOG": str(self.log),
            "STANDALONE_REPO": str(ROOT),
            "PYTHONPATH": os.pathsep.join([str(hook), str(ROOT)]),
        })
        for key in list(self.env):
            if "COORDINATOR" in key or key.startswith("LLM_BRIDGE_REMOTE"):
                del self.env[key]
        self.env.pop("LLM_SCHEDULER_USAGE_JSON", None)

    def run(self, tool: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / tool), *args], cwd=ROOT, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=scaled(120))

    def audit(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def assert_standalone(self) -> None:
        assert self.audit() == [], "coordinator-related imports or network activity"
        leftovers = [str(p.relative_to(self.state)) for p in self.state.rglob("*") if p.name in COORDINATOR_NAMES]
        assert leftovers == []
        for home in (Path(self.env["HOME"]), Path(self.env["XDG_CONFIG_HOME"]), Path(self.env["XDG_STATE_HOME"])):
            if home.exists():
                assert [p for p in home.rglob("*") if p.name in COORDINATOR_NAMES] == []


@pytest.fixture()
def sandbox(tmp_path: Path, env: dict[str, str]) -> Sandbox:
    return Sandbox(tmp_path, env)


USAGE = json.dumps({"generated_at": "2026-01-01T00:00:00Z",
                    "codex": {"provider": "codex", "available": True, "scopes": []}})


def test_llm_bridge_help_and_empty_mission_list(sandbox: Sandbox) -> None:
    helped = sandbox.run("llm-bridge", "--help")
    assert helped.returncode == 0 and "mission" in helped.stdout
    listed = sandbox.run("llm-bridge", "mission", "list", "--json")
    assert listed.returncode == 0, listed.stderr
    sandbox.assert_standalone()


def test_llm_bridge_mission_create_and_list(sandbox: Sandbox, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    created = sandbox.run("llm-bridge", "mission", "create", "--prompt", "Objective\n", "--cwd", str(repo), "--providers", "codex", "--json")
    assert created.returncode == 0, created.stderr
    listed = sandbox.run("llm-bridge", "mission", "list", "--json")
    assert listed.returncode == 0 and "codex" in (created.stdout + listed.stdout)
    sandbox.assert_standalone()


def test_llm_bridge_worker_status_without_enrolment_creates_nothing(sandbox: Sandbox) -> None:
    sandbox.run("llm-bridge", "worker", "status")
    assert not (sandbox.state / "worker").exists()
    assert [line for line in sandbox.audit() if line.startswith(("connect", "lookup"))] == []


def test_llm_usage_json_without_service(sandbox: Sandbox) -> None:
    sandbox.env.update({"LLM_USAGE_NO_SERVICE": "1", "LLM_USAGE_DISABLE_COPILOT": "1", "LLM_USAGE_DISABLE_CODEX_APP_SERVER": "1"})
    result = sandbox.run("llm-usage", "--json", "--no-service")
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) >= {"generated_at", "codex", "claude", "copilot"}
    sandbox.assert_standalone()


def test_llm_scheduler_dry_run(sandbox: Sandbox) -> None:
    sandbox.env["LLM_SCHEDULER_USAGE_JSON"] = USAGE
    result = sandbox.run("llm-scheduler", "--provider", "codex", "--prompt", "x", "--dry-run", "--command-template", "true")
    assert result.returncode == 0, result.stderr
    sandbox.assert_standalone()


def test_ralph_robin_dry_run(sandbox: Sandbox) -> None:
    sandbox.env["LLM_SCHEDULER_USAGE_JSON"] = USAGE
    result = sandbox.run("ralph-robin", "--prompt", "x", "--dry-run", "--command-template", "true")
    assert result.returncode == 0, result.stderr
    sandbox.assert_standalone()


def test_llm_sleep_soak_simulated(sandbox: Sandbox) -> None:
    result = sandbox.run("llm-sleep-soak", "--cycles", "1", "--period", "5s", "--gap", "0")
    assert result.returncode == 0, result.stderr
    sandbox.assert_standalone()
