import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from claubiz.budget import (
    BudgetExceeded,
    BudgetGuard,
    Ledger,
    UsageRecord,
    cost_of,
    cycle_start,
    usage_from_message,
)
from claubiz.config import Settings
from claubiz.planner import PlanContext, PlanError, validate_plan
from claubiz.render import render, unknown_urls
from claubiz.workspace import InputFile, Workspace, WorkspaceConfig


def test_cost_of_opus_with_cache_and_search():
    s = Settings()
    rec = UsageRecord(
        model="claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000, cache_write_5m=1_000_000,
        cache_read=1_000_000, web_searches=10,
    )
    assert cost_of(rec, s) == pytest.approx(4.0 + 2.0 + 5.0 + 0.20 + 0.10)


def test_cost_of_haiku_long_context():
    s = Settings()
    rec = UsageRecord(model="claude-haiku-5-5", input_tokens=200_000, output_tokens=1000)
    assert cost_of(rec, s) == pytest.approx((200_000 * 0.50 + 1000 * 2.50) / 1_000_000)


def test_usage_from_message_prefers_iterations():
    msg = SimpleNamespace(
        model="claude-opus-4-8",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0,
            cache_creation=None, server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=1),
            iterations=[
                SimpleNamespace(model="claude-opus-5-5", input_tokens=100, output_tokens=0, cache_creation=None,
                                cache_creation_input_tokens=0, cache_read_input_tokens=50),
                SimpleNamespace(model="claude-opus-4-8", input_tokens=100, output_tokens=40, cache_creation=None,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0),
            ],
        ),
    )
    recs = usage_from_message(msg)
    assert [r.model for r in recs] == ["claude-opus-5-5", "claude-opus-4-8"]
    assert recs[0].web_searches == 2 and recs[0].cache_read == 50


def test_cycle_start():
    assert cycle_start(datetime(2026, 11, 4, 12), 1) == datetime(2026, 11, 1)
    assert cycle_start(datetime(2026, 11, 4, 12), 15) == datetime(2026, 10, 15)
    assert cycle_start(datetime(2026, 1, 3), 10) == datetime(2025, 12, 10)


RUN = "r20261104-000000-abcdef"


def _spend(ledger, run_id, cost):
    ledger.record(workspace="ws", run_id=run_id, task_id="t1", role="worker", effort="low",
                  rec=UsageRecord(model="claude-opus-5-5"), cost=cost, request_id="")


async def test_budget_guard_waits_for_inflight_instead_of_blocking():
    ledger = Ledger()
    guard = BudgetGuard(ledger, Settings(), "ws", 0.0, RUN, run_cap=3.0)
    peak = 0

    async def call():
        nonlocal peak
        res = await guard.reserve(0.8, "w")
        peak = max(peak, guard._reserved)
        await asyncio.sleep(0.02)
        _spend(ledger, RUN, 0.3)
        await guard.release(res)

    await asyncio.wait_for(asyncio.gather(*(call() for _ in range(6))), 5)
    assert ledger.spent_run(RUN) == pytest.approx(1.8)
    assert peak <= 3.0


async def test_budget_guard_terminal_limits():
    ledger = Ledger()
    guard = BudgetGuard(ledger, Settings(), "ws", 0.0, RUN, run_cap=1.0)
    _spend(ledger, RUN, 0.9)
    with pytest.raises(BudgetExceeded):
        await guard.reserve(0.2, "run")
    other = BudgetGuard(ledger, Settings(), "ws", 0.0, "r20261104-000000-bbbbbb", run_cap=5.0)
    _spend(ledger, "elsewhere", 89.0)
    with pytest.raises(BudgetExceeded):
        await other.reserve(0.2, "month")
    ws_guard = BudgetGuard(ledger, Settings(), "ws", 89.95, "r20261104-000000-cccccc", run_cap=5.0)
    with pytest.raises(BudgetExceeded):
        await ws_guard.reserve(0.06, "workspace")


def _ws() -> Workspace:
    return Workspace.create(WorkspaceConfig(name="acme"))


def _ctx(inputs=None, web=True, reusable=None) -> PlanContext:
    return PlanContext(inputs or [], reusable or {}, None, web)


def _plan(tasks, **extra) -> str:
    base = {"title": "T", "approach": "A", "outline": ["요약"], "tasks": tasks}
    base.update(extra)
    return json.dumps(base)


def _task(tid, **kw):
    base = {"id": tid, "title": tid, "objective": "o", "use_web": True, "inputs": [], "depends_on": [],
            "effort": "low", "max_searches": 3, "reuse_ref": ""}
    base.update(kw)
    return base


def test_validate_plan_rejects_cycles():
    ws = _ws()
    raw = _plan([_task("t1", depends_on=["t2"]), _task("t2", depends_on=["t1"])])
    with pytest.raises(PlanError):
        validate_plan(raw, ws, _ctx(), Settings())


def test_validate_plan_sanitizes_tasks():
    ws = _ws()
    allowed = InputFile(name="a.md", sha256="0" * 64, classification="internal", size=1, chars=1,
                        imported_at="x", source_encoding="utf-8")
    raw = _plan([
        _task("t1", inputs=["a.md", "secret.md"], max_searches=99, effort="high"),
        _task("t2", depends_on=["t1", "t9", "t2"], reuse_ref="r20260101-000000-aaaaaa/t1"),
    ])
    plan, notes = validate_plan(raw, ws, _ctx(inputs=[allowed], web=False), Settings())
    t1, t2 = plan.tasks
    assert t1.inputs == ["a.md"] and not t1.use_web and t1.max_searches == 0
    assert t2.depends_on == ["t1"] and t2.reuse_ref == ""
    assert any("secret.md" in n for n in notes)


def test_validate_plan_truncates_task_count():
    ws = _ws()
    raw = _plan([_task(f"t{i}") for i in range(1, 10)])
    plan, _notes = validate_plan(raw, ws, _ctx(), Settings())
    assert len(plan.tasks) == Settings().orchestrator.max_tasks


def test_render_citations_and_tags():
    blocks = [
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text="시장 규모는 1조원이다.", citations=[
            SimpleNamespace(url="https://stats.example.org/a", title="통계"),
            SimpleNamespace(url="javascript:alert(1)", title="x"),
        ]),
        SimpleNamespace(type="text", text="\n<handoff>\n- 1조원\n</handoff>\n<status>partial</status>", citations=None),
    ]
    notes = render(blocks)
    assert notes.status == "partial"
    assert notes.handoff == "- 1조원"
    assert "[[1]](https://stats.example.org/a)" in notes.body
    assert "javascript" not in notes.as_markdown()
    assert "<handoff>" not in notes.body


def test_unknown_urls():
    known = ["https://a.example.com/x", "https://b.example.com/"]
    report = "인용 https://a.example.com/x. 그리고 https://b.example.com 그리고 https://fake.example.com/y"
    assert unknown_urls(report, known) == ["https://fake.example.com/y"]


def test_render_collects_web_results_and_flags_unverified():
    blocks = [
        SimpleNamespace(type="web_search_tool_result", content=[
            SimpleNamespace(url="https://news.example.com/a", title="뉴스 A"),
            SimpleNamespace(url="https://news.example.com/b", title="뉴스 B"),
        ]),
        SimpleNamespace(type="web_fetch_tool_result", content=SimpleNamespace(
            url="https://official.example.com/pricing", content=SimpleNamespace(title="가격표"))),
        SimpleNamespace(type="text", citations=None, text=(
            "가격은 https://official.example.com/pricing 참고, 보도 https://news.example.com/a, "
            "근거 없는 https://invented.example.com/z\n<handoff>- 가격</handoff><status>ok</status>")),
    ]
    notes = render(blocks)
    urls = [s.url for s in notes.consulted]
    assert urls == ["https://official.example.com/pricing", "https://news.example.com/a"]
    assert notes.unverified == ("https://invented.example.com/z",)
    assert notes.source_count == 2
    assert "https://news.example.com/b" in notes.verified_urls
    md = notes.as_markdown()
    assert "### 참고한 페이지" in md and "### 확인되지 않은 링크" in md
