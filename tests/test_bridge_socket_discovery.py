"""A station socket is found from any login environment."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_runtime import mission_socket_path

MISSION = "c" * 32


def _name() -> str:
    return f"m-{hashlib.sha256(MISSION.encode()).hexdigest()[:20]}.sock"


@pytest.fixture()
def short_tmp() -> Path:
    # Station sockets must stay below the sockaddr_un limit, so pytest's long
    # tmp_path cannot hold them.
    root = Path(tempfile.mkdtemp(prefix="lbt", dir="/tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def run_user(short_tmp, monkeypatch) -> Path:
    root = short_tmp / "ru"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(bridge_runtime, "_RUN_USER_ROOT", root)
    return root


def _station_socket(directory: Path) -> Path:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    socket = directory / _name()
    socket.touch()
    return socket


def test_existing_socket_in_run_user_dir_is_found_without_xdg_runtime_dir(run_user) -> None:
    existing = _station_socket(run_user / str(os.getuid()) / "llm-bridge")
    assert mission_socket_path(MISSION, env={}) == existing


def test_existing_socket_in_other_xdg_dir_is_preferred_over_creation_dir(short_tmp, run_user) -> None:
    existing = _station_socket(run_user / str(os.getuid()) / "llm-bridge")
    other = short_tmp / "other-xdg"
    other.mkdir(mode=0o700)
    found = mission_socket_path(MISSION, env={"XDG_RUNTIME_DIR": str(other)})
    assert found == existing
    # The creation directory was not made as a side effect of the lookup.
    assert not (other / "llm-bridge").exists()


def test_xdg_socket_wins_when_both_exist(short_tmp, run_user) -> None:
    _station_socket(run_user / str(os.getuid()) / "llm-bridge")
    xdg = short_tmp / "xdg"
    expected = _station_socket(xdg / "llm-bridge")
    assert mission_socket_path(MISSION, env={"XDG_RUNTIME_DIR": str(xdg)}) == expected


def test_socket_in_nonprivate_directory_is_ignored(short_tmp, run_user) -> None:
    bad = run_user / str(os.getuid()) / "llm-bridge"
    _station_socket(bad)
    bad.chmod(0o755)
    xdg = short_tmp / "xdg"
    xdg.mkdir(mode=0o700)
    path = mission_socket_path(MISSION, env={"XDG_RUNTIME_DIR": str(xdg)})
    assert path.parent == xdg / "llm-bridge"


def test_without_existing_socket_creation_directory_is_unchanged(short_tmp, run_user) -> None:
    xdg = short_tmp / "xdg"
    xdg.mkdir(mode=0o700)
    path = mission_socket_path(MISSION, env={"XDG_RUNTIME_DIR": str(xdg)})
    assert path == xdg / "llm-bridge" / _name()
    assert path.parent.stat().st_mode & 0o077 == 0
    # /run/user is never created by Bridge.
    assert not (run_user / str(os.getuid())).exists()
