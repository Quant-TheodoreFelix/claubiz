import os
import subprocess
import sys

from ..i18n import tr
from ._common import HostError, UnsupportedError

if sys.platform == "win32":
    from .windows import (
        check_data_location,
        is_private,
        lock,
        make_dir,
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
        make_dir,
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
    "make_dir",
    "open_fd",
    "open_with_default_app",
    "owned_by_me",
    "remove_tree",
    "replace",
    "restrict_dir",
    "restrict_file",
    "stat_is_link",
    "unlock",
]


def open_with_default_app(path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", str(path)], check=True, timeout=15, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform == "win32":
        os.startfile(str(path), "open")
    else:
        raise UnsupportedError(tr("파일 열기는 macOS와 Windows에서만 지원함"))


def is_link(path) -> bool:
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat_is_link(st)
