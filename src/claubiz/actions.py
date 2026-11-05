import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .paths import confine, ensure_private_dir, read_bytes_nofollow, write_private_text
from .policy import Classification
from .workspace import ACTION_TYPES, EXPORT_NAME_RE, RUN_ID_RE, Workspace, WorkspaceError, check_export_dir

ACTION_ID_RE = re.compile(r"^a[0-9a-f]{10}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MOMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)*$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")
MAX_PROPOSALS = 3
EXTENSIONS = {"email": "eml", "event": "ics", "table": "csv", "doc": "md"}
TYPE_LABELS = {"email": "메일 초안", "event": "일정", "table": "표", "doc": "문서"}
OPENABLE = frozenset(EXTENSIONS.values())

PROPOSAL_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actions"],
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "title", "reason", "to", "cc", "subject", "body", "start", "end", "location",
                             "columns", "rows"],
                "properties": {
                    "type": {"type": "string", "enum": list(ACTION_TYPES)},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "location": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                },
            },
        },
    },
}


class ActionError(Exception):
    pass


def _clean_line(value: str, limit: int) -> str:
    return " ".join(CONTROL_RE.sub(" ", value).split())[:limit]


def _clean_block(value: str, limit: int) -> str:
    text = CONTROL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return text.strip()[:limit]


class EmailDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[str] = Field(default_factory=list, max_length=10)
    cc: list[str] = Field(default_factory=list, max_length=10)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)

    @field_validator("to", "cc")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        out = []
        for raw in value:
            address = raw.strip()
            if not address:
                continue
            if not ADDRESS_RE.fullmatch(address):
                raise ValueError(f"올바른 메일 주소가 아님: {address[:80]}")
            if address not in out:
                out.append(address)
        return out

    @field_validator("subject")
    @classmethod
    def _subject(cls, value: str) -> str:
        return _clean_line(value, 200)


class EventDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str = ""
    location: str = Field("", max_length=200)
    body: str = Field("", max_length=4000)

    @field_validator("start", "end")
    @classmethod
    def _moment(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        if not (DAY_RE.fullmatch(value) or MOMENT_RE.fullmatch(value)):
            raise ValueError(f"날짜는 YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM 형식이어야 함: {value[:40]}")
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"올바른 날짜가 아님: {value}") from None
        return value

    def bounds(self) -> tuple[datetime | date, datetime | date]:
        if not self.start:
            raise ValueError("시작 날짜가 필요함")
        all_day = DAY_RE.fullmatch(self.start) is not None
        start = date.fromisoformat(self.start) if all_day else datetime.fromisoformat(self.start)
        if self.end and (DAY_RE.fullmatch(self.end) is not None) == all_day:
            end = date.fromisoformat(self.end) if all_day else datetime.fromisoformat(self.end)
            if all_day:
                end = end + timedelta(days=1)
            if end > start:
                return start, end
        return start, start + (timedelta(days=1) if all_day else timedelta(hours=1))


class TableDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1, max_length=20)
    rows: list[list[str]] = Field(default_factory=list, max_length=500)

    @field_validator("columns")
    @classmethod
    def _columns(cls, value: list[str]) -> list[str]:
        return [_clean_line(c, 100) for c in value]

    @field_validator("rows")
    @classmethod
    def _rows(cls, value: list[list[str]]) -> list[list[str]]:
        return [[_clean_block(cell, 2000) for cell in row[:20]] for row in value]


class DocDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=40000)


DRAFTS: dict[str, type[BaseModel]] = {"email": EmailDraft, "event": EventDraft, "table": TableDraft, "doc": DocDraft}


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    type: str
    title: str
    reason: str = ""
    payload: dict
    status: str = "pending"
    created: str
    decided: str = ""
    output: str = ""
    sha256: str = ""
    classification: Classification = "internal"

    def draft(self) -> BaseModel:
        return DRAFTS[self.type].model_validate(self.payload)


def new_action_id(seed: str) -> str:
    return "a" + hashlib.sha256(f"{seed}{time.time_ns()}".encode()).hexdigest()[:10]


def _payload(kind: str, item: dict) -> dict:
    if kind == "email":
        return {"to": item.get("to") or [], "cc": item.get("cc") or [], "subject": str(item.get("subject", "")),
                "body": _clean_block(str(item.get("body", "")), 20000)}
    if kind == "event":
        return {"start": str(item.get("start", "")), "end": str(item.get("end", "")),
                "location": _clean_line(str(item.get("location", "")), 200),
                "body": _clean_block(str(item.get("body", "")), 4000)}
    if kind == "table":
        columns = [str(c) for c in item.get("columns") or []]
        width = len(columns)
        rows = []
        for row in item.get("rows") or []:
            cells = [str(c) for c in row][:width]
            rows.append(cells + [""] * (width - len(cells)))
        return {"columns": columns, "rows": rows}
    return {"body": _clean_block(str(item.get("body", "")), 40000)}


def parse_proposals(raw: str, *, run_id: str, allowed: tuple[str, ...], classification: str
                    ) -> tuple[list[Action], list[str]]:
    data = json.loads(raw)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    actions: list[Action] = []
    problems: list[str] = []
    for item in data.get("actions") or []:
        if len(actions) >= MAX_PROPOSALS:
            break
        kind = str(item.get("type", ""))
        title = _clean_line(str(item.get("title", "")), 80)
        if kind not in allowed or not title:
            problems.append(f"허용되지 않거나 제목 없는 제안 제외 ({kind or '-'})")
            continue
        payload = _payload(kind, item)
        try:
            draft = DRAFTS[kind].model_validate(payload)
            if kind == "event":
                draft.bounds()
        except (ValidationError, ValueError) as exc:
            message = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
            problems.append(f"{TYPE_LABELS[kind]} 제안 제외: {str(message).removeprefix('Value error, ')[:120]}")
            continue
        actions.append(Action(
            id=new_action_id(title), run_id=run_id, type=kind, title=title,
            reason=_clean_line(str(item.get("reason", "")), 500), payload=draft.model_dump(), created=stamp,
            classification=classification,
        ))
    return actions, problems


def _ics_text(value: str) -> str:
    value = CONTROL_RE.sub("", value)
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _fold(line: str) -> list[str]:
    out: list[str] = []
    current = ""
    size = 0
    limit = 75
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += ch
        size += width
    out.append(current)
    return out


def _ics_value(moment: datetime | date) -> tuple[str, str]:
    if isinstance(moment, datetime):
        return "", moment.strftime("%Y%m%dT%H%M%S")
    return ";VALUE=DATE", moment.strftime("%Y%m%d")


def render_event(title: str, draft: EventDraft) -> bytes:
    start, end = draft.bounds()
    start_param, start_value = _ics_value(start)
    end_param, end_value = _ics_value(end)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//claubiz//KO",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@claubiz.invalid",
        f"DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART{start_param}:{start_value}",
        f"DTEND{end_param}:{end_value}",
        f"SUMMARY:{_ics_text(title)}",
    ]
    if draft.location:
        lines.append(f"LOCATION:{_ics_text(draft.location)}")
    if draft.body:
        lines.append(f"DESCRIPTION:{_ics_text(draft.body)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    folded = [part for line in lines for part in _fold(line)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")


def render_email(draft: EmailDraft) -> bytes:
    message = EmailMessage(policy=SMTP)
    if draft.to:
        message["To"] = ", ".join(draft.to)
    if draft.cc:
        message["Cc"] = ", ".join(draft.cc)
    message["Subject"] = draft.subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="claubiz.invalid")
    message["X-Unsent"] = "1"
    message.set_content(draft.body, subtype="plain", charset="utf-8")
    return message.as_bytes()


def _safe_cell(cell: str) -> str:
    if cell.startswith(FORMULA_PREFIX) and not NUMBER_RE.fullmatch(cell):
        return "'" + cell
    return cell


def render_table(draft: TableDraft) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_safe_cell(c) for c in draft.columns])
    for row in draft.rows:
        writer.writerow([_safe_cell(c) for c in row])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def render_doc(title: str, draft: DocDraft) -> bytes:
    body = draft.body.strip()
    if not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return (body + "\n").encode("utf-8")


def render_action(action: Action) -> bytes:
    draft = action.draft()
    if isinstance(draft, EmailDraft):
        return render_email(draft)
    if isinstance(draft, EventDraft):
        return render_event(action.title, draft)
    if isinstance(draft, TableDraft):
        return render_table(draft)
    return render_doc(action.title, draft)


def preview_markdown(action: Action) -> str:
    draft = action.draft()
    lines = [f"**{TYPE_LABELS.get(action.type, action.type)}** · {action.title}", ""]
    if action.reason:
        lines += [f"> 제안 이유: {action.reason}", ""]
    if isinstance(draft, EmailDraft):
        lines += [f"- 받는 사람: {', '.join(draft.to) or '(비어 있음, 메일 앱에서 입력)'}"]
        if draft.cc:
            lines.append(f"- 참조: {', '.join(draft.cc)}")
        lines += [f"- 제목: {draft.subject}", "", "```text", draft.body.replace("```", "'''"), "```"]
    elif isinstance(draft, EventDraft):
        start, end = draft.bounds()
        lines += [f"- 시작: {start}", f"- 끝: {end}"]
        if draft.location:
            lines.append(f"- 장소: {draft.location}")
        if draft.body:
            lines += ["", draft.body]
    elif isinstance(draft, TableDraft):
        def cell(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ")

        lines.append("| " + " | ".join(cell(c) for c in draft.columns) + " |")
        lines.append("|" + "---|" * len(draft.columns))
        lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in draft.rows[:50]]
        if len(draft.rows) > 50:
            lines.append(f"\n(외 {len(draft.rows) - 50}행)")
    else:
        lines += ["---", "", draft.body]
    return "\n".join(lines)


def _write_new(folder: Path, stem: str, ext: str, data: bytes) -> Path:
    for attempt in range(1, 100):
        name = f"{stem}.{ext}" if attempt == 1 else f"{stem} ({attempt}).{ext}"
        target = folder / name
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
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
        return target
    raise ActionError("같은 이름의 파일이 너무 많아 만들지 못함")


def _stem(action: Action) -> str:
    title = EXPORT_NAME_RE.sub(" ", action.title).strip(" .")
    title = " ".join(title.split())[:60].rstrip(" .") or action.id
    return f"{action.created[:10]} {title}"


class ActionStore:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def _dir(self) -> Path:
        return ensure_private_dir(confine(self.ws.root, "actions"))

    def outbox(self) -> Path:
        return ensure_private_dir(confine(self.ws.root, "outbox"))

    def _path(self, action_id: str) -> Path:
        if not ACTION_ID_RE.fullmatch(action_id):
            raise ActionError("올바르지 않은 제안 ID")
        return confine(self._dir(), f"{action_id}.json")

    def save(self, action: Action) -> None:
        write_private_text(self._path(action.id), json.dumps(action.model_dump(), ensure_ascii=False, indent=1))

    def get(self, action_id: str) -> Action | None:
        path = self._path(action_id)
        if not path.exists():
            return None
        return Action.model_validate_json(read_bytes_nofollow(path, 2 * 1024 * 1024))

    def list(self) -> list[Action]:
        folder = confine(self.ws.root, "actions")
        if not folder.is_dir():
            return []
        out = []
        for path in folder.glob("a*.json"):
            if not ACTION_ID_RE.fullmatch(path.stem):
                continue
            try:
                out.append(Action.model_validate_json(read_bytes_nofollow(path, 2 * 1024 * 1024)))
            except (ValidationError, OSError):
                continue
        out.sort(key=lambda a: (a.created, a.id), reverse=True)
        return out

    def pending(self) -> int:
        return sum(1 for a in self.list() if a.status == "pending")

    def output_path(self, action: Action) -> Path | None:
        if not action.output:
            return None
        path = confine(self.outbox(), action.output)
        try:
            st = path.lstat()
        except FileNotFoundError:
            return None
        return path if stat.S_ISREG(st.st_mode) else None

    def approve(self, action_id: str) -> Action:
        action = self.get(action_id)
        if action is None:
            raise ActionError("제안을 찾을 수 없음")
        if action.status != "pending":
            raise ActionError("이미 처리된 제안임")
        try:
            data = render_action(action)
        except (ValidationError, ValueError) as exc:
            raise ActionError(f"제안 내용이 올바르지 않음: {exc}") from None
        target = _write_new(self.outbox(), _stem(action), EXTENSIONS[action.type], data)
        done = action.model_copy(update={
            "status": "done", "decided": datetime.now(UTC).isoformat(timespec="seconds"), "output": target.name,
            "sha256": hashlib.sha256(data).hexdigest(),
        })
        self.save(done)
        return done

    def reject(self, action_id: str) -> Action:
        action = self.get(action_id)
        if action is None:
            raise ActionError("제안을 찾을 수 없음")
        if action.status != "pending":
            raise ActionError("이미 처리된 제안임")
        done = action.model_copy(update={"status": "rejected",
                                         "decided": datetime.now(UTC).isoformat(timespec="seconds")})
        self.save(done)
        return done

    def remove(self, action_id: str) -> bool:
        action = self.get(action_id)
        if action is None:
            return False
        output = self.output_path(action)
        if output is not None:
            output.unlink(missing_ok=True)
        self._path(action_id).unlink(missing_ok=True)
        return True

    def remove_run(self, run_id: str) -> int:
        if not RUN_ID_RE.fullmatch(run_id):
            return 0
        removed = 0
        for action in self.list():
            if action.run_id == run_id and self.remove(action.id):
                removed += 1
        return removed

    def count_run(self, run_id: str) -> int:
        return sum(1 for a in self.list() if a.run_id == run_id)

    def export(self, action_id: str, raw_dir: str) -> tuple[Path, str]:
        action = self.get(action_id)
        if action is None or action.status != "done":
            raise ActionError("승인해서 만든 파일만 복사할 수 있음")
        source = self.output_path(action)
        if source is None:
            raise ActionError("만든 파일이 없어졌음")
        data = read_bytes_nofollow(source, 16 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != action.sha256:
            raise ActionError("만든 파일이 승인 뒤 바뀌어 복사하지 않음")
        try:
            folder = check_export_dir(raw_dir)
        except WorkspaceError as exc:
            raise ActionError(str(exc)) from None
        target = _write_new(folder, source.stem, source.suffix.lstrip("."), data)
        return target, action.sha256

    def open_output(self, action_id: str) -> Path:
        action = self.get(action_id)
        if action is None or action.status != "done":
            raise ActionError("승인해서 만든 파일만 열 수 있음")
        path = self.output_path(action)
        if path is None:
            raise ActionError("만든 파일이 없어졌음")
        data = read_bytes_nofollow(path, 16 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != action.sha256:
            raise ActionError("만든 파일이 승인 뒤 바뀌어 열지 않음")
        if path.suffix.lstrip(".") not in OPENABLE:
            raise ActionError("열 수 없는 형식")
        if sys.platform != "darwin":
            raise ActionError("파일 열기는 macOS에서만 지원함")
        subprocess.run(["/usr/bin/open", str(path)], check=True, timeout=15,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return path
