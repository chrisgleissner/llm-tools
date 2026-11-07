"""Safe defaults of the Python-side settings (PRD-026).

Each test reads the default from the code, not from `docs/distributed/safe-defaults.md`. The
document names these tests in its table; a rename here needs the same change there
(`test_safe_defaults_document_names_existing_tests`).
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import stat
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

from llm_tools import bridge_remote, bridge_runtime, bridge_store, bridge_worker as worker, scheduler, ralph_robin, usage, usage_service
from llm_tools import common
from llm_tools.bridge import _build_parser as build_parser, main
from llm_tools.bridge_store import BridgeStore

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "llm_tools"
TOKEN = "s" * 40
CERT = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A fresh state directory that does not exist yet, under a permissive umask."""
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    previous = os.umask(0o022)
    yield directory
    os.umask(previous)


def create_mission(tmp_path: Path, capsys, *extra: str) -> str:
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["id"]


def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- worker opt-ins


def test_worker_opt_ins_default_off(state, tmp_path, capsys):
    """`worker enroll` without flags gives a read-only worker that streams no terminals."""
    args = build_parser().parse_args(["worker", "enroll", "--name", "ws", "--coordinator", "wss://c:1", "--token-file", "x"])
    assert args.allow_control is False and args.allow_terminal_view is False
    assert main(["worker", "enroll", "--name", "ws", "--coordinator", "wss://c:1", "--token-file", str(token_file(tmp_path))]) == 0
    config = worker.load_config()
    assert config.allow_control is False and config.terminal_view is False
    # A configuration file written before the terminal opt-in existed also reads as off.
    path = worker.worker_dir() / worker.WORKER_FILE
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["allow_terminal_view"]
    path.write_text(json.dumps(value), encoding="utf-8")
    assert worker.load_config().terminal_view is False
    assert worker.WorkerConfig("id", "ws", "wss://c", TOKEN, False).allow_terminal_view is False


def test_read_only_worker_advertises_no_mutating_operation(state, tmp_path, capsys):
    create_mission(tmp_path, capsys)
    config = worker.enroll("ws", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(state / "bridge.sqlite3"), software_version="1")
    capabilities = session.hello().payload["capabilities"]
    assert capabilities and not [spec for spec in capabilities if spec["mutating"]]


# --------------------------------------------------------------------------- TLS


def test_worker_and_remote_contexts_verify_certificates_and_hostnames(state):
    contexts = [worker.tls_context(), bridge_remote.Client(bridge_remote.RemoteConfig("https://c:1", TOKEN)).context]
    for context in contexts:
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_worker_refuses_a_plaintext_coordinator_at_enrolment_and_when_loading(state):
    with pytest.raises(worker.WorkerConfigError, match="wss://"):
        worker.enroll("ws", "ws://c:1", TOKEN, ca_pem=None, allow_control=False)
    worker.enroll("ws", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    path = worker.worker_dir() / worker.WORKER_FILE
    value = json.loads(path.read_text(encoding="utf-8"))
    value["coordinator_url"] = "ws://c:1"  # an edited file must not make the worker send its token in clear text
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(worker.WorkerConfigError, match="wss://"):
        worker.load_config()


def test_remote_refuses_a_plaintext_coordinator_at_login_and_when_loading(state):
    with pytest.raises(ValueError, match="https://"):
        bridge_remote.login("http://c:1", TOKEN, ca_pem=None)
    bridge_remote.login("https://c:1", TOKEN, ca_pem=None)
    path = bridge_remote.remote_dir() / bridge_remote.REMOTE_FILE
    value = json.loads(path.read_text(encoding="utf-8"))
    value["coordinator_url"] = "http://c:1"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(bridge_remote.RemoteError, match="https://"):
        bridge_remote.load()


def test_remote_client_never_follows_redirects(state):
    handler = bridge_remote._RefuseRedirect()
    request = urllib.request.Request("https://c:1/api", headers={"Authorization": "Bearer x"})
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere/") is None


# --------------------------------------------------------------------------- private files


def test_enrolment_login_and_audit_files_are_private_and_hold_the_token_only_where_needed(state):
    worker.enroll("ws", "wss://c:1", TOKEN, ca_pem=CERT, allow_control=False)
    bridge_remote.login("https://c:1", TOKEN, ca_pem=CERT)
    for directory in (worker.worker_dir(), bridge_remote.remote_dir()):
        assert mode(directory) == 0o700
        for entry in directory.iterdir():
            assert mode(entry) == 0o600, entry
    assert TOKEN not in (worker.worker_dir() / worker.AUDIT_FILE).read_text(encoding="utf-8")
    assert TOKEN not in json.dumps(worker.load_config().public())
    assert TOKEN not in json.dumps(bridge_remote.load().public())


def test_worker_enrolment_as_the_first_command_leaves_a_private_state_directory(state):
    """Enrolling before any mission exists must not leave a state directory that Bridge then refuses to open."""
    worker.enroll("ws", "wss://c:1", TOKEN, ca_pem=None, allow_control=False)
    assert mode(state) == 0o700
    with BridgeStore(state / "bridge.sqlite3"):
        pass


def test_remote_login_as_the_first_command_leaves_a_private_state_directory(state):
    bridge_remote.login("https://c:1", TOKEN, ca_pem=None)
    assert mode(state) == 0o700
    with BridgeStore(state / "bridge.sqlite3"):
        pass


def test_database_files_are_private_and_a_loose_state_directory_is_refused(state, tmp_path, capsys):
    create_mission(tmp_path, capsys)
    assert mode(state) == 0o700
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("SELECT COUNT(*) FROM events").fetchone()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(store.path) + suffix)
            if path.exists():
                assert mode(path) == 0o600, path
    state.chmod(0o755)
    with pytest.raises(PermissionError, match="private"):
        BridgeStore(state / "bridge.sqlite3")
    state.chmod(0o700)


def test_runtime_and_socket_directories_are_private(state, tmp_path, monkeypatch):
    runtime = bridge_runtime._runtime_directory("safe-defaults")
    assert mode(runtime) == 0o700 and runtime.is_relative_to(state)
    runtime.chmod(0o755)
    with pytest.raises(PermissionError):
        bridge_runtime._runtime_directory("safe-defaults")
    base = tmp_path / "xdg-runtime"
    base.mkdir(mode=0o700)
    socket_path = bridge_runtime.mission_socket_path("safe-defaults", env={"XDG_RUNTIME_DIR": str(base)})
    assert mode(socket_path.parent) == 0o700 and not socket_path.parent.stat().st_mode & 0o077


def test_backup_is_private_and_restore_keeps_the_replaced_database(state, tmp_path, capsys):
    create_mission(tmp_path, capsys)
    copy = tmp_path / "backup.sqlite3"
    assert main(["backup", "--output", str(copy)]) == 0
    assert mode(copy) == 0o600
    assert main(["restore", "--input", str(copy)]) == 0
    kept = list(state.glob("bridge.sqlite3.before-restore-*"))
    assert len(kept) == 1 and mode(kept[0]) == 0o600
    assert mode(state / "bridge.sqlite3") == 0o600
    assert main(["backup", "--output", str(copy)]) == 2  # an existing backup is never overwritten


def test_suspend_ledger_is_private(state):
    path = common.suspend_ledger_path()
    common._append_ledger(path, {"event": "suspend_start"})
    assert mode(path) == 0o600


def test_claude_statusline_copy_is_private(state, monkeypatch, tmp_path):
    """The statusline input names working folders and session ids; it is stored only for its owner."""
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"rate_limits":{"five_hour":{"used_percentage":10}},"cwd":"/private/project"}'))
    usage.statusline_mode()
    copy = common.usage_cache_dir() / "claude-status.json"
    assert copy.exists() and mode(copy) == 0o600


# --------------------------------------------------------------------------- usage service


def test_usage_service_is_a_private_unix_socket_and_nothing_else(state, tmp_path):
    base = tmp_path / "run"
    base.mkdir()
    sock = usage_service.socket_path({"XDG_RUNTIME_DIR": str(base)})
    assert sock.parent == base / "llm-tools"
    assert usage_service.trusted_directory(sock.parent) and mode(sock.parent) == 0o700
    sock.parent.chmod(0o755)
    assert usage_service.trusted_directory(sock.parent) and mode(sock.parent) == 0o700  # an older loose directory is tightened
    server = usage_service.ThreadingUnixServer(str(sock), usage_service.UsageSampler({}, 60), threading.Event())
    try:
        assert server.address_family == socket.AF_UNIX
        assert stat.S_ISSOCK(sock.stat().st_mode) and mode(sock) == 0o600
    finally:
        server.server_close()
    paths = usage_service.ServicePaths  # the dataclass lists every file the service owns
    assert {"socket", "pid", "latest", "history"} <= set(paths.__dataclass_fields__)


def test_usage_service_files_are_private(state, tmp_path):
    env = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    usage_service._atomic_json_write(usage_service.latest_path(env), {"ok": True})
    usage_service._append_history(usage_service.history_path(env), {"ok": True})
    assert mode(usage_service.service_dir(env)) == 0o700
    assert mode(usage_service.latest_path(env)) == 0o600
    assert mode(usage_service.history_path(env)) == 0o600


def test_usage_service_ignores_other_users(monkeypatch):
    class Peer:
        pass

    monkeypatch.setattr(usage_service, "peer_uid", lambda _sock: os.getuid() + 1)
    assert usage_service._peer_is_us(Peer()) is False  # type: ignore[arg-type]
    monkeypatch.setattr(usage_service, "peer_uid", lambda _sock: os.getuid())
    assert usage_service._peer_is_us(Peer()) is True  # type: ignore[arg-type]


def test_service_units_are_user_scoped_and_the_worker_unit_is_hardened(state, tmp_path):
    env = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache"), "PATH": os.environ.get("PATH", "")}
    assert usage_service.systemd_unit_path(env).is_relative_to(tmp_path / "home" / ".config" / "systemd" / "user")
    assert worker.service_unit_path(env).is_relative_to(tmp_path / "home" / ".config" / "systemd" / "user")
    unit = worker.service_unit_text(env)
    assert "UMask=0077" in unit and "NoNewPrivileges=yes" in unit and "WantedBy=default.target" in unit
    assert "Restart=on-failure" in unit and TOKEN not in unit
    for text in (unit, usage_service.systemd_unit_text(60, env)):
        assert "WantedBy=multi-user.target" not in text and "User=root" not in text


# --------------------------------------------------------------------------- no listener, no telemetry


def test_package_has_no_network_listener():
    """The only server in the package is the Unix-socket sampler; everything else is a client."""
    listeners = re.compile(r"AF_INET|HTTPServer|TCPServer|ThreadingTCP|asyncio\.start_server|\.listen\(")
    found = {path.name for path in PACKAGE.rglob("*.py") if listeners.search(path.read_text(encoding="utf-8"))}
    assert found <= {"usage_service.py"}, found  # `.listen(` appears there only for the Unix socket
    source = (PACKAGE / "usage_service.py").read_text(encoding="utf-8")
    assert "AF_INET" not in source and "TCPServer" not in source and "HTTPServer" not in source
    assert usage_service.ThreadingUnixServer.address_family == socket.AF_UNIX


KNOWN_HOSTS = {
    # Provider quota and sign-in endpoints used with the user's own credentials.
    "api.anthropic.com", "console.anthropic.com", "platform.claude.com", "claude.ai",
    "api.github.com", "api.z.ai", "open.bigmodel.cn", "api.minimax.io", "api.minimaxi.com",
    # Documentation and install links printed in messages, never contacted.
    "github.com", "opencode.ai", "www.sqlite.org", "docs.github.com", "docs.anthropic.com", "example.com", "example.org",
    "bridge.home.arpa", "coordinator.example", "www.apple.com",
}


def test_package_contacts_only_provider_and_documentation_hosts():
    """No telemetry: any new host in the source must be reviewed and added to KNOWN_HOSTS."""
    pattern = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    hosts: dict[str, set[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        for host in pattern.findall(path.read_text(encoding="utf-8")):
            hosts.setdefault(host.lower(), set()).add(path.name)
    assert not set(hosts) - KNOWN_HOSTS, {host: sorted(files) for host, files in hosts.items() if host not in KNOWN_HOSTS}


# --------------------------------------------------------------------------- suspend, launch and approval defaults


def test_mission_does_not_suspend_the_machine_unless_asked(state, tmp_path, capsys):
    mission_id = create_mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        config = store.get_mission(mission_id)["config"]
    assert not config.get("suspend_when_rate_limited")
    assert not config.get("auto_continue")  # native missions wait for the operator's approval
    parsed = build_parser().parse_args(["mission", "set", "--mission", mission_id, "--auto-continue", "off"])
    assert parsed.suspend_when_rate_limited is None


def test_mission_suspend_is_refused_without_the_opt_in(state, tmp_path, capsys):
    mission_id = create_mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.db.execute("UPDATE missions SET status = 'RUNNING' WHERE id = ?", (mission_id,))
        hooks = bridge_runtime.BridgeMissionHooks(mission_id, handle=None, headless=True, controller_logs=tmp_path / "logs")
        hooks._store = store
        assert hooks.suspend_allowed() is False
        assert hooks._suspend_block_reason == "mission-not-running-or-suspend-disabled"


def test_scheduler_ralph_and_soak_do_not_suspend_or_arm_wakes_by_default():
    scheduled = scheduler.parse_args(["--provider", "codex", "--prompt", "x"])
    assert not scheduled.wake and not scheduled.suspend_until_ready
    rotation = ralph_robin.parse_args(["--prompt", "x"])
    assert not rotation.wake and not rotation.suspend_until_ready and not rotation.watchdog


def test_provider_launch_commands_add_no_permission_bypass_flag():
    bypass = {"--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
              "--allow-all-tools", "--allow-all", "--auto", "--no-ask-user", "--approval-mode=yolo"}
    for provider in ("codex", "claude", "copilot", "kilo", "opencode"):
        for attached in (True, False):
            cfg = scheduler.parse_args(["--provider", provider, "--prompt", "x"])
            cfg.attached = attached
            assert not bypass & set(scheduler.provider_default_argv(cfg, "x")), (provider, attached)


def test_every_documented_test_exists():
    document = (ROOT / "docs" / "distributed" / "safe-defaults.md").read_text(encoding="utf-8")
    references = re.findall(r"`(tests/test_\w+\.py)::(test_\w+)`", document)
    assert len(references) >= 20
    missing = [f"{file}::{name}" for file, name in references
               if not re.search(rf"^def {name}\(", (ROOT / file).read_text(encoding="utf-8"), re.M)]
    assert not missing, missing
    mine = set(re.findall(r"^def (test_\w+)", Path(__file__).read_text(encoding="utf-8"), re.M)) - {"test_every_documented_test_exists"}
    uncovered = mine - {name for file, name in references if file == "tests/test_safe_defaults.py"}
    assert not uncovered, f"tests missing from the table in safe-defaults.md: {sorted(uncovered)}"
