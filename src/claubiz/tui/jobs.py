from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from rich.errors import MarkupError
from rich.table import Table
from rich.text import Text
from textual.events import Resize, Show
from textual.widgets import RichLog
from textual.worker import Worker

from ..orchestrator import Orchestrator, RunSession
from ..workspace import Workspace
from .theme import FAINT, MUTED

LOG_LIMIT = 800
ACTIVE_PHASES = frozenset({"planning", "running"})
BUSY_PHASES = frozenset({"planning", "planned", "running"})
FINISHED_PHASES = frozenset({"completed", "partial", "failed", "cancelled", "error"})


def clock_text(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class LogBuffer:
    def __init__(self, limit: int = LOG_LIMIT) -> None:
        self.lines: deque[tuple[str, str]] = deque(maxlen=limit)
        self.total = 0

    def append(self, body: str, ts: float | None = None) -> None:
        stamp = datetime.fromtimestamp(ts) if ts is not None else datetime.now()
        self.lines.append((stamp.strftime("%H:%M:%S"), body))
        self.total += 1

    def last(self) -> str:
        if not self.lines:
            return ""
        return _markup(self.lines[-1][1]).plain


def _markup(body: str) -> Text:
    try:
        return Text.from_markup(body)
    except MarkupError:
        return Text(body)


class ActivityLog(RichLog):
    def __init__(self, **kwargs) -> None:
        super().__init__(markup=False, wrap=True, min_width=1, max_lines=6000, **kwargs)
        self.buffer = LogBuffer()
        self._width = 0
        self._painted = 0

    def attach(self, buffer: LogBuffer) -> None:
        if buffer is not self.buffer:
            self.buffer = buffer
            self._width = 0
        self.sync()

    def _target_width(self) -> int:
        if not self.region.width:
            return 0
        return self.scrollable_content_region.width

    def _entry(self, stamp: str, body: str) -> Table:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style=FAINT, no_wrap=True)
        grid.add_column(overflow="fold")
        grid.add_row(stamp, _markup(body))
        return grid

    def sync(self) -> None:
        width = self._target_width()
        if width <= 0:
            return
        buffer = self.buffer
        fresh = buffer.total - self._painted
        if width != self._width or fresh < 0 or fresh > len(buffer.lines):
            self.clear()
            self._width = width
            fresh = len(buffer.lines)
        if fresh:
            for stamp, body in list(buffer.lines)[-fresh:]:
                self.write(self._entry(stamp, body), width=width, scroll_end=True)
        self._painted = buffer.total

    def on_resize(self, event: Resize) -> None:
        super().on_resize(event)
        self.call_after_refresh(self.sync)

    def on_show(self, event: Show) -> None:
        self.call_after_refresh(self.sync)


@dataclass
class Draft:
    goal: str = ""
    playbook: str | None = None
    cap: str = ""
    follow_run: str | None = None


@dataclass
class Job:
    ws: Workspace
    orch: Orchestrator
    run_id: str
    goal: str
    direct: bool = False
    resumed: bool = False
    phase: str = "planning"
    session: RunSession | None = None
    worker: Worker | None = None
    model: str | None = None
    started: float = 0.0
    finished: float | None = None
    done: int = 0
    total: int = 0
    cost: float = 0.0
    tasks: dict[str, dict] = field(default_factory=dict)
    log: LogBuffer = field(default_factory=LogBuffer)
    state: tuple[str, str] = ("", MUTED)
    report: bool = False
    seen: bool = True
    kind: str = "run"
    digest_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.ws.name}/{self.digest_id}" if self.kind == "digest" else self.ws.name

    @property
    def name(self) -> str:
        return self.ws.name

    @property
    def active(self) -> bool:
        return self.phase in ACTIVE_PHASES

    @property
    def busy(self) -> bool:
        return self.phase in BUSY_PHASES

    def elapsed(self, now: float) -> float:
        end = self.finished if self.finished is not None else now
        return max(0.0, end - self.started) if self.started else 0.0

    def load_plan(self, session: RunSession) -> None:
        self.session = session
        self.total = len(session.plan.tasks)
        self.tasks = {}
        for task in session.plan.tasks:
            prior = session.prior_states.get(task.id)
            if prior is not None:
                self.tasks[task.id] = {"status": prior.status, "cost": prior.cost, "effort": prior.effort}
            else:
                status = "reused" if task.reuse_ref and task.reuse_ref in session.context.reusable else "pending"
                self.tasks[task.id] = {"status": status, "cost": None, "effort": task.effort}
        self.done = len(session.prior_states)
        self.cost = sum(st.cost for st in session.prior_states.values())
