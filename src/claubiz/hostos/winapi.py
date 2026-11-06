import ctypes
from ctypes import wintypes
from functools import cache

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
CREATE_NEW = 1
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
TRUNCATE_EXISTING = 5
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
REPARSE_NAME_SURROGATE = 0x20000000
LOCKFILE_FAIL_IMMEDIATELY = 0x1
LOCKFILE_EXCLUSIVE_LOCK = 0x2
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_IO_PENDING = 997
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x1
DACL_SECURITY_INFORMATION = 0x4
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
SE_DACL_PROTECTED = 0x1000
TOKEN_QUERY = 0x8
TOKEN_USER_CLASS = 1
SDDL_REVISION_1 = 1
ACL_SIZE_INFORMATION_CLASS = 2
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_ALLOWED_CALLBACK_ACE_TYPE = 9
ALLOW_ACE_TYPES_WITH_OBJECT = (5, 11)
DRIVE_REMOTE = 4
FILE_PERSISTENT_ACLS = 0x8
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PVOID = ctypes.c_void_p
PPVOID = ctypes.POINTER(ctypes.c_void_p)


class FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class AclSizeInformation(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]


class AceHeader(ctypes.Structure):
    _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]


def _bind(lib, name: str, restype, *argtypes):
    func = getattr(lib, name)
    func.restype = restype
    func.argtypes = list(argtypes)
    return func


@cache
def kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    _bind(lib, "CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, PVOID,
          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    _bind(lib, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _bind(lib, "GetFileInformationByHandleEx", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, PVOID, wintypes.DWORD)
    _bind(lib, "GetFileInformationByHandle", wintypes.BOOL, wintypes.HANDLE,
          ctypes.POINTER(ByHandleFileInformation))
    _bind(lib, "LockFileEx", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
          wintypes.DWORD, ctypes.POINTER(Overlapped))
    _bind(lib, "UnlockFileEx", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
          ctypes.POINTER(Overlapped))
    _bind(lib, "GetCurrentProcess", wintypes.HANDLE)
    _bind(lib, "LocalFree", PVOID, PVOID)
    _bind(lib, "GetDriveTypeW", wintypes.UINT, wintypes.LPCWSTR)
    _bind(lib, "GetVolumePathNameW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    _bind(lib, "GetVolumeInformationW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
          wintypes.LPWSTR, wintypes.DWORD)
    return lib


@cache
def advapi32():
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    _bind(lib, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    _bind(lib, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int, PVOID, wintypes.DWORD,
          ctypes.POINTER(wintypes.DWORD))
    _bind(lib, "ConvertSidToStringSidW", wintypes.BOOL, PVOID, PPVOID)
    _bind(lib, "ConvertStringSecurityDescriptorToSecurityDescriptorW", wintypes.BOOL, wintypes.LPCWSTR,
          wintypes.DWORD, PPVOID, ctypes.POINTER(wintypes.ULONG))
    _bind(lib, "GetSecurityInfo", wintypes.DWORD, wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, PPVOID, PPVOID,
          PPVOID, PPVOID, PPVOID)
    _bind(lib, "SetSecurityInfo", wintypes.DWORD, wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, PVOID, PVOID,
          PVOID, PVOID)
    _bind(lib, "GetSecurityDescriptorDacl", wintypes.BOOL, PVOID, ctypes.POINTER(wintypes.BOOL), PPVOID,
          ctypes.POINTER(wintypes.BOOL))
    _bind(lib, "GetSecurityDescriptorControl", wintypes.BOOL, PVOID, ctypes.POINTER(wintypes.WORD),
          ctypes.POINTER(wintypes.DWORD))
    _bind(lib, "GetAclInformation", wintypes.BOOL, PVOID, PVOID, wintypes.DWORD, ctypes.c_int)
    _bind(lib, "GetAce", wintypes.BOOL, PVOID, wintypes.DWORD, PPVOID)
    return lib


def last_error() -> int:
    return ctypes.get_last_error()


def error(code: int, path=None) -> OSError:
    return OSError(0, ctypes.FormatError(code).strip(), None if path is None else str(path), code)
