"""Dead station artifacts: a socket with no listener is cleaned, a live one never is."""

from __future__ import annotations

import socket as unix_socket
import subprocess
import sys
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.terminal import DeadStationError, TerminalHandle, TmuxTerminalBackend
from tests.conftest import scaled

MARKER = "set-option -g @llm_bridge_station 1\n"


def _private_dir(tmp_path: Path, name: str = "run") -> Path:
    directory = tmp_path / name
    directory.mkdir(mode=0o700)
    return directory


def _dead_socket(path: Path) -> Path:
    """A socket file nothing listens on, as tmux leaves behind after kill-server."""
    sock = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    path.chmod(0o600)
    return path


def _conf(socket_path: Path, text: str) -> Path:
    conf = socket_path.with_name(socket_path.name + ".conf")
    conf.write_text(text, encoding="utf-8")
    conf.chmod(0o600)
    return conf


def _start_args(tmp_path: Path, socket_path: Path, name: str) -> dict:
    return {
        "provider_argv": [sys.executable, "-c", "import time; time.sleep(30)"],
        "bridge_argv": ["/bin/sh", "-c", "exec cat"],
        "cwd": tmp_path,
        "socket_path": socket_path,
        "session_name": name,
    }


def test_start_replaces_dead_socket_without_config(tmp_path: Path) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    backend = TmuxTerminalBackend()
    handle = backend.start(**_start_args(tmp_path, socket_path, "after-dead"))
    try:
        assert backend.is_alive(handle)
        assert handle.config_path.exists()
    finally:
        backend.stop(handle)


def test_start_replaces_dead_socket_with_bridge_config(tmp_path: Path) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    _conf(socket_path, "set-option -g mouse on\n" + MARKER)
    backend = TmuxTerminalBackend()
    handle = backend.start(**_start_args(tmp_path, socket_path, "after-dead-conf"))
    try:
        assert backend.is_alive(handle)
    finally:
        backend.stop(handle)


def test_start_refuses_dead_socket_whose_config_has_no_bridge_marker(tmp_path: Path) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    conf = _conf(socket_path, "not ours\n")
    with pytest.raises(FileExistsError):
        TmuxTerminalBackend().start(**_start_args(tmp_path, socket_path, "unprovable"))
    assert socket_path.exists() and conf.exists()


def test_start_refuses_a_live_foreign_server(tmp_path: Path) -> None:
    socket_path = _private_dir(tmp_path) / "foreign.sock"
    subprocess.run(["tmux", "-S", str(socket_path), "-f", "/dev/null", "new-session", "-d", "-s", "foreign", "sleep 30"], check=True)
    try:
        with pytest.raises(FileExistsError, match="socket already exists"):
            TmuxTerminalBackend().start(**_start_args(tmp_path, socket_path, "collision"))
        shown = subprocess.run(["tmux", "-S", str(socket_path), "list-sessions"], capture_output=True, text=True)
        assert "foreign" in shown.stdout, "the foreign server must keep running"
    finally:
        subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], check=False)


def test_reclaim_never_removes_a_socket_that_accepts_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The listener never answers tmux, so the probe waits out the whole command timeout.
    monkeypatch.setattr(TmuxTerminalBackend, "_COMMAND_TIMEOUT_SECONDS", scaled(1))
    socket_path = _private_dir(tmp_path) / "live.sock"
    listener = unix_socket.socket(unix_socket.AF_UNIX, unix_socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    socket_path.chmod(0o600)
    try:
        assert TmuxTerminalBackend().reclaim_dead_station(socket_path) is False
        assert socket_path.exists()
    finally:
        listener.close()


def test_reclaim_refuses_socket_outside_a_private_directory(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    socket_path = _dead_socket(shared / "station.sock")
    with pytest.raises(PermissionError, match="owned by this user and private"):
        TmuxTerminalBackend().reclaim_dead_station(socket_path)
    assert socket_path.exists()
    # start() reports the same refusal rather than deleting anything.
    with pytest.raises(PermissionError):
        TmuxTerminalBackend().start(**_start_args(tmp_path, socket_path, "shared"))
    assert socket_path.exists()


def test_reclaim_refuses_a_non_socket_and_a_loose_socket(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    regular = directory / "regular.sock"
    regular.write_text("data", encoding="utf-8")
    with pytest.raises(PermissionError, match="not a private socket"):
        TmuxTerminalBackend().reclaim_dead_station(regular)
    assert regular.exists()
    loose = _dead_socket(directory / "loose.sock")
    loose.chmod(0o666)
    with pytest.raises(PermissionError, match="not a private socket"):
        TmuxTerminalBackend().reclaim_dead_station(loose)
    assert loose.exists()


def test_reclaim_leaves_unprovable_config_and_reports_nothing_when_clean(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    backend = TmuxTerminalBackend()
    assert backend.reclaim_dead_station(directory / "none.sock") is False
    socket_path = _dead_socket(directory / "station.sock")
    conf = _conf(socket_path, "not ours\n")
    with pytest.raises(PermissionError, match="cannot prove ownership"):
        backend.reclaim_dead_station(socket_path)
    assert socket_path.exists() and conf.exists()


def test_reclaim_removes_orphan_marker_config_without_socket(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    conf = _conf(directory / "station.sock", MARKER)
    assert TmuxTerminalBackend().reclaim_dead_station(directory / "station.sock") is True
    assert not conf.exists()


def test_reclaim_skips_a_path_that_gains_a_listener_after_the_first_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    backend = TmuxTerminalBackend()
    monkeypatch.setattr(backend, "_probe_server", lambda path: backend._SERVER_ABSENT)
    monkeypatch.setattr(backend, "_socket_listener_state", lambda path: "reachable")
    assert backend.reclaim_dead_station(socket_path) is False
    assert socket_path.exists()


def test_open_existing_removes_dead_socket_and_reports_no_station(tmp_path: Path) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    _conf(socket_path, MARKER)
    with pytest.raises(DeadStationError, match="no live terminal station"):
        TmuxTerminalBackend().open_existing(socket_path, "mission-x")
    assert not socket_path.exists()
    assert not socket_path.with_name("station.sock.conf").exists()


def test_open_existing_still_refuses_a_foreign_server_and_unknown_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    backend = TmuxTerminalBackend()
    monkeypatch.setattr(backend, "_probe_server", lambda path: backend._SERVER_FOREIGN)
    with pytest.raises(PermissionError, match="not an LLM Bridge station"):
        backend.open_existing(socket_path, "mission-x")
    monkeypatch.setattr(backend, "_probe_server", lambda path: backend._SERVER_UNKNOWN)
    with pytest.raises(RuntimeError, match="cannot determine tmux server ownership"):
        backend.open_existing(socket_path, "mission-x")
    assert socket_path.exists()


def test_controller_open_existing_treats_dead_socket_as_no_station(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    monkeypatch.setattr(bridge_runtime, "mission_socket_path", lambda mission_id: socket_path)
    assert bridge_runtime._open_existing(TmuxTerminalBackend(), tmp_path, "mission-x") is None
    assert not socket_path.exists()


def test_guarded_attach_does_not_recreate_the_socket_of_a_stopped_station(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    backend = TmuxTerminalBackend()
    handle = backend.start(**_start_args(tmp_path, directory / "station.sock", "guarded"))
    backend.stop(handle)
    assert not handle.socket_path.exists()

    guarded = subprocess.run(["sh", "-c", backend._attach_if_present(handle)], capture_output=True, timeout=scaled(10))
    assert guarded.returncode != 0
    assert not handle.socket_path.exists()

    # Control: the raw command leaves the dead socket that broke restarts.
    subprocess.run(
        ["tmux", "-S", str(handle.socket_path), "attach-session", f"-t={handle.session_name}"],
        capture_output=True,
        timeout=scaled(10),
    )
    assert handle.socket_path.exists()
    assert backend.reclaim_dead_station(handle.socket_path) is True
    assert not handle.socket_path.exists()


def test_attach_removes_the_socket_a_failed_attach_leaves_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The station dies between the ownership check and attach-session."""
    directory = _private_dir(tmp_path)
    socket_path = directory / "station.sock"
    handle = TerminalHandle(socket_path, socket_path.with_name("station.sock.conf"), "gone", "%0", "%1", 6)
    backend = TmuxTerminalBackend()
    monkeypatch.setattr(backend, "_require_owned", lambda h: None)
    real_run = subprocess.run

    def attach(argv, **kwargs):
        if "attach-session" in argv:
            kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=scaled(10))
        return real_run(argv, **kwargs)

    monkeypatch.setattr("llm_tools.terminal.subprocess.run", attach)
    assert backend.attach(handle) != 0
    assert not socket_path.exists()


# stop(), an attach client that lost its server, and reclaim_dead_station() can
# all clean the same dead station at once. Each test below removes the files, as
# a concurrent cleaner would, at one specific point inside the shared removal helper. The tests
# differ only in that point, so each one is deterministic.
def _dead_station(tmp_path: Path) -> tuple[TmuxTerminalBackend, TerminalHandle]:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    conf = _conf(socket_path, MARKER)
    return TmuxTerminalBackend(), TerminalHandle(socket_path, conf, "s", "%0", "%1", 6)


def _remove_both(handle: TerminalHandle) -> None:
    handle.socket_path.unlink(missing_ok=True)
    handle.config_path.unlink(missing_ok=True)


def test_removal_survives_artifacts_removed_before_the_socket_is_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, handle = _dead_station(tmp_path)
    original = TmuxTerminalBackend._validate_socket_path

    def validate(path: Path, *, allow_missing: bool = False) -> None:
        _remove_both(handle)
        original(path, allow_missing=allow_missing)

    monkeypatch.setattr(TmuxTerminalBackend, "_validate_socket_path", staticmethod(validate))
    backend._remove_dead_server_artifacts(handle)
    assert not handle.socket_path.exists() and not handle.config_path.exists()


def test_removal_survives_socket_removed_between_validation_and_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, handle = _dead_station(tmp_path)
    original = TmuxTerminalBackend._validate_socket_path

    def validate(path: Path, *, allow_missing: bool = False) -> None:
        original(path, allow_missing=allow_missing)
        _remove_both(handle)

    monkeypatch.setattr(TmuxTerminalBackend, "_validate_socket_path", staticmethod(validate))
    backend._remove_dead_server_artifacts(handle)
    assert not handle.socket_path.exists() and not handle.config_path.exists()


def test_removal_survives_config_removed_while_its_marker_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, handle = _dead_station(tmp_path)

    def marker(config_path: Path) -> bool:
        handle.config_path.unlink()
        return False

    monkeypatch.setattr(TmuxTerminalBackend, "_has_bridge_config_marker", classmethod(lambda cls, path: marker(path)))
    backend._remove_dead_server_artifacts(handle)
    assert not handle.socket_path.exists() and not handle.config_path.exists()


def test_removal_survives_config_removed_before_its_ownership_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend, handle = _dead_station(tmp_path)
    original = TmuxTerminalBackend._validate_config_path

    def validate(socket_path: Path, config_path: Path) -> None:
        handle.config_path.unlink()
        original(socket_path, config_path)

    monkeypatch.setattr(TmuxTerminalBackend, "_validate_config_path", staticmethod(validate))
    backend._remove_dead_server_artifacts(handle)
    assert not handle.socket_path.exists() and not handle.config_path.exists()


def test_stop_still_refuses_a_config_that_is_present_but_unprovable(tmp_path: Path) -> None:
    socket_path = _dead_socket(_private_dir(tmp_path) / "station.sock")
    conf = _conf(socket_path, "not ours\n")
    handle = TerminalHandle(socket_path, conf, "s", "%0", "%1", 6)
    with pytest.raises(PermissionError, match="cannot prove ownership"):
        TmuxTerminalBackend().stop(handle)
    assert socket_path.exists() and conf.exists()
