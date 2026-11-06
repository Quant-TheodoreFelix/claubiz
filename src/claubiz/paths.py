import os
import stat
from pathlib import Path

from . import hostos
from .i18n import tr

HOME_ENV = "CLAUBIZ_HOME"


class UnsafePathError(PermissionError):
    pass


DEFAULT_DIR = ".claubiz"
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789\u00b9\u00b2\u00b3"}
)
INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')


def portable_name_problem(name: str) -> str | None:
    if any(ch in INVALID_NAME_CHARS or ord(ch) < 32 for ch in name):
        return tr("쓸 수 없는 문자가 있음 (<>:\"/\\|?* 와 제어 문자): {name}", name=repr(name))
    if name.endswith((" ", ".")):
        return tr("점이나 공백으로 끝나는 이름은 쓸 수 없음: {name}", name=repr(name))
    if name.split(".", 1)[0].rstrip(" ").upper() in RESERVED_NAMES:
        return tr("Windows 예약 이름은 쓸 수 없음: {name}", name=repr(name))
    return None


def home_path() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    if not raw:
        return Path.home() / DEFAULT_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute() or "\x00" in raw:
        raise UnsafePathError(tr("{env}는 절대 경로여야 함: {path}", env=HOME_ENV, path=raw))
    return path


def home() -> Path:
    path = home_path()
    try:
        hostos.check_data_location(path)
    except hostos.HostError as exc:
        raise UnsafePathError(str(exc)) from None
    return ensure_private_dir(path)


def ensure_private_dir(path: Path) -> Path:
    hostos.make_dir(path)
    st = path.lstat()
    if hostos.stat_is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(tr("디렉터리가 아니거나 심볼릭 링크임: {path}", path=path))
    if not hostos.owned_by_me(path, st):
        raise UnsafePathError(tr("현재 사용자 소유가 아님: {path}", path=path))
    hostos.restrict_dir(path, st)
    return path


def confine(root: Path, *parts: str) -> Path:
    base = root.resolve(strict=True)
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise UnsafePathError(tr("허용되지 않는 경로 요소: {part}", part=repr(part)))
        problem = portable_name_problem(part)
        if problem:
            raise UnsafePathError(problem)
    candidate = base.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise UnsafePathError(tr("작업 영역 밖 경로: {path}", path=candidate))
    if hostos.is_link(candidate):
        raise UnsafePathError(tr("심볼릭 링크는 허용되지 않음: {path}", path=candidate))
    return candidate


def write_private_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = hostos.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        hostos.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_private_text(path: Path, text: str) -> None:
    write_private_bytes(path, text.encode("utf-8"))


def read_bytes_nofollow(path: Path, limit: int) -> bytes:
    fd = hostos.open_fd(path, os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(tr("일반 파일이 아님: {path}", path=path))
        if st.st_size > limit:
            raise UnsafePathError(tr("파일 크기 제한({limit} bytes) 초과: {path}", limit=limit, path=path))
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit:
            raise UnsafePathError(tr("파일 크기 제한({limit} bytes) 초과: {path}", limit=limit, path=path))
        return data
    finally:
        os.close(fd)
