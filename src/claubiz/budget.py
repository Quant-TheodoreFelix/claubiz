import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import hostos
from .config import Settings
from .i18n import tr
from .paths import home


@dataclass(frozen=True)
class UsageRecord:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    web_searches: int = 0
    web_fetches: int = 0


def _int(obj, name: str) -> int:
    value = getattr(obj, name, None) if obj is not None else None
    return int(value or 0)


def _cache_writes(usage) -> tuple[int, int]:
    detail = getattr(usage, "cache_creation", None)
    w5, w1 = _int(detail, "ephemeral_5m_input_tokens"), _int(detail, "ephemeral_1h_input_tokens")
    total = _int(usage, "cache_creation_input_tokens")
    if w5 + w1 < total:
        w5 += total - (w5 + w1)
    return w5, w1


def usage_from_message(message) -> list[UsageRecord]:
    usage = message.usage
    server = getattr(usage, "server_tool_use", None)
    searches, fetches = _int(server, "web_search_requests"), _int(server, "web_fetch_requests")
    iterations = getattr(usage, "iterations", None) or []
    records: list[UsageRecord] = []
    if iterations:
        for it in iterations:
            w5, w1 = _cache_writes(it)
            records.append(
                UsageRecord(
                    model=getattr(it, "model", None) or message.model,
                    input_tokens=_int(it, "input_tokens"),
                    output_tokens=_int(it, "output_tokens"),
                    cache_write_5m=w5,
                    cache_write_1h=w1,
                    cache_read=_int(it, "cache_read_input_tokens"),
                )
            )
        first = records[0]
        records[0] = UsageRecord(**{**first.__dict__, "web_searches": searches, "web_fetches": fetches})
        return records
    w5, w1 = _cache_writes(usage)
    return [
        UsageRecord(
            model=message.model,
            input_tokens=_int(usage, "input_tokens"),
            output_tokens=_int(usage, "output_tokens"),
            cache_write_5m=w5,
            cache_write_1h=w1,
            cache_read=_int(usage, "cache_read_input_tokens"),
            web_searches=searches,
            web_fetches=fetches,
        )
    ]


def cost_of(record: UsageRecord, settings: Settings) -> float:
    price = settings.price_for(record.model)
    prompt_tokens = record.input_tokens + record.cache_write_5m + record.cache_write_1h + record.cache_read
    long_ctx = price.long_context_threshold and prompt_tokens > price.long_context_threshold
    p_in = price.long_input if long_ctx and price.long_input else price.input
    p_out = price.long_output if long_ctx and price.long_output else price.output
    usd = (
        record.input_tokens * p_in
        + record.output_tokens * p_out
        + record.cache_write_5m * p_in * price.cache_write_5m_multiplier
        + record.cache_write_1h * p_in * price.cache_write_1h_multiplier
        + record.cache_read * price.cache_read
    ) / 1_000_000
    usd += record.web_searches * settings.web_search_usd_per_1k / 1000
    return usd


def cycle_start(now: datetime, anchor_day: int) -> datetime:
    candidate = now.replace(day=anchor_day, hour=0, minute=0, second=0, microsecond=0)
    if candidate > now:
        prev_month_last = candidate.replace(day=1) - timedelta(days=1)
        candidate = prev_month_last.replace(day=anchor_day)
    return candidate


SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    workspace TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    role TEXT NOT NULL,
    effort TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_5m INTEGER NOT NULL,
    cache_write_1h INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    web_searches INTEGER NOT NULL,
    web_fetches INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    request_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
CREATE INDEX IF NOT EXISTS usage_run ON usage(run_id);
CREATE TABLE IF NOT EXISTS task_cost (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    web INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (home() / "ledger.sqlite3")
        fresh = not self.path.exists()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if fresh:
            hostos.restrict_file(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def record(
        self, *, workspace: str, run_id: str, task_id: str, role: str, effort: str, rec: UsageRecord, cost: float,
        request_id: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (ts, workspace, run_id, task_id, role, effort, model, input_tokens, output_tokens,"
                " cache_write_5m, cache_write_1h, cache_read, web_searches, web_fetches, cost_usd, request_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), workspace, run_id, task_id, role, effort, rec.model, rec.input_tokens,
                    rec.output_tokens, rec.cache_write_5m, rec.cache_write_1h, rec.cache_read, rec.web_searches,
                    rec.web_fetches, cost, request_id,
                ),
            )

    def record_task_cost(self, *, role: str, model: str, effort: str, web: bool, cost: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_cost (ts, role, model, effort, web, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), role, model, effort, int(web), cost),
            )

    def spent_since(self, since: datetime, workspace: str | None = None) -> float:
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts >= ?"
        args: list = [since.timestamp()]
        if workspace:
            query += " AND workspace = ?"
            args.append(workspace)
        with self._lock:
            return float(self._conn.execute(query, args).fetchone()[0])

    def spent_run(self, run_id: str) -> float:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE run_id = ?", (run_id,))
            return float(row.fetchone()[0])

    def task_cost_history(self, role: str, model: str, effort: str, web: bool, limit: int = 20) -> list[float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT cost_usd FROM task_cost WHERE role = ? AND model = ? AND effort = ? AND web = ?"
                " ORDER BY ts DESC LIMIT ?",
                (role, model, effort, int(web), limit),
            ).fetchall()
        return [float(r[0]) for r in rows]

    def summary_since(self, since: datetime) -> list[tuple]:
        with self._lock:
            return self._conn.execute(
                "SELECT workspace, model, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_read),"
                " SUM(cache_write_5m + cache_write_1h), SUM(web_searches), SUM(cost_usd)"
                " FROM usage WHERE ts >= ? GROUP BY workspace, model ORDER BY SUM(cost_usd) DESC",
                (since.timestamp(),),
            ).fetchall()


class BudgetExceeded(Exception):
    pass


@dataclass
class Reservation:
    amount: float
    label: str


class ReservationPool:
    def __init__(self) -> None:
        self.total = 0.0
        self.by_workspace: dict[str, float] = {}
        self.cond = asyncio.Condition()

    def add(self, workspace: str, amount: float) -> None:
        self.total += amount
        self.by_workspace[workspace] = self.by_workspace.get(workspace, 0.0) + amount

    def remove(self, workspace: str, amount: float) -> None:
        self.total = max(0.0, self.total - amount)
        left = max(0.0, self.by_workspace.get(workspace, 0.0) - amount)
        if left:
            self.by_workspace[workspace] = left
        else:
            self.by_workspace.pop(workspace, None)


class BudgetGuard:
    def __init__(
        self, ledger: Ledger, settings: Settings, workspace: str, workspace_cap: float, run_id: str, run_cap: float,
        pool: ReservationPool | None = None,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.workspace = workspace
        self.workspace_cap = workspace_cap
        self.run_id = run_id
        self.run_cap = run_cap
        self.pool = pool or ReservationPool()
        self._reserved = 0.0
        self.waiting = 0
        self.soft_warned = False

    def _cycle_start(self) -> datetime:
        return cycle_start(datetime.now(), self.settings.budget.cycle_anchor_day)

    def snapshot(self) -> dict:
        start = self._cycle_start()
        return {
            "cycle_start": start.date().isoformat(),
            "cycle_spent": self.ledger.spent_since(start),
            "soft_cap": self.settings.budget.monthly_soft_usd,
            "hard_cap": self.settings.budget.monthly_hard_usd,
            "workspace_spent": self.ledger.spent_since(start, self.workspace),
            "workspace_cap": self.workspace_cap,
            "run_spent": self.ledger.spent_run(self.run_id),
            "run_cap": self.run_cap,
            "reserved": self._reserved,
            "pool_reserved": self.pool.total,
            "workspace_reserved": self.pool.by_workspace.get(self.workspace, 0.0),
        }

    @staticmethod
    def _violation(snap: dict, extra: float, pending: bool = False) -> str | None:
        cycle = extra + (snap["pool_reserved"] if pending else 0.0)
        workspace = extra + (snap["workspace_reserved"] if pending else 0.0)
        run = extra + (snap["reserved"] if pending else 0.0)
        if snap["cycle_spent"] + cycle > snap["hard_cap"]:
            return tr("월 예산 상한 초과 예상: 사용 ${spent:.2f} + ${extra:.2f} > ${cap:.2f}",
                      spent=snap["cycle_spent"], extra=cycle, cap=snap["hard_cap"])
        if snap["workspace_cap"] and snap["workspace_spent"] + workspace > snap["workspace_cap"]:
            return tr("작업 영역 예산 초과 예상: 사용 ${spent:.2f} + ${extra:.2f} > ${cap:.2f}",
                      spent=snap["workspace_spent"], extra=workspace, cap=snap["workspace_cap"])
        if snap["run_spent"] + run > snap["run_cap"]:
            return tr("이번 실행 예산 초과 예상: 사용 ${spent:.2f} + ${extra:.2f} > ${cap:.2f}",
                      spent=snap["run_spent"], extra=run, cap=snap["run_cap"])
        return None

    async def reserve(self, amount: float, label: str) -> Reservation:
        cond = self.pool.cond
        async with cond:
            while True:
                snap = self.snapshot()
                terminal = self._violation(snap, amount)
                if terminal:
                    raise BudgetExceeded(terminal)
                if self._violation(snap, amount, pending=True) is None:
                    self._reserved += amount
                    self.pool.add(self.workspace, amount)
                    return Reservation(amount=amount, label=label)
                self.waiting += 1
                try:
                    await cond.wait()
                finally:
                    self.waiting -= 1

    async def release(self, reservation: Reservation) -> None:
        cond = self.pool.cond
        async with cond:
            self._reserved = max(0.0, self._reserved - reservation.amount)
            self.pool.remove(self.workspace, reservation.amount)
            cond.notify_all()

    def soft_cap_crossed(self) -> bool:
        if self.soft_warned:
            return False
        snap = self.snapshot()
        if snap["cycle_spent"] >= snap["soft_cap"]:
            self.soft_warned = True
            return True
        return False
