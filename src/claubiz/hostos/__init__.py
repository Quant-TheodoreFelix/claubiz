import os
import sys

from ._common import HostError, UnsupportedError

if sys.platform == "win32":
    from .windows import (
        check_data_location,
        is_private,
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
        check_data_location,
        is_private,
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
    "check_data_location",
    "is_link",
    "is_private",
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
