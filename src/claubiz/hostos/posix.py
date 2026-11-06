import fcntl
import os
import shutil
import stat
from pathlib import Path

from ..i18n import tr
from ._common import UnsupportedError


def open_fd(path, flags: int, mode: int = 0o600) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, mode)


def lock(fd: int, blocking: bool = True) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


def stat_is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode)


def owned_by_me(path, st: os.stat_result | None = None) -> bool:
    st = st or os.lstat(path)
    return st.st_uid == os.getuid()


def restrict_dir(path, st: os.stat_result | None = None) -> None:
    st = st or os.lstat(path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def make_dir(path) -> None:
    Path(path).mkdir(mode=0o700, parents=True, exist_ok=True)


def restrict_file(path) -> None:
    os.chmod(path, 0o600)


def is_private(path) -> bool:
    return not os.lstat(path).st_mode & 0o077


def check_data_location(path) -> None:
    del path


def replace(src, dst) -> None:
    os.replace(src, dst)


def remove_tree(path) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise UnsupportedError(tr("이 플랫폼에서는 안전한 디렉터리 삭제를 지원하지 않음"))
    shutil.rmtree(path)
