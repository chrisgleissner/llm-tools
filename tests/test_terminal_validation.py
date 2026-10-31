"""Ownership and privacy checks that guard the private tmux socket and its configuration."""

from __future__ import annotations

import socket as socket_module
from pathlib import Path

import pytest

from llm_tools.terminal import TmuxTerminalBackend


def _private_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory


def test_socket_validation_rejects_missing_directory_and_non_socket_paths(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="unavailable"):
        TmuxTerminalBackend._validate_socket_path(tmp_path / "absent" / "tmux.sock")
    directory = _private_dir(tmp_path)
    regular = directory / "tmux.sock"
    regular.write_text("not a socket", encoding="utf-8")
    with pytest.raises(PermissionError, match="not a private socket"):
        TmuxTerminalBackend._validate_socket_path(regular)
    with pytest.raises(FileNotFoundError):
        TmuxTerminalBackend._validate_socket_path(directory / "missing.sock")
    TmuxTerminalBackend._validate_socket_path(directory / "missing.sock", allow_missing=True)


def test_config_validation_requires_private_sibling_file(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    sock = directory / "tmux.sock"
    config = directory / "tmux.sock.conf"
    with pytest.raises(PermissionError, match="private sibling"):
        TmuxTerminalBackend._validate_config_path(sock, directory / "other.conf")
    with pytest.raises(PermissionError, match="unavailable"):
        TmuxTerminalBackend._validate_config_path(sock, config)
    config.write_text("", encoding="utf-8")
    config.chmod(0o644)
    with pytest.raises(PermissionError, match="private file"):
        TmuxTerminalBackend._validate_config_path(sock, config)
    config.chmod(0o600)
    TmuxTerminalBackend._validate_config_path(sock, config)


def test_unlink_helpers_only_remove_owned_private_artifacts(tmp_path: Path) -> None:
    directory = _private_dir(tmp_path)
    sock = directory / "s.sock"
    config = directory / "s.sock.conf"

    # A regular file at the socket path is never removed.
    sock.write_text("x", encoding="utf-8")
    TmuxTerminalBackend._unlink_owned_socket(sock)
    assert sock.exists()
    sock.unlink()

    # A real socket created by this user is removed.
    listener = socket_module.socket(socket_module.AF_UNIX)
    try:
        listener.bind(str(sock))
        sock.chmod(0o600)
        TmuxTerminalBackend._unlink_owned_socket(sock)
        assert not sock.exists()
    finally:
        listener.close()

    # A configuration that is not the socket's private sibling is left alone.
    other = directory / "other.conf"
    other.write_text("", encoding="utf-8")
    other.chmod(0o600)
    TmuxTerminalBackend._unlink_owned_config(sock, other)
    assert other.exists()

    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    TmuxTerminalBackend._unlink_owned_config(sock, config)
    assert not config.exists()
