"""Cross-component: the Python worker against the real Rust coordinator binary (TEST-003, TEST-005).

Skipped when the coordinator has not been built (``cargo build`` in
``coordinator/``) and ``BRIDGE_COORDINATOR_BIN`` is unset; CI builds it first.
"""

from __future__ import annotations

import json
import http.client
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for

ROOT = Path(__file__).resolve().parents[1]


def _binary() -> Path | None:
    override = os.environ.get("BRIDGE_COORDINATOR_BIN")
    if override:
        return Path(override)
    for profile in ("release", "debug"):
        candidate = ROOT / "coordinator" / "target" / profile / ("bridge-coordinator.exe" if os.name == "nt" else "bridge-coordinator")
        if candidate.exists():
            return candidate
    return None


BINARY = _binary()
if BINARY is None and os.environ.get("BRIDGE_REQUIRE_COORDINATOR") == "1":
    raise RuntimeError("BRIDGE_REQUIRE_COORDINATOR=1 but bridge-coordinator is not built")
pytestmark = pytest.mark.skipif(BINARY is None, reason="bridge-coordinator is not built")


def strict_context(ca: Path) -> ssl.SSLContext:
    """Client context with RFC 5280 strict checks, the default of Python 3.13 and later.

    Set explicitly so a certificate defect (missing key usage or key identifiers) fails on
    every Python version, not only on the newest interpreter in CI."""
    context = ssl.create_default_context(cafile=str(ca))
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


class Coordinator:
    def __init__(self, data: Path, config_overrides: dict[str, str] | None = None) -> None:
        self.data = data
        self.run("init", "--hostname", "localhost")
        config = data / "config.toml"
        config_text = config.read_text()
        config_text = re.sub(r'bind = ".*"', 'bind = "127.0.0.1:0"', config_text)
        # Stress the offline queue independently of production mutation limits. Rust hardening
        # tests exercise the defaults; this integration fixture raises only its address budget
        # because queue tests launch 16 concurrent API clients during reconnect churn.
        config_text = config_text.replace("mutation_limit_per_address = 120", "mutation_limit_per_address = 2000")
        config_text = config_text.replace("max_connections_per_ip = 32", "max_connections_per_ip = 128")
        for key, value in (config_overrides or {}).items():
            config_text = re.sub(rf"(?m)^{re.escape(key)} = .*?$", f"{key} = {value}", config_text)
        config.write_text(config_text)
        self.process = subprocess.Popen([str(BINARY), "serve", "--data-dir", str(data)], stderr=subprocess.PIPE, text=True, start_new_session=True)
        line = self.process.stderr.readline()
        match = re.search(r"https://(\S+)", line)
        assert match, line
        self.address = match.group(1)
        self.port = int(self.address.rsplit(":", 1)[1])
        self.context = strict_context(data / "ca.pem")
        # The helper reads workers and submits mutations through the coordinator API.
        # Tokens now default to read-only, so the fixture must opt into admin.
        self.api_token = self.run("token", "create", "cli", "--scope", "admin")

    def run(self, *args: str) -> str:
        return subprocess.run([str(BINARY), *args, "--data-dir", str(self.data)], check=True, capture_output=True, text=True, timeout=scaled(30)).stdout.strip()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        token: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {token or self.api_token}", "Content-Type": "application/json"}
        encoded = None if body is None else json.dumps(body).encode()
        if source_ip is not None:
            connection = http.client.HTTPSConnection(
                "localhost", self.port, context=self.context, timeout=scaled(30), source_address=(source_ip, 0),
            )
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            status = response.status
            connection.close()
            return status, json.loads(payload or b"{}")
        request = urllib.request.Request(
            f"https://localhost:{self.port}{path}", method=method, data=encoded, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=scaled(30)) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def mutation_tokens(self, count: int) -> list[str]:
        """Create distinct admin credentials for stress tests under normal per-token limits."""
        if count < 1:
            raise ValueError("at least one token is required")
        return [self.api_token] + [
            self.run("token", "create", f"load-{index}", "--scope", "admin") for index in range(1, count)
        ]

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=scaled(10))
        finally:
            if self.process.poll() is None:
                self.process.kill()
            self.process.stderr.close()


@pytest.fixture
def system(tmp_path, monkeypatch, capsys):
    if sys.platform == "win32":
        pytest.skip("workers and the mission store are not supported on Windows (docs/distributed/DECISIONS.md D8)")
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    coordinator = Coordinator(tmp_path / "coordinator")
    token = coordinator.run("worker", "add", "ws")
    worker.enroll("ws", f"wss://localhost:{coordinator.port}", token, ca_pem=(coordinator.data / "ca.pem").read_text(), allow_control=True)
    stop = threading.Event()
    session = worker.default_session("test")
    thread = threading.Thread(target=worker.run, args=(session, worker.websocket_connector()), kwargs={"stop": stop, "idle": 0.05}, daemon=True)
    thread.start()
    try:
        yield coordinator, mission_id, state
    finally:
        stop.set()
        thread.join(scaled(10))
        coordinator.stop()


def _online(coordinator: Coordinator) -> bool:
    status, body = coordinator.request("GET", "/api/v1/workers")
    return status == 200 and any(item.get("name") == "ws" and item.get("state") == "online" for item in body["workers"])


def _submit(coordinator: Coordinator, request: CommandRequest) -> dict:
    status, body = coordinator.request("POST", "/api/v1/commands", {"target": "ws", "request": request.to_json(), "wait_ms": int(scaled(10) * 1000)})
    assert status in {200, 202}, body
    return body


def test_remote_commands_match_local_semantics_and_are_idempotent(system):
    coordinator, mission_id, state = system
    wait_for(lambda: _online(coordinator), timeout=scaled(15), interval=0.05, message="worker never came online")

    status_request = CommandRequest("mission.status", mission_id=mission_id, target="ws")
    remote = _submit(coordinator, status_request)
    assert remote["state"] == "done"
    local = commands.execute(CommandRequest("mission.status", mission_id=mission_id))
    assert remote["result"]["outcome"] == "ok"
    redacted = {"cwd", "repo_identity", "objective", "config"}
    local_mission = local.result["mission"]
    remote_mission = remote["result"]["result"]["mission"]
    assert remote_mission == {key: value for key, value in local_mission.items() if key not in redacted}
    assert not redacted & remote_mission.keys()

    cancel = CommandRequest("mission.cancel", mission_id=mission_id[:8], target="ws", request_id="remote-cancel")
    first = _submit(coordinator, cancel)
    assert first["result"]["outcome"] == "applied"
    again = _submit(coordinator, cancel)
    assert again["result"] == first["result"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "CANCELLED"
        assert store.db.execute("SELECT COUNT(*) FROM commands WHERE id='remote-cancel'").fetchone()[0] == 1

    status, missions = coordinator.request("GET", "/api/v1/missions")
    assert status == 200 and mission_id in json.dumps(missions)

    def replicated() -> bool:
        code, body = coordinator.request("GET", "/api/v1/events?worker=ws&limit=1000")
        return code == 200 and "command-applied" in json.dumps(body)

    wait_for(replicated, timeout=scaled(15), interval=0.1, message="events were not replicated")


def test_worker_survives_coordinator_restart(system):
    coordinator, mission_id, _ = system
    wait_for(lambda: _online(coordinator), timeout=scaled(15), interval=0.05, message="worker never came online")
    port = coordinator.port
    coordinator.stop()
    config = coordinator.data / "config.toml"
    config.write_text(re.sub(r'bind = ".*"', f'bind = "127.0.0.1:{port}"', config.read_text()))
    time.sleep(0.2)  # the worker observes the disconnect and starts its backoff
    restarted = subprocess.Popen([str(BINARY), "serve", "--data-dir", str(coordinator.data)], stderr=subprocess.PIPE, text=True, start_new_session=True)
    coordinator.process = restarted
    restarted.stderr.readline()
    wait_for(lambda: _online(coordinator), timeout=scaled(30), interval=0.1, message="worker did not reconnect")
    result = _submit(coordinator, CommandRequest("mission.status", mission_id=mission_id, target="ws"))
    assert result["result"]["outcome"] == "ok"


def test_remote_cli_preserves_mission_identity_without_private_fields(system, tmp_path, capsys):
    coordinator, mission_id, state = system
    wait_for(lambda: _online(coordinator), timeout=scaled(15), interval=0.05, message="worker never came online")
    token_file = tmp_path / "api-token"
    token_file.write_text(coordinator.api_token)
    assert main(["remote", "login", "--coordinator", f"https://localhost:{coordinator.port}", "--token-file", str(token_file),
                 "--ca-file", str(coordinator.data / "ca.pem")]) == 0
    capsys.readouterr()

    def run(*argv):
        code = main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    for argv in (["mission", "list", "--json"], ["task", "list", "--mission", mission_id, "--json"], ["task", "show", "--mission", mission_id, "--task", "1", "--json"]):
        code, local_out, _ = run(*argv)
        remote_code, remote_out, err = run("--target", "ws", *argv)
        assert (code, remote_code) == (0, 0), err
        local, remote = json.loads(local_out), json.loads(remote_out)
        if argv[0] == "mission":
            local_row = local["missions"][0]
            remote_row = remote["missions"][0]
            redacted = {"cwd", "repo_identity", "objective", "config"}
            assert remote_row == {key: value for key, value in local_row.items() if key not in redacted}
            assert not redacted & remote_row.keys()
        else:
            assert remote == local

    code, out, _ = run("--target", "ws", "status", "--mission", mission_id[:8])
    assert code == 0 and mission_id[:8] in out
    code, out, err = run("--target", "ws", "task", "pause", "--mission", mission_id, "--task", "1", "--json")
    assert code == 0, err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.list_tasks(mission_id)[0]["status"] == "PAUSED"
    code, out, _ = run("--target", "all", "mission", "list", "--json")
    assert code == 0 and json.loads(out)["targets"]["ws"]["outcome"] == "ok"
    code, out, _ = run("worker", "list", "--json")
    assert code == 0 and [worker["name"] for worker in json.loads(out)["workers"]] == ["ws"]
    code, _, err = run("--target", "nowhere", "status", "--mission", mission_id)
    assert code == 2 and "unknown-target" in err
    code, _, err = run("--target", "ws", "status")
    assert code == 2 and "--mission is required" in err
    code, _, err = run("--target", "all", "cancel", "--mission", mission_id)
    assert code == 2 and "read-only" in err
    code, _, err = run("--target", "ws", "task", "show", "--mission", mission_id, "--task", "7")
    assert code == 2 and "ws:" in err


def test_remote_client_reaches_the_coordinator_without_a_worker(tmp_path, monkeypatch, capsys):
    """The part of the CLI that Windows supports: remote login/status/logout and worker list."""
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    coordinator = Coordinator(tmp_path / "coordinator")
    try:
        coordinator.run("worker", "add", "ws")
        token_file = tmp_path / "api-token"
        token_file.write_text(coordinator.api_token)
        assert main(["remote", "login", "--coordinator", f"https://localhost:{coordinator.port}", "--token-file", str(token_file),
                     "--ca-file", str(coordinator.data / "ca.pem")]) == 0
        capsys.readouterr()
        assert main(["remote", "status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["coordinator_url"] == f"https://localhost:{coordinator.port}"
        assert main(["worker", "list", "--json"]) == 0
        workers = json.loads(capsys.readouterr().out)["workers"]
        assert [(worker["name"], worker["state"]) for worker in workers] == [("ws", "offline")]
        assert main(["--target", "ws", "status", "--mission", "abc"]) != 0  # an offline worker is reported, never run locally
        capsys.readouterr()
        assert main(["remote", "logout"]) == 0
    finally:
        coordinator.stop()


def _handshake(port: int, ca: Path) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=scaled(10)) as raw:
        with strict_context(ca).wrap_socket(raw, server_hostname="localhost") as tls:
            return tls.getpeercert()


def test_generated_certificates_pass_strict_verification_before_and_after_renewal(tmp_path):
    coordinator = Coordinator(tmp_path / "coordinator")
    try:
        assert ("DNS", "localhost") in _handshake(coordinator.port, coordinator.data / "ca.pem")["subjectAltName"]
    finally:
        coordinator.stop()
    before = (coordinator.data / "server.pem").read_text()
    coordinator.run("cert", "renew", "--hostname", "localhost")
    assert (coordinator.data / "server.pem").read_text() != before
    process = subprocess.Popen([str(BINARY), "serve", "--data-dir", str(coordinator.data)], stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        match = re.search(r"https://\S+:(\d+)", process.stderr.readline())
        assert match
        assert ("DNS", "localhost") in _handshake(int(match.group(1)), coordinator.data / "ca.pem")["subjectAltName"]
    finally:
        process.terminate()
        process.wait(timeout=scaled(10))
        process.stderr.close()
