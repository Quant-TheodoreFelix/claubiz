import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import hostos
from .config import MODEL_CHOICES
from .paths import confine, read_bytes_nofollow, write_private_text
from .policy import scope_allows_web
from .render import urls_in
from .schedule import ScheduleError, parse_schedule
from .workspace import REPORT_FOOTER, Workspace, WorkspaceError

DIGEST_ID_RE = re.compile(r"^d[0-9a-f]{6}$")
DEFAULT_DIGEST_MODEL = "claude-sonnet-5-5"
DEFAULT_DIGEST_CAP = 0.5
MAX_SEEN_URLS = 200
SEEN_IN_PROMPT = 60
MAX_FAILURES = 3
FIRST_LOOKBACK_DAYS = 7
MAX_DIGESTS = 30


class DigestError(Exception):
    pass


class DigestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    title: str = Field(min_length=1, max_length=60)
    request: str = Field(min_length=1, max_length=2000)
    schedule: str
    model: str = DEFAULT_DIGEST_MODEL
    max_searches: int = Field(4, ge=1, le=20)
    run_cap: float = Field(DEFAULT_DIGEST_CAP, ge=0.05, le=20)
    enabled: bool = True
    created: str
    last_attempt: str = ""
    last_run_id: str = ""
    last_status: str = ""
    last_success: str = ""
    last_success_run: str = ""
    failures: int = 0
    seen_urls: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not DIGEST_ID_RE.fullmatch(value):
            raise ValueError("올바르지 않은 정기 정리 ID")
        return value

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str) -> str:
        try:
            return parse_schedule(value).describe()
        except ScheduleError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        if value and value not in dict(MODEL_CHOICES):
            raise ValueError(f"고를 수 없는 모델: {value}")
        return value

    def next_due(self) -> datetime:
        base = datetime.fromisoformat(self.last_attempt or self.created)
        return parse_schedule(self.schedule).next_after(base)

    def is_due(self, now: datetime) -> bool:
        return self.enabled and self.next_due() <= now

    def since(self, now: datetime) -> datetime:
        if self.last_success:
            return datetime.fromisoformat(self.last_success)
        gap = parse_schedule(self.schedule).nominal_gap()
        return now - max(gap, timedelta(days=FIRST_LOOKBACK_DAYS))


def new_digest_id(seed: str) -> str:
    return "d" + hashlib.sha256(f"{seed}{time.time_ns()}".encode()).hexdigest()[:6]


@dataclass
class DigestClaim:
    workspace: str
    digest_id: str
    started: datetime
    fd: int

    def release(self) -> None:
        if self.fd >= 0:
            try:
                hostos.unlock(self.fd)
            finally:
                os.close(self.fd)
                self.fd = -1


class DigestStore:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def _path(self):
        return confine(self.ws.root, "digests.json")

    def _lock_path(self, name: str):
        return confine(self.ws.root, name)

    @staticmethod
    def _open_lock(path) -> int:
        return hostos.open_fd(path, os.O_RDWR | os.O_CREAT, 0o600)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = self._open_lock(self._lock_path("digests.lock"))
        try:
            hostos.lock(fd)
            yield
        finally:
            hostos.unlock(fd)
            os.close(fd)

    def load(self) -> list[DigestSpec]:
        path = self._path()
        if not path.exists():
            return []
        data = json.loads(read_bytes_nofollow(path, 2 * 1024 * 1024).decode("utf-8"))
        out = []
        for item in data.get("digests", []):
            try:
                out.append(DigestSpec.model_validate(item))
            except ValidationError:
                continue
        return out

    def _save(self, specs: list[DigestSpec]) -> None:
        payload = {"digests": [s.model_dump() for s in specs]}
        write_private_text(self._path(), json.dumps(payload, ensure_ascii=False, indent=1))

    def get(self, digest_id: str) -> DigestSpec | None:
        return next((s for s in self.load() if s.id == digest_id), None)

    def add(self, spec: DigestSpec) -> DigestSpec:
        if not scope_allows_web(self.ws.config.scope):
            raise DigestError("정기 정리는 웹 조사가 필요해 자료 전용 작업 영역에서는 쓸 수 없음")
        with self._locked():
            specs = self.load()
            if len(specs) >= MAX_DIGESTS:
                raise DigestError(f"정기 정리는 작업 영역마다 최대 {MAX_DIGESTS}개")
            if any(s.id == spec.id for s in specs):
                raise DigestError("이미 있는 정기 정리 ID")
            specs.append(spec)
            self._save(specs)
        return spec

    def update(self, digest_id: str, **changes) -> DigestSpec:
        with self._locked():
            specs = self.load()
            for i, spec in enumerate(specs):
                if spec.id == digest_id:
                    updated = DigestSpec.model_validate({**spec.model_dump(), **changes})
                    specs[i] = updated
                    self._save(specs)
                    return updated
        raise DigestError("정기 정리를 찾을 수 없음")

    def remove(self, digest_id: str) -> bool:
        with self._locked():
            specs = self.load()
            kept = [s for s in specs if s.id != digest_id]
            if len(kept) == len(specs):
                return False
            self._save(kept)
        with _suppress_missing():
            self._lock_path(f"digest-{digest_id}.lock").unlink()
        return True

    def claim(self, digest_id: str, now: datetime, force: bool = False) -> DigestClaim | None:
        if not DIGEST_ID_RE.fullmatch(digest_id):
            return None
        fd = self._open_lock(self._lock_path(f"digest-{digest_id}.lock"))
        if not hostos.lock(fd, blocking=False):
            os.close(fd)
            return None
        try:
            with self._locked():
                specs = self.load()
                index = next((i for i, s in enumerate(specs) if s.id == digest_id), None)
                if index is None or not (force or specs[index].is_due(now)):
                    raise LookupError
                specs[index] = specs[index].model_copy(
                    update={"last_attempt": now.isoformat(timespec="seconds"), "last_status": "running"}
                )
                self._save(specs)
        except LookupError:
            hostos.unlock(fd)
            os.close(fd)
            return None
        except BaseException:
            hostos.unlock(fd)
            os.close(fd)
            raise
        return DigestClaim(self.ws.name, digest_id, now, fd)

    def is_running(self, digest_id: str) -> bool:
        if not DIGEST_ID_RE.fullmatch(digest_id):
            return False
        path = self._lock_path(f"digest-{digest_id}.lock")
        if not path.exists():
            return False
        fd = self._open_lock(path)
        try:
            if not hostos.lock(fd, blocking=False):
                return True
            hostos.unlock(fd)
            return False
        finally:
            os.close(fd)

    def due(self, now: datetime) -> list[DigestSpec]:
        out = []
        for spec in self.load():
            try:
                if spec.is_due(now):
                    out.append(spec)
            except (ScheduleError, ValueError):
                continue
        return out

    def finish(self, claim: DigestClaim, run_id: str, status: str, report: str = "") -> DigestSpec | None:
        success = status in ("completed", "partial")
        with self._locked():
            specs = self.load()
            for i, spec in enumerate(specs):
                if spec.id != claim.digest_id:
                    continue
                if success:
                    failures = 0
                elif status == "cancelled":
                    failures = spec.failures
                else:
                    failures = spec.failures + 1
                seen = list(spec.seen_urls)
                if success and report:
                    for url in urls_in(report.rsplit(REPORT_FOOTER, 1)[0]):
                        if url in seen:
                            seen.remove(url)
                        seen.append(url)
                changes = {
                    "last_run_id": run_id,
                    "last_status": status,
                    "failures": failures,
                    "seen_urls": seen[-MAX_SEEN_URLS:],
                }
                if success:
                    changes["last_success"] = claim.started.isoformat(timespec="seconds")
                    changes["last_success_run"] = run_id
                if failures >= MAX_FAILURES:
                    changes["enabled"] = False
                specs[i] = spec.model_copy(update=changes)
                self._save(specs)
                return specs[i]
        return None


@contextmanager
def _suppress_missing() -> Iterator[None]:
    try:
        yield
    except FileNotFoundError:
        pass


def digest_context(ws: Workspace, spec: DigestSpec, now: datetime) -> dict:
    previous = ""
    if spec.last_success_run:
        record = ws.load_run(spec.last_success_run)
        if record and record.get("status") in ("completed", "partial"):
            previous = str(record.get("summary") or "")[:1500]
    return {
        "id": spec.id,
        "title": spec.title,
        "since": spec.since(now).strftime("%Y-%m-%d %H:%M"),
        "seen": spec.seen_urls[-SEEN_IN_PROMPT:],
        "previous": previous,
        "max_searches": spec.max_searches,
    }


def make_spec(ws: Workspace, *, title: str, request: str, schedule: str, model: str = DEFAULT_DIGEST_MODEL,
              max_searches: int = 4, run_cap: float = DEFAULT_DIGEST_CAP, enabled: bool = True,
              now: datetime | None = None) -> DigestSpec:
    if max_searches > ws.config.web.max_searches_per_task:
        raise DigestError(f"검색 수는 작업 영역 상한({ws.config.web.max_searches_per_task}회) 이하여야 함")
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        return DigestSpec(
            id=new_digest_id(title), title=title, request=request, schedule=schedule, model=model,
            max_searches=max_searches, run_cap=run_cap, enabled=enabled, created=stamp,
        )
    except ValidationError as exc:
        raise DigestError("; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())) from None


def all_due(now: datetime) -> list[tuple[Workspace, DigestSpec]]:
    found = []
    for name in Workspace.list_names():
        try:
            ws = Workspace.open(name)
        except WorkspaceError:
            continue
        if not scope_allows_web(ws.config.scope):
            continue
        found += [(ws, spec) for spec in DigestStore(ws).due(now)]
    return found


def start_digest(runtime, ws: Workspace, spec: DigestSpec, claim: DigestClaim):
    orch = runtime.orchestrator(ws)
    orch.set_model(spec.model or None)
    session = orch.direct(spec.request, None, spec.run_cap, digest=digest_context(ws, spec, claim.started))
    runtime.audit.append("digest.started", workspace=ws.name, digest_id=spec.id, run_id=session.run_id,
                         model=spec.model or "config", run_cap=spec.run_cap)
    return orch, session


def finish_digest(runtime, ws: Workspace, claim: DigestClaim, run_id: str, status: str,
                  report_path=None) -> DigestSpec | None:
    report = ""
    if report_path is not None and run_id:
        report = ws.read_report(run_id) or ""
    try:
        spec = DigestStore(ws).finish(claim, run_id, status, report)
    finally:
        claim.release()
    runtime.audit.append("digest.finished", workspace=ws.name, digest_id=claim.digest_id, run_id=run_id,
                         status=status, disabled=bool(spec is not None and not spec.enabled))
    return spec


async def run_claimed(runtime, ws: Workspace, spec: DigestSpec, claim: DigestClaim):
    run_id = ""
    status = "failed"
    report_path = None
    try:
        orch, session = start_digest(runtime, ws, spec, claim)
        run_id = session.run_id
        outcome = await orch.execute(session)
        status, report_path = outcome.status, outcome.report_path
        return outcome
    except (asyncio.CancelledError, KeyboardInterrupt):
        status = "cancelled"
        raise
    finally:
        finish_digest(runtime, ws, claim, run_id, status, report_path)


async def run_due(runtime, now: datetime | None = None) -> list[tuple[str, str, str]]:
    moment = now or datetime.now()
    results: list[tuple[str, str, str]] = []
    for ws, spec in all_due(moment):
        claim = DigestStore(ws).claim(spec.id, moment)
        if claim is None:
            results.append((ws.name, spec.id, "skipped"))
            continue
        try:
            outcome = await run_claimed(runtime, ws, spec, claim)
        except Exception as exc:
            results.append((ws.name, spec.id, f"failed: {str(exc)[:200]}"))
            continue
        results.append((ws.name, spec.id, outcome.status))
    return results
