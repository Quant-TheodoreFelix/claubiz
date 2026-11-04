import asyncio
import json
from types import SimpleNamespace

from claubiz.audit import AuditLog
from claubiz.budget import BudgetExceeded, Ledger, UsageRecord
from claubiz.config import Settings
from claubiz.events import EventBus
from claubiz.llm import AgentResult
from claubiz.orchestrator import Orchestrator
from claubiz.workspace import Workspace, WorkspaceConfig

PLAN = {
    "title": "테스트 계획",
    "approach": "병렬 조사 후 종합",
    "outline": ["요약", "시장", "경쟁사"],
    "tasks": [
        {"id": "t1", "title": "시장", "objective": "o", "use_web": True, "inputs": [], "depends_on": [],
         "effort": "low", "max_searches": 2, "reuse_ref": ""},
        {"id": "t2", "title": "경쟁사", "objective": "o", "use_web": True, "inputs": [], "depends_on": [],
         "effort": "low", "max_searches": 2, "reuse_ref": ""},
        {"id": "t3", "title": "자료", "objective": "o", "use_web": False, "inputs": ["memo.md"], "depends_on": [],
         "effort": "low", "max_searches": 0, "reuse_ref": ""},
        {"id": "t4", "title": "시사점", "objective": "o", "use_web": False, "inputs": [], "depends_on": ["t1", "t2"],
         "effort": "medium", "max_searches": 0, "reuse_ref": ""},
    ],
}


def text(value, citations=None):
    return SimpleNamespace(type="text", text=value, citations=citations)


class FakeLLM:
    def __init__(self, fail_task=None):
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.order = []
        self.fail_task = fail_task
        self.messages = {}
        self.models = []
        self.hold = None
        self.held = set()

    async def run(self, *, ctx, role, effort, system, messages, guard, tools=None, max_tokens=None, schema=None,
                  on_warm=None):
        self.calls.append((ctx.role, ctx.task_id, effort, bool(tools)))
        self.models.append((ctx.run_id, ctx.task_id, role.model))
        self.messages[(ctx.task_id, effort)] = messages[0]["content"]
        if ctx.role == "planner" and ctx.task_id == "brief":
            return AgentResult([text("## 목적\n- 고객사 A 시장 조사 지원")], "end_turn", 0.004, [], role.model)
        if ctx.role == "planner":
            return AgentResult([text(json.dumps(PLAN))], "end_turn", 0.01, [], role.model)
        if ctx.role == "synthesizer":
            body = "# 보고서\n- 근거 https://stats.example.org/a\n- 조작 https://made-up.example.com/z\n<handoff>- 핵심</handoff>"
            return AgentResult([text(body)], "end_turn", 0.02, [], role.model)
        if ctx.task_id == self.fail_task:
            raise BudgetExceeded("테스트 차단")
        reservation = await guard.reserve(0.8, ctx.task_id)
        try:
            return await self._work(ctx, role, effort, tools, on_warm, guard)
        finally:
            await guard.release(reservation)

    async def _work(self, ctx, role, effort, tools, on_warm, guard):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(("start", ctx.task_id))
        await asyncio.sleep(0.05)
        if self.hold is not None and (not self.held or ctx.task_id in self.held):
            await self.hold.wait()
        if on_warm:
            on_warm()
        await asyncio.sleep(0.05)
        self.active -= 1
        self.order.append(("end", ctx.task_id))
        guard.ledger.record(workspace=guard.workspace, run_id=ctx.run_id, task_id=ctx.task_id, role="worker",
                            effort=effort, rec=UsageRecord(model=role.model), cost=0.03, request_id="")
        if ctx.task_id == "t2" and effort == "low":
            return AgentResult([text("부족\n<status>insufficient</status>")], "end_turn", 0.01, [], role.model)
        cites = [SimpleNamespace(url="https://stats.example.org/a", title="통계")] if tools else None
        return AgentResult(
            [text(f"{ctx.task_id} 노트", cites), text(f"\n<handoff>- {ctx.task_id} 요점</handoff>\n<status>ok</status>")],
            "end_turn", 0.03, [], role.model,
        )


def _setup(tmp_path, fail_task=None):
    ws = Workspace.create(WorkspaceConfig(name="acme"))
    memo = tmp_path / "memo.md"
    memo.write_text("담당자 010-1234-5678 에게 연락. 계약 금액 1억", encoding="utf-8")
    ws.import_file(memo, "internal")
    secret = tmp_path / "secret.md"
    secret.write_text("극비", encoding="utf-8")
    ws.import_file(secret, "confidential")
    fake = FakeLLM(fail_task)
    bus = EventBus()
    events = []
    bus.listen(events.append)
    orch = Orchestrator(Settings(), ws, fake, Ledger(), AuditLog(), bus)
    return ws, fake, orch, events


async def test_end_to_end_flow(tmp_path):
    ws, fake, orch, _events = _setup(tmp_path)
    session = await orch.plan("국내 전자계약 시장 조사", "market-research")
    assert [t.id for t in session.plan.tasks] == ["t1", "t2", "t3", "t4"]
    assert session.estimate["total"] > 0
    outcome = await orch.execute(session)
    assert outcome.status == "completed"
    report = outcome.report_path.read_text(encoding="utf-8")
    assert "검증되지 않은 링크" in report and "made-up.example.com" in report
    assert any("노트에 없는 링크" in w for w in outcome.warnings)

    t4_start = fake.order.index(("start", "t4"))
    assert fake.order.index(("end", "t1")) < t4_start and fake.order.index(("end", "t2")) < t4_start
    assert fake.max_active >= 2

    efforts_t2 = [c[2] for c in fake.calls if c[1] == "t2"]
    assert efforts_t2 == ["low", "medium"]

    t3_msg = fake.messages[("t3", "low")]
    assert "010-1234-5678" not in t3_msg and "[전화번호 가림]" in t3_msg
    assert "극비" not in "".join(fake.messages.values())
    assert "upstream_findings" in fake.messages[("t4", "medium")]

    record = ws.load_run(session.run_id)
    assert record["status"] == "completed" and record["summary"] == "- 핵심"
    assert {t["status"] for t in record["tasks"]} == {"done"}
    ok, count, _ = orch.audit.verify()
    assert ok and count > 5


async def test_budget_block_skips_dependents(tmp_path):
    _ws, _fake, orch, events = _setup(tmp_path, fail_task="t1")
    session = await orch.plan("조사", None)
    outcome = await orch.execute(session)
    statuses = {t["id"]: t["status"] for t in outcome.tasks}
    assert statuses["t1"] == "blocked"
    assert statuses["t4"] == "skipped"
    assert outcome.status == "partial"
    assert any(e.kind == "task.blocked" for e in events)


async def test_reuse_prior_artifact(tmp_path):
    _ws, fake, orch, _events = _setup(tmp_path)
    first = await orch.plan("조사", None)
    await orch.execute(first)
    ref = f"{first.run_id}/t1"
    PLAN["tasks"][0]["reuse_ref"] = ref
    try:
        second = await orch.plan("조사 갱신", None)
        assert second.plan.tasks[0].reuse_ref == ref
        assert second.estimate["tasks"]["t1"] == 0.0
        before = len([c for c in fake.calls if c[1] == "t1"])
        outcome = await orch.execute(second)
        after = len([c for c in fake.calls if c[1] == "t1"])
        assert after == before
        assert {t["id"]: t["status"] for t in outcome.tasks}["t1"] == "reused"
    finally:
        PLAN["tasks"][0]["reuse_ref"] = ""


async def test_confidential_requires_opt_in(tmp_path):
    _ws, _fake, orch, _events = _setup(tmp_path)
    PLAN["tasks"][2]["inputs"] = ["memo.md", "secret.md"]
    try:
        session = await orch.plan("요약", None)
        assert session.plan.tasks[2].inputs == ["memo.md"]
        session = await orch.plan("요약", None, allow_confidential=True)
        assert session.plan.tasks[2].inputs == ["memo.md", "secret.md"]
    finally:
        PLAN["tasks"][2]["inputs"] = ["memo.md"]
