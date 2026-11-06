import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from . import hostos
from .i18n import tr
from .paths import home
from .secrets import redact

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(prev: str, body: str) -> str:
    return hashlib.sha256(f"{prev}\n{body}".encode()).hexdigest()


def _last_line(fd: int) -> bytes:
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return b""
    block = 4096
    pos = size
    buf = b""
    while pos > 0:
        step = min(block, pos)
        pos -= step
        os.lseek(fd, pos, os.SEEK_SET)
        buf = os.read(fd, step) + buf
        stripped = buf.rstrip(b"\n")
        idx = stripped.rfind(b"\n")
        if idx >= 0:
            return stripped[idx + 1 :]
    return buf.rstrip(b"\n")


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (home() / "audit.jsonl")
        self._lock = threading.Lock()

    def append(self, event: str, **data) -> str:
        with self._lock:
            fd = hostos.open_fd(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                hostos.lock(fd)
                last = _last_line(fd)
                if last:
                    prev_record = json.loads(last)
                    prev, seq = prev_record["hash"], prev_record["seq"] + 1
                else:
                    prev, seq = GENESIS, 0
                body = {
                    "seq": seq,
                    "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "event": event,
                    "data": json.loads(redact(_canonical(data))),
                }
                text = _canonical(body)
                digest = _digest(prev, text)
                line = _canonical({**body, "prev": prev, "hash": digest}) + "\n"
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
                return digest
            finally:
                hostos.unlock(fd)
                os.close(fd)

    def verify(self) -> tuple[bool, int, str]:
        if not self.path.exists():
            return True, 0, tr("감사 로그 없음")
        prev = GENESIS
        count = 0
        with open(self.path, "rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    return False, count, tr("{line}행 JSON 손상", line=lineno)
                body = {k: record[k] for k in ("seq", "ts", "event", "data")}
                if record.get("prev") != prev:
                    return False, count, tr("{line}행 체인 연결 불일치", line=lineno)
                if record.get("seq") != count:
                    return False, count, tr("{line}행 순번 불일치", line=lineno)
                if _digest(prev, _canonical(body)) != record.get("hash"):
                    return False, count, tr("{line}행 해시 불일치(변조 의심)", line=lineno)
                prev = record["hash"]
                count += 1
        return True, count, tr("정상")
