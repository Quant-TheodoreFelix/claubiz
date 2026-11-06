import ctypes
import errno
import os
import stat
import time
from functools import cache
from pathlib import Path

from . import winapi as w
from ._common import HostError

TRUSTED_SIDS = frozenset({"S-1-5-18", "S-1-5-32-544"})
LOCK_OFFSET_LOW = 0xFFFFFFFE
LOCK_OFFSET_HIGH = 0x7FFFFFFF
RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
ONEDRIVE_VARS = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")


def _msvcrt():
    import msvcrt

    return msvcrt


def _sid_string(sid) -> str:
    out = ctypes.c_void_p()
    if not w.advapi32().ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise w.error(w.last_error())
    try:
        return ctypes.wstring_at(out.value)
    finally:
        w.kernel32().LocalFree(out)


@cache
def current_user_sid() -> str:
    k32, adv = w.kernel32(), w.advapi32()
    token = ctypes.c_void_p()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), w.TOKEN_QUERY, ctypes.byref(token)):
        raise w.error(w.last_error())
    try:
        size = ctypes.c_ulong(0)
        adv.GetTokenInformation(token, w.TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, w.TOKEN_USER_CLASS, buffer, size, ctypes.byref(size)):
            raise w.error(w.last_error())
        sid = ctypes.c_void_p.from_buffer(buffer).value
        return _sid_string(sid)
    finally:
        k32.CloseHandle(token)


class _Descriptor:
    def __init__(self, sddl: str) -> None:
        self.pointer = ctypes.c_void_p()
        if not w.advapi32().ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, w.SDDL_REVISION_1, ctypes.byref(self.pointer), None
        ):
            raise w.error(w.last_error())

    def dacl(self) -> ctypes.c_void_p:
        present, defaulted, acl = ctypes.c_long(0), ctypes.c_long(0), ctypes.c_void_p()
        if not w.advapi32().GetSecurityDescriptorDacl(self.pointer, ctypes.byref(present), ctypes.byref(acl),
                                                      ctypes.byref(defaulted)):
            raise w.error(w.last_error())
        return acl

    def close(self) -> None:
        if self.pointer:
            w.kernel32().LocalFree(self.pointer)
            self.pointer = ctypes.c_void_p()


def _owner_only(directory: bool) -> _Descriptor:
    inherit = "OICI" if directory else ""
    return _Descriptor(f"D:PAI(A;{inherit};FA;;;{current_user_sid()})")


def _create(path, access: int, disposition: int, flags: int, attributes=None) -> int:
    handle = w.kernel32().CreateFileW(str(path), access, w.FILE_SHARE_ALL, attributes, disposition, flags, None)
    if handle is None or handle == w.INVALID_HANDLE_VALUE:
        raise w.error(w.last_error(), path)
    return handle


def _close(handle: int) -> None:
    w.kernel32().CloseHandle(handle)


def _tag_info(handle: int) -> w.FileAttributeTagInfo:
    info = w.FileAttributeTagInfo()
    if not w.kernel32().GetFileInformationByHandleEx(handle, w.FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info),
                                                     ctypes.sizeof(info)):
        raise w.error(w.last_error())
    return info


def _file_id(handle: int) -> tuple[int, int, int]:
    info = w.ByHandleFileInformation()
    if not w.kernel32().GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise w.error(w.last_error())
    return info.dwVolumeSerialNumber, info.nFileIndexHigh, info.nFileIndexLow


def _is_surrogate(attributes: int, tag: int) -> bool:
    return bool(attributes & w.FILE_ATTRIBUTE_REPARSE_POINT and tag & w.REPARSE_NAME_SURROGATE)


def _open_meta(path, access: int) -> int:
    handle = _create(path, access | w.FILE_READ_ATTRIBUTES, w.OPEN_EXISTING,
                     w.FILE_FLAG_OPEN_REPARSE_POINT | w.FILE_FLAG_BACKUP_SEMANTICS)
    try:
        info = _tag_info(handle)
        if _is_surrogate(info.FileAttributes, info.ReparseTag):
            raise OSError(errno.ELOOP, "링크는 허용되지 않음", str(path))
    except BaseException:
        _close(handle)
        raise
    return handle


def _disposition(flags: int) -> int:
    create, excl, trunc = flags & os.O_CREAT, flags & os.O_EXCL, flags & os.O_TRUNC
    if create and excl:
        return w.CREATE_NEW
    if create and trunc:
        return w.CREATE_ALWAYS
    if create:
        return w.OPEN_ALWAYS
    if trunc:
        return w.TRUNCATE_EXISTING
    return w.OPEN_EXISTING


def open_fd(path, flags: int, mode: int = 0o600) -> int:
    del mode
    rw = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
    access = w.FILE_READ_ATTRIBUTES | w.READ_CONTROL
    if rw in (os.O_RDONLY, os.O_RDWR):
        access |= w.GENERIC_READ
    if rw in (os.O_WRONLY, os.O_RDWR):
        access |= w.GENERIC_WRITE
    disposition = _disposition(flags)
    descriptor = _owner_only(False) if flags & os.O_CREAT else None
    attributes = None
    if descriptor is not None:
        attributes = w.SecurityAttributes(ctypes.sizeof(w.SecurityAttributes), descriptor.pointer.value, False)
    try:
        handle = _create(path, access, disposition, w.FILE_ATTRIBUTE_NORMAL | w.FILE_FLAG_OPEN_REPARSE_POINT,
                         None if attributes is None else ctypes.byref(attributes))
    finally:
        if descriptor is not None:
            descriptor.close()
    try:
        info = _tag_info(handle)
        if info.FileAttributes & w.FILE_ATTRIBUTE_REPARSE_POINT:
            if _is_surrogate(info.FileAttributes, info.ReparseTag):
                raise OSError(errno.ELOOP, "링크는 열 수 없음", str(path))
            first = handle
            handle = _create(path, access, w.OPEN_EXISTING, w.FILE_ATTRIBUTE_NORMAL)
            try:
                same = _file_id(first) == _file_id(handle)
            finally:
                _close(first)
            if not same:
                raise OSError(errno.ELOOP, "파일이 열리는 도중 바뀜", str(path))
        crt = os.O_NOINHERIT
        if rw == os.O_RDONLY:
            crt |= os.O_RDONLY
        if flags & os.O_APPEND:
            crt |= os.O_APPEND
        return _msvcrt().open_osfhandle(handle, crt)
    except BaseException:
        _close(handle)
        raise


def _lock_region() -> w.Overlapped:
    region = w.Overlapped()
    region.Offset = LOCK_OFFSET_LOW
    region.OffsetHigh = LOCK_OFFSET_HIGH
    return region


def lock(fd: int, blocking: bool = True) -> bool:
    flags = w.LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else w.LOCKFILE_FAIL_IMMEDIATELY)
    region = _lock_region()
    if w.kernel32().LockFileEx(_msvcrt().get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(region)):
        return True
    code = w.last_error()
    if not blocking and code in (w.ERROR_LOCK_VIOLATION, w.ERROR_IO_PENDING):
        return False
    raise w.error(code)


def unlock(fd: int) -> None:
    region = _lock_region()
    if not w.kernel32().UnlockFileEx(_msvcrt().get_osfhandle(fd), 0, 1, 0, ctypes.byref(region)):
        raise w.error(w.last_error())


def stat_is_link(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    return _is_surrogate(getattr(st, "st_file_attributes", 0), getattr(st, "st_reparse_tag", 0))


class _Security:
    def __init__(self, path, what: int) -> None:
        self.owner, self.dacl, self.descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        handle = _open_meta(path, w.READ_CONTROL)
        try:
            code = w.advapi32().GetSecurityInfo(handle, w.SE_FILE_OBJECT, what, ctypes.byref(self.owner), None,
                                                ctypes.byref(self.dacl), None, ctypes.byref(self.descriptor))
        finally:
            _close(handle)
        if code:
            raise w.error(code, path)

    def protected(self) -> bool:
        control, revision = ctypes.c_ushort(0), ctypes.c_ulong(0)
        if not w.advapi32().GetSecurityDescriptorControl(self.descriptor, ctypes.byref(control),
                                                         ctypes.byref(revision)):
            raise w.error(w.last_error())
        return bool(control.value & w.SE_DACL_PROTECTED)

    def allowed_sids(self) -> list[str] | None:
        if not self.dacl:
            return None
        adv = w.advapi32()
        info = w.AclSizeInformation()
        if not adv.GetAclInformation(self.dacl, ctypes.byref(info), ctypes.sizeof(info),
                                     w.ACL_SIZE_INFORMATION_CLASS):
            raise w.error(w.last_error())
        sids = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(self.dacl, index, ctypes.byref(ace)):
                raise w.error(w.last_error())
            header = w.AceHeader.from_address(ace.value)
            if header.AceType in w.ALLOW_ACE_TYPES_WITH_OBJECT:
                return None
            if header.AceType in (w.ACCESS_ALLOWED_ACE_TYPE, w.ACCESS_ALLOWED_CALLBACK_ACE_TYPE):
                sids.append(_sid_string(ace.value + 8))
        return sids

    def close(self) -> None:
        if self.descriptor:
            w.kernel32().LocalFree(self.descriptor)
            self.descriptor = ctypes.c_void_p()


def owned_by_me(path, st: os.stat_result | None = None) -> bool:
    del st
    security = _Security(path, w.OWNER_SECURITY_INFORMATION)
    try:
        return bool(security.owner) and _sid_string(security.owner) == current_user_sid()
    finally:
        security.close()


def is_private(path) -> bool:
    security = _Security(path, w.DACL_SECURITY_INFORMATION)
    try:
        sids = security.allowed_sids()
        if sids is None or not security.protected():
            return False
        allowed = TRUSTED_SIDS | {current_user_sid()}
        return all(sid in allowed for sid in sids)
    finally:
        security.close()


def _set_owner_only(path, directory: bool) -> None:
    descriptor = _owner_only(directory)
    try:
        handle = _open_meta(path, w.READ_CONTROL | w.WRITE_DAC)
        try:
            code = w.advapi32().SetSecurityInfo(
                handle, w.SE_FILE_OBJECT, w.DACL_SECURITY_INFORMATION | w.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, descriptor.dacl(), None,
            )
        finally:
            _close(handle)
        if code:
            raise w.error(code, path)
    finally:
        descriptor.close()


def restrict_dir(path, st: os.stat_result | None = None) -> None:
    del st
    if not is_private(path):
        _set_owner_only(path, True)


def restrict_file(path) -> None:
    _set_owner_only(path, False)


def _retry(action, *args) -> None:
    for delay in RETRY_DELAYS:
        try:
            action(*args)
            return
        except PermissionError:
            time.sleep(delay)
    action(*args)


def replace(src, dst) -> None:
    _retry(os.replace, src, dst)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        _retry(os.unlink, path)


def remove_tree(path) -> None:
    root = os.fspath(path)
    if stat_is_link(os.lstat(root)):
        raise HostError(f"링크라서 삭제하지 않음: {root}")
    pending = [root]
    folders = []
    while pending:
        folder = pending.pop()
        folders.append(folder)
        with os.scandir(folder) as entries:
            items = list(entries)
        for entry in items:
            st = entry.stat(follow_symlinks=False)
            if stat_is_link(st):
                raise HostError(f"링크가 있어 삭제를 멈춤: {entry.path}")
            if stat.S_ISDIR(st.st_mode):
                pending.append(entry.path)
            else:
                _unlink(entry.path)
    for folder in reversed(folders):
        _retry(os.rmdir, folder)


def _volume_root(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    if w.kernel32().GetVolumePathNameW(str(path), buffer, len(buffer)):
        return buffer.value
    return path.anchor


def _inside(path: Path, root: str) -> bool:
    child = os.path.normcase(os.path.abspath(path))
    parent = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    return child == parent or child.startswith(parent + os.sep)


def check_data_location(path) -> None:
    path = Path(path)
    text = str(path)
    if text.startswith(("\\\\", "//")):
        raise HostError(f"네트워크 경로나 장치 경로에는 데이터 폴더를 둘 수 없음: {text}")
    for name in ONEDRIVE_VARS:
        root = os.environ.get(name, "").strip()
        if root and _inside(path, root):
            raise HostError(f"OneDrive 동기화 폴더 안에는 데이터 폴더를 둘 수 없음 (업무 자료가 클라우드로 올라감): {text}")
    volume = _volume_root(path)
    k32 = w.kernel32()
    if k32.GetDriveTypeW(volume) == w.DRIVE_REMOTE:
        raise HostError(f"네트워크 드라이브에는 데이터 폴더를 둘 수 없음: {text}")
    flags = ctypes.c_ulong(0)
    if not k32.GetVolumeInformationW(volume, None, 0, None, None, ctypes.byref(flags), None, 0):
        raise w.error(w.last_error(), volume)
    if not flags.value & w.FILE_PERSISTENT_ACLS:
        raise HostError(f"권한(ACL)을 지원하지 않는 드라이브(FAT32, exFAT 등)에는 데이터 폴더를 둘 수 없음: {text}")
