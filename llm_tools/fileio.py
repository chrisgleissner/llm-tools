"""Small file helpers with no heavy imports (usable by commands that must start fast)."""
from __future__ import annotations

import errno
import os
import stat
from typing import Any


def open_regular_file(path: str | os.PathLike[str], *, follow_symlinks: bool = True) -> Any:
    """Open `path` for binary reading, refusing anything but a regular file.

    A caller that has checked a path with `stat` can still be handed a FIFO or a
    device if the path is replaced before it is opened, and opening a FIFO for
    reading blocks until a writer appears. The file is opened non-blocking, its
    type is checked on the open descriptor, and only then is it made blocking.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", os.fspath(path))
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
