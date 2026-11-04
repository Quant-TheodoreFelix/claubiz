import hashlib
import hmac
import logging
import os
import re
import stat
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .paths import UnsafePathError, read_bytes_nofollow

KEY_PATTERN = re.compile(r"^sk-ant-[a-z0-9]{2,16}-[A-Za-z0-9_\-]{20,256}$")
REDACT_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}")
SECURE_BACKENDS = frozenset(
    {
        "keyring.backends.macOS.Keyring",
        "keyring.backends.SecretService.Keyring",
        "keyring.backends.libsecret.Keyring",
        "keyring.backends.Windows.WinVaultKeyring",
    }
)
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_WORKSPACE_ID",
)


class SecretError(Exception):
    pass


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def fingerprint(self) -> str:
        return hashlib.sha256(self._value.encode()).hexdigest()[:12]

    def matches(self, other: "SecretValue") -> bool:
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    def __repr__(self) -> str:
        return "SecretValue(****)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("SecretValue는 직렬화할 수 없음")


def redact(text: str) -> str:
    return REDACT_PATTERN.sub("sk-ant-****", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def scrub_credential_env() -> list[str]:
    removed = [name for name in SCRUBBED_ENV if name in os.environ]
    for name in removed:
        os.environ.pop(name, None)
    return removed


def validate_key(value: str) -> SecretValue:
    candidate = value.strip()
    if not KEY_PATTERN.fullmatch(candidate):
        raise SecretError("API 키 형식이 올바르지 않음")
    if candidate.startswith("sk-ant-admin"):
        raise SecretError("Admin API 키는 에이전트 실행용으로 허용하지 않음")
    if candidate.startswith("sk-ant-oat"):
        raise SecretError("OAuth 토큰은 허용하지 않음, Console에서 발급한 API 키를 사용해야 함")
    return SecretValue(candidate)


def backend_name() -> str:
    backend = keyring.get_keyring()
    return f"{type(backend).__module__}.{type(backend).__name__}"


def _secure_backend():
    name = backend_name()
    if name not in SECURE_BACKENDS:
        raise SecretError(f"안전하지 않은 키링 백엔드: {name}")
    return keyring.get_keyring()


class KeychainStore:
    def __init__(self, service: str, account: str) -> None:
        self.service = service
        self.account = account

    def get(self) -> SecretValue | None:
        try:
            raw = _secure_backend().get_password(self.service, self.account)
        except KeyringError as exc:
            raise SecretError(f"키체인 읽기 실패: {type(exc).__name__}") from None
        return None if raw is None else validate_key(raw)

    def require(self) -> SecretValue:
        value = self.get()
        if value is None:
            raise SecretError("키체인에 API 키가 없음, `claubiz auth import-file` 또는 `claubiz auth set`으로 등록 필요")
        return value

    def set(self, value: SecretValue) -> None:
        backend = _secure_backend()
        try:
            backend.set_password(self.service, self.account, value.reveal())
        except KeyringError as exc:
            raise SecretError(f"키체인 저장 실패: {type(exc).__name__}") from None
        stored = self.get()
        if stored is None or not stored.matches(value):
            raise SecretError("키체인 저장 후 검증 실패")

    def delete(self) -> bool:
        try:
            _secure_backend().delete_password(self.service, self.account)
            return True
        except PasswordDeleteError:
            return False
        except KeyringError as exc:
            raise SecretError(f"키체인 삭제 실패: {type(exc).__name__}") from None

    def import_file(self, path: Path) -> SecretValue:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise SecretError("키 파일은 일반 파일이어야 함")
        if st.st_uid != os.getuid():
            raise SecretError("현재 사용자 소유의 키 파일만 가져올 수 있음")
        try:
            raw = read_bytes_nofollow(path, 4096)
        except UnsafePathError as exc:
            raise SecretError(str(exc)) from None
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise SecretError("키 파일에 ASCII 외 문자가 포함됨") from None
        value = validate_key(text)
        self.set(value)
        return value


def secure_unlink(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        os.write(fd, os.urandom(size))
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\x00" * size)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.unlink(path)
