import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from claubiz import hostos
from claubiz.audit import AuditLog
from claubiz.paths import (
    UnsafePathError,
    confine,
    ensure_private_dir,
    home,
    read_bytes_nofollow,
    write_private_bytes,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows backend")

LOCK_PROBE = (
    "import os, sys\n"
    "from claubiz import hostos\n"
    "fd = hostos.open_fd(sys.argv[1], os.O_RDWR)\n"
    "print(hostos.lock(fd, blocking=False))\n"
)


def test_private_dir_is_owner_only_and_children_inherit(tmp_path):
    from claubiz.hostos import windows

    folder = ensure_private_dir(tmp_path / "data")
    assert hostos.is_private(folder) and hostos.owned_by_me(folder)
    assert windows._owner(folder) == windows.current_user_sid()
    (folder / "plain.txt").write_text("x", encoding="utf-8")
    (folder / "sub").mkdir()
    assert hostos.is_private(folder / "plain.txt")
    assert hostos.is_private(folder / "sub")


def test_files_created_outside_data_dir_are_private(tmp_path):
    target = tmp_path / "out.bin"
    os.close(hostos.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
    assert hostos.is_private(target) and hostos.owned_by_me(target)
    from claubiz.hostos import windows

    assert windows._owner(target) == windows.current_user_sid()
    with pytest.raises(FileExistsError):
        hostos.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with pytest.raises(FileNotFoundError):
        hostos.open_fd(tmp_path / "missing", os.O_RDONLY)


def test_restrict_dir_repairs_open_acl(tmp_path):
    folder = tmp_path / "open"
    folder.mkdir()
    subprocess.run(["icacls", str(folder), "/grant", "*S-1-1-0:(OI)(CI)R"], check=True, capture_output=True)
    assert not hostos.is_private(folder)
    hostos.restrict_dir(folder)
    assert hostos.is_private(folder)


def test_bytes_round_trip_without_newline_translation(tmp_path):
    data = b"a\r\nb\nc\x1ad\x00e"
    path = tmp_path / "blob"
    write_private_bytes(path, data)
    assert read_bytes_nofollow(path, 1024) == data
    assert path.read_bytes() == data


def test_audit_chain_has_lf_only(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for index in range(3):
        log.append("event", value=f"line {index}\nnext")
    ok, count, _ = log.verify()
    assert ok and count == 3
    assert b"\r\n" not in (tmp_path / "audit.jsonl").read_bytes()


def test_junctions_count_as_links(tmp_path):
    import _winapi

    target = tmp_path / "target"
    target.mkdir()
    (target / "f.txt").write_text("x", encoding="utf-8")
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(junction))
    assert hostos.is_link(junction) and not hostos.is_link(target)
    with pytest.raises(UnsafePathError):
        confine(tmp_path, "junction")
    with pytest.raises(hostos.HostError):
        hostos.remove_tree(junction)
    holder = tmp_path / "holder"
    holder.mkdir()
    _winapi.CreateJunction(str(target), str(holder / "inner"))
    hostos.remove_tree(holder)
    assert not holder.exists() and (target / "f.txt").exists()


def test_remove_tree_handles_read_only_files(tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "file.txt").write_text("x", encoding="utf-8")
    os.chmod(root / "a" / "b" / "file.txt", 0o444)
    hostos.remove_tree(root)
    assert not root.exists()


def test_lock_conflicts_across_processes(tmp_path):
    path = tmp_path / "x.lock"
    fd = hostos.open_fd(path, os.O_RDWR | os.O_CREAT)
    try:
        assert hostos.lock(fd)
        probe = [sys.executable, "-c", LOCK_PROBE, str(path)]
        assert subprocess.run(probe, capture_output=True, text=True, check=True).stdout.strip() == "False"
        assert path.read_bytes() == b""
        hostos.unlock(fd)
        assert subprocess.run(probe, capture_output=True, text=True, check=True).stdout.strip() == "True"
    finally:
        os.close(fd)


def test_data_location_rules(tmp_path, monkeypatch):
    with pytest.raises(hostos.HostError):
        hostos.check_data_location(Path(r"\\server\share\claubiz"))
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    with pytest.raises(hostos.HostError):
        hostos.check_data_location(tmp_path / "OneDrive" / "claubiz")
    monkeypatch.setenv("CLAUBIZ_HOME", str(tmp_path / "OneDrive" / "claubiz"))
    with pytest.raises(UnsafePathError):
        home()
    hostos.check_data_location(tmp_path / "data")


@pytest.mark.skipif(not os.environ.get("CLAUBIZ_TEST_CREDSTORE"), reason="writes to Credential Manager")
def test_api_key_is_stored_without_roaming():
    import keyring

    from claubiz.secrets import KeychainStore, validate_key

    service = f"claubiz-test-{uuid.uuid4().hex[:8]}"
    store = KeychainStore(service, "anthropic-api-key")
    store.set(validate_key("sk-ant-api03-" + "A" * 80))
    try:
        credential = keyring.get_keyring()._read_credential(service)
        assert credential["Persist"] == 2
    finally:
        store.delete()
