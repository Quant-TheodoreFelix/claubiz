import os
import sys

from ._common import HostError, UnsupportedError

if sys.platform == "win32":
    from .windows import (
        lock,
        open_fd,
        owned_by_me,
        remove_tree,
        replace,
        restrict_dir,
        restrict_file,
        stat_is_link,
        unlock,
    )
else:
    from .posix import (
        lock,
        open_fd,
        owned_by_me,
        remove_tree,
        replace,
        restrict_dir,
        restrict_file,
        stat_is_link,
        unlock,
    )

__all__ = [
    "HostError",
    "UnsupportedError",
    "is_link",
    "lock",
    "open_fd",
    "owned_by_me",
    "remove_tree",
    "replace",
    "restrict_dir",
    "restrict_file",
    "stat_is_link",
    "unlock",
]


def is_link(path) -> bool:
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat_is_link(st)
