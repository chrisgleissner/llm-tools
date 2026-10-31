"""When every shared /tmp socket name is taken, Bridge uses a private cache directory instead of failing."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from llm_tools import bridge_runtime
from llm_tools.bridge_runtime import mission_socket_path
from tests.test_bridge_security_socket_dir import MISSION, _name, preferred


@pytest.fixture()
def tmp_root(monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(tempfile.mkdtemp(prefix="lbx", dir="/tmp"))
    monkeypatch.setattr(bridge_runtime, "_TMP_ROOT", root)
    monkeypatch.setattr(bridge_runtime, "_RUN_USER_ROOT", root / "no-run-user")
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _block_every_tmp_name(tmp_root: Path) -> Path:
    """Every numbered name Bridge would use in /tmp is a symlink planted by someone else."""
    target = tmp_root / "attacker-controlled"
    target.mkdir(mode=0o700)
    for name in [preferred(), *(f"{preferred()}-{index}" for index in range(1, 5))]:
        (tmp_root / name).symlink_to(target)
    return target


def test_exhausted_tmp_names_fall_back_to_a_private_cache_directory(tmp_root: Path) -> None:
    target = _block_every_tmp_name(tmp_root)
    cache = tmp_root / "cache"
    env = {"XDG_CACHE_HOME": str(cache)}
    path = mission_socket_path(MISSION, env=env)
    assert path == cache / "llm-tools" / "runtime" / _name()
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert mission_socket_path(MISSION, env=env) == path, "the fallback is stable across calls"
    assert not any(target.iterdir()), "nothing may be created through the planted symlinks"


def test_cache_fallback_defaults_to_the_home_cache(tmp_root: Path) -> None:
    _block_every_tmp_name(tmp_root)
    home = tmp_root / "h"
    home.mkdir()
    assert mission_socket_path(MISSION, env={"HOME": str(home)}).parent == home / ".cache" / "llm-tools" / "runtime"


def test_cache_fallback_must_be_short_and_private(tmp_root: Path) -> None:
    _block_every_tmp_name(tmp_root)
    with pytest.raises(OSError, match="short private runtime directory"):
        mission_socket_path(MISSION, env={"XDG_CACHE_HOME": str(tmp_root / ("x" * 90))})
    loose = tmp_root / "loose" / "llm-tools" / "runtime"
    loose.mkdir(parents=True, mode=0o755)
    loose.chmod(0o755)
    with pytest.raises(OSError, match="short private runtime directory"):
        mission_socket_path(MISSION, env={"XDG_CACHE_HOME": str(tmp_root / "loose")})
    blocked = tmp_root / "file"
    blocked.write_text("not a directory")
    with pytest.raises(OSError, match="short private runtime directory"):
        mission_socket_path(MISSION, env={"XDG_CACHE_HOME": str(blocked)})
