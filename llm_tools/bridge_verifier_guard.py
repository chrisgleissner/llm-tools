"""Detect edits to a mission's verifier since the mission was created.

A configured verifier is code inside the mission folder that the provider can
also edit. A provider that rewrites the verifier to ``exit 0`` would otherwise
complete its own task. Bridge records a SHA-256 of the verifier executable and
of the script it interprets when the mission is created, and a later passing
run no longer completes the task by itself when one of them has changed: the
operator has to approve it, with the changed paths recorded as the reason.

Only files inside the mission folder are tracked. A system interpreter such as
``/usr/bin/python3`` is not in the provider's working tree and is not hashed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "dash", "node", "ruby", "perl", "php", "env"})
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MISSING = "missing"


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _digest(path: Path) -> str:
    """Content hash of a path; a symlink also records its target so repointing it is a change."""
    try:
        info = path.stat()
    except OSError:
        return _MISSING
    if not path.is_file() or info.st_size > _MAX_FILE_BYTES:
        return _MISSING
    sha = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                sha.update(chunk)
    except OSError:
        return _MISSING
    value = sha.hexdigest()
    return f"{value}@{os.readlink(path)}" if path.is_symlink() else value


def _candidate_paths(argv: list[str], cwd: Path) -> list[Path]:
    """The verifier executable and, for an interpreter, the script it runs (inside ``cwd`` only)."""
    if not argv:
        return []
    first = argv[0]
    found: list[Path] = []
    located = (cwd / first) if "/" in first else None
    if located is None:
        which = shutil.which(first)
        located = Path(which) if which else None
    if located is not None:
        found.append(Path(os.path.normpath(located if located.is_absolute() else cwd / located)))
    if Path(first).name.rstrip("0123456789.") in _INTERPRETERS:
        script = next((arg for arg in argv[1:] if not arg.startswith("-") and not arg.startswith("{")), None)
        if script and (cwd / script).is_file():
            found.append(Path(os.path.normpath(cwd / script)))
    return found


def verifier_fingerprints(argv: list[str], cwd: str | Path, extra_files: list[str] | None = None) -> dict[str, str]:
    """``{path relative to cwd: digest}`` for the tracked verifier files inside the mission folder."""
    root = Path(os.path.abspath(cwd))
    paths = _candidate_paths(argv, root)
    for item in extra_files or []:
        paths.append(Path(os.path.normpath(root / item)))
    result: dict[str, str] = {}
    for path in paths:
        if _inside(path, root):
            result[str(path.relative_to(root))] = _digest(path)
    return result


def changed_verifier_files(verifier: dict[str, Any], cwd: str | Path) -> list[str]:
    """Tracked verifier files whose current digest differs from the one recorded at mission creation."""
    recorded = verifier.get("fingerprints")
    if not isinstance(recorded, dict):
        return []
    root = Path(os.path.abspath(cwd))
    changed: list[str] = []
    for relative, digest in sorted(recorded.items()):
        path = Path(os.path.normpath(root / str(relative)))
        if not _inside(path, root) or _digest(path) != digest:
            changed.append(str(relative))
    return changed
