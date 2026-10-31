"""The shared /tmp socket fallback must not trust a directory another user could have planted."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_runtime import mission_socket_path
from tests.test_bridge_socket_discovery import MISSION, _name


@pytest.fixture()
def tmp_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(tempfile.mkdtemp(prefix="lbs", dir="/tmp"))
    monkeypatch.setattr(bridge_runtime, "_TMP_ROOT", root)
    monkeypatch.setattr(bridge_runtime, "_RUN_USER_ROOT", root / "no-run-user")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def preferred() -> str:
    return f"lb-{os.getuid()}"


def test_private_directory_is_created_and_reused(tmp_root: Path) -> None:
    path = mission_socket_path(MISSION, env={})
    assert path == tmp_root / preferred() / _name()
    assert (tmp_root / preferred()).stat().st_mode & 0o777 == 0o700
    assert mission_socket_path(MISSION, env={}) == path


def test_symlink_planted_at_the_preferred_name_is_not_followed(tmp_root: Path) -> None:
    target = tmp_root / "attacker-controlled"
    target.mkdir(mode=0o700)
    (tmp_root / preferred()).symlink_to(target)
    path = mission_socket_path(MISSION, env={})
    assert path.parent == tmp_root / f"{preferred()}-1"
    assert not any(target.iterdir()), "nothing may be created through the symlink"


def test_directory_with_loose_permissions_is_skipped_not_fatal(tmp_root: Path) -> None:
    loose = tmp_root / preferred()
    loose.mkdir(mode=0o755)
    loose.chmod(0o755)
    path = mission_socket_path(MISSION, env={})
    assert path.parent == tmp_root / f"{preferred()}-1"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert mission_socket_path(MISSION, env={}) == path, "the fallback is stable across calls"


def test_directory_owned_by_someone_else_is_skipped(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_root / preferred()).mkdir(mode=0o700)
    # Pretend the preferred directory belongs to another user by treating our uid as different there only.
    original = bridge_runtime._private_owned_directory

    def owned(directory: Path, uid: int) -> bool:
        if directory.name == preferred():
            return original(directory, uid + 1)
        return original(directory, uid)

    monkeypatch.setattr(bridge_runtime, "_private_owned_directory", owned)
    path = mission_socket_path(MISSION, env={})
    assert path.parent.name == f"{preferred()}-1"


def test_existing_station_socket_inside_a_symlinked_directory_is_not_trusted(tmp_root: Path) -> None:
    target = tmp_root / "elsewhere"
    target.mkdir(mode=0o700)
    (target / _name()).touch()
    (tmp_root / preferred()).symlink_to(target)
    path = mission_socket_path(MISSION, env={})
    assert path.parent != tmp_root / preferred()
