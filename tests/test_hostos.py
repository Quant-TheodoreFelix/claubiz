import os
import subprocess
import sys
import textwrap

import pytest

from claubiz import hostos

PRELOAD = ("asyncio", "subprocess", "sqlite3", "anthropic", "keyring", "pydantic", "rich", "textual.app")


def test_imports_without_posix_only_apis():
    code = textwrap.dedent(
        f"""
        import importlib, os, sys
        for name in {PRELOAD!r}:
            importlib.import_module(name)
        sys.modules["fcntl"] = None
        for attr in ("getuid", "geteuid", "getgid", "O_NOFOLLOW"):
            if hasattr(os, attr):
                delattr(os, attr)
        sys.platform = "win32"
        import claubiz.cli
        import claubiz.tui.app
        from claubiz import hostos
        assert hostos.open_fd.__module__ == "claubiz.hostos.windows", hostos.open_fd.__module__
        """
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stderr


def test_lock_is_exclusive_across_descriptors(tmp_path):
    path = tmp_path / "x.lock"
    first = hostos.open_fd(path, os.O_RDWR | os.O_CREAT)
    second = hostos.open_fd(path, os.O_RDWR | os.O_CREAT)
    try:
        assert hostos.lock(first, blocking=False)
        assert not hostos.lock(second, blocking=False)
        hostos.unlock(first)
        assert hostos.lock(second, blocking=False)
        hostos.unlock(second)
    finally:
        os.close(first)
        os.close(second)


def test_open_fd_refuses_links(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"x")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not permitted")
    assert hostos.is_link(link)
    assert not hostos.is_link(target)
    assert not hostos.is_link(tmp_path / "missing")
    with pytest.raises(OSError):
        hostos.open_fd(link, os.O_RDONLY)
