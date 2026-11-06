import hashlib
import json
import os
import re
import stat
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import hostos
from .paths import (
    confine,
    ensure_private_dir,
    home,
    home_path,
    portable_name_problem,
    read_bytes_nofollow,
    write_private_text,
)
from .policy import Classification, Scope, scope_allows_inputs, validate_domains

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
FILE_RE = re.compile(r"^[\w가-힣][\w가-힣 .()+-]{0,120}$")
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".log", ".yaml", ".yml"})
MAX_INPUT_BYTES = 5 * 1024 * 1024
RUN_ID_RE = re.compile(r"^r\d{8}-\d{6}-[0-9a-f]{6}$")
MAX_BRIEF_CHARS = 20000
REPORT_FOOTERS = ("\n---\n실행 ", "\n---\nRun ")
EXPORT_NAME_RE = re.compile(r"[^\w가-힣 ._()-]+")
CLASS_ORDER = ("public", "internal", "confidential")
BUSINESS_TYPES: tuple[tuple[str, str], ...] = (
    ("consulting", "컨설팅·자문"),
    ("saas", "SaaS·IT 서비스"),
    ("commerce", "이커머스·리테일"),
    ("manufacturing", "제조·유통"),
    ("finance", "금융·투자"),
    ("public", "공공·조달"),
    ("marketing", "마케팅·에이전시"),
    ("healthcare", "의료·헬스케어"),
    ("education", "교육"),
    ("realestate", "부동산·건설"),
    ("startup", "스타트업 일반"),
    ("other", "기타"),
)
PROFILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("industry", "Industry and niche"),
    ("offering", "Main products or services"),
    ("customers", "Main customers"),
    ("competitors", "Competitors and benchmarks"),
    ("markets", "Regions and markets of interest"),
    ("audience", "Readers of the deliverables"),
    ("focus", "Recurring work and issues to watch"),
)
ACTION_TYPES: tuple[str, ...] = ("email", "event", "table", "doc")


class WorkspaceError(Exception):
    pass


class WebPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    max_searches_per_task: int = Field(5, ge=0, le=20)
    max_fetches_per_task: int = Field(3, ge=0, le=20)
    fetch_max_content_tokens: int = Field(12000, ge=1000, le=100000)

    @field_validator("allowed_domains", "blocked_domains")
    @classmethod
    def _domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_domains(value)

    @model_validator(mode="after")
    def _exclusive(self) -> "WebPolicy":
        if self.allowed_domains and self.blocked_domains:
            raise ValueError("allowed_domains와 blocked_domains는 동시에 쓸 수 없음")
        return self


class BusinessProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    business_type: str = ""
    industry: str = Field("", max_length=200)
    offering: str = Field("", max_length=300)
    customers: str = Field("", max_length=300)
    competitors: str = Field("", max_length=300)
    markets: str = Field("", max_length=200)
    audience: str = Field("", max_length=200)
    focus: str = Field("", max_length=1500)

    @field_validator("business_type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value and value not in dict(BUSINESS_TYPES):
            raise ValueError(f"알 수 없는 비즈니스 유형: {value}")
        return value

    @field_validator("industry", "offering", "customers", "competitors", "markets", "audience")
    @classmethod
    def _single_line(cls, value: str) -> str:
        return " ".join(value.split())

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in ("business_type", *dict(PROFILE_FIELDS)))

    def type_label(self) -> str:
        return dict(BUSINESS_TYPES).get(self.business_type, "")

    def lines(self) -> list[str]:
        out = []
        if self.business_type:
            out.append(f"Business type: {self.type_label()}")
        for name, label in PROFILE_FIELDS:
            value = getattr(self, name)
            if value:
                out.append(f"{label}: {value}")
        return out


class WorkspaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = Field("", max_length=500)
    scope: Scope = "research"
    max_send_classification: Classification = "internal"
    redact_pii: bool = True
    budget_monthly_usd: float = Field(0.0, ge=0)
    reuse_max_age_days: int = Field(7, ge=0, le=365)
    knowledge: bool = True
    actions: tuple[str, ...] = ACTION_TYPES
    web: WebPolicy = WebPolicy()
    profile: BusinessProfile = BusinessProfile()

    @field_validator("actions")
    @classmethod
    def _actions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [a for a in value if a not in ACTION_TYPES]
        if unknown:
            raise ValueError(f"알 수 없는 행동 유형: {', '.join(unknown)}")
        return tuple(dict.fromkeys(value))

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not NAME_RE.fullmatch(value):
            raise ValueError("작업 영역 이름은 소문자, 숫자, -, _ 만 쓸 수 있음 (최대 48자)")
        return value

    @model_validator(mode="after")
    def _confidential(self) -> "WorkspaceConfig":
        if self.max_send_classification == "confidential":
            raise ValueError("confidential 등급은 기본 전송 허용 대상으로 둘 수 없음, 실행 시 명시적으로 허용해야 함")
        return self


class InputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sha256: str
    classification: Classification
    size: int
    chars: int
    imported_at: str
    source_encoding: str


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(v) for v in values) + "]"


def render_workspace_toml(cfg: WorkspaceConfig) -> str:
    web = cfg.web
    return (
        f"name = {_toml_str(cfg.name)}\n"
        f"description = {_toml_str(cfg.description)}\n"
        f"scope = {_toml_str(cfg.scope)}\n"
        f"max_send_classification = {_toml_str(cfg.max_send_classification)}\n"
        f"redact_pii = {'true' if cfg.redact_pii else 'false'}\n"
        f"budget_monthly_usd = {cfg.budget_monthly_usd}\n"
        f"reuse_max_age_days = {cfg.reuse_max_age_days}\n"
        f"knowledge = {'true' if cfg.knowledge else 'false'}\n"
        f"actions = {_toml_list(cfg.actions)}\n"
        "\n[web]\n"
        f"allowed_domains = {_toml_list(web.allowed_domains)}\n"
        f"blocked_domains = {_toml_list(web.blocked_domains)}\n"
        f"max_searches_per_task = {web.max_searches_per_task}\n"
        f"max_fetches_per_task = {web.max_fetches_per_task}\n"
        f"fetch_max_content_tokens = {web.fetch_max_content_tokens}\n"
        "\n[profile]\n"
        + "".join(f"{name} = {_toml_str(getattr(cfg.profile, name))}\n"
                  for name in ("business_type", *dict(PROFILE_FIELDS)))
    )


def workspaces_root() -> Path:
    return ensure_private_dir(home() / "workspaces")


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"r{stamp}-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:6]}"


class Workspace:
    def __init__(self, root: Path, config: WorkspaceConfig) -> None:
        self.root = root
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @classmethod
    def create(cls, config: WorkspaceConfig) -> "Workspace":
        base = workspaces_root()
        root = confine(base, config.name)
        if root.exists():
            raise WorkspaceError(f"이미 존재하는 작업 영역: {config.name}")
        ensure_private_dir(root)
        for sub in ("inputs", "outputs", "runs"):
            ensure_private_dir(root / sub)
        write_private_text(root / "workspace.toml", render_workspace_toml(config))
        write_private_text(root / "inputs" / "manifest.json", json.dumps({"files": []}, ensure_ascii=False, indent=2))
        return cls(root, config)

    @classmethod
    def open(cls, name: str) -> "Workspace":
        if not NAME_RE.fullmatch(name):
            raise WorkspaceError(f"올바르지 않은 작업 영역 이름: {name}")
        root = confine(workspaces_root(), name)
        if not root.is_dir():
            raise WorkspaceError(f"작업 영역이 없음: {name}")
        ensure_private_dir(root)
        try:
            raw = read_bytes_nofollow(root / "workspace.toml", 64 * 1024).decode("utf-8")
            config = WorkspaceConfig.model_validate(tomllib.loads(raw))
        except (tomllib.TOMLDecodeError, ValidationError, UnicodeDecodeError) as exc:
            raise WorkspaceError(f"workspace.toml 오류: {exc}") from None
        if config.name != name:
            raise WorkspaceError("workspace.toml의 name이 폴더 이름과 다름")
        return cls(root, config)

    @staticmethod
    def list_names() -> list[str]:
        base = workspaces_root()
        return sorted(p.name for p in base.iterdir() if p.is_dir() and not hostos.is_link(p) and NAME_RE.fullmatch(p.name))

    def save_config(self, config: WorkspaceConfig) -> None:
        if config.name != self.name:
            raise WorkspaceError("작업 영역 이름은 바꿀 수 없음")
        write_private_text(self.root / "workspace.toml", render_workspace_toml(config))
        self.config = config

    def _manifest_path(self) -> Path:
        return confine(self.root, "inputs", "manifest.json")

    def inputs(self) -> list[InputFile]:
        path = self._manifest_path()
        if not path.exists():
            return []
        data = json.loads(read_bytes_nofollow(path, 4 * 1024 * 1024).decode("utf-8"))
        return [InputFile.model_validate(item) for item in data.get("files", [])]

    def _write_manifest(self, files: list[InputFile]) -> None:
        payload = {"files": [f.model_dump() for f in files]}
        write_private_text(self._manifest_path(), json.dumps(payload, ensure_ascii=False, indent=2))

    def import_file(self, source: Path, classification: str, name: str | None = None) -> InputFile:
        if not scope_allows_inputs(self.config.scope):
            raise WorkspaceError(f"이 작업 영역의 활동 범위({self.config.scope})는 로컬 자료를 다루지 않음")
        src = source.expanduser()
        if hostos.is_link(src) or not src.is_file():
            raise WorkspaceError(f"일반 파일만 가져올 수 있음: {source}")
        if src.suffix.lower() not in TEXT_EXTENSIONS:
            raise WorkspaceError(f"지원하지 않는 형식: {src.suffix} (지원: {', '.join(sorted(TEXT_EXTENSIONS))})")
        target_name = name or src.name
        if not FILE_RE.fullmatch(target_name) or target_name == "manifest.json":
            raise WorkspaceError(f"사용할 수 없는 파일 이름: {target_name}")
        problem = portable_name_problem(target_name)
        if problem:
            raise WorkspaceError(f"사용할 수 없는 파일 이름: {problem}")
        raw = read_bytes_nofollow(src, MAX_INPUT_BYTES)
        encoding = "utf-8"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp949")
                encoding = "cp949"
            except UnicodeDecodeError:
                raise WorkspaceError("UTF-8 또는 CP949 텍스트만 지원함") from None
        if "\x00" in text:
            raise WorkspaceError("바이너리 파일은 가져올 수 없음")
        return self._store_input(target_name, text, classification, encoding)

    def _store_input(self, target_name: str, text: str, classification: str, encoding: str) -> InputFile:
        target = confine(self.root, "inputs", target_name)
        write_private_text(target, text)
        entry = InputFile(
            name=target_name,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            classification=classification,
            size=len(text.encode("utf-8")),
            chars=len(text),
            imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
            source_encoding=encoding,
        )
        files = [f for f in self.inputs() if f.name != target_name]
        files.append(entry)
        self._write_manifest(files)
        return entry

    def import_run_report(self, source: "Workspace", run_id: str, classification: str) -> InputFile:
        if not scope_allows_inputs(self.config.scope):
            raise WorkspaceError(f"이 작업 영역의 활동 범위({self.config.scope})는 로컬 자료를 다루지 않음")
        if source.name == self.name:
            raise WorkspaceError("같은 작업 영역의 기록은 '이어서 작업'으로 활용하면 됨")
        record = source.load_run(run_id)
        report = source.read_report(run_id)
        if record is None or not report:
            raise WorkspaceError(f"보고서가 있는 실행 기록이 아님: {source.name}/{run_id}")
        floor = report_classification(record)
        if CLASS_ORDER.index(classification) < CLASS_ORDER.index(floor):
            raise WorkspaceError(f"이 보고서는 {floor} 이상 등급으로만 가져올 수 있음")
        body = strip_report_footer(report)
        goal = " ".join(str(record.get("goal", "")).split())[:200]
        header = (f"> 출처: 작업 영역 {source.name}, 실행 {run_id} ({str(record.get('created', ''))[:10]})\n"
                  f"> 목표: {goal}\n\n")
        return self._store_input(f"{source.name}-{run_id}.md", header + body + "\n", classification, "utf-8")

    def remove_input(self, name: str) -> bool:
        files = self.inputs()
        keep = [f for f in files if f.name != name]
        if len(keep) == len(files):
            return False
        confine(self.root, "inputs", name).unlink(missing_ok=True)
        self._write_manifest(keep)
        return True

    def read_input(self, entry: InputFile) -> str:
        target = confine(self.root, "inputs", entry.name)
        text = read_bytes_nofollow(target, MAX_INPUT_BYTES).decode("utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != entry.sha256:
            raise WorkspaceError(f"입력 파일 무결성 검증 실패(가져온 뒤 변경됨): {entry.name}")
        return text

    def output_dir(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise WorkspaceError("올바르지 않은 실행 ID")
        out = ensure_private_dir(confine(self.root, "outputs", run_id))
        ensure_private_dir(out / "tasks")
        return out

    def save_run(self, run_id: str, record: dict) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise WorkspaceError("올바르지 않은 실행 ID")
        path = confine(self.root, "runs", f"{run_id}.json")
        write_private_text(path, json.dumps(record, ensure_ascii=False, indent=2, default=str))
        return path

    def load_run(self, run_id: str) -> dict | None:
        if not RUN_ID_RE.fullmatch(run_id):
            return None
        path = confine(self.root, "runs", f"{run_id}.json")
        if not path.exists():
            return None
        return json.loads(read_bytes_nofollow(path, 8 * 1024 * 1024).decode("utf-8"))

    def recent_runs(self, limit: int = 20) -> list[dict]:
        runs_dir = confine(self.root, "runs")
        names = sorted((p.stem for p in runs_dir.glob("r*.json") if RUN_ID_RE.fullmatch(p.stem)), reverse=True)
        out = []
        for run_id in names[:limit]:
            record = self.load_run(run_id)
            if record:
                out.append(record)
        return out

    @classmethod
    def delete(cls, name: str) -> None:
        if not NAME_RE.fullmatch(name):
            raise WorkspaceError(f"올바르지 않은 작업 영역 이름: {name}")
        root = confine(workspaces_root(), name)
        if not root.is_dir():
            raise WorkspaceError(f"작업 영역이 없음: {name}")
        _remove_tree(root)

    def delete_run(self, run_id: str) -> bool:
        if not RUN_ID_RE.fullmatch(run_id):
            raise WorkspaceError("올바르지 않은 실행 ID")
        removed = False
        out = confine(self.root, "outputs", run_id)
        if out.exists():
            _remove_tree(out)
            removed = True
        record = confine(self.root, "runs", f"{run_id}.json")
        if record.exists():
            record.unlink()
            removed = True
        return removed

    def has_brief(self) -> bool:
        return confine(self.root, "brief.md").exists()

    def read_brief(self) -> str:
        path = confine(self.root, "brief.md")
        if not path.exists():
            return ""
        return read_bytes_nofollow(path, MAX_BRIEF_CHARS * 4).decode("utf-8")

    def write_brief(self, text: str) -> int:
        text = text.strip()
        if len(text) > MAX_BRIEF_CHARS:
            raise WorkspaceError(f"지침은 {MAX_BRIEF_CHARS:,}자 이하여야 함")
        path = confine(self.root, "brief.md")
        if not text:
            if path.exists():
                path.unlink()
            return 0
        write_private_text(path, text + "\n")
        return len(text)

    def read_report(self, run_id: str) -> str | None:
        if not RUN_ID_RE.fullmatch(run_id):
            return None
        path = confine(self.root, "outputs", run_id, "report.md")
        if not path.exists():
            return None
        return read_bytes_nofollow(path, 8 * 1024 * 1024).decode("utf-8")

    def report_size(self, run_id: str) -> int | None:
        if not RUN_ID_RE.fullmatch(run_id):
            return None
        path = confine(self.root, "outputs", run_id, "report.md")
        try:
            st = path.lstat()
        except FileNotFoundError:
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None

    def task_notes(self, run_id: str) -> list[tuple[str, str]]:
        if not RUN_ID_RE.fullmatch(run_id):
            return []
        folder = confine(self.root, "outputs", run_id)
        tasks_dir = folder / "tasks"
        if not tasks_dir.is_dir() or hostos.is_link(tasks_dir):
            return []
        ids = sorted(
            (p.stem for p in tasks_dir.glob("t*.md") if re.fullmatch(r"t\d{1,2}", p.stem)),
            key=lambda t: int(t[1:]),
        )
        notes = []
        for task_id in ids:
            text = self.read_task_artifact(run_id, task_id)
            if text is not None:
                notes.append((task_id, text))
        return notes

    def read_task_artifact(self, run_id: str, task_id: str) -> str | None:
        if not RUN_ID_RE.fullmatch(run_id) or not re.fullmatch(r"t\d{1,2}", task_id):
            return None
        path = confine(self.root, "outputs", run_id, "tasks", f"{task_id}.md")
        if not path.exists():
            return None
        return read_bytes_nofollow(path, 4 * 1024 * 1024).decode("utf-8")


def strip_report_footer(report: str) -> str:
    cut = max(report.rfind(marker) for marker in REPORT_FOOTERS)
    return (report[:cut] if cut >= 0 else report).strip()


def report_classification(record: dict) -> str:
    return "confidential" if record.get("allow_confidential") else "internal"


def export_file_name(record: dict) -> str:
    plan = record.get("plan") or {}
    title = EXPORT_NAME_RE.sub(" ", str(plan.get("title") or record.get("goal") or "")).strip(" .")
    title = " ".join(title.split())[:60].rstrip(" .")
    day = str(record.get("created", ""))[:10]
    return f"{day} {title}".strip() if title else str(record.get("run_id", "report"))


def check_export_dir(raw: str) -> Path:
    if not raw.strip() or "\x00" in raw:
        raise WorkspaceError("복사할 폴더를 입력해야 함")
    folder = Path(os.path.expanduser(raw.strip()))
    if not folder.is_absolute():
        start = "~\\ 또는 C:\\ 처럼" if os.sep == "\\" else "~/ 또는 / 로"
        raise WorkspaceError(f"폴더는 {start} 시작하는 절대 경로여야 함")
    try:
        st = folder.lstat()
    except FileNotFoundError:
        raise WorkspaceError(f"폴더가 없음: {raw}") from None
    if hostos.stat_is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise WorkspaceError("심볼릭 링크가 아닌 실제 폴더만 고를 수 있음")
    if not hostos.owned_by_me(folder, st):
        raise WorkspaceError("현재 사용자 소유 폴더에만 복사할 수 있음")
    resolved = folder.resolve(strict=True)
    data = home_path().expanduser().resolve()
    if resolved == data or resolved.is_relative_to(data):
        raise WorkspaceError("claubiz 데이터 폴더 안으로는 복사할 수 없음")
    return resolved


def export_report(ws: Workspace, run_id: str, raw_dir: str) -> tuple[Path, str]:
    record = ws.load_run(run_id)
    report = ws.read_report(run_id)
    if record is None or not report:
        raise WorkspaceError("이 실행에는 복사할 보고서가 없음")
    folder = check_export_dir(raw_dir)
    data = report.encode("utf-8")
    stem = export_file_name(record)
    for attempt in range(1, 100):
        name = f"{stem}.md" if attempt == 1 else f"{stem} ({attempt}).md"
        target = folder / name
        try:
            fd = hostos.open_fd(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target, hashlib.sha256(data).hexdigest()
    raise WorkspaceError("같은 이름의 파일이 너무 많아 복사하지 못함")


def _remove_tree(path: Path) -> None:
    if hostos.is_link(path) or not path.is_dir():
        raise WorkspaceError(f"삭제 대상이 디렉터리가 아님: {path.name}")
    try:
        hostos.remove_tree(path)
    except hostos.HostError as exc:
        raise WorkspaceError(str(exc)) from None
