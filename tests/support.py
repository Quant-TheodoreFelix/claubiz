import sys
from pathlib import Path

from claubiz import hostos


def assert_private(path: Path, mode: int) -> None:
    if sys.platform == "win32":
        assert hostos.is_private(path), path
    else:
        assert path.stat().st_mode & 0o777 == mode, oct(path.stat().st_mode)
