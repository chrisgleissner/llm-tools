"""Where a mission's tmux station socket lives.

``mission_socket_path`` picks a short, private socket path for a mission and finds an existing one
in any candidate directory. This module uses only the standard library so that read-only commands
(``mission list`` asks whether each station is listening) can resolve the path without loading
``bridge_runtime``, ``common`` or the configuration.

``bridge_runtime`` re-exports the names below. Tests that move the ``/tmp`` or ``/run/user`` root
patch them here, because the functions look the names up in this module.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

_RUN_USER_ROOT = Path("/run/user")
_TMP_ROOT = Path("/tmp")


def _socket_candidate_dirs(uid: int, env: dict[str, str] | None) -> tuple[list[Path], list[Path]]:
    """Return (lookup, creation) socket directories, most specific first."""
    source = os.environ if env is None else env
    xdg_runtime = source.get("XDG_RUNTIME_DIR", "")
    primary = [Path(xdg_runtime) / "llm-bridge"] if xdg_runtime else []
    tmp = _tmp_fallback_dirs(uid)
    last_resort = [_cache_runtime_dir(source)]
    creation = [*primary, *tmp, *last_resort]
    # A second login (sudo -iu, some SSH/cron setups) may lack or differ in
    # XDG_RUNTIME_DIR; the per-user runtime directory is still discoverable.
    lookup = [*primary, _RUN_USER_ROOT / str(uid) / "llm-bridge", *tmp, *last_resort]
    return list(dict.fromkeys(lookup)), creation


def _cache_runtime_dir(env: Mapping[str, str]) -> Path:
    """Private per-user directory used when every shared ``/tmp`` name is taken by someone else."""
    base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".cache")
    return Path(base) / "llm-tools" / "runtime"


def _tmp_fallback_dirs(uid: int) -> list[Path]:
    """Short ``/tmp`` socket directories, tried in order.

    ``/tmp`` is shared, so another user can pre-create ``/tmp/lb-<uid>`` (or
    replace it with a symlink). A directory that is not a real, private
    directory owned by this user is skipped and the next numbered name is used;
    the names are fixed so later commands find the same station.
    """
    return [_TMP_ROOT / f"lb-{uid}", *(_TMP_ROOT / f"lb-{uid}-{index}" for index in range(1, 5))]


def _trusted_tmp_directory(directory: Path, uid: int) -> bool:
    """Create ``directory`` (mode 0700) if absent; accept it only if it is ours and private."""
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError:
        return False
    return _private_owned_directory(directory, uid)


def _short_enough(socket: Path) -> bool:
    # Keep below the conservative 100-byte sockaddr_un limit, including the
    # sibling config filename used by the terminal backend.
    return len(os.fsencode(str(socket.with_name(socket.name + ".conf")))) < 96


def _private_owned_directory(directory: Path, uid: int) -> bool:
    try:
        info = directory.lstat()  # a symlink is never accepted, even to a directory we own
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not info.st_mode & 0o077


def mission_socket_path(mission_id: str, *, env: dict[str, str] | None = None) -> Path:
    """Return a short, private tmux socket path stable across controller restarts.

    Unix-domain sockets have a small platform-dependent path limit. Durable
    Bridge state can live below a long XDG state path, so tmux sockets live in
    a separate user-private runtime directory and use a hash of the mission id.

    An already existing socket for this mission is found in any candidate
    directory (``$XDG_RUNTIME_DIR/llm-bridge``, ``/run/user/<uid>/llm-bridge``,
    ``/tmp/lb-<uid>``) so a login with a different environment still reaches a
    running station. Only when none exists is a creation directory chosen.
    """
    uid = os.getuid()
    lookup, creation = _socket_candidate_dirs(uid, env)
    digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()[:20]
    for directory in lookup:
        socket = directory / f"m-{digest}.sock"
        if _short_enough(socket) and _private_owned_directory(directory, uid) and socket.exists():
            return socket
    for directory in creation:
        socket = directory / f"m-{digest}.sock"
        if not _short_enough(socket):
            continue
        if directory.parent == _TMP_ROOT:
            if _trusted_tmp_directory(directory, uid):
                return socket
            continue
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = directory.stat()
        except OSError:
            continue
        if info.st_uid != uid or info.st_mode & 0o077:
            continue
        return socket
    raise OSError("could not create a short private runtime directory for the tmux socket")
