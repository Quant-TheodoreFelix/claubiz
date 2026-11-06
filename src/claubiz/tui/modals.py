import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Markdown,
    OptionList,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option

from .. import hostos
from ..actions import ActionError, ActionStore
from ..audit import AuditLog
from ..config import MODEL_CHOICES
from ..digest import DEFAULT_DIGEST_CAP, DEFAULT_DIGEST_MODEL, DigestError, DigestSpec, DigestStore, make_spec
from ..paths import home_path
from ..playbooks import PlaybookError, PlaybookSpec, save_custom_playbook
from ..policy import PolicyError
from ..schedule import ScheduleError, parse_schedule
from ..workspace import (
    ACTION_TYPES,
    BUSINESS_TYPES,
    CLASS_ORDER,
    MAX_BRIEF_CHARS,
    MAX_INPUT_BYTES,
    NAME_RE,
    TEXT_EXTENSIONS,
    BusinessProfile,
    WebPolicy,
    Workspace,
    WorkspaceConfig,
    WorkspaceError,
    export_file_name,
    export_report,
    report_classification,
)
from .theme import CLAY, ERROR, FAINT, MUTED, PERIWINKLE
from .widgets import SafeMarkdownViewer, human_size, status_text

BriefGenerator = Callable[..., Awaitable[tuple[str, float]]]

SCOPE_OPTIONS = [
    ("조사: 웹 + 로컬 자료", "research"),
    ("웹 전용", "web-only"),
    ("자료 전용 (웹 차단)", "inputs-only"),
]
CLASS_OPTIONS = [("internal 이하 전송", "internal"), ("public만 전송", "public")]
WEB_MODES = [("제한 없음", "none"), ("허용 목록만 사용", "allow"), ("차단 목록 제외", "block")]
INPUT_CLASSES = [("internal (내부)", "internal"), ("public (공개)", "public"), ("confidential (기밀)", "confidential")]
BRIEF_MODES = [("없음", "none"), ("직접 작성", "manual"), ("AI가 초안 작성", "ai")]
BRIEF_HELP = {
    "manual": "모든 에이전트 요청에 함께 전달되는 지침. 회사 소개, 독자, 선호 출처, 작성 방식, 용어 등을 적어줘",
    "ai": "참고할 내용을 적고(선택) 'AI 초안 만들기'를 누르면 초안이 여기에 채워짐. 확인하고 고친 뒤 만들기",
}
TYPE_OPTIONS = [("미지정", ""), *((label, key) for key, label in BUSINESS_TYPES)]
PROFILE_INPUTS = (
    ("industry", "업종·세부 분야", "예: B2B 전자계약 SaaS", 200),
    ("offering", "주요 제품·서비스", "예: 전자서명, 계약 관리", 300),
    ("customers", "주요 고객", "예: 중견 제조사 법무팀", 300),
    ("competitors", "경쟁사·비교 대상", "예: 모두싸인, 글로싸인, DocuSign", 300),
    ("markets", "관심 지역·시장", "예: 국내, 일본", 200),
    ("audience", "보고서 독자", "예: 대표, 영업팀장", 200),
)
FOCUS_HINT = "예: 매주 경쟁사 가격과 신기능 확인, 전자문서법 개정 동향, 공공 조달 공고 중 전자계약 관련 건"
WEEKDAY_NAMES = "월화수목금토일"
DIGEST_MODELS = [("기본 설정", ""), *((f"{label} (추천)" if model == DEFAULT_DIGEST_MODEL else label, model)
                                      for model, label in MODEL_CHOICES)]
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "Library", "Applications"})
INDEX_LIMIT = 4000
INDEX_DEPTH = 5
MATCH_LIMIT = 60
LIST_LIMIT = 200


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())


def web_note(mode: str, domains: list[str] | tuple[str, ...]) -> str:
    if mode == "allow" and domains:
        return "use only these domains: " + ", ".join(domains)
    if mode == "block" and domains:
        return "never use these domains: " + ", ".join(domains)
    return "no domain restriction"


class FormModal(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기")]

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def show_error(self, message: str) -> None:
        self.query_one(".modal-error", Static).update(Text(message))


def profile_grid(profile: BusinessProfile) -> ComposeResult:
    yield Label("비즈니스 유형")
    yield Select(TYPE_OPTIONS, value=profile.business_type, allow_blank=False, id="f-type")
    for name, label, hint, limit in PROFILE_INPUTS:
        yield Label(label)
        yield Input(getattr(profile, name), placeholder=hint, id=f"f-{name}", max_length=limit)


def read_profile(screen: ModalScreen) -> BusinessProfile:
    data = {name: screen.query_one(f"#f-{name}", Input).value for name, *_ in PROFILE_INPUTS}
    data["business_type"] = str(screen.query_one("#f-type", Select).value)
    data["focus"] = screen.query_one("#f-focus", TextArea).text
    return BusinessProfile.model_validate(data)


def automation_actions(enabled: bool, current: tuple[str, ...]) -> tuple[str, ...]:
    if not enabled:
        return ()
    return current or ACTION_TYPES


class CreateWorkspaceModal(FormModal):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기"), Binding("ctrl+s", "submit", "만들기")]

    def __init__(self, generator: BriefGenerator | None = None) -> None:
        super().__init__()
        self.generator = generator
        self.generated = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-box", id="create-box"):
            yield Static("새 작업 영역", classes="modal-title")
            yield Static("고객사나 프로젝트별로 예산, 출처 정책, 자료, 실행 기록을 분리함", classes="modal-sub")
            with Grid(classes="form-grid"):
                yield Label("이름")
                yield Input(placeholder="acme  (소문자, 숫자, -, _)", id="f-name", max_length=48)
                yield Label("설명")
                yield Input(placeholder="예: 고객사 A 시장 조사", id="f-desc", max_length=500)
            yield Static("업무 프로필 · 모든 에이전트가 무엇이 중요한지 판단하는 기준 (비워도 됨)", classes="field-title")
            with Grid(classes="form-grid"):
                yield from profile_grid(BusinessProfile())
            yield Static("반복 업무·관심 이슈 · 정기 정리와 행동 제안의 기준", classes="field-title")
            yield TextArea(id="f-focus", soft_wrap=True, classes="guide-area focus-area")
            yield Static("정책과 자동화", classes="field-title")
            with Grid(classes="form-grid"):
                yield Label("활동 범위")
                yield Select(SCOPE_OPTIONS, value="research", allow_blank=False, id="f-scope")
                yield Label("전송 허용 등급")
                yield Select(CLASS_OPTIONS, value="internal", allow_blank=False, id="f-class")
                yield Label("웹 출처 정책")
                yield Select(WEB_MODES, value="none", allow_blank=False, id="f-webmode")
                yield Label("도메인", id="l-domains")
                yield Input(placeholder="example.com, korea.kr", id="f-domains")
                yield Label("작업당 검색 / 페치")
                with Horizontal(classes="pair"):
                    yield Input("5", type="integer", id="f-search", max_length=2)
                    yield Input("3", type="integer", id="f-fetch", max_length=2)
                yield Label("월 예산 (USD)")
                yield Input(placeholder="0 = 전체 예산 공유", type="number", id="f-budget")
                yield Label("개인정보 가림")
                yield Switch(value=True, id="f-redact")
                yield Label("누적 지식")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=True, id="f-knowledge")
                    yield SwitchLabel("출처 있는 사실을 쌓아 다음 실행에 재사용", "f-knowledge")
                yield Label("행동 제안")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=True, id="f-actions")
                    yield SwitchLabel("메일 초안·일정·표·문서 제안 (승인 시 파일 생성)", "f-actions")
                yield Label("작업 영역 지침")
                modes = BRIEF_MODES if self.generator else BRIEF_MODES[:2]
                yield Select(modes, value="none", allow_blank=False, id="f-brief-mode")
            with Vertical(id="brief-area"):
                yield Static("", id="brief-help", classes="modal-sub")
                yield TextArea(id="f-brief", soft_wrap=True, show_line_numbers=False)
                with Horizontal(id="brief-actions"):
                    yield Button("AI 초안 만들기", id="gen", classes="ghost")
                    yield Static("", id="gen-state")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("만들기", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#brief-area").display = False
        self.query_one("#l-domains").display = False
        self.query_one("#f-domains").display = False
        self.query_one("#f-focus", TextArea).placeholder = FOCUS_HINT
        self.query_one("#f-name", Input).focus()

    @on(Select.Changed, "#f-brief-mode")
    def _brief_mode(self, event: Select.Changed) -> None:
        mode = str(event.value)
        self.query_one("#brief-area").display = mode != "none"
        self.query_one("#brief-actions").display = mode == "ai"
        self.query_one("#brief-help", Static).update(Text(BRIEF_HELP.get(mode, ""), style=MUTED))
        self.query_one("#f-brief", TextArea).placeholder = (
            "예: 고객사 A는 B2B 결제 솔루션 기업, 보고서는 임원용 1~2쪽, 금액은 원화 기준" if mode == "ai"
            else "예: 독자는 영업팀장. 결론부터, 표 위주, 출처는 공식 자료 우선"
        )
        if mode != "none":
            self.call_after_refresh(self.query_one("#create-box", VerticalScroll).scroll_end, animate=False)

    def _web(self) -> tuple[str, list[str]]:
        mode = str(self.query_one("#f-webmode", Select).value)
        domains = [d for d in re.split(r"[,\s]+", self.query_one("#f-domains", Input).value) if d]
        return mode, domains

    @on(Button.Pressed, "#gen")
    def _generate(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        if not NAME_RE.fullmatch(name):
            self.show_error("AI 초안을 만들려면 먼저 올바른 이름을 입력해야 함")
            return
        if self.generator is None:
            return
        mode, domains = self._web()
        try:
            profile = read_profile(self)
        except ValidationError as exc:
            self.show_error(_validation_message(exc))
            return
        self.query_one("#gen", Button).disabled = True
        self.query_one("#gen-state", Static).update(Text("초안 작성 중…", style=MUTED))
        self.show_error("")
        self._run_generation(
            name=name, description=self.query_one("#f-desc", Input).value.strip(),
            scope=str(self.query_one("#f-scope", Select).value), web_note=web_note(mode, domains),
            hints=self.query_one("#f-brief", TextArea).text, redact=self.query_one("#f-redact", Switch).value,
            profile=profile.lines(),
        )

    @work(exclusive=True, group="brief", exit_on_error=False)
    async def _run_generation(self, **kwargs) -> None:
        try:
            text, cost = await self.generator(**kwargs)
        except Exception as exc:
            with suppress(NoMatches):
                self.show_error(f"AI 초안 실패: {str(exc)[:200]}")
                self.query_one("#gen-state", Static).update("")
        else:
            with suppress(NoMatches):
                self.query_one("#f-brief", TextArea).text = text
                self.generated = True
                self.query_one("#create-box", VerticalScroll).scroll_end(animate=False)
                self.query_one("#gen-state", Static).update(
                    Text(f"초안 완료 · ${cost:.4f} · 확인하고 고친 뒤 만들기", style=MUTED)
                )
        finally:
            with suppress(NoMatches):
                self.query_one("#gen", Button).disabled = False

    @on(Select.Changed, "#f-webmode")
    def _web_mode(self, event: Select.Changed) -> None:
        field = self.query_one("#f-domains", Input)
        visible = event.value != "none"
        field.display = visible
        self.query_one("#l-domains").display = visible
        field.placeholder = {
            "allow": "이 도메인에서만 검색: example.com, korea.kr",
            "block": "제외할 도메인: spam.example.com",
        }.get(str(event.value), "example.com, korea.kr")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted)
    def action_submit(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        mode, domains = self._web()
        brief_mode = str(self.query_one("#f-brief-mode", Select).value)
        brief = self.query_one("#f-brief", TextArea).text.strip() if brief_mode != "none" else ""
        if brief_mode == "ai" and not self.generated:
            self.show_error("'AI 초안 만들기'로 초안을 먼저 만들거나 지침 방식을 바꿔야 함")
            return
        if len(brief) > MAX_BRIEF_CHARS:
            self.show_error(f"지침은 {MAX_BRIEF_CHARS:,}자 이하여야 함")
            return
        if mode != "none" and not domains:
            self.show_error("출처 정책을 쓰려면 도메인을 하나 이상 입력해야 함")
            return
        try:
            web = WebPolicy(
                allowed_domains=tuple(domains) if mode == "allow" else (),
                blocked_domains=tuple(domains) if mode == "block" else (),
                max_searches_per_task=int(self.query_one("#f-search", Input).value or 0),
                max_fetches_per_task=int(self.query_one("#f-fetch", Input).value or 0),
            )
            config = WorkspaceConfig(
                name=name,
                description=self.query_one("#f-desc", Input).value.strip(),
                scope=str(self.query_one("#f-scope", Select).value),
                max_send_classification=str(self.query_one("#f-class", Select).value),
                redact_pii=self.query_one("#f-redact", Switch).value,
                budget_monthly_usd=float(self.query_one("#f-budget", Input).value or 0),
                knowledge=self.query_one("#f-knowledge", Switch).value,
                actions=automation_actions(self.query_one("#f-actions", Switch).value, ACTION_TYPES),
                web=web,
                profile=read_profile(self),
            )
            ws = Workspace.create(config)
        except ValidationError as exc:
            self.show_error(_validation_message(exc))
            return
        except (WorkspaceError, PolicyError, ValueError) as exc:
            self.show_error(str(exc))
            return
        audit = AuditLog()
        audit.append("workspace.created", name=ws.name, scope=config.scope, web=web.model_dump(),
                     business_type=config.profile.business_type, knowledge=config.knowledge,
                     actions=list(config.actions), via="tui")
        if brief:
            chars = ws.write_brief(brief)
            audit.append("workspace.brief_set", workspace=ws.name, chars=chars, source=brief_mode, via="tui")
        self.dismiss(ws.name)


@dataclass(frozen=True)
class FileEntry:
    path: Path
    is_dir: bool
    size: int
    label: str


def _excluded(path: Path, excluded: Path) -> bool:
    try:
        return path.resolve() == excluded or path.resolve().is_relative_to(excluded)
    except OSError:
        return True


def scan_files(base: Path, excluded: Path, limit: int = INDEX_LIMIT, depth: int = INDEX_DEPTH) -> list[FileEntry]:
    found: list[FileEntry] = []
    queue: deque[tuple[Path, int]] = deque([(base, 0)])
    while queue and len(found) < limit:
        folder, level = queue.popleft()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or len(found) >= limit:
                continue
            try:
                if hostos.stat_is_link(entry.stat(follow_symlinks=False)):
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if level < depth and entry.name not in SKIP_DIRS and not _excluded(path, excluded):
                        queue.append((path, level + 1))
                elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in TEXT_EXTENSIONS:
                    rel = os.path.relpath(path, base)
                    found.append(FileEntry(path, False, entry.stat(follow_symlinks=False).st_size, rel))
            except OSError:
                continue
    return found


def _subsequence(query: str, text: str) -> bool:
    it = iter(text)
    return all(ch in it for ch in query)


def match_files(index: list[FileEntry], query: str, limit: int = MATCH_LIMIT) -> list[FileEntry]:
    q = query.lower()
    ranked = []
    for entry in index:
        name = entry.path.name.lower()
        rel = entry.label.lower()
        if name.startswith(q):
            score = 0
        elif q in name:
            score = 1
        elif q in rel:
            score = 2
        elif _subsequence(q, name):
            score = 3
        else:
            continue
        ranked.append((score, len(rel), rel, entry))
    ranked.sort(key=lambda r: r[:3])
    return [r[3] for r in ranked[:limit]]


SEPARATORS = "/\\" if os.sep == "\\" else "/"
DRIVE_RE = re.compile(r"^[A-Za-z]:")


def folder_hint(path: Path | str) -> str:
    text = str(path).replace(str(Path.home()), "~", 1)
    return text.rstrip(SEPARATORS) + os.sep


def looks_like_path(raw: str) -> bool:
    if raw.startswith(("~", "./", "../")) or any(sep in raw for sep in SEPARATORS):
        return True
    return os.sep == "\\" and bool(DRIVE_RE.match(raw))


def list_directory(raw: str, base: Path, excluded: Path) -> list[FileEntry]:
    expanded = os.path.expanduser(raw if raw != "~" else "~" + os.sep)
    cut = max(expanded.rfind(sep) for sep in SEPARATORS)
    folder_text, prefix = expanded[: cut + 1], expanded[cut + 1 :]
    folder = Path(folder_text or os.sep)
    if not folder.is_absolute():
        folder = base / folder
    if hostos.is_link(folder) or not folder.is_dir() or _excluded(folder, excluded):
        return []
    show_hidden = prefix.startswith(".")
    low = prefix.lower()
    dirs: list[FileEntry] = []
    files: list[FileEntry] = []
    try:
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name.lower())
    except OSError:
        return []
    for entry in entries:
        if (entry.name.startswith(".") and not show_hidden) or (low and low not in entry.name.lower()):
            continue
        try:
            if hostos.stat_is_link(entry.stat(follow_symlinks=False)):
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if not _excluded(path, excluded):
                    dirs.append(FileEntry(path, True, 0, entry.name + os.sep))
            elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in TEXT_EXTENSIONS:
                files.append(FileEntry(path, False, entry.stat(follow_symlinks=False).st_size, entry.name))
        except OSError:
            continue
        if len(dirs) + len(files) >= LIST_LIMIT:
            break
    return dirs + files


def _short_path(path: Path, width: int) -> str:
    text = str(path).replace(str(Path.home()), "~", 1)
    return text if len(text) <= width else "…" + text[-(width - 1):]


def _short_dir(path: Path, width: int = 36) -> str:
    return _short_path(path.parent, width)


class SelectedList(OptionList):
    BINDINGS = [Binding("delete,backspace", "screen.remove_selected", "선택 해제")]


class ImportModal(FormModal):
    BINDINGS = [
        Binding("escape", "dismiss_none", "닫기"),
        Binding("ctrl+s", "submit", "가져오기"),
        Binding("down", "match_move(1)", show=False),
        Binding("up", "match_move(-1)", show=False),
    ]

    def __init__(self, workspace: Workspace, base_dir: Path | None = None) -> None:
        super().__init__()
        self.workspace = workspace
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self.excluded = home_path().expanduser().resolve()
        self.index: list[FileEntry] | None = None
        self.matches: list[FileEntry] = []
        self.selected: list[FileEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="import-box"):
            yield Static(f"자료 가져오기 · {self.workspace.name}", classes="modal-title")
            yield Static(
                Text.assemble(
                    ("검색 위치 ", MUTED), (_short_path(self.base_dir, 60), "bold"),
                    ("\n@이름 일부로 파일을 찾고, ~/ 나 / 로 시작하면 폴더를 탐색함\n", MUTED),
                    ("↑↓", "bold"), (" 이동  ", MUTED), ("Enter", "bold"), (" 추가  ", MUTED),
                    ("Ctrl+S", "bold"), (" 가져오기", MUTED),
                ),
                classes="modal-sub",
            )
            yield Input(placeholder="@meeting  또는  ~/Documents/", id="f-path")
            yield OptionList(id="f-matches")
            yield Static("", id="f-total", classes="picker-label")
            yield SelectedList(id="f-selected")
            with Grid(classes="form-grid"):
                yield Label("등급")
                yield Select(INPUT_CLASSES, value="internal", allow_blank=False, id="f-cls")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("가져오기", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#f-matches").display = False
        self._refresh_selected()
        self.query_one("#f-path", Input).focus()
        self._build_index(self.app)

    @work(thread=True, exclusive=True, group="index", exit_on_error=False)
    def _build_index(self, app) -> None:
        files = scan_files(self.base_dir, self.excluded)
        app.call_from_thread(self._index_ready, files)

    def _index_ready(self, files: list[FileEntry]) -> None:
        self.index = files
        with suppress(NoMatches):
            self._update_matches(self.query_one("#f-path", Input).value)

    def _entry_prompt(self, entry: FileEntry) -> Text:
        if entry.is_dir:
            return Text.assemble((entry.label, "bold"), ("  폴더", FAINT))
        too_big = entry.size > MAX_INPUT_BYTES
        text = Text(entry.label)
        text.append(f"  {human_size(entry.size)}", style=ERROR if too_big else MUTED)
        if too_big:
            text.append("  5MB 초과", style=ERROR)
        return text

    def _update_matches(self, value: str) -> None:
        raw = value.strip().removeprefix("@")
        if not value.strip():
            self.matches = []
        elif looks_like_path(raw):
            self.matches = list_directory(raw, self.base_dir, self.excluded)
        elif self.index is None:
            self.matches = []
        elif not raw:
            self.matches = self.index[:MATCH_LIMIT]
        else:
            self.matches = match_files(self.index, raw)
        box = self.query_one("#f-matches", OptionList)
        box.clear_options()
        box.add_options([Option(self._entry_prompt(e)) for e in self.matches])
        box.display = bool(value.strip())
        if self.matches:
            box.highlighted = 0
        elif value.strip():
            box.add_option(Option(Text("찾는 중…" if self.index is None else "일치하는 파일 없음", style=FAINT),
                                  disabled=True))

    @on(Input.Changed, "#f-path")
    def _path_changed(self, event: Input.Changed) -> None:
        self._update_matches(event.value)

    def action_match_move(self, step: int) -> None:
        box = self.query_one("#f-matches", OptionList)
        if not self.matches or not box.display:
            return
        current = box.highlighted if box.highlighted is not None else -1
        box.highlighted = max(0, min(len(self.matches) - 1, current + step))

    def _pick(self, entry: FileEntry) -> None:
        field = self.query_one("#f-path", Input)
        if entry.is_dir:
            field.value = folder_hint(entry.path)
            field.cursor_position = len(field.value)
            return
        self._add(entry)
        field.value = ""

    def _add(self, entry: FileEntry) -> None:
        if entry.size > MAX_INPUT_BYTES:
            self.show_error(f"{entry.path.name}: 5MB를 넘는 파일은 가져올 수 없음")
            return
        if any(e.path == entry.path for e in self.selected):
            self.show_error(f"{entry.path.name}: 이미 선택함")
            return
        self.show_error("")
        self.selected.append(entry)
        self._refresh_selected()

    def _refresh_selected(self) -> None:
        box = self.query_one("#f-selected", SelectedList)
        box.clear_options()
        for entry in self.selected:
            text = Text(entry.path.name, style="bold")
            text.append(f"  {human_size(entry.size)}", style=MUTED)
            text.append(f"  {_short_dir(entry.path)}", style=FAINT)
            box.add_option(Option(text))
        box.display = bool(self.selected)
        total = sum(e.size for e in self.selected)
        label = self.query_one("#f-total", Static)
        if self.selected:
            label.update(Text.assemble(("선택한 파일 ", MUTED), (f"{len(self.selected)}개", "bold"),
                                       (" · 합계 ", MUTED), (human_size(total), "bold"),
                                       ("   Del로 선택 해제", FAINT)))
        else:
            label.update(Text("선택한 파일 없음", style=FAINT))

    def action_remove_selected(self) -> None:
        box = self.query_one("#f-selected", SelectedList)
        if box.highlighted is None or not self.selected:
            return
        index = box.highlighted
        del self.selected[index]
        self._refresh_selected()
        if self.selected:
            box.highlighted = min(index, len(self.selected) - 1)
            box.focus()
        else:
            self.query_one("#f-path", Input).focus()

    @on(OptionList.OptionSelected, "#f-matches")
    def _match_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self.matches):
            self._pick(self.matches[event.option_index])
            self.query_one("#f-path", Input).focus()

    @on(Input.Submitted, "#f-path")
    def _path_submitted(self, event: Input.Submitted) -> None:
        box = self.query_one("#f-matches", OptionList)
        if self.matches and box.highlighted is not None:
            self._pick(self.matches[box.highlighted])
            return
        raw = event.value.strip().removeprefix("@")
        if not raw:
            return
        path = Path(os.path.expanduser(raw))
        path = path if path.is_absolute() else self.base_dir / path
        if hostos.is_link(path) or not path.is_file():
            self.show_error(f"{raw}: 일반 파일을 찾을 수 없음")
            return
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            self.show_error(f"{path.name}: 텍스트 계열 파일만 가져올 수 있음")
            return
        self._add(FileEntry(path, False, path.lstat().st_size, path.name))
        event.input.value = ""

    def action_dismiss_none(self) -> None:
        box = self.query_one("#f-matches", OptionList)
        if box.display:
            self.query_one("#f-path", Input).value = ""
            box.display = False
            return
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        if not self.selected:
            self.show_error("가져올 파일을 하나 이상 선택해야 함")
            return
        cls = str(self.query_one("#f-cls", Select).value)
        audit = AuditLog()
        imported = []
        for entry in list(self.selected):
            try:
                result = self.workspace.import_file(entry.path, cls)
            except (WorkspaceError, OSError) as exc:
                self.show_error(f"{entry.path.name}: {exc}")
                return
            audit.append(
                "input.imported", workspace=self.workspace.name, name=result.name,
                classification=result.classification, sha256=result.sha256, via="tui",
            )
            imported.append(result.name)
            self.selected.remove(entry)
        self.dismiss(imported)


class RunImportModal(FormModal):
    BINDINGS = [
        Binding("escape", "dismiss_none", "닫기"),
        Binding("ctrl+s", "submit", "가져오기"),
        Binding("space", "toggle", "선택", show=False),
    ]

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace
        self.entries: list[tuple[Workspace, dict, int]] = []
        self.chosen: set[int] = set()

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="run-import-box"):
            yield Static(f"다른 작업 영역 기록 가져오기 · {self.workspace.name}", classes="modal-title")
            yield Static(
                Text.assemble(
                    ("다른 작업 영역에서 완료된 보고서를 이 작업 영역의 입력 자료로 복사함\n", MUTED),
                    ("Space/Enter", "bold"), (" 선택  ", MUTED), ("Ctrl+S", "bold"), (" 가져오기", MUTED),
                ),
                classes="modal-sub",
            )
            yield OptionList(id="r-list")
            yield Static("", id="r-total", classes="picker-label")
            with Grid(classes="form-grid"):
                yield Label("등급")
                yield Select(INPUT_CLASSES, value="internal", allow_blank=False, id="r-cls")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("가져오기", id="submit", variant="primary")

    def on_mount(self) -> None:
        for name in Workspace.list_names():
            if name == self.workspace.name:
                continue
            try:
                source = Workspace.open(name)
                records = source.recent_runs(limit=60)
            except (WorkspaceError, OSError, ValueError):
                continue
            for record in records:
                if record.get("status") not in ("completed", "partial"):
                    continue
                size = source.report_size(record["run_id"])
                if size:
                    self.entries.append((source, record, size))
        box = self.query_one("#r-list", OptionList)
        if self.entries:
            box.add_options([Option(self._prompt(i)) for i in range(len(self.entries))])
            box.highlighted = 0
        else:
            box.add_option(Option(Text("가져올 수 있는 다른 작업 영역의 보고서가 없음", style=FAINT), disabled=True))
        self._refresh_total()
        box.focus()

    def _prompt(self, index: int) -> Text:
        source, record, size = self.entries[index]
        mark = "[x] " if index in self.chosen else "[ ] "
        text = Text(mark, style=f"bold {CLAY}" if index in self.chosen else FAINT)
        text.append(f"{source.name:<16}", style=f"bold {PERIWINKLE}")
        text.append(f" {str(record.get('created', ''))[5:16].replace('T', ' ')}  ", style=MUTED)
        goal = " ".join(str((record.get("plan") or {}).get("title") or record.get("goal", "")).split())
        text.append(goal[:40] + ("…" if len(goal) > 40 else ""))
        text.append(f"  {human_size(size)}", style=MUTED)
        if report_classification(record) == "confidential":
            text.append("  confidential", style=ERROR)
        return text

    def _refresh_total(self) -> None:
        total = sum(self.entries[i][2] for i in self.chosen)
        label = self.query_one("#r-total", Static)
        if self.chosen:
            label.update(Text.assemble(("선택한 보고서 ", MUTED), (f"{len(self.chosen)}개", "bold"),
                                       (" · 합계 ", MUTED), (human_size(total), "bold")))
        else:
            label.update(Text("선택한 보고서 없음", style=FAINT))

    def action_toggle(self) -> None:
        box = self.query_one("#r-list", OptionList)
        index = box.highlighted
        if index is None or not self.entries:
            return
        self.chosen.symmetric_difference_update({index})
        box.replace_option_prompt_at_index(index, self._prompt(index))
        self._refresh_total()

    @on(OptionList.OptionSelected, "#r-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.action_toggle()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        if not self.chosen:
            self.show_error("가져올 보고서를 하나 이상 선택해야 함")
            return
        wanted = str(self.query_one("#r-cls", Select).value)
        audit = AuditLog()
        imported: list[str] = []
        for index in sorted(self.chosen):
            source, record, _ = self.entries[index]
            floor = report_classification(record)
            cls = max(wanted, floor, key=CLASS_ORDER.index)
            try:
                result = self.workspace.import_run_report(source, record["run_id"], cls)
            except (WorkspaceError, OSError) as exc:
                self.show_error(f"{source.name}/{record['run_id']}: {exc}")
                return
            audit.append(
                "input.imported", workspace=self.workspace.name, name=result.name,
                classification=result.classification, sha256=result.sha256, source_workspace=source.name,
                source_run=record["run_id"], via="tui",
            )
            imported.append(result.name)
        self.dismiss(imported)


class ExportModal(FormModal):
    BINDINGS = [
        Binding("escape", "dismiss_none", "닫기"),
        Binding("ctrl+s", "submit", "복사"),
        Binding("down", "match_move(1)", show=False),
        Binding("up", "match_move(-1)", show=False),
    ]

    def __init__(self, workspace: Workspace, record: dict, start_dir: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.record = record
        self.start_dir = start_dir
        self.excluded = home_path().expanduser().resolve()
        self.dirs: list[FileEntry] = []

    heading = "보고서 복사"

    def file_label(self) -> str:
        return export_file_name(self.record) + ".md"

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="export-box"):
            yield Static(self.heading, classes="modal-title")
            yield Static(
                Text.assemble(
                    ("파일 ", MUTED), (self.file_label(), "bold"),
                    ("\n복사할 폴더 경로를 적거나 목록에서 고름. 같은 이름이 있으면 번호를 붙여 새로 만듦\n", MUTED),
                    ("↑↓", "bold"), (" 이동  ", MUTED), ("Enter", "bold"), (" 폴더 열기  ", MUTED),
                    ("Ctrl+S", "bold"), (" 복사", MUTED),
                ),
                classes="modal-sub",
            )
            yield Input(value=self.start_dir, placeholder=folder_hint(Path.home() / "Documents"), id="x-path")
            yield OptionList(id="x-dirs")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("복사", id="submit", variant="primary")

    def on_mount(self) -> None:
        field = self.query_one("#x-path", Input)
        field.focus()
        field.cursor_position = len(field.value)
        self._update(field.value)

    def _update(self, value: str) -> None:
        raw = value.strip()
        self.dirs = [e for e in list_directory(raw, Path.home(), self.excluded) if e.is_dir] if raw else []
        box = self.query_one("#x-dirs", OptionList)
        box.clear_options()
        box.add_options([Option(Text(e.label, style="bold")) for e in self.dirs])
        box.display = bool(self.dirs)
        box.highlighted = None

    @on(Input.Changed, "#x-path")
    def _changed(self, event: Input.Changed) -> None:
        self._update(event.value)

    def action_match_move(self, step: int) -> None:
        box = self.query_one("#x-dirs", OptionList)
        if not self.dirs:
            return
        current = box.highlighted if box.highlighted is not None else -1
        box.highlighted = max(0, min(len(self.dirs) - 1, current + step))

    def _enter(self, entry: FileEntry) -> None:
        field = self.query_one("#x-path", Input)
        field.value = folder_hint(entry.path)
        field.cursor_position = len(field.value)
        field.focus()

    @on(OptionList.OptionSelected, "#x-dirs")
    def _dir_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self.dirs):
            self._enter(self.dirs[event.option_index])

    @on(Input.Submitted, "#x-path")
    def _submitted(self, event: Input.Submitted) -> None:
        box = self.query_one("#x-dirs", OptionList)
        if self.dirs and box.highlighted is not None:
            self._enter(self.dirs[box.highlighted])
            return
        self.action_submit()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        raw = self.query_one("#x-path", Input).value
        try:
            target, digest = export_report(self.workspace, self.record["run_id"], raw)
        except (WorkspaceError, OSError) as exc:
            self.show_error(str(exc))
            return
        AuditLog().append(
            "report.exported", workspace=self.workspace.name, run_id=self.record["run_id"], path=str(target),
            sha256=digest, classification=report_classification(self.record), via="tui",
        )
        self.dismiss(str(target))


class ActionExportModal(ExportModal):
    heading = "제안 파일 복사"

    def __init__(self, workspace: Workspace, action_id: str, file_name: str, start_dir: str) -> None:
        super().__init__(workspace, {}, start_dir)
        self.action_id = action_id
        self.file_name = file_name

    def file_label(self) -> str:
        return self.file_name

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        raw = self.query_one("#x-path", Input).value
        try:
            target, digest = ActionStore(self.workspace).export(self.action_id, raw)
        except (ActionError, WorkspaceError, OSError) as exc:
            self.show_error(str(exc))
            return
        AuditLog().append("action.exported", workspace=self.workspace.name, action_id=self.action_id,
                          path=str(target), sha256=digest, via="tui")
        self.dismiss(str(target))


class ProfileModal(FormModal):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기"), Binding("ctrl+s", "submit", "저장")]

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        cfg = self.workspace.config
        with VerticalScroll(classes="modal-box", id="profile-box"):
            yield Static(f"업무 프로필 · {cfg.name}", classes="modal-title")
            yield Static(
                "모든 에이전트 요청에 함께 전달되어 무엇이 중요한지 판단하는 기준이 됨 (캐시되어 추가 비용은 작음). "
                "사실의 출처로는 쓰지 않음",
                classes="modal-sub",
            )
            with Grid(classes="form-grid"):
                yield from profile_grid(cfg.profile)
            yield Static("반복 업무·관심 이슈 · 정기 정리와 행동 제안의 기준", classes="field-title")
            yield TextArea(cfg.profile.focus, id="f-focus", soft_wrap=True, classes="guide-area focus-area")
            yield Static("자동화", classes="field-title")
            with Horizontal(classes="switch-row"):
                yield Switch(value=cfg.knowledge, id="f-knowledge")
                yield SwitchLabel("누적 지식", "f-knowledge")
                yield Switch(value=bool(cfg.actions), id="f-actions")
                yield SwitchLabel("행동 제안", "f-actions")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("저장", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#f-focus", TextArea).placeholder = FOCUS_HINT
        self.query_one("#f-type", Select).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        cfg = self.workspace.config
        try:
            profile = read_profile(self)
            config = WorkspaceConfig.model_validate({
                **cfg.model_dump(), "profile": profile.model_dump(),
                "knowledge": self.query_one("#f-knowledge", Switch).value,
                "actions": automation_actions(self.query_one("#f-actions", Switch).value, cfg.actions),
            })
            self.workspace.save_config(config)
        except ValidationError as exc:
            self.show_error(_validation_message(exc))
            return
        except (WorkspaceError, OSError) as exc:
            self.show_error(str(exc))
            return
        AuditLog().append("workspace.profile_set", workspace=config.name, business_type=profile.business_type,
                          knowledge=config.knowledge, actions=list(config.actions), via="tui")
        self.dismiss(True)


def schedule_preview(text: str, now: datetime | None = None) -> Text:
    try:
        schedule = parse_schedule(text)
    except ScheduleError as exc:
        return Text(str(exc), style=ERROR)
    moment = schedule.next_after(now or datetime.now())
    return Text.assemble(
        (schedule.describe(), "bold"),
        (f"  ·  다음 실행 {moment:%m-%d}({WEEKDAY_NAMES[moment.weekday()]}) {moment:%H:%M}", MUTED),
    )


class DigestModal(FormModal):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기"), Binding("ctrl+s", "submit", "저장")]

    def __init__(self, workspace: Workspace, spec: DigestSpec | None = None) -> None:
        super().__init__()
        self.workspace = workspace
        self.spec = spec

    def compose(self) -> ComposeResult:
        spec = self.spec
        limit = self.workspace.config.web.max_searches_per_task
        with VerticalScroll(classes="modal-box", id="digest-box"):
            yield Static("정기 정리 편집" if spec else "새 정기 정리", classes="modal-title")
            yield Static(
                "정한 일정마다 에이전트 하나가 지난 실행 이후 새 소식만 웹에서 찾아 정리함. 이미 보고한 링크는 건너뛰고, "
                "결과는 기록 탭에 쌓이며 누적 지식과 행동 제안으로 이어짐. TUI가 열려 있을 때 실행되고, "
                "항상 실행하려면 `claubiz agent install`",
                classes="modal-sub",
            )
            with Grid(classes="form-grid"):
                yield Label("이름")
                yield Input(spec.title if spec else "", placeholder="예: 경쟁사 주간 동향", id="d-title", max_length=60)
                yield Label("일정")
                yield Input(spec.schedule if spec else "평일 09:00", placeholder="매일 09:00, 평일 08:30, 월,목 10:00, "
                            "6시간마다", id="d-schedule", max_length=80)
                yield Label("")
                yield Static("", id="d-next")
                yield Label("모델")
                yield Select(DIGEST_MODELS, value=spec.model if spec else DEFAULT_DIGEST_MODEL, allow_blank=False,
                             id="d-model")
                yield Label("검색 / 실행 상한")
                with Horizontal(classes="pair"):
                    yield Input(str(spec.max_searches if spec else min(4, limit)), type="integer", id="d-searches",
                                max_length=2, tooltip=f"실행마다 최대 웹 검색 수 (작업 영역 상한 {limit}회)")
                    yield Input(str(spec.run_cap if spec else DEFAULT_DIGEST_CAP), type="number", id="d-cap",
                                tooltip="실행마다 예산 상한 (USD)")
                yield Label("켜기")
                yield Switch(value=spec.enabled if spec else True, id="d-enabled")
            yield Static("요청 · 무엇을 찾아 어떻게 정리할지", classes="field-title")
            yield TextArea(spec.request if spec else "", id="d-request", soft_wrap=True, classes="guide-area")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("저장", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#d-request", TextArea).placeholder = (
            "예: 국내 전자계약 경쟁사의 신기능, 가격 변경, 투자 소식. 중요도 순으로 5개 이내, 우리에게 미칠 영향 한 줄씩"
        )
        self.query_one("#d-next", Static).update(schedule_preview(self.query_one("#d-schedule", Input).value))
        self.query_one("#d-title", Input).focus()

    @on(Input.Changed, "#d-schedule")
    def _schedule_changed(self, event: Input.Changed) -> None:
        self.query_one("#d-next", Static).update(schedule_preview(event.value))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        values = {
            "title": self.query_one("#d-title", Input).value.strip(),
            "request": self.query_one("#d-request", TextArea).text.strip(),
            "schedule": self.query_one("#d-schedule", Input).value,
            "model": str(self.query_one("#d-model", Select).value),
            "enabled": self.query_one("#d-enabled", Switch).value,
        }
        try:
            values["max_searches"] = int(self.query_one("#d-searches", Input).value or 0)
            values["run_cap"] = float(self.query_one("#d-cap", Input).value or 0)
        except ValueError:
            self.show_error("검색 수와 실행 상한은 숫자여야 함")
            return
        store = DigestStore(self.workspace)
        audit = AuditLog()
        try:
            if self.spec is None:
                spec = store.add(make_spec(self.workspace, **values))
                audit.append("digest.created", workspace=self.workspace.name, digest_id=spec.id,
                             schedule=spec.schedule, model=spec.model or "config", run_cap=spec.run_cap, via="tui")
            else:
                if values["max_searches"] > self.workspace.config.web.max_searches_per_task:
                    raise DigestError("검색 수는 작업 영역 상한 이하여야 함")
                if values["enabled"] and not self.spec.enabled:
                    values["failures"] = 0
                spec = store.update(self.spec.id, **values)
                audit.append("digest.updated", workspace=self.workspace.name, digest_id=spec.id,
                             schedule=spec.schedule, model=spec.model or "config", run_cap=spec.run_cap,
                             enabled=spec.enabled, via="tui")
        except ValidationError as exc:
            self.show_error(_validation_message(exc))
            return
        except (DigestError, OSError) as exc:
            self.show_error(str(exc))
            return
        self.dismiss(spec.id)


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "answer(False)", "취소"),
        Binding("y", "answer(True)", "제거"),
    ]

    def __init__(self, title: str, detail: str, confirm_label: str = "제거") -> None:
        super().__init__(classes="toast-screen")
        self.title_text = title
        self.detail = detail
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.title_text, classes="confirm-title")
            yield Static(Text(self.detail, style=MUTED), classes="confirm-detail")
            with Horizontal(classes="confirm-actions"):
                yield Static(Text.assemble(("Y", "bold"), (f" {self.confirm_label}  ", MUTED), ("N/Esc", "bold"),
                                           (" 취소", MUTED)), classes="confirm-keys")
                yield Button("취소", id="no", classes="ghost")
                yield Button(self.confirm_label, id="yes", variant="error")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)

    def on_click(self, event: events.Click) -> None:
        if not self.query_one("#confirm-box").region.contains(event.screen_x, event.screen_y):
            self.dismiss(False)


class ContextMenu(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기")]

    def __init__(self, items: list[tuple[str, str]], offset: Offset, title: str = "") -> None:
        super().__init__(classes="menu-screen")
        self.items = items
        self.at = offset
        self.title_text = title

    def compose(self) -> ComposeResult:
        menu = OptionList(*[Option(label, id=key) for label, key in self.items], id="context-menu")
        menu.border_title = self.title_text
        yield menu

    def on_mount(self) -> None:
        menu = self.query_one("#context-menu", OptionList)
        width, height = 24, len(self.items) + 2
        x = max(0, min(self.at.x, self.size.width - width - 1))
        y = max(0, min(self.at.y, self.size.height - height - 1))
        menu.styles.width = width
        menu.styles.offset = (x, y)
        menu.highlighted = 0
        menu.focus()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#context-menu")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        if not self.query_one("#context-menu").region.contains(event.screen_x, event.screen_y):
            self.dismiss(None)


class BriefModal(FormModal):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기"), Binding("ctrl+s", "submit", "저장")]

    def __init__(self, workspace: Workspace, generator: BriefGenerator | None = None) -> None:
        super().__init__()
        self.workspace = workspace
        self.generator = generator

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box", id="brief-box"):
            yield Static(f"작업 영역 지침 · {self.workspace.name}", classes="modal-title")
            yield Static(
                "이 작업 영역의 모든 에이전트 요청에 함께 전달됨(캐시되어 추가 비용은 작음). 비우고 저장하면 지침이 삭제됨. "
                "'AI로 다듬기'는 지금 내용을 참고해 새 초안을 만듦",
                classes="modal-sub",
            )
            yield TextArea(self.workspace.read_brief(), id="f-brief", soft_wrap=True)
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Static("", id="gen-state")
                yield Button("AI로 다듬기", id="gen", classes="ghost", disabled=self.generator is None)
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("저장", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#f-brief", TextArea).focus()

    @on(Button.Pressed, "#gen")
    def _generate(self) -> None:
        if self.generator is None:
            return
        cfg = self.workspace.config
        web = cfg.web
        mode = "allow" if web.allowed_domains else ("block" if web.blocked_domains else "none")
        self.query_one("#gen", Button).disabled = True
        self.query_one("#gen-state", Static).update(Text("초안 작성 중…", style=MUTED))
        self._run_generation(
            name=cfg.name, description=cfg.description, scope=cfg.scope,
            web_note=web_note(mode, web.allowed_domains or web.blocked_domains),
            hints=self.query_one("#f-brief", TextArea).text, ws_budget=cfg.budget_monthly_usd, redact=cfg.redact_pii,
            profile=cfg.profile.lines(),
        )

    @work(exclusive=True, group="brief", exit_on_error=False)
    async def _run_generation(self, **kwargs) -> None:
        try:
            text, cost = await self.generator(**kwargs)
        except Exception as exc:
            with suppress(NoMatches):
                self.show_error(f"AI 초안 실패: {str(exc)[:200]}")
                self.query_one("#gen-state", Static).update("")
        else:
            with suppress(NoMatches):
                self.query_one("#f-brief", TextArea).text = text
                self.query_one("#gen-state", Static).update(Text(f"초안 완료 · ${cost:.4f}", style=MUTED))
        finally:
            with suppress(NoMatches):
                self.query_one("#gen", Button).disabled = False

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        try:
            chars = self.workspace.write_brief(self.query_one("#f-brief", TextArea).text)
        except WorkspaceError as exc:
            self.show_error(str(exc))
            return
        AuditLog().append("workspace.brief_set", workspace=self.workspace.name, chars=chars, via="tui")
        self.dismiss(chars)


PLAYBOOK_OPTIONS = (
    ("p-web", "웹 조사 필요", True),
    ("p-inputs", "입력 자료 필요", False),
    ("p-history", "이전 실행과 비교", False),
)


class SwitchLabel(Static):
    def __init__(self, label: str, target: str) -> None:
        super().__init__(label, classes="switch-label")
        self.target = target

    def on_click(self) -> None:
        switch = self.screen.query_one(f"#{self.target}", Switch)
        switch.toggle()
        switch.focus()


class PlaybookModal(FormModal):
    BINDINGS = [Binding("escape", "dismiss_none", "닫기"), Binding("ctrl+s", "submit", "저장")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-box", id="playbook-box"):
            yield Static("새 플레이북", classes="modal-title")
            yield Static(
                "자주 하는 작업 유형을 저장해 두고 목표만 바꿔 다시 씀. 모든 작업 영역에서 사용 가능", classes="modal-sub"
            )
            with Grid(classes="form-grid"):
                yield Label("ID")
                yield Input(placeholder="partner-scan  (소문자, 숫자, -)", id="p-id", max_length=40)
                yield Label("이름")
                yield Input(placeholder="예: 협력사 후보 발굴", id="p-title", max_length=60)
                yield Label("설명")
                yield Input(placeholder="목록에 보일 한 줄 설명", id="p-desc", max_length=300)
                yield Label("목표 예시")
                yield Input(placeholder="예: 국내 물류 스타트업 중 협력 후보 10곳", id="p-hint", max_length=200)
            yield Static("옵션 · 이름을 눌러도 켜고 끌 수 있음", classes="field-title")
            with Horizontal(classes="switch-row"):
                for switch_id, label, value in PLAYBOOK_OPTIONS:
                    yield Switch(value=value, id=switch_id)
                    yield SwitchLabel(label, switch_id)
            yield Static("계획 지침 · 작업을 어떻게 나눌지", classes="field-title")
            yield TextArea(id="p-plan", soft_wrap=True, classes="guide-area")
            yield Static("보고서 지침 · 결과물을 어떤 형태로 쓸지", classes="field-title")
            yield TextArea(id="p-synth", soft_wrap=True, classes="guide-area")
            yield Static("", classes="modal-error")
            with Horizontal(classes="modal-actions"):
                yield Button("취소", id="cancel", classes="ghost")
                yield Button("저장", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#p-plan", TextArea).placeholder = "예: 후보 기업을 업종별로 나눠 조사하고, 기업마다 규모, 주요 고객, 최근 소식 정리"
        self.query_one("#p-synth", TextArea).placeholder = "예: 요약 3줄, 후보 비교표, 우선순위와 접촉 제안 순서"
        self.query_one("#p-id", Input).focus()

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def action_submit(self) -> None:
        try:
            spec = PlaybookSpec(
                id=self.query_one("#p-id", Input).value.strip(),
                title=self.query_one("#p-title", Input).value,
                description=self.query_one("#p-desc", Input).value,
                goal_hint=self.query_one("#p-hint", Input).value,
                planner_guidance=self.query_one("#p-plan", TextArea).text,
                synth_guidance=self.query_one("#p-synth", TextArea).text,
                needs_web=self.query_one("#p-web", Switch).value,
                needs_inputs=self.query_one("#p-inputs", Switch).value,
                uses_history=self.query_one("#p-history", Switch).value,
            )
            playbook = save_custom_playbook(spec)
        except ValidationError as exc:
            self.show_error(_validation_message(exc))
            return
        except (PlaybookError, OSError) as exc:
            self.show_error(str(exc))
            return
        AuditLog().append("playbook.created", id=playbook.id, needs_web=playbook.needs_web,
                          needs_inputs=playbook.needs_inputs, via="tui")
        self.dismiss(playbook.id)


def _plan_markdown(record: dict) -> str:
    plan = record.get("plan") or {}
    tasks = {t["id"]: t for t in record.get("tasks", [])}
    lines = [f"# {plan.get('title', '계획')}", "", plan.get("approach", ""), ""]
    lines += ["| ID | 작업 | effort | 웹 | 선행 | 상태 | 비용 |", "|---|---|---|---|---|---|---|"]
    for t in plan.get("tasks", []):
        state = tasks.get(t["id"], {})
        web = f"{t.get('max_searches', 0)}회" if t.get("use_web") else "-"
        cost = f"${state.get('cost', 0):.4f}" if state else "-"
        title = str(t.get("title", "")).replace("|", "/")
        deps = ", ".join(t.get("depends_on", [])) or "-"
        lines.append(f"| {t['id']} | {title} | {state.get('effort', t.get('effort'))} | {web} | {deps} | "
                     f"{state.get('status', '-')} | {cost} |")
    if plan.get("outline"):
        lines += ["", "## 보고서 목차", ""] + [f"{i}. {h}" for i, h in enumerate(plan["outline"], start=1)]
    if record.get("plan_notes"):
        lines += ["", "## 계획 조정", ""] + [f"- {n}" for n in record["plan_notes"]]
    lines += ["", "## 목표", "", record.get("goal", "")]
    return "\n".join(lines)


class RunViewerModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "dismiss", "닫기"),
        Binding("ctrl+enter", "follow", "이어서 작업"),
        Binding("c", "export", "보고서 복사"),
    ]

    def action_follow(self) -> None:
        self.dismiss("follow")

    def action_export(self) -> None:
        self.app.start_export(self.workspace, self.record)

    def __init__(self, workspace: Workspace, record: dict) -> None:
        super().__init__()
        self.workspace = workspace
        self.record = record
        self.notes = workspace.task_notes(record["run_id"])
        self.report = workspace.read_report(record["run_id"])

    def compose(self) -> ComposeResult:
        record = self.record
        plan = record.get("plan") or {}
        with Vertical(classes="modal-box", id="viewer-box"):
            with Horizontal(id="viewer-head"):
                yield Static(Text(plan.get("title") or record.get("goal", "")[:60], style="bold"), id="viewer-title")
                meta = status_text(record.get("status", ""))
                meta.append(f"   {record.get('created', '').replace('T', ' ')}   ${record.get('cost_total', 0):.4f}",
                            style=MUTED)
                yield Static(meta, id="viewer-meta")
            initial = "pane-report" if self.report else ("pane-notes" if self.notes else "pane-plan")
            with TabbedContent(initial=initial):
                with TabPane("보고서", id="pane-report"):
                    yield SafeMarkdownViewer(self.report or "# 보고서 없음\n\n이 실행은 보고서를 만들지 않았음",
                                             show_table_of_contents=bool(self.report), id="report-view")
                with TabPane(f"작업 노트 {len(self.notes)}", id="pane-notes"):
                    with Horizontal():
                        yield OptionList(*[Option(self._note_label(tid), id=tid) for tid, _ in self.notes],
                                         id="note-list")
                        yield SafeMarkdownViewer(self.notes[0][1] if self.notes else "작업 노트 없음",
                                                 show_table_of_contents=False, id="note-view")
                with TabPane("계획", id="pane-plan"):
                    yield Markdown(_plan_markdown(record), open_links=False, id="plan-view")
            yield Static(
                Text(f"Esc 닫기 · Ctrl+Enter 이어서 작업 또는 재개 · C 보고서 복사 · 링크는 브라우저로 열림 · "
                     f"{self.workspace.root / 'outputs' / record['run_id']}",
                     style=MUTED),
                classes="modal-hint",
            )

    def _note_label(self, task_id: str) -> Text:
        tasks = {t["id"]: t for t in self.record.get("tasks", [])}
        info = tasks.get(task_id, {})
        label = status_text(info.get("status", "done"))
        label.append(f"  {task_id} {str(info.get('title', ''))[:22]}")
        return label

    @on(OptionList.OptionHighlighted, "#note-list")
    async def _show_note(self, event: OptionList.OptionHighlighted) -> None:
        notes = dict(self.notes)
        text = notes.get(event.option.id or "")
        if text is not None:
            await self.query_one("#note-view", SafeMarkdownViewer).document.update(text)
