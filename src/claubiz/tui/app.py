import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from rich.cells import cell_len, set_cell_size
from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.timer import Timer
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Markdown,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option, OptionDoesNotExist

from .. import eventfmt
from ..actions import TYPE_LABELS, ActionError, ActionStore, preview_markdown
from ..agent import registration_path
from ..brief import generate_brief
from ..budget import cycle_start
from ..config import MODEL_CHOICES, MODEL_ROLES, ConfigError, Settings, load_settings, model_label
from ..derived import derived_counts, purge_derived
from ..digest import DigestClaim, DigestError, DigestSpec, DigestStore, finish_digest, start_digest
from ..events import Event
from ..knowledge import KnowledgeStore
from ..orchestrator import FOLLOWABLE as RECORD_FOLLOWABLE
from ..orchestrator import RunError
from ..planner import estimate_plan, finish_estimate
from ..playbooks import Playbook, all_playbooks
from ..policy import scope_allows_inputs, scope_allows_web
from ..workspace import REPORT_FOOTER, Workspace, WorkspaceError, new_run_id, report_classification
from .compositor import install as install_compositor_fix
from .jobs import FINISHED_PHASES, ActivityLog, Draft, Job, LogBuffer, clock_text
from .modals import (
    ActionExportModal,
    BriefModal,
    ConfirmModal,
    ContextMenu,
    CreateWorkspaceModal,
    DigestModal,
    ExportModal,
    ImportModal,
    PlaybookModal,
    ProfileModal,
    RunImportModal,
    RunViewerModal,
    folder_hint,
)
from .theme import CLAY, ERROR, FAINT, MUTED, PERIWINKLE, STATUS_STYLES, SUCCESS, THEMES, WARNING
from .widgets import (
    ActionsTable,
    BudgetMeter,
    DigestsTable,
    FactsTable,
    FitTable,
    InputsTable,
    JobsCard,
    RunsTable,
    WorkspaceList,
    brand,
    open_link,
    status_text,
)

SCOPE_LABELS = {"research": "웹 + 자료", "web-only": "웹 전용", "inputs-only": "자료 전용"}
TASK_EVENT_STATUS = {
    "task.queued": "queued",
    "task.started": "running",
    "task.done": "done",
    "task.reused": "reused",
    "task.failed": "failed",
    "task.blocked": "blocked",
    "task.skipped": "skipped",
}
TASK_END_EVENTS = frozenset({"task.done", "task.reused", "task.failed", "task.blocked", "task.skipped"})
FOLLOWABLE = ("completed", "partial")
RESUMABLE_VIEW = ("cancelled", "failed", "interrupted")
PHASE_LABELS = {
    "planning": "계획 중",
    "planned": "승인 대기",
    "running": "실행 중",
    "completed": "완료",
    "partial": "일부 완료",
    "failed": "실패",
    "cancelled": "중단됨",
    "error": "오류",
    "stopped": "계획 중단",
    "rejected": "취소됨",
}
PHASE_COLORS = {"completed": SUCCESS, "partial": WARNING, "failed": ERROR, "error": ERROR, "cancelled": WARNING}
CONFIG_MODEL = "config"
DEFAULT_GOAL_HINT = "무엇을 조사할까요? 정리가 필요하신가요? 무엇이든 적어주세요. 예: 국내 전자계약 SaaS 시장과 주요 경쟁사 조사"
FOLLOW_GOAL_HINT = "이전 결과를 바탕으로 더 할 일을 적어주세요. 예: 경쟁사 가격 정책을 비교표로 추가"
ESC_FIRST_GAP = 0.5
ESC_REPEAT_GAP = 0.16
ESC_HINT_AFTER = 0.3
ESC_HOLD_SECONDS = 2.0
CARD_CELLS = 26
TAB_FOCUS = {"tab-run": "#goal", "tab-history": "#runs", "tab-inputs": "#inputs", "tab-digests": "#digests",
             "tab-actions": "#action-list"}
DIGEST_CHECK_SECONDS = 30
NARROW_LABELS = {
    "#new-pb": ("+", "+ 플레이북"),
    "#import": ("+ 자료", "+ 자료 가져오기"),
    "#import-run": ("+ 다른 영역", "+ 다른 작업 영역 기록"),
    "#edit-brief": ("지침", "지침 편집"),
    "#edit-profile": ("프로필", "업무 프로필"),
}
ACTION_STATUS = {"pending": ("●", "승인 대기", CLAY), "done": ("✓", "파일 생성", SUCCESS),
                 "rejected": ("○", "거절", FAINT)}
DIGEST_RESULT = {"completed": ("완료", SUCCESS), "partial": ("일부 완료", WARNING), "failed": ("실패", ERROR),
                 "cancelled": ("중단", WARNING), "running": ("실행 중", CLAY)}


CLASS_STYLE = {"public": MUTED, "internal": CLAY, "confidential": ERROR}


def _pad(label: str, cells: int) -> str:
    return label + " " * max(1, cells - cell_len(label))


def _kv(rows: list[tuple[str, str]], cells: int) -> Text:
    text = Text()
    for i, (key, value) in enumerate(rows):
        if i:
            text.append("\n")
        text.append(_pad(key, cells), style=MUTED)
        text.append(value)
    return text


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso[:16]


def _short(text: str, width: int) -> str:
    line = " ".join(text.split())
    return line if len(line) <= width else line[: width - 1] + "…"


def _reset_table(table: DataTable) -> None:
    table.clear()
    for column in table.columns.values():
        column.content_width = cell_len(column.label.plain)


def _fit(text: str, cells: int) -> str:
    line = " ".join(text.split())
    if cell_len(line) <= cells:
        return line
    return set_cell_size(line, max(1, cells - 1)).rstrip() + "…"


class ClaubizApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "claubiz"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+n", "new_workspace", "새 작업 영역", priority=True),
        Binding("f5", "plan", "계획", priority=True),
        Binding("f9", "direct", "바로 실행", priority=True),
        Binding("f6", "execute", "실행", priority=True),
        Binding("escape", "interrupt", "중단 (길게: 종료)"),
        Binding("f8", "cancel", "취소", show=False, priority=True),
        Binding("f2", "show_tab('tab-run')", "실행 탭", show=False, priority=True),
        Binding("f3", "show_tab('tab-history')", "기록", show=False, priority=True),
        Binding("f4", "show_tab('tab-inputs')", "자료", show=False, priority=True),
        Binding("f1", "show_tab('tab-digests')", "정기 정리", show=False, priority=True),
        Binding("f10", "show_tab('tab-actions')", "제안", show=False, priority=True),
        Binding("f7", "open_report", "보고서", priority=True),
        Binding("ctrl+o", "import_inputs", "자료 가져오기", show=False, priority=True),
        Binding("ctrl+q", "quit", "종료"),
    ]
    MAIN_SCREEN_ACTIONS = frozenset({
        "new_workspace", "plan", "direct", "execute", "cancel", "show_tab", "open_report", "import_inputs",
        "follow_up", "delete_run", "delete_workspace", "edit_brief", "new_playbook", "export_report", "import_runs",
        "goto_ws", "delete_input", "propose_actions", "delete_fact", "run_digest", "toggle_digest", "edit_digest",
        "delete_digest", "new_digest", "approve_action", "reject_action", "open_action", "export_action",
        "delete_action", "edit_profile",
    })

    def __init__(self, initial_workspace: str | None = None, runtime_factory: Callable | None = None) -> None:
        try:
            settings = load_settings()
        except ConfigError:
            settings = Settings()
        theme = settings.ui.theme
        self.theme_name = theme
        self.config_models = {role: getattr(settings.models, role).model for role in MODEL_ROLES}
        install_compositor_fix()
        self._cursor_fallback = Offset(0, 0)
        super().__init__(ansi_color=theme == "claubiz")
        self.initial_workspace = initial_workspace
        self.runtime_factory = runtime_factory
        self.runtime = None
        self.ws: Workspace | None = None
        self.history: dict[str, dict] = {}
        self.playbooks: dict[str, Playbook] = all_playbooks()
        self.jobs: dict[str, Job] = {}
        self.runs: dict[str, Job] = {}
        self.digest_jobs: dict[str, Job] = {}
        self.digest_specs: dict[str, DigestSpec] = {}
        self.facts_by_id: dict = {}
        self.action_items: dict = {}
        self.now: Callable[[], datetime] = datetime.now
        self.drafts: dict[str, Draft] = {}
        self.model_pref: str | None = None
        self.idle_log = LogBuffer()
        self.export_dir = folder_hint(Path.home())
        self.clock: Callable[[], float] = time.monotonic
        self._esc_start = 0.0
        self._esc_last: float | None = None
        self._esc_count = 0
        self._esc_fresh = True
        self._esc_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(brand(), id="brand")
            yield Static("", id="crumb")
            yield Static("", id="hold-hint")
            yield BudgetMeter("", id="meter")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("작업 영역", classes="section-title")
                yield WorkspaceList(id="ws-list")
                yield Button("+ 새 작업 영역", id="new-ws", classes="ghost")
                yield JobsCard("", id="jobs-card", classes="card")
                yield Static("", id="ws-card", classes="card")
            with Vertical(id="main"):
                with TabbedContent(initial="tab-run", id="tabs"):
                    with TabPane("실행", id="tab-run"):
                        with Vertical(id="composer", classes="card") as composer:
                            composer.border_title = "새 목표"
                            with Horizontal(id="composer-row"):
                                yield Select(self._playbook_options(), prompt="플레이북 없이 자유 목표", id="pb")
                                yield Button("+ 플레이북", id="new-pb", classes="ghost")
                                model = Select(self._model_options(), value=CONFIG_MODEL, allow_blank=False, id="model")
                                model.tooltip = "모든 역할에 쓸 모델 · 실행 중에도 바꿀 수 있고 다음 호출부터 적용됨"
                                yield model
                                with Horizontal(id="cap-box") as cap_box:
                                    cap_box.tooltip = "이번 실행의 예산 상한 (USD)"
                                    yield Static("$", id="cap-prefix")
                                    yield Input(placeholder="실행 상한", type="number", id="cap")
                            with Horizontal(id="follow-bar"):
                                yield Static("", id="follow-text")
                                yield Button("해제", id="follow-clear", classes="ghost")
                            yield TextArea(id="goal", placeholder=DEFAULT_GOAL_HINT, soft_wrap=True)
                            with Horizontal(id="actions"):
                                yield Button("계획  F5", id="plan", variant="primary")
                                yield Button("바로 실행  F9", id="direct")
                                yield Button("실행  F6", id="exec", variant="success")
                                yield Button("취소  Esc", id="cancel", variant="error")
                                yield Static("", id="run-state")
                        yield Static("", id="plan-card", classes="card")
                        with Horizontal(id="run-split"):
                            tasks = FitTable(id="tasks", cursor_type="row", zebra_stripes=False, classes="card")
                            tasks.border_title = "작업"
                            yield tasks
                            log = ActivityLog(id="log", classes="card")
                            log.border_title = "활동"
                            yield log
                    with TabPane("기록", id="tab-history"):
                        runs = RunsTable(id="runs", cursor_type="row", classes="card")
                        runs.border_title = "실행 기록 · Enter 열기 · Ctrl+Enter 이어서 작업/재개 · C 보고서 복사 · Del 제거"
                        yield runs
                        with VerticalScroll(id="preview-box", classes="card") as preview:
                            preview.border_title = "미리보기"
                            yield Markdown("", open_links=False, id="run-preview")
                    with TabPane("자료", id="tab-inputs"):
                        with Horizontal(id="inputs-actions"):
                            yield Button("+ 자료 가져오기", id="import", classes="ghost")
                            yield Button("+ 다른 작업 영역 기록", id="import-run", classes="ghost")
                            yield Button("지침 편집", id="edit-brief", classes="ghost")
                            yield Button("업무 프로필", id="edit-profile", classes="ghost")
                            yield Static("", id="brief-state")
                        inputs = InputsTable(id="inputs", cursor_type="row", classes="card")
                        inputs.border_title = "입력 자료 · Del 제거"
                        yield inputs
                        facts = FactsTable(id="facts", cursor_type="row", classes="card")
                        facts.border_title = "누적 지식 · Del 제거"
                        yield facts
                        yield Static("", id="fact-detail")
                        policy = Static("", id="policy", classes="card")
                        policy.border_title = "정책"
                        yield policy
                    with TabPane("정기 정리", id="tab-digests"):
                        with Horizontal(id="digest-actions"):
                            yield Button("+ 새 정기 정리", id="new-digest", classes="ghost")
                            yield Static("", id="agent-state")
                        digests = DigestsTable(id="digests", cursor_type="row", classes="card")
                        digests.border_title = "정기 정리 · R 지금 실행 · Space 켜기/끄기 · E 편집 · Del 제거"
                        yield digests
                        with VerticalScroll(id="digest-preview-box", classes="card") as digest_box:
                            digest_box.border_title = "최근 결과"
                            yield Markdown("", open_links=False, id="digest-preview")
                    with TabPane("제안", id="tab-actions"):
                        actions = ActionsTable(id="action-list", cursor_type="row", classes="card")
                        actions.border_title = "행동 제안 · Y 승인 · N 거절 · O 열기 · C 복사 · Del 제거"
                        yield actions
                        with VerticalScroll(id="action-preview-box", classes="card") as action_box:
                            action_box.border_title = "미리보기"
                            yield Markdown("", open_links=False, id="action-preview")
        yield Footer()

    def on_mount(self) -> None:
        for theme in THEMES.values():
            self.register_theme(theme)
        self.theme = self.theme_name if self.theme_name in self.available_themes else "claubiz"
        self._setup_tables()
        self.query_one("#follow-bar").display = False
        self.query_one("#jobs-card").display = False
        from ..runtime import Runtime

        factory = self.runtime_factory or Runtime.create
        try:
            self.runtime = factory()
        except Exception as exc:
            self.log_line(f"[{ERROR}]초기화 실패: {escape(str(exc))}[/]")
            self.notify(str(exc), title="초기화 실패", severity="error", timeout=10)
        else:
            self.runtime.bus.listen(self._on_event)
            self.query_one("#cap", Input).value = self._default_cap()
        self._reload_workspaces(select=self.initial_workspace)
        self._refresh_meter()
        self.set_interval(1.0, self._tick)
        self.set_interval(DIGEST_CHECK_SECONDS, self._check_digests)
        self.set_timer(2.0, self._check_digests)

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < 140
        self.screen.set_class(narrow, "-narrow")
        with suppress(NoMatches):
            for selector, (short, full) in NARROW_LABELS.items():
                button = self.query_one(selector, Button)
                button.label = short if narrow else full
                button.refresh(layout=True)

    def _setup_tables(self) -> None:
        tasks = self.query_one("#tasks", DataTable)
        for label, key in (("ID", "id"), ("작업", "title"), ("effort", "effort"), ("웹", "web"),
                           ("상태", "status"), ("비용", "cost")):
            tasks.add_column(label, key=key)
        runs = self.query_one("#runs", DataTable)
        for label, key in (("일시", "when"), ("상태", "status"), ("방식", "playbook"), ("목표", "goal"),
                           ("작업", "tasks"), ("비용", "cost")):
            runs.add_column(label, key=key)
        inputs = self.query_one("#inputs", DataTable)
        for label, key in (("파일", "name"), ("등급", "class"), ("글자 수", "chars"), ("가져온 시각", "at")):
            inputs.add_column(label, key=key)
        facts = self.query_one("#facts", DataTable)
        for label, key in (("날짜", "as_of"), ("주제", "topic"), ("내용", "text"), ("등급", "class")):
            facts.add_column(label, key=key)
        digests = self.query_one("#digests", DataTable)
        for label, key in (("이름", "title"), ("일정", "schedule"), ("다음 실행", "next"), ("상태", "state"),
                           ("최근 결과", "last"), ("모델", "model"), ("상한", "cap")):
            digests.add_column(label, key=key)
        actions = self.query_one("#action-list", DataTable)
        for label, key in (("생성", "created"), ("유형", "type"), ("제목", "title"), ("상태", "status"),
                           ("등급", "class")):
            actions.add_column(label, key=key)

    @property
    def cursor_position(self) -> Offset:
        try:
            focused = self.screen.focused
        except Exception:
            focused = None
        if isinstance(focused, (Input, TextArea)) and focused.region:
            return focused.cursor_screen_offset
        return self._cursor_fallback

    @cursor_position.setter
    def cursor_position(self, value: Offset) -> None:
        self._cursor_fallback = value

    @property
    def running(self) -> bool:
        job = self._job()
        return job is not None and job.phase == "running"

    @property
    def follow_run(self) -> str | None:
        return self._draft().follow_run if self.ws is not None else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return action not in self.MAIN_SCREEN_ACTIONS or len(self.screen_stack) == 1

    def _playbook_options(self) -> list[tuple[str, str]]:
        return [(p.title if p.builtin else f"{p.title} · 내 플레이북", p.id) for p in self.playbooks.values()]

    def _model_options(self) -> list[tuple[str, str]]:
        models = set(self.config_models.values())
        default = f"기본 {model_label(next(iter(models)))}" if len(models) == 1 else "기본 설정"
        return [(default, CONFIG_MODEL)] + [(label, model) for model, label in MODEL_CHOICES]

    def _default_cap(self) -> str:
        return str(self.runtime.settings.budget.run_default_usd) if self.runtime is not None else ""

    def _job(self) -> Job | None:
        return self.jobs.get(self.ws.name) if self.ws is not None else None

    def _draft(self) -> Draft:
        if self.ws is None:
            return Draft()
        return self.drafts.setdefault(self.ws.name, Draft(cap=self._default_cap()))

    def _job_cost(self, job: Job) -> float:
        if self.runtime is None:
            return job.cost
        return self.runtime.ledger.spent_run(job.run_id)

    def _is_selected(self, job: Job) -> bool:
        return self.ws is not None and self.ws.name == job.name

    def log_line(self, markup: str) -> None:
        job = self._job()
        (job.log if job is not None else self.idle_log).append(markup)
        self.query_one("#log", ActivityLog).sync()

    def _refresh_meter(self) -> None:
        if self.runtime is None:
            return
        b = self.runtime.settings.budget
        spent = self.runtime.ledger.spent_since(cycle_start(datetime.now(), b.cycle_anchor_day))
        self.query_one("#meter", BudgetMeter).show(spent, b.monthly_soft_usd, b.monthly_hard_usd)

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key) and event.key == "escape" and not event.is_forwarded:
            self._track_escape()
        await super().on_event(event)

    def _track_escape(self) -> None:
        now = self.clock()
        gap = ESC_FIRST_GAP if self._esc_count == 1 else ESC_REPEAT_GAP
        self._esc_fresh = self._esc_last is None or now - self._esc_last > gap
        if self._esc_fresh:
            self._esc_start = now
            self._esc_count = 0
        self._esc_count += 1
        self._esc_last = now
        held = now - self._esc_start
        if held >= ESC_HOLD_SECONDS:
            self.exit()
            return
        if self._esc_timer is not None:
            self._esc_timer.stop()
        self._esc_timer = self.set_timer(ESC_FIRST_GAP + 0.05, self._clear_hold_hint)
        if held >= ESC_HINT_AFTER:
            remaining = ESC_HOLD_SECONDS - held
            self.query_one("#hold-hint", Static).update(
                Text(f"Esc를 계속 누르면 종료 · {remaining:.1f}초", style=f"bold {WARNING}")
            )

    def _clear_hold_hint(self) -> None:
        self._esc_last = None
        self._esc_count = 0
        with suppress(NoMatches):
            self.query_one("#hold-hint", Static).update("")

    def action_interrupt(self) -> None:
        if not self._esc_fresh:
            return
        job = self._job()
        if job is not None and job.active:
            self.action_cancel()

    def _tick(self) -> None:
        digests = [job for job in self.digest_jobs.values() if job.active]
        if not digests and not any(job.active for job in self.jobs.values()):
            return
        job = self._job()
        if job is not None and job.active:
            self._paint_state()
        self._paint_jobs_card()
        for digest_job in digests:
            if self._is_selected(digest_job):
                self._refresh_digest_row(digest_job.digest_id)

    def _ws_label(self, name: str) -> Text:
        text = Text(name, style="bold")
        job = self.jobs.get(name)
        if job is not None and (job.busy or not job.seen):
            color = CLAY if job.busy else PHASE_COLORS.get(job.phase, MUTED)
            text.append(f"  {'●' if job.active else '○'} {PHASE_LABELS.get(job.phase, job.phase)}", style=color)
        try:
            desc = Workspace.open(name).config.description
        except WorkspaceError:
            desc = "설정 오류"
        if desc:
            text.append("\n" + _short(desc, 26), style=MUTED)
        return text

    def _refresh_ws_label(self, name: str) -> None:
        with suppress(OptionDoesNotExist, NoMatches):
            self.query_one("#ws-list", WorkspaceList).replace_option_prompt(name, self._ws_label(name))

    def _reload_workspaces(self, select: str | None = None) -> None:
        names = Workspace.list_names()
        option_list = self.query_one("#ws-list", WorkspaceList)
        option_list.clear_options()
        option_list.add_options([Option(self._ws_label(n), id=n) for n in names])
        current = self.ws.name if self.ws else None
        target = select if select in names else (current if current in names else (names[0] if names else None))
        if target is None:
            self._select_workspace(None, force=True)
            return
        option_list.highlighted = names.index(target)
        self._select_workspace(target, force=True)

    @on(WorkspaceList.OptionHighlighted, "#ws-list")
    def _ws_highlighted(self, event: WorkspaceList.OptionHighlighted) -> None:
        self._select_workspace(event.option.id)

    @on(WorkspaceList.MenuRequested)
    def _ws_menu(self, event: WorkspaceList.MenuRequested) -> None:
        option_list = self.query_one("#ws-list", WorkspaceList)
        option = option_list.get_option_at_index(event.index)
        option_list.highlighted = event.index
        name = option.id

        def chosen(action: str | None) -> None:
            if action == "brief":
                self.action_edit_brief()
            elif action == "profile":
                self.action_edit_profile()
            elif action == "import":
                self.action_import_inputs()
            elif action == "runs":
                self.action_import_runs()
            elif action == "delete":
                self.action_delete_workspace(name)

        items = [("지침 편집", "brief"), ("업무 프로필", "profile"), ("자료 가져오기", "import"),
                 ("다른 영역 기록 가져오기", "runs"), ("제거", "delete")]
        self.push_screen(ContextMenu(items, event.offset, title=name), chosen)

    def action_goto_ws(self, name: str, tab: str = "tab-run") -> None:
        option_list = self.query_one("#ws-list", WorkspaceList)
        with suppress(OptionDoesNotExist):
            option_list.highlighted = option_list.get_option_index(name)
            self.action_show_tab(tab)

    def _save_draft(self) -> None:
        if self.ws is None:
            return
        draft = self._draft()
        draft.goal = self.query_one("#goal", TextArea).text
        value = self.query_one("#pb", Select).value
        draft.playbook = None if value is Select.NULL else str(value)
        draft.cap = self.query_one("#cap", Input).value

    def _restore_composer(self) -> None:
        draft = self._draft()
        goal = self.query_one("#goal", TextArea)
        if goal.text != draft.goal:
            goal.text = draft.goal
            goal.move_cursor(goal.document.end)
        select = self.query_one("#pb", Select)
        with select.prevent(Select.Changed):
            select.value = draft.playbook if draft.playbook in self.playbooks else Select.NULL
        self.query_one("#cap", Input).value = draft.cap or self._default_cap()
        self._sync_model_select()
        self._paint_follow()

    def _sync_model_select(self) -> None:
        job = self._job()
        model = job.model if job is not None and job.busy else self.model_pref
        select = self.query_one("#model", Select)
        with select.prevent(Select.Changed):
            select.value = model or CONFIG_MODEL

    def _select_workspace(self, name: str | None, force: bool = False) -> None:
        if not force and self.ws is not None and self.ws.name == name:
            return
        self._save_draft()
        if name is None:
            self.ws = None
            self.query_one("#crumb", Static).update(Text("작업 영역 없음 · Ctrl+N으로 만들기", style=MUTED))
            card = self.query_one("#ws-card", Static)
            card.border_title = ""
            card.update(Text("왼쪽 위 '+ 새 작업 영역'으로 시작", style=MUTED))
            self._restore_composer()
            self._fill_history()
            self._fill_inputs()
            self._fill_facts()
            self._fill_digests()
            self._fill_actions()
            self._paint_job()
            self._paint_jobs_card()
            return
        try:
            ws = Workspace.open(name)
        except WorkspaceError as exc:
            self.notify(str(exc), title="작업 영역 오류", severity="error")
            return
        self.ws = ws
        crumb = Text(ws.name, style="bold")
        if ws.config.description:
            crumb.append("  " + _short(ws.config.description, 50), style=MUTED)
        self.query_one("#crumb", Static).update(crumb)
        job = self.jobs.get(name)
        if job is not None and not job.seen:
            job.seen = True
            self._refresh_ws_label(name)
        self._restore_composer()
        self._fill_history()
        self._fill_card()
        self._fill_inputs()
        self._fill_facts()
        self._fill_digests()
        self._fill_actions()
        self._paint_job()
        self._paint_jobs_card()

    def _fill_card(self) -> None:
        ws = self.ws
        if ws is None:
            return
        cfg = ws.config
        runs = list(self.history.values()) or ws.recent_runs(limit=100)
        completed = sum(1 for r in runs if r.get("status") in FOLLOWABLE)
        spent = 0.0
        if self.runtime is not None:
            spent = self.runtime.ledger.spent_since(
                cycle_start(datetime.now(), self.runtime.settings.budget.cycle_anchor_day), ws.name
            )
        web = cfg.web
        if web.allowed_domains:
            source = f"허용 {len(web.allowed_domains)}개 도메인"
        elif web.blocked_domains:
            source = f"차단 {len(web.blocked_domains)}개 도메인"
        else:
            source = "제한 없음"
        try:
            facts = len(KnowledgeStore(ws).facts())
            specs = DigestStore(ws).load()
            pending = ActionStore(ws).pending()
        except (OSError, ValueError):
            facts, specs, pending = 0, [], 0
        rows = [
            ("범위", SCOPE_LABELS.get(cfg.scope, cfg.scope)),
            ("유형", cfg.profile.type_label() or "미지정"),
            ("자료", f"{len(ws.inputs())}개" + (" · 지침 있음" if ws.has_brief() else "")),
            ("지식", f"{facts}개" if cfg.knowledge else "꺼짐"),
            ("정기 정리", f"{len(specs)}개 · 켜짐 {sum(1 for s in specs if s.enabled)}" if specs else "없음"),
            ("제안", f"대기 {pending}건" if cfg.actions else "꺼짐"),
            ("실행", f"{len(runs)}회 · 완료 {completed}"),
            ("이번 주기", f"${spent:,.2f}" + (f" / ${cfg.budget_monthly_usd:,.0f}" if cfg.budget_monthly_usd else "")),
            ("출처", source),
        ]
        card = self.query_one("#ws-card", Static)
        card.border_title = cfg.name
        card.update(_kv(rows, 11))

    def _run_label(self, record: dict) -> Text:
        digest = record.get("digest") or {}
        if digest:
            return Text(f"정기 · {_short(str(digest.get('title', '')), 14)}", style=PERIWINKLE)
        playbook = self.playbooks.get(record.get("playbook") or "")
        text = Text(playbook.title if playbook else "자유 목표", style="" if playbook else MUTED)
        if record.get("mode") == "direct":
            text.append(" · 바로", style=PERIWINKLE)
        return text

    def _record_status(self, record: dict) -> str:
        status = str(record.get("status", ""))
        if status == "running":
            job = self.runs.get(record.get("run_id", ""))
            if job is None or job.phase != "running":
                return "interrupted"
        return status

    def _fill_history(self) -> None:
        table = self.query_one("#runs", DataTable)
        _reset_table(table)
        self.history = {}
        if self.ws is None:
            self.call_later(self._show_preview, None)
            return
        for record in self.ws.recent_runs(limit=200):
            run_id = record["run_id"]
            self.history[run_id] = record
            tasks = record.get("tasks") or []
            done = sum(1 for t in tasks if t.get("status") in ("done", "reused"))
            total = len(tasks) or len((record.get("plan") or {}).get("tasks", []))
            created = record.get("created", "")
            goal = Text("↳ ", style=PERIWINKLE) if record.get("base_run") else Text()
            goal.append(_short(record.get("goal", ""), 42))
            table.add_row(
                Text(created[5:16].replace("T", " "), style=MUTED),
                status_text(self._record_status(record)),
                self._run_label(record),
                goal,
                f"{done}/{total}",
                f"${record.get('cost_total', 0):.3f}",
                key=run_id,
            )
        self.call_later(self._show_preview, next(iter(self.history), None))

    def _update_history_row(self, job: Job) -> None:
        table = self.query_one("#runs", DataTable)
        if not self._is_selected(job) or job.run_id not in table.rows:
            return
        status = job.phase if job.phase in STATUS_STYLES else "running"
        table.update_cell(job.run_id, "status", status_text(status))
        table.update_cell(job.run_id, "tasks", f"{job.done}/{job.total}")
        if self.runtime is not None:
            table.update_cell(job.run_id, "cost", f"${self.runtime.ledger.spent_run(job.run_id):.3f}")

    async def _show_preview(self, run_id: str | None) -> None:
        try:
            preview = self.query_one("#run-preview", Markdown)
        except NoMatches:
            return
        if run_id is None or run_id not in self.history:
            await preview.update("실행 기록이 없습니다. 실행 탭에서 목표를 입력하고 F5로 계획하거나 F9로 바로 실행할 수 있습니다.")
            return
        record = self.history[run_id]
        lines = [f"**목표**  {record.get('goal', '').strip()}", ""]
        if self._record_status(record) in RESUMABLE_VIEW:
            lines += ["> 끝나지 않은 실행임. Ctrl+Enter로 완료된 작업은 그대로 두고 남은 작업부터 재개할 수 있음", ""]
        elif record.get("status") in FOLLOWABLE:
            learned = record.get("knowledge") or 0
            proposed = len(record.get("actions") or [])
            hint = "> A로 이 보고서에 대한 행동 제안을 받을 수 있음" if not proposed else f"> 행동 제안 {proposed}건 · F10"
            if learned:
                hint += f" · 지식 {learned}개 추가됨"
            lines += [hint, ""]
        base = record.get("base_run")
        if base:
            parent = self.history.get(base)
            label = _short(parent.get("goal", ""), 60) if parent else f"{base} (삭제됨)"
            lines += [f"**이어서 한 실행**  {label}", ""]
        models = record.get("models") or {}
        if models:
            names = sorted({model_label(m) for m in models.values()})
            lines += [f"**모델**  {', '.join(names)}", ""]
        summary = (record.get("summary") or "").strip()
        lines += ["**요약**", "", summary or "(요약 없음)", ""]
        tasks = record.get("tasks") or []
        if tasks:
            lines += ["**작업**", ""]
            lines += [
                f"- {t['id']} {t.get('title', '')} · {STATUS_STYLES.get(t.get('status', ''), ('', t.get('status'), ''))[1]}"
                f" · ${t.get('cost', 0):.4f}"
                for t in tasks
            ]
        for warning in record.get("warnings") or []:
            lines += ["", f"> {warning}"]
        await preview.update("\n".join(lines))

    def _cursor_run(self) -> str | None:
        table = self.query_one("#runs", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    @on(DataTable.RowHighlighted, "#runs")
    async def _run_highlighted(self, event: DataTable.RowHighlighted) -> None:
        await self._show_preview(event.row_key.value if event.row_key else None)

    @on(DataTable.RowSelected, "#runs")
    def _run_selected(self, event: DataTable.RowSelected) -> None:
        self._open_run(event.row_key.value if event.row_key else None)

    def _open_run(self, run_id: str | None) -> None:
        if self.ws is None or run_id is None:
            return
        record = self.ws.load_run(run_id)
        if record is None:
            self.notify("실행 기록을 읽을 수 없음", severity="warning")
            return

        def closed(result: str | None) -> None:
            if result == "follow":
                self.action_follow_up(run_id)

        self.push_screen(RunViewerModal(self.ws, record), closed)

    def _fill_inputs(self) -> None:
        table = self.query_one("#inputs", DataTable)
        _reset_table(table)
        policy = self.query_one("#policy", Static)
        import_button = self.query_one("#import", Button)
        runs_button = self.query_one("#import-run", Button)
        brief_button = self.query_one("#edit-brief", Button)
        self.query_one("#edit-profile", Button).disabled = self.ws is None
        if self.ws is None:
            policy.update("")
            import_button.disabled = True
            runs_button.disabled = True
            brief_button.disabled = True
            self.query_one("#brief-state", Static).update("")
            return
        cfg = self.ws.config
        import_button.disabled = not scope_allows_inputs(cfg.scope)
        runs_button.disabled = not scope_allows_inputs(cfg.scope)
        brief_button.disabled = False
        for f in self.ws.inputs():
            table.add_row(
                f.name, Text(f.classification, style=CLASS_STYLE.get(f.classification, MUTED)), f"{f.chars:,}",
                Text(_local_time(f.imported_at), style=MUTED), key=f.name,
            )
        brief = Text("지침 ", style=MUTED)
        chars = len(self.ws.read_brief().strip())
        brief.append(f"{chars:,}자" if chars else "없음")
        self.query_one("#brief-state", Static).update(brief)
        web = cfg.web
        domains = ", ".join(web.allowed_domains or web.blocked_domains) or "-"
        mode = "허용 목록" if web.allowed_domains else ("차단 목록" if web.blocked_domains else "제한 없음")
        rows = [
            ("활동 범위", SCOPE_LABELS.get(cfg.scope, cfg.scope)),
            ("전송 허용 등급", f"{cfg.max_send_classification} 이하 (confidential은 실행마다 명시 허용)"),
            ("개인정보 가림", "켜짐" if cfg.redact_pii else "꺼짐"),
            ("웹 출처", f"{mode} · {domains}"),
            ("작업당 검색/페치", f"{web.max_searches_per_task} / {web.max_fetches_per_task}"),
            ("조사 재사용", f"{cfg.reuse_max_age_days}일 이내"),
            ("누적 지식", "켜짐 · 출처 있는 사실만 저장" if cfg.knowledge else "꺼짐"),
            ("행동 제안", ", ".join(TYPE_LABELS[a] for a in cfg.actions) + " · 승인해야 파일 생성"
             if cfg.actions else "꺼짐"),
            ("월 예산", f"${cfg.budget_monthly_usd:,.2f}" if cfg.budget_monthly_usd else "전체 예산 공유"),
        ]
        policy.update(_kv(rows, 18))

    def _task_row(self, job: Job, task) -> tuple:
        info = job.tasks.get(task.id, {})
        cost = info.get("cost")
        if cost is None:
            estimate = job.session.estimate["tasks"].get(task.id, 0.0) if job.session else 0.0
            cost_cell = Text(f"~${estimate:.3f}", style=MUTED)
        else:
            cost_cell = Text(f"${cost:.4f}")
        return (
            task.id,
            _short(task.title, 40),
            str(info.get("effort") or task.effort),
            f"{task.max_searches}회" if task.use_web else Text("-", style=FAINT),
            status_text(info.get("status", "pending")),
            cost_cell,
        )

    def _paint_job(self) -> None:
        job = self._job()
        table = self.query_one("#tasks", DataTable)
        _reset_table(table)
        card = self.query_one("#plan-card", Static)
        if job is not None and job.session is not None and job.phase not in ("stopped", "rejected"):
            for task in job.session.plan.tasks:
                table.add_row(*self._task_row(job, task), key=task.id)
            self._paint_plan_card(job)
        else:
            card.remove_class("-visible")
            card.update("")
        self.query_one("#log", ActivityLog).attach(job.log if job is not None else self.idle_log)
        self._paint_state()
        self._paint_buttons()
        self._paint_lock()

    def _paint_plan_card(self, job: Job) -> None:
        session = job.session
        card = Text()
        card.append(session.plan.title, style="bold")
        card.append("\n" + session.plan.approach, style=MUTED)
        if session.context.base:
            card.append(f"\n↳ 이어서 작업: {_short(session.context.base['goal'], 60)}", style=PERIWINKLE)
        if session.resumed:
            reused = len(session.prior_states)
            spent = self.runtime.ledger.spent_run(job.run_id) if self.runtime is not None else 0.0
            card.append(f"\n재개: 완료된 작업 {reused}개는 그대로 쓰고 {len(session.plan.tasks) - reused}개 실행"
                        f" · 이미 사용 ${spent:.3f}", style=PERIWINKLE)
        card.append("\n예상 ", style=MUTED)
        card.append(f"${session.estimate['total']:.3f}", style=f"bold {CLAY}")
        if session.direct:
            card.append(f"  ·  상한 ${session.run_cap:.2f}  ·  에이전트 1개, 계획과 종합 단계 없음", style=MUTED)
        else:
            card.append(f"  ·  상한 ${session.run_cap:.2f}  ·  계획 비용 ${session.plan_cost:.4f}"
                        f"  ·  종합 effort {session.plan.synth_effort}", style=MUTED)
        names = sorted({model_label(m) for m in job.orch.models_in_use().values()})
        card.append(f"  ·  모델 {', '.join(names)}", style=MUTED)
        for note in session.plan_notes:
            card.append(f"\n조정: {note}", style=WARNING)
        kind = "재개" if session.resumed else ("바로 실행" if session.direct else "계획")
        if job.phase == "planned":
            title = f"{kind} · 확인 후 F6"
        elif job.phase == "running":
            title = f"{kind} · 실행 중"
        else:
            title = f"{kind} · 종료"
        plan_card = self.query_one("#plan-card", Static)
        plan_card.border_title = title
        plan_card.update(card)
        plan_card.add_class("-visible")

    def _paint_state(self) -> None:
        job = self._job()
        widget = self.query_one("#run-state", Static)
        if job is None:
            widget.update("")
            return
        label, color = job.state
        elapsed = clock_text(job.elapsed(self.clock()))
        if job.phase == "running":
            text = f"{label} · {job.done}/{job.total} · ${self._job_cost(job):.3f} · {elapsed} · Esc 중단"
        elif job.phase == "planning":
            text = f"{label} · {elapsed} · Esc 중단"
        elif job.started and job.phase in FINISHED_PHASES:
            text = f"{label} · {elapsed}"
        else:
            text = label
        widget.update(Text(text, style=color))

    def _paint_buttons(self) -> None:
        job = self._job()
        phase = job.phase if job is not None else "idle"
        ready = self.runtime is not None
        plan_button = self.query_one("#plan", Button)
        plan_button.display = ready and phase not in ("planning", "running")
        plan_button.label = "다시 계획  F5" if phase == "planned" else "계획  F5"
        self.query_one("#direct", Button).display = ready and phase not in ("planning", "running")
        exec_button = self.query_one("#exec", Button)
        exec_button.display = phase == "planned"
        exec_button.label = "재개  F6" if job is not None and job.resumed else "실행  F6"
        cancel = self.query_one("#cancel", Button)
        cancel.display = phase in ("planning", "planned", "running")
        cancel.label = "중단  Esc" if phase in ("planning", "running") else "취소  F8"
        for button in (plan_button, exec_button, cancel):
            button.refresh(layout=True)

    def _paint_lock(self) -> None:
        job = self._job()
        locked = job is not None and job.active
        goal = self.query_one("#goal", TextArea)
        goal.read_only = locked
        goal.set_class(locked, "-locked")
        self.query_one("#pb", Select).disabled = locked
        self.query_one("#cap", Input).disabled = locked
        self.query_one("#follow-clear", Button).disabled = locked

    def _paint_jobs_card(self) -> None:
        card = self.query_one("#jobs-card", JobsCard)
        selected = self.ws.name if self.ws is not None else None
        shown = [job for job in self.jobs.values()
                 if job.name != selected and (job.busy or (job.phase in FINISHED_PHASES and not job.seen))]
        shown += [job for job in self.digest_jobs.values() if job.active]
        if not shown:
            card.display = False
            card.targets = []
            return
        now = self.clock()
        text = Text()
        targets: list[tuple[int, int, str]] = []
        line = 0
        for job in shown:
            if targets:
                text.append("\n\n")
                line += 2
            start = line
            color = CLAY if job.busy else PHASE_COLORS.get(job.phase, MUTED)
            label = ("정기 " if job.kind == "digest" else "") + PHASE_LABELS.get(job.phase, job.phase)
            clock = f" {clock_text(job.elapsed(now))}" if job.started else ""
            text.append("● " if job.active else "○ ", style=color)
            text.append(_fit(job.name, CARD_CELLS - 2 - cell_len(label) - cell_len(clock) - 1), style="bold underline")
            text.append(f" {label}", style=color)
            text.append(clock, style=MUTED)
            text.append("\n" + _fit(job.goal, CARD_CELLS))
            line += 2
            if job.total:
                text.append(f"\n{job.done}/{job.total} · ${self._job_cost(job):.3f}", style=MUTED)
                line += 1
            last = job.log.last()
            if last:
                text.append("\n" + _fit(last, CARD_CELLS), style=FAINT)
                line += 1
            targets.append((start, line, job.name, "tab-digests" if job.kind == "digest" else "tab-run"))
        card.targets = targets
        card.border_title = "진행 중인 다른 작업"
        card.update(text)
        card.display = True

    def _job_changed(self, job: Job) -> None:
        with suppress(NoMatches):
            if self._is_selected(job):
                self._paint_job()
            self._paint_jobs_card()
            self._refresh_ws_label(job.name)

    def _on_event(self, event: Event) -> None:
        if event.kind == "llm.usage":
            self._refresh_meter()
        job = self.runs.get(event.run_id)
        if job is None:
            return
        body = eventfmt.describe(event)
        tid = event.task_id
        if body is not None:
            job.log.append((escape(f"[{tid}] ") if tid else "") + body, event.ts)
        info = job.tasks.get(tid) if tid else None
        if info is not None:
            if event.kind in TASK_EVENT_STATUS:
                info["status"] = TASK_EVENT_STATUS[event.kind]
            if event.kind == "task.done":
                info["cost"] = float(event.data.get("cost", 0))
            if event.kind == "task.retry":
                info["effort"] = str(event.data.get("effort"))
        if event.kind in TASK_END_EVENTS:
            job.done += 1
        if event.kind == "llm.usage":
            job.cost += float(event.data.get("cost", 0))
        if event.kind == "synth.started":
            job.state = ("종합 중", CLAY)
        elif event.kind == "knowledge.started":
            job.state = ("지식 정리 중", CLAY)
        elif event.kind == "actions.started":
            job.state = ("행동 제안 정리 중", CLAY)
        if job.kind == "digest":
            with suppress(NoMatches):
                self._paint_jobs_card()
                if self._is_selected(job):
                    self._refresh_digest_row(job.digest_id)
                    if event.kind in TASK_END_EVENTS or event.kind == "run.started":
                        self._update_history_row(job)
            return
        with suppress(NoMatches):
            if not self._is_selected(job):
                self._paint_jobs_card()
                return
            self.query_one("#log", ActivityLog).sync()
            table = self.query_one("#tasks", DataTable)
            if info is not None and tid in table.rows and job.session is not None:
                task = next((t for t in job.session.plan.tasks if t.id == tid), None)
                if task is not None:
                    row = self._task_row(job, task)
                    for key, value in (("effort", row[2]), ("status", row[4]), ("cost", row[5])):
                        table.update_cell(tid, key, value)
            self._paint_state()
            if event.kind in TASK_END_EVENTS or event.kind == "run.started":
                self._update_history_row(job)

    @on(Button.Pressed, "#plan")
    def _plan_pressed(self) -> None:
        self.action_plan()

    @on(Button.Pressed, "#direct")
    def _direct_pressed(self) -> None:
        self.action_direct()

    @on(Button.Pressed, "#exec")
    def _exec_pressed(self) -> None:
        self.action_execute()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#new-ws")
    def _new_ws_pressed(self) -> None:
        self.action_new_workspace()

    @on(Button.Pressed, "#new-pb")
    def _new_pb_pressed(self) -> None:
        self.action_new_playbook()

    @on(Button.Pressed, "#import")
    def _import_pressed(self) -> None:
        self.action_import_inputs()

    @on(Button.Pressed, "#import-run")
    def _import_run_pressed(self) -> None:
        self.action_import_runs()

    @on(Button.Pressed, "#edit-brief")
    def _brief_pressed(self) -> None:
        self.action_edit_brief()

    @on(Button.Pressed, "#follow-clear")
    def _follow_clear_pressed(self) -> None:
        self._clear_follow()
        self.query_one("#goal", TextArea).focus()

    @on(Select.Changed, "#pb")
    def _playbook_changed(self, event: Select.Changed) -> None:
        self._update_goal_hint()

    @on(Select.Changed, "#model")
    def _model_changed(self, event: Select.Changed) -> None:
        value = None if event.value in (CONFIG_MODEL, Select.NULL) else str(event.value)
        self.model_pref = value
        label = model_label(value) if value else "설정 기본값"
        job = self._job()
        if job is not None and job.busy:
            try:
                job.orch.set_model(value, job.run_id if job.phase == "running" else None)
            except RunError as exc:
                self.notify(str(exc), title="모델 변경 실패", severity="error")
                return
            job.model = value
            if job.phase != "running":
                job.log.append(f"[{WARNING}]모델 변경: {escape(label)} (다음 호출부터 적용)[/]")
            if job.phase == "planned" and job.session is not None and self.runtime is not None:
                session = job.session
                estimate = estimate_plan(session.plan, job.ws, job.orch.settings, self.runtime.ledger,
                                         synthesize=not session.direct)
                for task_id in session.prior_states:
                    estimate["tasks"][task_id] = 0.0
                session.estimate = finish_estimate(estimate)
            self._paint_job()
        if value == "claude-fable-5-1":
            self.notify("Fable 5.1은 Opus 5.5보다 토큰 단가가 약 2.5배 높음", title="모델 비용", severity="warning")

    def _update_goal_hint(self) -> None:
        value = self.query_one("#pb", Select).value
        playbook = self.playbooks.get(str(value)) if value is not Select.NULL else None
        if self.follow_run:
            hint = FOLLOW_GOAL_HINT
        else:
            hint = playbook.goal_hint if playbook and playbook.goal_hint else DEFAULT_GOAL_HINT
        self.query_one("#goal", TextArea).placeholder = hint

    @on(Markdown.LinkClicked)
    def _markdown_link(self, event: Markdown.LinkClicked) -> None:
        event.stop()
        open_link(self, event.href)

    def action_show_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab
        focused = self.screen.focused
        panes = [node for node in focused.ancestors_with_self if isinstance(node, TabPane)] if focused else []
        if tab in TAB_FOCUS and (not panes or panes[0].id != tab):
            self.query_one(TAB_FOCUS[tab]).focus()

    async def _generate_brief(self, **kwargs) -> tuple[str, float]:
        if self.runtime is None:
            raise RuntimeError("런타임이 초기화되지 않음")
        text, cost = await generate_brief(self.runtime, **kwargs)
        self._refresh_meter()
        return text, cost

    def action_new_workspace(self) -> None:
        def created(name: str | None) -> None:
            if name:
                self.notify(f"작업 영역 '{name}' 생성", timeout=3)
                self._reload_workspaces(select=name)

        generator = self._generate_brief if self.runtime is not None else None
        self.push_screen(CreateWorkspaceModal(generator), created)

    def action_edit_brief(self) -> None:
        if self.ws is None:
            self.notify("먼저 작업 영역을 선택해야 함", severity="warning")
            return

        def saved(chars: int | None) -> None:
            if chars is None:
                return
            self.notify(f"지침 저장 ({chars:,}자)" if chars else "지침 삭제됨", timeout=3)
            self._fill_inputs()
            self._fill_card()

        generator = self._generate_brief if self.runtime is not None else None
        self.push_screen(BriefModal(self.ws, generator), saved)

    def action_new_playbook(self) -> None:
        def created(playbook_id: str | None) -> None:
            if not playbook_id:
                return
            self.playbooks = all_playbooks()
            select = self.query_one("#pb", Select)
            select.set_options(self._playbook_options())
            if not select.disabled:
                select.value = playbook_id
            self.notify(f"플레이북 '{self.playbooks[playbook_id].title}' 추가", timeout=3)

        self.push_screen(PlaybookModal(), created)

    def _inputs_allowed(self) -> bool:
        if self.ws is None:
            self.notify("먼저 작업 영역을 선택해야 함", severity="warning")
            return False
        if not scope_allows_inputs(self.ws.config.scope):
            self.notify("이 작업 영역은 웹 전용이라 자료를 가져올 수 없음", severity="warning")
            return False
        return True

    def _inputs_imported(self, names: list[str] | None) -> None:
        if names:
            self.notify(f"{len(names)}개 가져옴: {', '.join(names)}", timeout=4)
            self._fill_inputs()
            self._fill_card()

    def action_import_inputs(self) -> None:
        if self._inputs_allowed():
            self.push_screen(ImportModal(self.ws), self._inputs_imported)

    def action_import_runs(self) -> None:
        if self._inputs_allowed():
            self.push_screen(RunImportModal(self.ws), self._inputs_imported)

    def _input_blocker(self, ws: Workspace, name: str) -> str | None:
        job = self.jobs.get(ws.name)
        if job is None or not job.busy:
            return None
        if job.phase == "planning":
            return "계획을 세우는 중이라 자료를 제거할 수 없음. Esc로 중단한 뒤 제거해야 함"
        if job.session is not None and any(name in task.inputs for task in job.session.plan.tasks):
            if job.phase == "planned":
                return "승인 대기 중인 계획이 이 자료를 사용함. F8로 계획을 취소한 뒤 제거해야 함"
            return "진행 중인 실행이 이 자료를 사용함. Esc로 중단한 뒤 제거해야 함"
        return None

    def action_delete_input(self) -> None:
        if self.ws is None:
            return
        table = self.query_one("#inputs", DataTable)
        if not table.row_count:
            return
        name = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        ws = self.ws
        entry = next((f for f in ws.inputs() if f.name == name), None)
        if entry is None:
            self._fill_inputs()
            return
        blocker = self._input_blocker(ws, name)
        if blocker:
            self.notify(blocker, severity="warning")
            return
        row = table.cursor_row
        detail = (
            f"'{_short(name, 40)}' ({entry.classification}, {entry.chars:,}자) 자료가 영구 삭제됨. "
            "이 자료로 만든 보고서와 기록은 남지만, 이 자료가 필요한 중단된 실행은 재개할 수 없게 됨"
        )

        def answered(ok: bool | None) -> None:
            if not ok or self.ws is not ws:
                return
            blocker = self._input_blocker(ws, name)
            if blocker:
                self.notify(blocker, severity="warning")
                return
            try:
                removed = ws.remove_input(name)
            except (WorkspaceError, OSError) as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            if removed:
                self.runtime_audit(
                    "input.removed", workspace=ws.name, name=name, classification=entry.classification,
                    sha256=entry.sha256,
                )
                self.notify(f"자료 '{_short(name, 40)}'을 제거함", timeout=3)
            self._fill_inputs()
            self._fill_card()
            if table.row_count:
                table.move_cursor(row=min(row, table.row_count - 1))

        self.push_screen(ConfirmModal("정말로 제거하시겠습니까?", detail), answered)

    def action_open_report(self) -> None:
        if self.ws is None:
            return
        if self.query_one("#tabs", TabbedContent).active == "tab-history":
            run_id = self._cursor_run()
            if run_id:
                self._open_run(run_id)
                return
        job = self._job()
        target = job.run_id if job is not None and job.report else next(iter(self.history), None)
        if target is None:
            self.notify("볼 실행 기록이 없음", severity="warning")
            return
        self._open_run(target)

    def action_export_report(self) -> None:
        if self.ws is None:
            return
        run_id = self._cursor_run()
        record = self.ws.load_run(run_id) if run_id else None
        if record is None:
            self.notify("복사할 실행 기록이 없음", severity="warning")
            return
        self.start_export(self.ws, record)

    def start_export(self, ws: Workspace, record: dict) -> None:
        if not ws.report_size(record["run_id"]):
            self.notify("이 실행에는 보고서가 없음", severity="warning")
            return

        def done(path: str | None) -> None:
            if not path:
                return
            folder = str(Path(path).parent)
            self.export_dir = folder_hint(folder)
            self.notify(path, title="보고서를 복사함", timeout=6)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.push_screen(ExportModal(ws, record, self.export_dir), done)

        if report_classification(record) == "confidential":
            detail = ("confidential 자료 전송을 허용했던 실행의 보고서임. claubiz 밖으로 복사하면 "
                      "작업 영역 정책과 감사 범위를 벗어나므로 보관 위치를 확인해야 함")
            self.push_screen(ConfirmModal("기밀 자료가 담긴 보고서", detail, confirm_label="복사"), confirmed)
        else:
            confirmed(True)

    def _paint_follow(self) -> None:
        draft = self._draft()
        bar = self.query_one("#follow-bar")
        record = self.history.get(draft.follow_run or "") if draft.follow_run else None
        if draft.follow_run and record is None and self.ws is not None:
            record = self.ws.load_run(draft.follow_run)
        if record is None:
            draft.follow_run = None
            bar.display = False
        else:
            title = (record.get("plan") or {}).get("title") or record.get("goal", "")
            text = Text("↳ 이어서 작업  ", style=f"bold {PERIWINKLE}")
            text.append(_short(title, 60))
            text.append(f"   {record.get('created', '')[:10]}", style=MUTED)
            self.query_one("#follow-text", Static).update(text)
            bar.display = True
        self._update_goal_hint()

    def action_follow_up(self, run_id: str | None = None) -> None:
        if self.ws is None:
            return
        run_id = run_id or self._cursor_run()
        record = self.history.get(run_id or "") or (self.ws.load_run(run_id) if run_id else None)
        if record is None:
            return
        status = self._record_status(record)
        if status in RESUMABLE_VIEW:
            self._resume(record)
            return
        if status not in FOLLOWABLE:
            self.notify("완료된 실행은 이어서 작업하고, 중단되거나 실패한 실행은 재개할 수 있음", severity="warning")
            return
        job = self._job()
        if job is not None and job.active:
            self.notify("이 작업 영역의 진행 중인 작업이 끝난 뒤 이어서 작업할 수 있음", severity="warning")
            return
        draft = self._draft()
        draft.follow_run = record["run_id"]
        playbook = record.get("playbook")
        select = self.query_one("#pb", Select)
        select.value = playbook if playbook in self.playbooks else Select.NULL
        self.query_one("#goal", TextArea).text = ""
        self._paint_follow()
        self.action_show_tab("tab-run")
        self.query_one("#goal", TextArea).focus()

    def _clear_follow(self) -> None:
        if self.ws is not None:
            self._draft().follow_run = None
        self._paint_follow()

    def _resume(self, record: dict) -> None:
        ws = self.ws
        if ws is None or self.runtime is None:
            return
        run_id = record["run_id"]
        current = self.jobs.get(ws.name)
        if current is not None and current.active:
            self.notify("이 작업 영역의 진행 중인 작업이 끝난 뒤 재개할 수 있음", severity="warning")
            return
        try:
            orch = self.runtime.orchestrator(ws)
            orch.set_model(self.model_pref)
            session = orch.resume(run_id)
        except (RunError, WorkspaceError, OSError) as exc:
            self.notify(str(exc), title="재개할 수 없음", severity="warning")
            return
        if current is not None and current.phase == "planned" and current.session is not None:
            current.orch.cancel_planned(current.session)
        job = self._new_job(ws, orch, run_id, session.goal, direct=session.direct, resumed=True)
        job.load_plan(session)
        job.phase = "planned"
        reused = len(session.prior_states)
        job.log.append(f"재개 준비: 완료된 작업 {reused}개는 그대로 쓰고 {len(session.plan.tasks) - reused}개를 실행함")
        job.state = ("재개 대기 · F6으로 재개", CLAY)
        draft = self._draft()
        draft.goal = session.goal
        draft.follow_run = None
        self._restore_composer()
        self.action_show_tab("tab-run")
        self._job_changed(job)

    def action_delete_run(self) -> None:
        if self.ws is None:
            return
        run_id = self._cursor_run()
        if run_id is None:
            return
        owner = self.runs.get(run_id)
        if owner is not None and owner.active:
            self.notify("진행 중인 실행 기록은 제거할 수 없음", severity="warning")
            return
        record = self.history.get(run_id, {})
        ws = self.ws
        facts, actions = derived_counts(ws, run_id)
        derived = ""
        if facts or actions:
            derived = f"이 실행에서 나온 지식 {facts}개와 행동 제안 {actions}건(만든 파일 포함)도 함께 삭제됨. "
        detail = (
            f"'{_short(record.get('goal', run_id), 40)}' 실행 기록과 보고서, 작업 노트가 영구 삭제됨. {derived}"
            f"이미 쓴 비용 ${record.get('cost_total', 0):.3f}은 예산 장부에 그대로 남음"
        )

        def answered(ok: bool | None) -> None:
            if not ok or self.ws is not ws:
                return
            job = self.runs.get(run_id)
            if job is not None:
                if job.active:
                    self.notify("진행 중인 실행 기록은 제거할 수 없음", severity="warning")
                    return
                if job.phase == "planned" and job.session is not None:
                    job.orch.cancel_planned(job.session)
                self.runs.pop(run_id, None)
                if self.jobs.get(job.name) is job:
                    del self.jobs[job.name]
            try:
                ws.delete_run(run_id)
                removed_facts, removed_actions = purge_derived(ws, run_id)
            except (WorkspaceError, ActionError, OSError) as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            self.runtime_audit("run.deleted", workspace=ws.name, run_id=run_id, knowledge=removed_facts,
                               actions=removed_actions)
            if self._draft().follow_run == run_id:
                self._clear_follow()
            self._fill_history()
            self._fill_facts()
            self._fill_actions()
            self._fill_digests()
            self._fill_card()
            self._paint_job()
            self._refresh_ws_label(ws.name)
            self.notify("실행 기록을 제거함", timeout=3)

        self.push_screen(ConfirmModal("정말로 제거하시겠습니까?", detail), answered)

    def action_delete_workspace(self, name: str | None = None) -> None:
        option_list = self.query_one("#ws-list", WorkspaceList)
        if name is None:
            if option_list.highlighted is None:
                return
            name = option_list.get_option_at_index(option_list.highlighted).id
        if name is None:
            return
        job = self.jobs.get(name)
        if (job is not None and job.active) or self._digest_busy(name):
            self.notify("이 작업 영역에서 작업이 진행 중이라 제거할 수 없음", severity="warning")
            return
        try:
            target = Workspace.open(name)
            inputs, runs = len(target.inputs()), len(target.recent_runs(limit=10000))
        except WorkspaceError:
            inputs, runs = 0, 0
        detail = (
            f"작업 영역 '{name}'의 자료 {inputs}개, 실행 기록 {runs}개, 보고서와 지침이 영구 삭제됨. "
            "이미 쓴 비용은 예산 장부에 그대로 남음"
        )

        def answered(ok: bool | None) -> None:
            if not ok:
                return
            job = self.jobs.get(name)
            if self._digest_busy(name):
                self.notify("이 작업 영역에서 정기 정리가 진행 중이라 제거할 수 없음", severity="warning")
                return
            for key in [k for k, j in self.digest_jobs.items() if j.name == name]:
                self.runs.pop(self.digest_jobs.pop(key).run_id, None)
            if job is not None:
                if job.active:
                    self.notify("이 작업 영역에서 작업이 진행 중이라 제거할 수 없음", severity="warning")
                    return
                if job.phase == "planned" and job.session is not None:
                    job.orch.cancel_planned(job.session)
                self.runs.pop(job.run_id, None)
                del self.jobs[name]
            self.drafts.pop(name, None)
            if self.ws is not None and self.ws.name == name:
                self.ws = None
            try:
                Workspace.delete(name)
            except (WorkspaceError, OSError) as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            self.runtime_audit("workspace.deleted", name=name, inputs=inputs, runs=runs)
            self._reload_workspaces()
            self.notify(f"작업 영역 '{name}'을 제거함", timeout=3)

        self.push_screen(ConfirmModal("정말로 제거하시겠습니까?", detail), answered)

    def _cursor_key(self, selector: str) -> str | None:
        table = self.query_one(selector, DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    @staticmethod
    def _keep_cursor(table: DataTable, row: int) -> None:
        if table.row_count:
            table.move_cursor(row=min(max(row, 0), table.row_count - 1))

    def _fill_facts(self) -> None:
        table = self.query_one("#facts", DataTable)
        row = table.cursor_row
        _reset_table(table)
        self.facts_by_id = {}
        if self.ws is None:
            table.border_title = "누적 지식"
            self._show_fact(None)
            return
        try:
            facts = KnowledgeStore(self.ws).facts()
        except (OSError, ValueError):
            facts = []
        facts.sort(key=lambda f: (f.as_of, f.added), reverse=True)
        for fact in facts:
            self.facts_by_id[fact.id] = fact
            table.add_row(
                Text(fact.as_of, style=MUTED), _short(fact.topic or "-", 16), _short(fact.text, 90),
                Text(fact.classification, style=CLASS_STYLE.get(fact.classification, MUTED)), key=fact.id,
            )
        state = "" if self.ws.config.knowledge else " · 꺼짐 (업무 프로필에서 켜기)"
        table.border_title = f"누적 지식 {len(facts)}개{state} · Del 제거"
        self._keep_cursor(table, row)
        self._show_fact(self._cursor_key("#facts"))

    def _show_fact(self, fact_id: str | None) -> None:
        try:
            detail = self.query_one("#fact-detail", Static)
        except NoMatches:
            return
        fact = self.facts_by_id.get(fact_id or "")
        if fact is None:
            detail.update(Text("실행이 끝나면 보고서에서 출처가 있는 사실만 골라 여기에 쌓음. 다음 실행은 이를 참고해 "
                               "같은 조사를 반복하지 않음", style=MUTED))
            return
        text = Text(fact.text)
        record = self.history.get(fact.run_id)
        origin = _short(record.get("goal", ""), 40) if record else f"{fact.run_id} (기록 없음)"
        text.append(f"\n{fact.as_of} · {' · '.join(fact.sources[:3])} · 실행: {origin}", style=MUTED)
        detail.update(text)

    @on(DataTable.RowHighlighted, "#facts")
    def _fact_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_fact(event.row_key.value if event.row_key else None)

    def action_delete_fact(self) -> None:
        if self.ws is None:
            return
        fact_id = self._cursor_key("#facts")
        fact = self.facts_by_id.get(fact_id or "")
        if fact is None:
            return
        ws = self.ws

        def answered(ok: bool | None) -> None:
            if not ok or self.ws is not ws:
                return
            try:
                removed = KnowledgeStore(ws).remove([fact.id])
            except OSError as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            if removed:
                self.runtime_audit("knowledge.removed", workspace=ws.name, ids=[fact.id], removed=removed)
            self._fill_facts()
            self._fill_card()

        detail = f"'{_short(fact.text, 60)}'. 다음 실행부터 참고하지 않음"
        self.push_screen(ConfirmModal("이 지식을 지울까요?", detail), answered)

    def action_edit_profile(self) -> None:
        if self.ws is None:
            self.notify("먼저 작업 영역을 선택해야 함", severity="warning")
            return
        ws = self.ws

        def saved(ok: bool | None) -> None:
            if not ok:
                return
            self.notify("업무 프로필 저장", timeout=3)
            if self.ws is ws:
                self._fill_inputs()
                self._fill_facts()
                self._fill_actions()
                self._fill_card()

        self.push_screen(ProfileModal(ws), saved)

    def _digest_busy(self, name: str) -> bool:
        return any(job.active and job.name == name for job in self.digest_jobs.values())

    def _digest_job(self, digest_id: str) -> Job | None:
        if self.ws is None:
            return None
        job = self.digest_jobs.get(f"{self.ws.name}/{digest_id}")
        return job if job is not None and job.active else None

    def _digest_row(self, spec: DigestSpec) -> tuple:
        job = self._digest_job(spec.id)
        try:
            due = spec.next_due().strftime("%m-%d %H:%M") if spec.enabled else "-"
        except ValueError:
            due = "-"
        if job is not None:
            state = Text(f"● 실행 중 {clock_text(job.elapsed(self.clock()))}", style=CLAY)
        elif not spec.enabled:
            state = Text("꺼짐" + (" · 연속 실패" if spec.failures >= 3 else ""), style=FAINT)
        else:
            state = Text("켜짐", style=SUCCESS)
        status = spec.last_status
        if status == "running" and job is None and self.ws is not None:
            status = "running" if DigestStore(self.ws).is_running(spec.id) else "cancelled"
        if status:
            label, color = DIGEST_RESULT.get(status, (status, MUTED))
            last = Text(f"{label} · {_local_time(spec.last_attempt)[5:]}", style=color)
        else:
            last = Text("-", style=FAINT)
        model = model_label(spec.model) if spec.model else "기본"
        return (_short(spec.title, 24), spec.schedule, due, state, last, model, f"${spec.run_cap:.2f}")

    def _fill_digests(self) -> None:
        table = self.query_one("#digests", DataTable)
        row = table.cursor_row
        _reset_table(table)
        self.digest_specs = {}
        agent = self.query_one("#agent-state", Static)
        button = self.query_one("#new-digest", Button)
        if self.ws is None:
            button.disabled = True
            agent.update("")
            self.call_later(self._show_digest_preview, None)
            return
        allowed = scope_allows_web(self.ws.config.scope)
        button.disabled = not allowed
        try:
            specs = DigestStore(self.ws).load()
        except (OSError, ValueError):
            specs = []
        for spec in specs:
            self.digest_specs[spec.id] = spec
            table.add_row(*self._digest_row(spec), key=spec.id)
        if not allowed:
            agent.update(Text("자료 전용 작업 영역이라 정기 정리를 쓸 수 없음", style=MUTED))
        elif registration_path().is_file():
            agent.update(Text("백그라운드 실행 등록됨 · TUI를 닫아도 실행", style=SUCCESS))
        else:
            agent.update(Text("TUI 실행 중에만 동작 · 상시 실행: claubiz agent install", style=MUTED))
        self._keep_cursor(table, row)
        self.call_later(self._show_digest_preview, self._cursor_key("#digests"))

    def _refresh_digest_row(self, digest_id: str) -> None:
        table = self.query_one("#digests", DataTable)
        spec = self.digest_specs.get(digest_id)
        if spec is None or digest_id not in table.rows:
            return
        values = self._digest_row(spec)
        for key, value in zip(("title", "schedule", "next", "state", "last", "model", "cap"), values, strict=True):
            table.update_cell(digest_id, key, value)

    async def _show_digest_preview(self, digest_id: str | None) -> None:
        try:
            preview = self.query_one("#digest-preview", Markdown)
        except NoMatches:
            return
        spec = self.digest_specs.get(digest_id or "")
        if spec is None or self.ws is None:
            await preview.update(
                "정기 정리가 없습니다. '+ 새 정기 정리'로 요청과 일정을 정하면 그때마다 에이전트 하나가 지난 실행 이후의 "
                "새 소식만 찾아 정리합니다. 이미 보고한 링크는 건너뛰어 토큰을 아낍니다."
            )
            return
        model = model_label(spec.model) if spec.model else "설정 기본값"
        lines = [f"**요청**  {spec.request}", "",
                 (f"**일정**  {spec.schedule} · 모델 {model} · 검색 {spec.max_searches}회 · 상한 ${spec.run_cap:.2f}"
                 f" · 보고한 링크 {len(spec.seen_urls)}개 기억")]
        runs = [r for r in self.history.values() if (r.get("digest") or {}).get("id") == spec.id][:5]
        if runs:
            lines += ["", "**최근 실행**", ""]
            lines += [f"- {_local_time(r.get('created', ''))} · "
                      f"{STATUS_STYLES.get(self._record_status(r), ('', r.get('status'), ''))[1]}"
                      f" · ${r.get('cost_total', 0):.3f}" for r in runs]
        report = self.ws.read_report(spec.last_success_run) if spec.last_success_run else None
        if report:
            lines += ["", "---", "", report.rsplit(REPORT_FOOTER, 1)[0].strip()]
        await preview.update("\n".join(lines))

    @on(DataTable.RowHighlighted, "#digests")
    async def _digest_highlighted(self, event: DataTable.RowHighlighted) -> None:
        await self._show_digest_preview(event.row_key.value if event.row_key else None)

    @on(DataTable.RowSelected, "#digests")
    def _digest_selected(self, event: DataTable.RowSelected) -> None:
        self.action_edit_digest()

    @on(Button.Pressed, "#new-digest")
    def _new_digest_pressed(self) -> None:
        self.action_new_digest()

    @on(Button.Pressed, "#edit-profile")
    def _profile_pressed(self) -> None:
        self.action_edit_profile()

    def _digest_saved(self, ws: Workspace) -> Callable[[str | None], None]:
        def saved(digest_id: str | None) -> None:
            if not digest_id or self.ws is not ws:
                return
            self._fill_digests()
            self._fill_card()
            table = self.query_one("#digests", DataTable)
            with suppress(Exception):
                table.move_cursor(row=table.get_row_index(digest_id))
            spec = self.digest_specs.get(digest_id)
            if spec is not None:
                self.notify(f"{spec.title} · {spec.schedule} · 다음 실행 {spec.next_due():%m-%d %H:%M}", timeout=4)

        return saved

    def action_new_digest(self) -> None:
        if self.ws is None:
            self.notify("먼저 작업 영역을 선택해야 함", severity="warning")
            return
        if not scope_allows_web(self.ws.config.scope):
            self.notify("자료 전용 작업 영역은 정기 정리를 쓸 수 없음", severity="warning")
            return
        self.push_screen(DigestModal(self.ws), self._digest_saved(self.ws))

    def action_edit_digest(self) -> None:
        if self.ws is None:
            return
        spec = self.digest_specs.get(self._cursor_key("#digests") or "")
        if spec is None:
            self.action_new_digest()
            return
        self.push_screen(DigestModal(self.ws, spec), self._digest_saved(self.ws))

    def action_toggle_digest(self) -> None:
        if self.ws is None:
            return
        spec = self.digest_specs.get(self._cursor_key("#digests") or "")
        if spec is None:
            return
        enabled = not spec.enabled
        changes = {"enabled": enabled, "failures": 0} if enabled else {"enabled": False}
        try:
            DigestStore(self.ws).update(spec.id, **changes)
        except (DigestError, OSError, ValueError) as exc:
            self.notify(str(exc), title="변경 실패", severity="error")
            return
        self.runtime_audit("digest.updated", workspace=self.ws.name, digest_id=spec.id, enabled=enabled)
        self._fill_digests()
        self._fill_card()

    def action_delete_digest(self) -> None:
        if self.ws is None:
            return
        spec = self.digest_specs.get(self._cursor_key("#digests") or "")
        if spec is None:
            return
        ws = self.ws

        def answered(ok: bool | None) -> None:
            if not ok or self.ws is not ws:
                return
            if self._digest_job(spec.id) is not None or DigestStore(ws).is_running(spec.id):
                self.notify("실행 중인 정기 정리는 제거할 수 없음", severity="warning")
                return
            try:
                DigestStore(ws).remove(spec.id)
            except (DigestError, OSError) as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            self.runtime_audit("digest.removed", workspace=ws.name, digest_id=spec.id)
            self._fill_digests()
            self._fill_card()

        detail = f"'{spec.title}' 일정이 삭제됨. 이미 만든 정리 보고서는 기록 탭에 그대로 남음"
        self.push_screen(ConfirmModal("정기 정리를 제거할까요?", detail), answered)

    def action_run_digest(self) -> None:
        if self.ws is None or self.runtime is None:
            return
        spec = self.digest_specs.get(self._cursor_key("#digests") or "")
        if spec is None:
            return
        if self._digest_job(spec.id) is not None:
            self.notify("이미 실행 중임", severity="warning")
            return
        claim = DigestStore(self.ws).claim(spec.id, self.now(), force=True)
        if claim is None:
            self.notify("이 정기 정리는 다른 곳(백그라운드 실행 등)에서 실행 중임", severity="warning")
            return
        self._launch_digest(self.ws, spec, claim)

    def _check_digests(self) -> None:
        if self.runtime is None:
            return
        now = self.now()
        for name in Workspace.list_names():
            try:
                ws = Workspace.open(name)
                if not scope_allows_web(ws.config.scope):
                    continue
                store = DigestStore(ws)
                due = store.due(now)
            except (WorkspaceError, OSError, ValueError):
                continue
            for spec in due:
                running = self.digest_jobs.get(f"{name}/{spec.id}")
                if running is not None and running.active:
                    continue
                claim = store.claim(spec.id, now)
                if claim is not None:
                    self._launch_digest(ws, spec, claim)

    def _launch_digest(self, ws: Workspace, spec: DigestSpec, claim: DigestClaim) -> Job | None:
        try:
            orch, session = start_digest(self.runtime, ws, spec, claim)
        except Exception as exc:
            finish_digest(self.runtime, ws, claim, "", "failed")
            self.notify(f"{ws.name} · {spec.title}: {str(exc)[:160]}", title="정기 정리 실패", severity="error")
            with suppress(NoMatches):
                if self.ws is not None and self.ws.name == ws.name:
                    self._fill_digests()
            return None
        job = Job(ws=ws, orch=orch, run_id=session.run_id, goal=spec.title, direct=True, kind="digest",
                  digest_id=spec.id, model=spec.model or None)
        job.load_plan(session)
        job.phase = "running"
        job.started = self.clock()
        job.state = ("정기 정리 실행 중", CLAY)
        job.log.append(f"정기 정리 '{escape(spec.title)}' 시작 ({escape(spec.schedule)})")
        old = self.digest_jobs.get(job.key)
        if old is not None:
            self.runs.pop(old.run_id, None)
        self.digest_jobs[job.key] = job
        self.runs[job.run_id] = job
        job.worker = self.run_worker(self._do_digest(job, claim), group=f"digest:{job.key}", exclusive=True,
                                     exit_on_error=False)
        with suppress(NoMatches):
            if self._is_selected(job):
                self._fill_history()
                self._fill_digests()
            self._paint_jobs_card()
        return job

    async def _do_digest(self, job: Job, claim: DigestClaim) -> None:
        status = "failed"
        report_path = None
        try:
            outcome = await job.orch.execute(job.session)
            status, report_path = outcome.status, outcome.report_path
            job.phase = outcome.status
            job.report = report_path is not None
            job.cost = outcome.cost
            label = PHASE_LABELS.get(outcome.status, outcome.status)
            extra = f" · 제안 {len(outcome.proposed)}건 F10" if outcome.proposed else ""
            self.notify(f"{job.name} · {job.goal} · {label} · ${outcome.cost:.4f}{extra}", title="정기 정리 완료",
                        timeout=8)
        except asyncio.CancelledError:
            record = job.ws.load_run(job.run_id) or {}
            status = str(record.get("status")) if record.get("status") in RECORD_FOLLOWABLE else "cancelled"
            job.phase = status
            raise
        except Exception as exc:
            job.phase = "error"
            job.log.append(f"[{ERROR}]정기 정리 오류: {escape(str(exc))}[/]")
            self.notify(f"{job.name} · {job.goal}: {str(exc)[:160]}", title="정기 정리 실패", severity="error")
        finally:
            job.finished = self.clock()
            spec = finish_digest(self.runtime, job.ws, claim, job.run_id, status, report_path)
            if spec is not None and not spec.enabled and status not in FOLLOWABLE:
                self.notify(f"{job.name} · {job.goal}: 연속 실패로 꺼둠, 정기 정리 탭에서 확인", severity="warning")
            with suppress(NoMatches):
                self._refresh_meter()
                if self._is_selected(job):
                    self._fill_history()
                    self._fill_digests()
                    self._fill_facts()
                    self._fill_actions()
                    self._fill_card()
                self._paint_jobs_card()

    def _fill_actions(self) -> None:
        table = self.query_one("#action-list", DataTable)
        row = table.cursor_row
        _reset_table(table)
        self.action_items = {}
        pending = 0
        if self.ws is not None:
            try:
                items = ActionStore(self.ws).list()
            except (OSError, ValueError):
                items = []
            for action in items:
                self.action_items[action.id] = action
                pending += action.status == "pending"
                icon, label, color = ACTION_STATUS.get(action.status, ("○", action.status, MUTED))
                table.add_row(
                    Text(_local_time(action.created)[5:], style=MUTED), TYPE_LABELS.get(action.type, action.type),
                    _short(action.title, 48), Text(f"{icon} {label}", style=color),
                    Text(action.classification, style=CLASS_STYLE.get(action.classification, MUTED)), key=action.id,
                )
        with suppress(NoMatches, KeyError, AttributeError):
            tab = self.query_one("#tabs", TabbedContent).get_tab("tab-actions")
            tab.label = f"제안 {pending}" if pending else "제안"
        state = "" if self.ws is None or self.ws.config.actions else " · 꺼짐 (업무 프로필에서 켜기)"
        table.border_title = f"행동 제안{state} · Y 승인 · N 거절 · O 열기 · C 복사 · Del 제거"
        self._keep_cursor(table, row)
        self.call_later(self._show_action_preview, self._cursor_key("#action-list"))

    async def _show_action_preview(self, action_id: str | None) -> None:
        try:
            preview = self.query_one("#action-preview", Markdown)
        except NoMatches:
            return
        action = self.action_items.get(action_id or "")
        if action is None:
            await preview.update(
                "제안이 없습니다. 실행이 끝나면 보고서를 바탕으로 메일 초안, 일정, 표, 문서를 제안합니다. 승인해야만 작업 영역 "
                "outbox에 파일이 만들어지고, 메일은 보내지 않고 초안만 만듭니다. 기록 탭에서 A로 지난 실행에 대한 제안도 "
                "받을 수 있습니다."
            )
            return
        try:
            body = preview_markdown(action)
        except ValueError as exc:
            body = f"제안 내용이 올바르지 않음: {exc}"
        record = self.history.get(action.run_id)
        origin = _short(record.get("goal", ""), 60) if record else action.run_id
        notes = [f"> 출처 실행: {origin} · 등급 {action.classification}"]
        if action.status == "pending":
            notes.append("> Y로 승인하면 작업 영역 outbox에 파일을 만듦. 보내거나 등록하지 않으며 O로 열어 직접 처리")
        elif action.status == "done":
            notes.append(f"> 파일: outbox/{action.output} · O로 열기 · C로 다른 폴더에 복사")
        await preview.update("\n\n".join(notes) + "\n\n" + body)

    @on(DataTable.RowHighlighted, "#action-list")
    async def _action_highlighted(self, event: DataTable.RowHighlighted) -> None:
        await self._show_action_preview(event.row_key.value if event.row_key else None)

    def _cursor_action(self):
        return self.action_items.get(self._cursor_key("#action-list") or "")

    def _confirm_confidential(self, action, verb: str, then: Callable[[], None]) -> None:
        if action.classification != "confidential":
            then()
            return

        def answered(ok: bool | None) -> None:
            if ok:
                then()

        detail = "confidential 자료로 만든 제안임. 파일을 다루기 전에 보관 위치와 공유 대상을 확인해야 함"
        self.push_screen(ConfirmModal("기밀 등급 제안", detail, confirm_label=verb), answered)

    def action_approve_action(self) -> None:
        action = self._cursor_action()
        if self.ws is None or action is None or action.status != "pending":
            return
        ws = self.ws

        def approve() -> None:
            try:
                done = ActionStore(ws).approve(action.id)
            except (ActionError, OSError) as exc:
                self.notify(str(exc), title="승인 실패", severity="error")
                return
            self.runtime_audit("action.approved", workspace=ws.name, action_id=done.id, type=done.type,
                               output=done.output, sha256=done.sha256, classification=done.classification)
            self.notify(f"만듦: {done.output} · O로 열기", title="제안 승인", timeout=5)
            if self.ws is ws:
                self._fill_actions()
                self._fill_card()

        self._confirm_confidential(action, "만들기", approve)

    def action_reject_action(self) -> None:
        action = self._cursor_action()
        if self.ws is None or action is None or action.status != "pending":
            return
        try:
            ActionStore(self.ws).reject(action.id)
        except (ActionError, OSError) as exc:
            self.notify(str(exc), title="거절 실패", severity="error")
            return
        self.runtime_audit("action.rejected", workspace=self.ws.name, action_id=action.id)
        self._fill_actions()
        self._fill_card()

    def action_open_action(self) -> None:
        action = self._cursor_action()
        if self.ws is None or action is None:
            return
        if action.status != "done":
            self.notify("Y로 승인해 파일을 만든 뒤 열 수 있음", severity="warning")
            return
        ws = self.ws

        def open_file() -> None:
            try:
                path = ActionStore(ws).open_output(action.id)
            except (ActionError, OSError, ValueError) as exc:
                self.notify(str(exc), title="열기 실패", severity="error")
                return
            self.runtime_audit("action.opened", workspace=ws.name, action_id=action.id, output=path.name)
            self.notify(path.name, title="기본 앱으로 열기", timeout=3)

        self._confirm_confidential(action, "열기", open_file)

    def action_export_action(self) -> None:
        action = self._cursor_action()
        if self.ws is None or action is None:
            return
        if action.status != "done":
            self.notify("Y로 승인해 파일을 만든 뒤 복사할 수 있음", severity="warning")
            return
        ws = self.ws

        def done(path: str | None) -> None:
            if not path:
                return
            folder = str(Path(path).parent)
            self.export_dir = folder_hint(folder)
            self.notify(path, title="파일을 복사함", timeout=6)

        self._confirm_confidential(
            action, "복사", lambda: self.push_screen(ActionExportModal(ws, action.id, action.output, self.export_dir),
                                                    done),
        )

    def action_delete_action(self) -> None:
        action = self._cursor_action()
        if self.ws is None or action is None:
            return
        ws = self.ws

        def answered(ok: bool | None) -> None:
            if not ok or self.ws is not ws:
                return
            try:
                ActionStore(ws).remove(action.id)
            except (ActionError, OSError) as exc:
                self.notify(str(exc), title="제거 실패", severity="error")
                return
            self.runtime_audit("action.deleted", workspace=ws.name, action_id=action.id, output=action.output)
            self._fill_actions()
            self._fill_card()

        extra = f" outbox의 '{action.output}' 파일도 함께 삭제됨" if action.output else ""
        self.push_screen(ConfirmModal("제안을 제거할까요?", f"'{_short(action.title, 50)}'.{extra}"), answered)

    def action_propose_actions(self) -> None:
        if self.ws is None or self.runtime is None:
            return
        run_id = self._cursor_run()
        record = self.history.get(run_id or "")
        if record is None or record.get("status") not in FOLLOWABLE or not self.ws.report_size(run_id):
            self.notify("보고서가 있는 완료된 실행에서만 제안을 받을 수 있음", severity="warning")
            return
        if not self.ws.config.actions:
            self.notify("이 작업 영역은 행동 제안이 꺼져 있음 (업무 프로필에서 켜기)", severity="warning")
            return
        self.notify("행동 제안을 정리하는 중", timeout=3)
        self.run_worker(self._do_propose(self.ws, run_id), group=f"propose:{self.ws.name}", exclusive=True,
                        exit_on_error=False)

    async def _do_propose(self, ws: Workspace, run_id: str) -> None:
        orch = self.runtime.orchestrator(ws)
        try:
            ids = await orch.propose_actions(run_id)
        except Exception as exc:
            self.notify(str(exc)[:200], title="행동 제안 실패", severity="error")
            return
        finally:
            self._refresh_meter()
        self.notify(f"제안 {len(ids)}건 · F10에서 확인" if ids else "이 보고서에서는 제안할 행동이 없음",
                    title="행동 제안", timeout=5)
        with suppress(NoMatches):
            if self.ws is not None and self.ws.name == ws.name:
                self._fill_actions()
                self._fill_history()
                self._fill_card()

    def runtime_audit(self, event: str, **data) -> None:
        if self.runtime is not None:
            self.runtime.audit.append(event, via="tui", **data)
        else:
            from ..audit import AuditLog

            AuditLog().append(event, via="tui", **data)

    def _composer(self) -> tuple[str, str | None, float] | None:
        if self.runtime is None:
            return None
        if self.ws is None:
            self.notify("작업 영역을 먼저 선택하거나 만들어야 함 (Ctrl+N)", severity="warning")
            return None
        job = self._job()
        if job is not None and job.active:
            return None
        goal = self.query_one("#goal", TextArea).text.strip()
        if not goal:
            self.notify("목표를 입력해야 함", severity="warning")
            self.query_one("#goal", TextArea).focus()
            return None
        playbook = self.query_one("#pb", Select).value
        playbook_id = None if playbook is Select.NULL else str(playbook)
        try:
            cap = float(self.query_one("#cap", Input).value or self.runtime.settings.budget.run_default_usd)
        except ValueError:
            self.notify("실행 예산은 숫자여야 함", severity="warning")
            return None
        return goal, playbook_id, cap

    def _new_job(self, ws: Workspace, orch, run_id: str, goal: str, direct: bool = False,
                 resumed: bool = False) -> Job:
        old = self.jobs.get(ws.name)
        if old is not None:
            self.runs.pop(old.run_id, None)
        job = Job(ws=ws, orch=orch, run_id=run_id, goal=goal, direct=direct, resumed=resumed, model=self.model_pref)
        self.jobs[ws.name] = job
        self.runs[run_id] = job
        return job

    def _prepare_job(self, direct: bool) -> tuple[Job, tuple[str, str | None, float]] | None:
        values = self._composer()
        if values is None:
            return None
        ws = self.ws
        orch = self.runtime.orchestrator(ws)
        try:
            orch.set_model(self.model_pref)
        except RunError as exc:
            self.notify(str(exc), title="모델 오류", severity="error")
            return None
        current = self._job()
        if current is not None and current.phase == "planned" and current.session is not None:
            current.orch.cancel_planned(current.session)
        self._save_draft()
        job = self._new_job(ws, orch, new_run_id(), values[0], direct=direct)
        return job, values

    def action_plan(self) -> None:
        prepared = self._prepare_job(direct=False)
        if prepared is None:
            return
        job, (goal, playbook_id, cap) = prepared
        job.phase = "planning"
        job.started = self.clock()
        job.state = ("계획 수립 중…", CLAY)
        self.action_show_tab("tab-run")
        self._job_changed(job)
        job.worker = self.run_worker(self._do_plan(job, goal, playbook_id, cap, self.follow_run),
                                     group=f"job:{job.name}", exclusive=True, exit_on_error=False)

    async def _do_plan(self, job: Job, goal: str, playbook_id: str | None, cap: float, base_run: str | None) -> None:
        try:
            session = await job.orch.plan(goal, playbook_id, cap, base_run_id=base_run, run_id=job.run_id)
        except Exception as exc:
            job.log.append(f"[{ERROR}]계획 실패: {escape(str(exc))}[/]")
            self.notify(str(exc)[:200], title=f"계획 실패 · {job.name}", severity="error")
            job.phase = "error"
            job.finished = self.clock()
            job.state = ("계획 실패", ERROR)
            self._job_changed(job)
            return
        finally:
            self._refresh_meter()
        job.load_plan(session)
        job.phase = "planned"
        job.finished = self.clock()
        job.state = ("승인 대기 · F6으로 실행", CLAY)
        if self._is_selected(job):
            self._fill_history()
        else:
            self.notify(f"{job.name} · 계획 완료, 그 작업 영역에서 F6으로 실행", title="계획 완료", timeout=6)
        self._job_changed(job)

    def action_direct(self) -> None:
        prepared = self._prepare_job(direct=True)
        if prepared is None:
            return
        job, (goal, playbook_id, cap) = prepared
        try:
            session = job.orch.direct(goal, playbook_id, cap, base_run_id=self.follow_run, run_id=job.run_id)
        except Exception as exc:
            self.notify(str(exc)[:200], title="바로 실행 실패", severity="error")
            job.log.append(f"[{ERROR}]바로 실행 실패: {escape(str(exc))}[/]")
            job.phase = "error"
            job.state = ("바로 실행 실패", ERROR)
            self._job_changed(job)
            return
        job.load_plan(session)
        job.phase = "planned"
        self.action_show_tab("tab-run")
        self._fill_history()
        self._start(job)

    def action_execute(self) -> None:
        job = self._job()
        if job is None or job.phase != "planned" or job.session is None:
            return
        self._start(job)

    def _start(self, job: Job) -> None:
        job.phase = "running"
        job.started = self.clock()
        job.finished = None
        job.state = ("실행 중", CLAY)
        draft = self.drafts.get(job.name)
        if draft is not None:
            draft.follow_run = None
        if self._is_selected(job):
            self._paint_follow()
        self._job_changed(job)
        self._update_history_row(job)
        job.worker = self.run_worker(self._do_execute(job), group=f"job:{job.name}", exclusive=True,
                                     exit_on_error=False)

    async def _do_execute(self, job: Job) -> None:
        try:
            outcome = await job.orch.execute(job.session)
        except asyncio.CancelledError:
            record = job.ws.load_run(job.run_id) or {}
            if record.get("status") in RECORD_FOLLOWABLE:
                job.phase = str(record["status"])
                job.report = True
                job.state = ("완료 · 후속 정리 중단", WARNING)
            else:
                job.phase = "cancelled"
                job.state = ("중단됨 · 기록 탭에서 Ctrl+Enter로 재개", WARNING)
            raise
        except Exception as exc:
            job.log.append(f"[{ERROR}]실행 오류: {escape(str(exc))}[/]")
            job.phase = "error"
            job.state = ("실행 오류", ERROR)
        else:
            for warning in outcome.warnings:
                job.log.append(f"[{WARNING}]{escape(warning)}[/]")
            job.phase = outcome.status
            job.report = outcome.report_path is not None
            job.cost = outcome.cost
            label = PHASE_LABELS.get(outcome.status, outcome.status)
            job.state = (f"종료 · {label} · ${outcome.cost:.4f}", PHASE_COLORS.get(outcome.status, CLAY))
            if outcome.report_path:
                extra = f" · 제안 {len(outcome.proposed)}건 F10" if outcome.proposed else ""
                self.notify(f"{job.name} · {label} · ${outcome.cost:.4f} · F7로 보고서 보기{extra}",
                            title="실행 완료", timeout=8)
            else:
                self.notify(f"{job.name} · {label}", title="실행 종료", severity="warning", timeout=8)
        finally:
            job.finished = self.clock()
            job.seen = self._is_selected(job)
            with suppress(NoMatches):
                self._refresh_meter()
                if self._is_selected(job):
                    self._fill_history()
                    self._fill_card()
                    self._fill_facts()
                    self._fill_actions()
            self._job_changed(job)

    def action_cancel(self) -> None:
        job = self._job()
        if job is None:
            return
        if job.phase == "running" and job.worker is not None:
            job.worker.cancel()
            job.log.append(f"[{WARNING}]실행 중단 요청됨[/]")
            job.state = ("중단 중…", WARNING)
            self.query_one("#log", ActivityLog).sync()
            self._paint_state()
            return
        if job.phase == "planning" and job.worker is not None:
            job.worker.cancel()
            job.phase = "stopped"
            job.finished = self.clock()
            job.state = ("계획을 중단함", WARNING)
            job.log.append(f"[{WARNING}]계획 수립 중단됨[/]")
            self._job_changed(job)
            return
        if job.phase == "planned" and job.session is not None:
            job.orch.cancel_planned(job.session)
            job.phase = "rejected"
            job.state = ("재개를 취소함" if job.resumed else "계획을 취소함", MUTED)
            self._fill_history()
            self._job_changed(job)
            self.notify(job.state[0], timeout=3)

    async def on_unmount(self) -> None:
        if self.runtime is not None:
            await self.runtime.aclose()
