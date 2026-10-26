"""Teardown must preserve ownership metadata until tmux is gone."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required for teardown tests")


def _start(tmp_path, backend: TmuxTerminalBackend) -> TerminalHandle:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return backend.start(
        provider_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        bridge_argv=["/bin/sh", "-c", "exec cat"],
        cwd=tmp_path,
        socket_path=runtime / "station.sock",
        session_name="station",
    )


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_stop_failure_preserves_owned_socket_and_config(tmp_path, monkeypatch, failure) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(tmp_path, backend)
    real_run = backend._run

    def fail_kill(socket_path, *args, **kwargs):
        if args and args[0] == "kill-server":
            if failure == "timeout":
                raise subprocess.TimeoutExpired(["tmux", "kill-server"], 10)
            return subprocess.CompletedProcess(["tmux", "kill-server"], 1, "", "kill failed")
        return real_run(socket_path, *args, **kwargs)

    monkeypatch.setattr(backend, "_run", fail_kill)
    with pytest.raises(RuntimeError, match="kill-server|unverified"):
        backend.stop(handle)

    assert backend.server_owned(handle.socket_path), "provider/UI server must still be live after rejected teardown"
    assert handle.socket_path.exists(), "socket is needed to retry or inspect the failed station"
    assert handle.config_path.exists(), "config identifies the owned station"

    monkeypatch.setattr(backend, "_run", real_run)
    backend.stop(handle)
    assert not backend.server_exists(handle)
    assert not handle.socket_path.exists()
    assert not handle.config_path.exists()


def test_stop_removes_metadata_for_an_already_dead_owned_server(tmp_path) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(tmp_path, backend)
    subprocess.run(["tmux", "-S", str(handle.socket_path), "kill-server"], check=True, timeout=5)

    assert not backend.server_exists(handle)
    # The generated private config is the remaining ownership record.
    assert handle.config_path.exists()
    backend.stop(handle)
    assert not handle.socket_path.exists()
    assert not handle.config_path.exists()


def test_stop_rejects_a_live_foreign_server_without_unlinking_it(tmp_path) -> None:
    runtime = tmp_path / "foreign"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "foreign.sock"
    subprocess.run(
        ["tmux", "-S", str(socket_path), "-f", "/dev/null", "new-session", "-d", "-s", "foreign"],
        check=True,
        timeout=5,
    )
    handle = TerminalHandle(socket_path, socket_path.with_name("foreign.sock.conf"), "foreign", "%0", "%1", 6)
    backend = TmuxTerminalBackend()
    try:
        with pytest.raises(PermissionError, match="non-Bridge"):
            backend.stop(handle)
        assert backend.server_exists(handle)
        assert socket_path.exists()
    finally:
        subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], check=False, timeout=5)


def test_server_exists_does_not_treat_an_unqueryable_live_socket_as_dead(tmp_path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(tmp_path, backend)
    real_run = backend._run

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tmux", "show-option"], 10)

    monkeypatch.setattr(backend, "_run", timeout)
    with pytest.raises(RuntimeError, match="cannot determine"):
        backend.server_exists(handle)

    monkeypatch.setattr(backend, "_run", real_run)
    backend.stop(handle)


def test_stop_preserves_metadata_when_live_server_cannot_be_queried(tmp_path, monkeypatch) -> None:
    backend = TmuxTerminalBackend()
    handle = _start(tmp_path, backend)
    real_run = backend._run

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tmux", "kill-server"], 10)

    monkeypatch.setattr(backend, "_run", timeout)
    with pytest.raises(RuntimeError, match="preserving metadata"):
        backend.stop(handle)
    assert handle.socket_path.exists()
    assert handle.config_path.exists()

    monkeypatch.setattr(backend, "_run", real_run)
    assert backend.server_owned(handle.socket_path)
    backend.stop(handle)
