import os
import stat

from ._common import UnsupportedError


def _pending(*_args, **_kwargs):
    raise UnsupportedError("Windows 보안 기능이 아직 구현되지 않음")


open_fd = _pending
lock = _pending
unlock = _pending
owned_by_me = _pending
restrict_dir = _pending
restrict_file = _pending
replace = _pending
remove_tree = _pending


def stat_is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode)
