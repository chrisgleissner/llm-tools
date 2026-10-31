"""Bounded activity evidence; repository changes are never completion evidence."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from .fileio import open_regular_file


def _git(cwd: Path, *args: str) -> bytes | None:
    import tempfile  # loaded only when a repository is actually read

    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(['git','-c','core.fsmonitor=false','-C',str(cwd),*args],
                stdout=output, stderr=subprocess.DEVNULL, timeout=2, check=False)
            output.seek(0)
            data = output.read(262145)
        return data if result.returncode == 0 and len(data) <= 262144 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _has_git_marker(root: Path) -> bool:
    current = root
    while True:
        try:
            marker = (current / '.git').lstat()
        except OSError:
            pass
        else:
            if stat.S_ISDIR(marker.st_mode) or stat.S_ISREG(marker.st_mode):
                return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def repository_checkpoint(cwd: str | Path, *, max_files: int = 512, max_bytes: int = 8*1024*1024) -> dict[str, Any]:
    """Hash a complete bounded tracked/untracked workspace, or report unknown.

    Ignored files are excluded by Git. Symlinks contribute their link text,
    never target contents. Metadata/content races invalidate the observation.
    """
    unknown = {'fingerprint':None,'files':0,'bytes':0,'reason':'incomplete repository observation'}
    try:
        root = Path(cwd).resolve(strict=True)
        if not os.environ.get('GIT_DIR') and not os.environ.get('GIT_WORK_TREE') and not _has_git_marker(root):
            return {'fingerprint':None,'files':0,'bytes':0,'reason':'not a Git worktree'}
        raw = _git(root,'ls-files','-z','--cached','--others','--exclude-standard')
        if raw is None:
            return unknown
        paths = sorted(set(item for item in raw.split(b'\0') if item))
        if len(paths) > max_files:
            return unknown
        import hashlib

        digest = hashlib.sha256(_git(root,'rev-parse','--verify','HEAD') or b'unborn')
        size = 0
        count = 0
        for encoded in paths:
            relative = Path(os.fsdecode(encoded))
            if relative.is_absolute() or '..' in relative.parts:
                return unknown
            path = root / relative
            if not path.parent.resolve(strict=True).is_relative_to(root):
                return unknown
            try:
                before = path.lstat()
            except FileNotFoundError:
                digest.update(b'deleted\0'+encoded+b'\0')
                continue
            digest.update(encoded+b'\0')
            digest.update(str(before.st_mode & 0o111).encode()+b'\0')
            if stat.S_ISLNK(before.st_mode):
                data = os.fsencode(os.readlink(path))
                size += len(data)
                digest.update(b'link\0'+data)
            elif stat.S_ISREG(before.st_mode):
                with open_regular_file(path, follow_symlinks=False) as stream:
                    while data := stream.read(min(65536,max(1,max_bytes-size+1))):
                        size += len(data)
                        if size > max_bytes:
                            return unknown
                        digest.update(data)
            else:
                return unknown
            if size > max_bytes:
                return unknown
            after = path.lstat()
            if (before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns):
                return unknown
            digest.update(b'\0')
            count += 1
        return {'fingerprint':digest.hexdigest(),'files':count,'bytes':size,'reason':None}
    except (OSError, ValueError):
        return unknown


def convergence_signals(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Recognize repeated boundary evidence, never an elapsed-time-only stall."""
    rows = observations[-32:]
    unchanged = 0
    failures = 0
    last_failure = rows[-1].get('output_sha256') if rows else None
    for row in reversed(rows):
        if row.get('before') and row.get('before') == row.get('after'):
            unchanged += 1
        else:
            break
    for row in reversed(rows):
        if last_failure and row.get('exit_code') not in (None,0) and row.get('output_sha256') == last_failure:
            failures += 1
        else:
            break
    cycle = unchanged >= 3 and len({row.get('station') for row in rows[-unchanged:] if row.get('station')}) >= 2
    fingerprints = [row.get('after') for row in rows[-4:]]
    reversal = len(fingerprints)==4 and all(fingerprints) and fingerprints[0]==fingerprints[2] and fingerprints[1]==fingerprints[3] and fingerprints[0]!=fingerprints[1]
    warnings = []
    if unchanged >= 3: warnings.append(f'{unchanged} operations without observed repository change')
    if failures >= 3: warnings.append(f'{failures} identical provider failures')
    if cycle: warnings.append('providers cycled without observed repository change')
    if reversal: warnings.append('repeated repository fingerprint reversal')
    return {'unchanged_operations':unchanged,'identical_failures':failures,
            'provider_cycles_without_change':cycle,'repeated_reversal':reversal,'warnings':warnings}
