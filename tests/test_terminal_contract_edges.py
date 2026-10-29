"""Security, validation, and recoverability contracts for the private tmux backend."""

from __future__ import annotations

import shutil
import socket as unix_socket
import subprocess
import sys

import pytest

from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def test_command_and_socket_config_validators_reject_unsafe_inputs(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    assert backend._command(["provider cli", "--model", "x y"]) == "exec 'provider cli' --model 'x y'"
    with pytest.raises(ValueError, match="non-empty argv"):
        backend._command([])
    with pytest.raises(ValueError, match="NUL"):
        backend._command(["provider", "bad\x00arg"])

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    socket = private / "station.sock"
    with pytest.raises(FileNotFoundError):
        backend._validate_socket_path(socket)
    backend._validate_socket_path(socket, allow_missing=True)
    socket.write_text("not a socket", encoding="utf-8")
    with pytest.raises(PermissionError, match="not a private socket"):
        backend._validate_socket_path(socket)
    socket.unlink()

    config = private / "station.sock.conf"
    config.write_text("config", encoding="utf-8")
    config.chmod(0o600)
    backend._validate_config_path(socket, config)
    with pytest.raises(PermissionError, match="private sibling"):
        backend._validate_config_path(socket, private / "elsewhere.conf")
    config.chmod(0o644)
    with pytest.raises(PermissionError, match="private file"):
        backend._validate_config_path(socket, config)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"session_name": "bad/name"}, "session_name"),
        ({"size": (79, 24)}, "at least 80"),
        ({"size": (80, 23)}, "at least 80"),
        ({"bridge_rows": 4}, "bridge_rows"),
        ({"bridge_rows": 9}, "bridge_rows"),
    ],
)
def test_start_rejects_invalid_station_configuration_before_creating_artifacts(tmp_path, kwargs, message) -> None:
    args = {
        "provider_argv": [sys.executable, "-c", "pass"],
        "bridge_argv": ["/bin/cat"],
        "cwd": tmp_path,
        "socket_path": tmp_path / "runtime" / "station.sock",
        "session_name": "valid-name",
    }
    args.update(kwargs)
    with pytest.raises(ValueError, match=message):
        TmuxTerminalBackend().start(**args)
    assert not (tmp_path / "runtime" / "station.sock.conf").exists()


def test_dead_server_cleanup_requires_bridge_configuration_marker(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket = runtime / "stale.sock"
    config = socket.with_name(socket.name + ".conf")
    config.write_text("set-option -g mouse on\n", encoding="utf-8")
    handle = TerminalHandle(socket, config, "stale", "%0", "%1", 6)
    with pytest.raises(PermissionError, match="cannot prove ownership"):
        TmuxTerminalBackend().stop(handle)
    assert not socket.exists() and config.exists()


def test_dead_socket_artifact_is_removed_only_with_bridge_config_marker(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "orphan.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
    finally:
        listener.close()
    socket_path.chmod(0o600)
    config = socket_path.with_name(socket_path.name + ".conf")
    config.write_text(TmuxTerminalBackend._BRIDGE_CONFIG_MARKER, encoding="utf-8")
    config.chmod(0o600)
    handle = TerminalHandle(socket_path, config, "orphan", "%0", "%1", 6)
    TmuxTerminalBackend().stop(handle)
    assert not socket_path.exists() and not config.exists()


def test_start_config_write_error_removes_partial_private_config(tmp_path, monkeypatch) -> None:
    import os
    import llm_tools.terminal as terminal

    real_fdopen = os.fdopen

    def fail_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise OSError("simulated private config write setup failure")

    monkeypatch.setattr(terminal.os, "fdopen", fail_fdopen)
    socket_path = tmp_path / "runtime" / "station.sock"
    with pytest.raises(OSError, match="simulated"):
        TmuxTerminalBackend().start(
            provider_argv=[sys.executable, "-c", "pass"],
            bridge_argv=["/bin/cat"],
            cwd=tmp_path,
            socket_path=socket_path,
            session_name="write-error",
        )
    assert not socket_path.with_name(socket_path.name + ".conf").exists()
    monkeypatch.setattr(terminal.os, "fdopen", real_fdopen)


def test_start_rejects_existing_config_and_missing_working_directory(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "station.sock"
    common = {
        "provider_argv": [sys.executable, "-c", "pass"],
        "bridge_argv": ["/bin/cat"],
        "cwd": tmp_path,
        "socket_path": socket_path,
        "session_name": "config-collision",
    }
    config = socket_path.with_name(socket_path.name + ".conf")
    config.write_text("owned?", encoding="utf-8")
    config.chmod(0o600)
    with pytest.raises(FileExistsError, match="configuration already exists"):
        TmuxTerminalBackend().start(**common)
    config.unlink()
    with pytest.raises(ValueError, match="working directory does not exist"):
        TmuxTerminalBackend().start(**{**common, "cwd": tmp_path / "missing"})


def test_open_existing_rejects_station_with_wrong_pane_shape(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=runtime / "station.sock",
        session_name="shape",
    )
    try:
        subprocess.run(["tmux", "-S", str(handle.socket_path), "split-window", "-d", "-t", handle.session_name], check=True, timeout=5)
        with pytest.raises(RuntimeError, match="exactly one provider pane"):
            backend.open_existing(handle.socket_path, handle.session_name)
        with pytest.raises(ValueError, match="bridge_rows"):
            backend.open_existing(handle.socket_path, handle.session_name, bridge_rows=9)
    finally:
        backend.stop(handle)


def test_existing_dead_socket_collision_is_not_replaced(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "stale.sock"
    stale = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    try:
        stale.bind(str(socket_path))
    finally:
        stale.close()
    socket_path.chmod(0o600)
    with pytest.raises(FileExistsError, match="socket already exists"):
        TmuxTerminalBackend().start(
            provider_argv=[sys.executable, "-c", "pass"],
            bridge_argv=["/bin/cat"],
            cwd=tmp_path,
            socket_path=socket_path,
            session_name="collision",
        )
    assert socket_path.exists()


def test_absent_unknown_and_malformed_metadata_states_are_distinct(tmp_path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    absent = TerminalHandle(runtime / "absent.sock", runtime / "absent.sock.conf", "absent", "%0", "%1", 6)
    assert not backend.server_exists(absent)
    backend.stop(absent)  # no metadata is a benign already-dead server

    non_socket = runtime / "regular.file"
    non_socket.write_text("not a socket", encoding="utf-8")
    assert backend._socket_listener_state(non_socket) == backend._SERVER_UNKNOWN
    assert not backend._has_bridge_config_marker(runtime / "no-conf-extension")

    monkeypatch.setattr(backend, "_probe_server", lambda _socket: backend._SERVER_UNKNOWN)
    with pytest.raises(RuntimeError, match="cannot determine tmux server ownership"):
        backend.server_owned(runtime / "unknown.sock")


def test_start_failure_after_server_creation_tears_down_only_the_owned_station(tmp_path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket = runtime / "broken-start.sock"
    real_run = backend._run
    failed = False

    def fail_once(socket_path, *args, **kwargs):
        nonlocal failed
        if args and args[0] == "bind-key" and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, ["tmux", *args])
        return real_run(socket_path, *args, **kwargs)

    monkeypatch.setattr(backend, "_run", fail_once)
    with pytest.raises(subprocess.CalledProcessError):
        backend.start(
            provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
            bridge_argv=["/bin/sh", "-c", "exec cat"],
            cwd=tmp_path,
            socket_path=socket,
            session_name="broken-start",
        )
    assert failed
    assert not socket.exists() and not socket.with_name(socket.name + ".conf").exists()


@pytest.mark.parametrize("post_kill_state, message", [("owned", "remains alive"), ("foreign", "replaced"), ("unknown", "unverified")])
def test_stop_preserves_artifacts_for_unconfirmed_post_kill_state(tmp_path, monkeypatch, post_kill_state, message) -> None:
    backend = TmuxTerminalBackend()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=runtime / "unconfirmed.sock",
        session_name="unconfirmed",
    )
    real_run = backend._run
    real_probe = backend._probe_server
    probed_after_kill = False

    def no_op_kill(socket_path, *args, **kwargs):
        if args and args[0] == "kill-server":
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        return real_run(socket_path, *args, **kwargs)

    def post_kill_probe(socket_path):
        nonlocal probed_after_kill
        if probed_after_kill:
            return post_kill_state
        probed_after_kill = True
        return TmuxTerminalBackend._SERVER_OWNED

    monkeypatch.setattr(backend, "_run", no_op_kill)
    monkeypatch.setattr(backend, "_probe_server", post_kill_probe)
    monkeypatch.setattr(backend, "_TEARDOWN_CONFIRM_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("llm_tools.terminal.time.sleep", lambda _delay: None)
    with pytest.raises(RuntimeError, match=message):
        backend.stop(handle)
    assert handle.socket_path.exists() and handle.config_path.exists()
    monkeypatch.setattr(backend, "_probe_server", real_probe)
    monkeypatch.setattr(backend, "_run", real_run)
    backend.stop(handle)


def test_capture_and_send_keys_reject_unknown_pane_but_literal_text_is_safe(tmp_path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    handle = backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=runtime / "station.sock",
        session_name="io",
    )
    try:
        with pytest.raises(ValueError, match="pane must"):
            backend.capture(handle, pane="other")
        with pytest.raises(ValueError, match="pane must"):
            backend.send_keys(handle, pane="other", keys="payload")
        backend.send_keys(handle, pane="bridge", keys="; kill-server")
        assert backend.capture(handle, pane="bridge") is not None
    finally:
        backend.stop(handle)
