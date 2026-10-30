"""The usage-service socket directory and peer must belong to the current user."""
from __future__ import annotations

import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from llm_tools import usage_service


@pytest.fixture()
def fallback_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], Path]:
    """No XDG_RUNTIME_DIR: the directory is ``<tmp>/llm-tools-<uid>``."""
    root = tmp_path / "tmp"
    root.mkdir()
    monkeypatch.setattr(usage_service.tempfile, "gettempdir", lambda: str(root))
    env = {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    return env, root / f"llm-tools-{os.getuid()}"


def test_private_fallback_directory_is_created_and_trusted(fallback_env: tuple[dict[str, str], Path]) -> None:
    env, directory = fallback_env
    sock = usage_service.trusted_socket_path(env)
    assert sock == directory / "llm-usage.sock"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_loose_mode_on_our_own_directory_is_tightened(fallback_env: tuple[dict[str, str], Path]) -> None:
    env, directory = fallback_env
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    assert usage_service.trusted_socket_path(env) is not None
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_directory_owned_by_someone_else_is_refused(fallback_env: tuple[dict[str, str], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    env, directory = fallback_env
    directory.mkdir(mode=0o700)  # pre-created by "another user": our uid differs from the owner
    real = os.getuid()
    monkeypatch.setattr(usage_service.os, "getuid", lambda: real + 1)
    assert usage_service.trusted_socket_path(env) is None
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700  # and nothing was repaired


def test_symlinked_directory_is_refused(fallback_env: tuple[dict[str, str], Path], tmp_path: Path) -> None:
    env, directory = fallback_env
    target = tmp_path / "attacker"
    target.mkdir(mode=0o700)
    directory.symlink_to(target)
    assert usage_service.trusted_socket_path(env) is None


def test_untrusted_directory_means_direct_reads_and_no_service(fallback_env: tuple[dict[str, str], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env, directory = fallback_env
    target = tmp_path / "attacker"
    target.mkdir(mode=0o700)
    directory.symlink_to(target)
    # A hostile server already listening behind the planted path must not be queried.
    hostile = target / "llm-usage.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(hostile))
    server.listen(1)
    try:
        monkeypatch.setattr(usage_service, "start_ephemeral_service", lambda *a, **k: pytest.fail("must not start a service"))
        assert usage_service.request_snapshot(env=env) is None
        assert usage_service.running_status(env)["running"] is False
        assert usage_service.run_service(interval=60, env=env) == 1
        assert "untrusted socket directory" in capsys.readouterr().err
    finally:
        server.close()


def test_server_socket_is_owner_only_even_with_a_permissive_umask(tmp_path: Path) -> None:
    class Sampler:
        def snapshot(self, max_age=None):  # type: ignore[no-untyped-def]
            return {}

    sock = tmp_path / "svc.sock"
    old = os.umask(0)
    try:
        server = usage_service.ThreadingUnixServer(str(sock), Sampler(), threading.Event())  # type: ignore[arg-type]
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    finally:
        server.server_close()


def test_peer_uid_is_ours_for_a_local_connection(tmp_path: Path) -> None:
    a, b = socket.socketpair()
    try:
        if not hasattr(socket, "SO_PEERCRED"):
            assert usage_service.peer_uid(a) is None
        else:
            assert usage_service.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


def test_peer_uid_is_skipped_where_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = socket.socketpair()
    try:
        monkeypatch.delattr(usage_service.socket, "SO_PEERCRED", raising=False)
        assert usage_service.peer_uid(a) is None
        assert usage_service._peer_is_us(a) is True
    finally:
        a.close()
        b.close()


def test_snapshot_from_a_peer_with_another_uid_is_ignored_by_client_and_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Sampler:
        def snapshot(self, max_age=None):  # type: ignore[no-untyped-def]
            return {"generated_at_epoch": 1, "spoofed": True}

    sock = tmp_path / "svc.sock"
    server = usage_service.ThreadingUnixServer(str(sock), Sampler(), threading.Event())  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert usage_service._request(sock, {"op": "snapshot"}) is not None
        monkeypatch.setattr(usage_service, "peer_uid", lambda _sock: os.getuid() + 1)
        assert usage_service._request(sock, {"op": "snapshot"}, timeout=1.0) is None  # client rejects the server
        # And the server refuses a client that is not us: it reads nothing and answers nothing.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2.0)
            client.connect(str(sock))
            client.sendall(b'{"op":"snapshot"}\n')
            try:
                assert client.recv(1024) == b""
            except ConnectionResetError:
                pass  # the server closed with our request unread; also "no answer"
    finally:
        server.shutdown()
        server.server_close()
