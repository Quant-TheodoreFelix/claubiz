import os
import stat
from pathlib import Path

from . import hostos

HOME_ENV = "CLAUBIZ_HOME"


class UnsafePathError(PermissionError):
    pass


DEFAULT_DIR = ".claubiz"


def home_path() -> Path:
    raw = os.environ.get(HOME_ENV, "").strip()
    if not raw:
        return Path.home() / DEFAULT_DIR
    path = Path(raw).expanduser()
    if not path.is_absolute() or "\x00" in raw:
        raise UnsafePathError(f"{HOME_ENV}는 절대 경로여야 함: {raw}")
    return path


def home() -> Path:
    return ensure_private_dir(home_path())


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if hostos.stat_is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(f"디렉터리가 아니거나 심볼릭 링크임: {path}")
    if not hostos.owned_by_me(path, st):
        raise UnsafePathError(f"현재 사용자 소유가 아님: {path}")
    hostos.restrict_dir(path, st)
    return path


def confine(root: Path, *parts: str) -> Path:
    base = root.resolve(strict=True)
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise UnsafePathError(f"허용되지 않는 경로 요소: {part!r}")
    candidate = base.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise UnsafePathError(f"작업 영역 밖 경로: {candidate}")
    if hostos.is_link(candidate):
        raise UnsafePathError(f"심볼릭 링크는 허용되지 않음: {candidate}")
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
            raise UnsafePathError(f"일반 파일이 아님: {path}")
        if st.st_size > limit:
            raise UnsafePathError(f"파일 크기 제한({limit} bytes) 초과: {path}")
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
            raise UnsafePathError(f"파일 크기 제한({limit} bytes) 초과: {path}")
        return data
    finally:
        os.close(fd)
