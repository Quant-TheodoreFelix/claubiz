import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import anthropic
from pydantic import ValidationError

from .actions import PROPOSAL_SCHEMA, ActionStore, parse_proposals
from .audit import AuditLog
from .budget import BudgetExceeded, BudgetGuard, Ledger, ReservationPool
from .config import MODEL_ROLES, ConfigError, Settings, with_model
from .events import EventBus
from .i18n import tr
from .knowledge import (
    EXTRACT_SCHEMA,
    Fact,
    KnowledgeStore,
    fact_sources,
    facts_block,
    parse_extraction,
    select_facts,
)
from .llm import LLM, CallContext, RefusalError, web_tools
from .paths import write_private_text
from .planner import (
    PLAN_SCHEMA,
    REUSE_RE,
    Plan,
    PlanContext,
    PlanError,
    PlannedTask,
    build_context,
    estimate_plan,
    finish_estimate,
    planner_message,
    validate_plan,
)
from .playbooks import Playbook, get_playbook
from .policy import CLASS_RANK, redact_pii
from .prompts import (
    ACTIONS_SYSTEM,
    DIGEST_SYSTEM,
    DIRECT_SYSTEM,
    KNOWLEDGE_SYSTEM,
    PLANNER_SYSTEM,
    SYNTH_SYSTEM,
    WORKER_SYSTEM,
)
from .render import plain_text, render, strip_unverified, unknown_urls, urls_in
from .workspace import RUN_ID_RE, Workspace, new_run_id, strip_report_footer

MAX_GOAL_CHARS = 4000
MAX_TOKENS_CEILING = 64000
MAX_BASE_REPORT_CHARS = 24000
DIRECT_INPUT_CHARS = 120000
FOLLOWABLE = ("completed", "partial")
RESUMABLE = ("cancelled", "failed", "running")
TASK_FACTS = 8
TASK_FACT_CHARS = 1600
SYNTH_FACTS = 25
SYNTH_FACT_CHARS = 4000
HELPER_REPORT_CHARS = 40000
LEARN_CANDIDATES = 40
MANUAL_HELPER_CAP = 0.3


class RunError(Exception):
    pass


@dataclass
class TaskState:
    task: PlannedTask
    status: str = "pending"
    effort: str = ""
    attempts: int = 0
    cost: float = 0.0
    notes_md: str = ""
    handoff: str = ""
    result_status: str = ""
    error: str = ""
    sources: int = 0
    verified_urls: set[str] = field(default_factory=set)

    def summary(self) -> dict:
        return {
            "id": self.task.id,
            "title": self.task.title,
            "status": self.status,
            "effort": self.effort or self.task.effort,
            "use_web": self.task.use_web,
            "attempts": self.attempts,
            "cost": round(self.cost, 6),
            "handoff": self.handoff,
            "result_status": self.result_status,
            "error": self.error,
            "sources": self.sources,
        }


@dataclass
class RunSession:
    run_id: str
    goal: str
    playbook: Playbook | None
    plan: Plan
    plan_notes: list[str]
    estimate: dict
    run_cap: float
    allow_confidential: bool
    context: PlanContext
    created: str
    plan_cost: float
    direct: bool = False
    base_report: str = ""
    resumed: bool = False
    prior_states: dict[str, TaskState] = field(default_factory=dict)
    digest: dict | None = None
    facts_used: dict[str, Fact] = field(default_factory=dict)


@dataclass
class RunOutcome:
    run_id: str
    status: str
    report_path: Path | None
    cost: float
    tasks: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    learned: int = 0
    proposed: list[str] = field(default_factory=list)


class CacheGate:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._events: dict[str, asyncio.Event] = {}

    async def enter(self, key: str) -> Callable[[], None]:
        if not self.enabled:
            return lambda: None
        event = self._events.get(key)
        if event is None:
            event = asyncio.Event()
            self._events[key] = event
            return event.set
        try:
            await asyncio.wait_for(event.wait(), timeout=90)
        except TimeoutError:
            pass
        return lambda: None


class Orchestrator:
    def __init__(
        self, settings: Settings, ws: Workspace, llm: LLM, ledger: Ledger, audit: AuditLog, bus: EventBus,
        pool: ReservationPool | None = None,
    ) -> None:
        self.base_settings = settings
        self.settings = settings
        self.model: str | None = None
        self.ws = ws
        self.llm = llm
        self.ledger = ledger
        self.audit = audit
        self.bus = bus
        self.pool = pool or ReservationPool()

    def set_model(self, model: str | None, run_id: str | None = None) -> None:
        try:
            self.settings = with_model(self.base_settings, model)
        except ConfigError as exc:
            raise RunError(str(exc)) from None
        if model == self.model:
            return
        self.model = model
        if run_id:
            self.audit.append("run.model_changed", run_id=run_id, model=model or "config")
            self.bus.emit("run.model_changed", run_id, model=model or "config")

    def models_in_use(self) -> dict[str, str]:
        return {role: getattr(self.settings.models, role).model for role in MODEL_ROLES}

    def _guard(self, run_id: str, run_cap: float) -> BudgetGuard:
        return BudgetGuard(
            self.ledger, self.settings, self.ws.name, self.ws.config.budget_monthly_usd, run_id, run_cap, self.pool
        )

    def _brief(self) -> str:
        text = self.ws.read_brief()
        if self.ws.config.redact_pii:
            text, _ = redact_pii(text)
        return text.strip()

    def _profile(self) -> str:
        lines = self.ws.config.profile.lines()
        if not lines:
            return ""
        text = "\n".join(lines)
        if self.ws.config.redact_pii:
            text, _ = redact_pii(text)
        return f"<business_profile>\n{text}\n</business_profile>"

    def _system(self, prompt: str) -> list[dict]:
        blocks: list[dict] = [{"type": "text", "text": prompt}]
        profile = self._profile()
        if profile:
            blocks.append({"type": "text", "text": profile})
        brief = self._brief()
        if brief:
            blocks.append({"type": "text", "text": f"<workspace_brief>\n{brief}\n</workspace_brief>"})
        blocks.append(
            {
                "type": "text",
                "text": f"Output language: {self.settings.orchestrator.output_language}.",
                "cache_control": {"type": "ephemeral"},
            }
        )
        return blocks

    def _record(self, session: RunSession, status: str, states: dict[str, TaskState] | None = None, **extra) -> None:
        record = {
            "run_id": session.run_id,
            "workspace": self.ws.name,
            "created": session.created,
            "goal": session.goal,
            "playbook": session.playbook.id if session.playbook else None,
            "status": status,
            "plan": session.plan.model_dump(),
            "plan_notes": session.plan_notes,
            "estimate": session.estimate,
            "run_cap": session.run_cap,
            "mode": "direct" if session.direct else "planned",
            "base_run": session.context.base["run_id"] if session.context.base else None,
            "allow_confidential": session.allow_confidential,
            "models": self.models_in_use(),
            "digest": {"id": session.digest["id"], "title": session.digest["title"]} if session.digest else None,
            "tasks": [s.summary() for s in states.values()] if states else [],
            "cost_total": round(self.ledger.spent_run(session.run_id), 6),
            **extra,
        }
        self.ws.save_run(session.run_id, record)

    def _prepare(
        self, goal: str, playbook_id: str | None, run_cap: float | None, allow_confidential: bool,
        base_run_id: str | None,
    ) -> tuple[str, Playbook | None, float, PlanContext, str]:
        goal = goal.strip()
        if not goal or len(goal) > MAX_GOAL_CHARS:
            raise RunError(tr("목표는 1자 이상 {limit}자 이하여야 함", limit=MAX_GOAL_CHARS))
        playbook = get_playbook(playbook_id)
        cap = run_cap or self.settings.budget.run_default_usd
        if cap > self.settings.budget.monthly_hard_usd:
            raise RunError(tr("실행 예산이 월 상한보다 클 수 없음"))
        base_record = None
        base_report = ""
        if base_run_id:
            base_record = self.ws.load_run(base_run_id)
            if base_record is None or base_record.get("status") not in FOLLOWABLE:
                raise RunError(tr("이어서 작업할 수 있는 실행이 아님 (완료 또는 일부 완료된 실행만 가능)"))
            if base_record.get("allow_confidential") and not allow_confidential:
                raise RunError(tr("confidential 자료를 보낸 실행은 confidential 전송을 허용해야 이어서 작업할 수 있음"))
            base_report = self._base_report(base_run_id)
        ctx = build_context(self.ws, playbook, allow_confidential, base_record)
        if playbook and playbook.needs_inputs and not ctx.sendable_inputs:
            raise RunError(tr("이 플레이북은 작업 영역에 전송 가능한 입력 자료가 필요함 (`claubiz ws import`)"))
        if playbook and playbook.needs_web and not ctx.web_allowed:
            raise RunError(tr("이 플레이북은 웹 접근이 필요하지만 작업 영역 활동 범위가 {scope}임", scope=self.ws.config.scope))
        return goal, playbook, cap, ctx, base_report

    def _base_report(self, run_id: str) -> str:
        text = self.ws.read_report(run_id) or ""
        text = strip_report_footer(text)
        if self.ws.config.redact_pii:
            text, _ = redact_pii(text)
        if len(text) > MAX_BASE_REPORT_CHARS:
            text = text[:MAX_BASE_REPORT_CHARS] + tr("\n\n(이하 생략)")
        return text

    def _fresh_run_id(self, run_id: str | None) -> str:
        if run_id is None:
            return new_run_id()
        if not RUN_ID_RE.fullmatch(run_id) or self.ws.load_run(run_id) is not None:
            raise RunError(tr("사용할 수 없는 실행 ID"))
        return run_id

    def _requested(self, event: str, run_id: str, goal: str, playbook: Playbook | None, ctx: PlanContext,
                   allow_confidential: bool, cap: float) -> None:
        self.audit.append(
            event, workspace=self.ws.name, run_id=run_id, playbook=playbook.id if playbook else None,
            goal_sha256=hashlib.sha256(goal.encode()).hexdigest(), allow_confidential=allow_confidential, run_cap=cap,
            base_run=ctx.base["run_id"] if ctx.base else None, models=self.models_in_use(),
        )

    async def plan(
        self, goal: str, playbook_id: str | None = None, run_cap: float | None = None, allow_confidential: bool = False,
        base_run_id: str | None = None, run_id: str | None = None,
    ) -> RunSession:
        goal, playbook, cap, ctx, base_report = self._prepare(
            goal, playbook_id, run_cap, allow_confidential, base_run_id
        )
        run_id = self._fresh_run_id(run_id)
        created = datetime.now().isoformat(timespec="seconds")
        self._requested("run.plan_requested", run_id, goal, playbook, ctx, allow_confidential, cap)
        self.bus.emit("run.planning", run_id, goal=goal)
        guard = self._guard(run_id, cap)
        role = self.settings.models.planner
        result = await self.llm.run(
            ctx=CallContext(self.ws.name, run_id, "plan", "planner"),
            role=role,
            effort=role.effort,
            system=self._system(PLANNER_SYSTEM),
            messages=[{"role": "user", "content": planner_message(goal, self.ws, playbook, ctx, self.settings)}],
            guard=guard,
            schema=PLAN_SCHEMA,
        )
        if result.stop_reason != "end_turn":
            raise PlanError(tr("계획 생성이 완료되지 않음 (stop_reason={reason})", reason=result.stop_reason))
        plan, notes = validate_plan(plain_text(result.blocks), self.ws, ctx, self.settings)
        estimate = estimate_plan(plan, self.ws, self.settings, self.ledger)
        session = RunSession(
            run_id=run_id, goal=goal, playbook=playbook, plan=plan, plan_notes=notes, estimate=estimate, run_cap=cap,
            allow_confidential=allow_confidential, context=ctx, created=created, plan_cost=result.cost,
            base_report=base_report,
        )
        self._record(session, "planned")
        self.audit.append("run.planned", run_id=run_id, tasks=len(plan.tasks), estimate=round(estimate["total"], 4))
        self.bus.emit("run.planned", run_id, tasks=[t.model_dump() for t in plan.tasks], estimate=estimate, notes=notes)
        return session

    def direct(
        self, goal: str, playbook_id: str | None = None, run_cap: float | None = None, allow_confidential: bool = False,
        base_run_id: str | None = None, run_id: str | None = None, digest: dict | None = None,
    ) -> RunSession:
        goal, playbook, cap, ctx, base_report = self._prepare(
            goal, playbook_id, run_cap, allow_confidential, base_run_id
        )
        notes: list[str] = []
        use_web = ctx.web_allowed and (playbook is None or playbook.needs_web)
        inputs: list[str] = []
        worker = self.settings.models.worker
        limit = self.ws.config.web.max_searches_per_task
        if digest is not None:
            if not ctx.web_allowed:
                raise RunError(tr("정기 정리는 웹 조사가 필요하지만 이 작업 영역은 웹을 쓸 수 없음"))
            title = f"{digest['title']} · {datetime.now().strftime('%Y-%m-%d')}"
            effort = worker.clamp("low")
            searches = max(1, min(int(digest.get("max_searches") or limit), limit))
            approach = tr("정기 정리: {since} 이후 새 소식만 에이전트 하나가 조사해 정리함", since=digest["since"])
        else:
            used = 0
            for f in ctx.sendable_inputs:
                if used + f.chars > DIRECT_INPUT_CHARS:
                    notes.append(tr("입력 자료 분량 제한으로 제외: {name}", name=f.name))
                    continue
                inputs.append(f.name)
                used += f.chars
            if not use_web and not inputs:
                notes.append(tr("웹과 입력 자료가 모두 없음, 모델 지식만으로 작성됨"))
            title = " ".join(goal.split())
            title = title if len(title) <= 60 else title[:59] + "…"
            effort = worker.clamp("medium")
            searches = limit if use_web else 0
            approach = tr("계획 단계 없이 에이전트 하나가 목표를 바로 조사하고 보고서를 작성함")
        task = PlannedTask(
            id="t1", title=title, objective=goal, use_web=use_web, inputs=inputs, depends_on=[],
            effort=effort, max_searches=searches, reuse_ref="",
        )
        plan = Plan(title=title, approach=approach, tasks=[task], outline=[], synth_effort="low")
        run_id = self._fresh_run_id(run_id)
        created = datetime.now().isoformat(timespec="seconds")
        self._requested("run.direct_requested", run_id, goal, playbook, ctx, allow_confidential, cap)
        estimate = estimate_plan(plan, self.ws, self.settings, self.ledger, synthesize=False)
        session = RunSession(
            run_id=run_id, goal=goal, playbook=playbook, plan=plan, plan_notes=notes, estimate=estimate, run_cap=cap,
            allow_confidential=allow_confidential, context=ctx, created=created, plan_cost=0.0, direct=True,
            base_report=base_report, digest=digest,
        )
        self._record(session, "planned")
        self.bus.emit("run.planned", run_id, tasks=[task.model_dump()], estimate=estimate, notes=notes)
        return session

    def _documents(self, session: RunSession, task: PlannedTask) -> list[str]:
        parts: list[str] = []
        entries = {f.name: f for f in session.context.sendable_inputs}
        for name in task.inputs:
            entry = entries[name]
            text = self.ws.read_input(entry)
            counts: dict[str, int] = {}
            if self.ws.config.redact_pii:
                text, counts = redact_pii(text)
            self.audit.append(
                "input.sent", run_id=session.run_id, task_id=task.id, name=name, classification=entry.classification,
                sha256=entry.sha256, redactions=counts,
            )
            parts.append(f'<document name="{name}" classification="{entry.classification}">\n{text}\n</document>')
        return parts

    def _use_facts(self, session: RunSession, query: str, limit: int, max_chars: int) -> tuple[str, list[Fact]]:
        chosen = select_facts(list(session.context.facts), query, limit)
        block = facts_block(chosen, max_chars)
        if not block:
            return "", []
        shown = [f for f in chosen if f"[{f.id}]" in block]
        for fact in shown:
            session.facts_used[fact.id] = fact
        return block, shown

    def _worker_message(self, session: RunSession, st: TaskState, states: dict[str, TaskState]) -> str:
        task = st.task
        lines = [
            f"Today: {date.today().isoformat()}",
            f"<overall_goal>\n{session.goal}\n</overall_goal>",
            f"Your task ({task.id}): {task.title}",
            f"Objective: {task.objective}",
        ]
        digest = session.digest
        if digest is not None:
            lines.append(f"Digest name: {digest['title']}")
            lines.append(f"Reporting period: from {digest['since']} until now.")
            if digest.get("previous"):
                lines.append(f"<previous_digest>\n{digest['previous']}\n</previous_digest>")
            if digest.get("seen"):
                lines.append("<already_reported>\n" + "\n".join(digest["seen"]) + "\n</already_reported>")
        if task.use_web:
            lines.append(
                f"Search budget: aim for {task.max_searches} web searches or fewer "
                f"(hard limit {self.ws.config.web.max_searches_per_task}). Stop as soon as you can answer well."
            )
        else:
            lines.append("You have no web access for this task. Use only the material below.")
        if session.direct:
            if session.playbook:
                lines.append(f"Deliverable guidance: {session.playbook.synth_guidance}")
            if session.base_report:
                base = session.context.base
                lines.append(f'<base_report run="{base["run_id"]}" date="{base["date"]}">\n'
                             f"{session.base_report}\n</base_report>")
            elif session.context.previous_summary:
                prev = session.context.previous_summary
                lines.append(f'<previous_summary date="{prev["date"]}">\n{prev["summary"]}\n</previous_summary>')
        elif session.context.base and session.context.base["summary"]:
            base = session.context.base
            lines.append(f'<earlier_findings date="{base["date"]}">\n{base["summary"][:1500]}\n</earlier_findings>')
        upstream = [states[d] for d in task.depends_on if states[d].handoff]
        if upstream:
            lines.append("<upstream_findings>")
            lines += [f"From {u.task.id} ({u.task.title}):\n{u.handoff}" for u in upstream]
            lines.append("</upstream_findings>")
        facts, _ = self._use_facts(session, f"{session.goal}\n{task.title}\n{task.objective}", TASK_FACTS,
                                   TASK_FACT_CHARS)
        if facts:
            lines.append("Known facts from earlier runs (id, date, fact, sources):\n" + facts)
        docs = self._documents(session, task)
        if docs:
            lines.append("<documents>\n" + "\n".join(docs) + "\n</documents>")
        return "\n\n".join(lines)

    @staticmethod
    def _worker_prompt(session: RunSession) -> str:
        if session.digest is not None:
            return DIGEST_SYSTEM
        return DIRECT_SYSTEM if session.direct else WORKER_SYSTEM

    def _write_task(self, out_dir: Path, st: TaskState) -> None:
        header = tr("# {id}. {title}\n\n> 상태: {status} | effort: {effort} | 비용: ${cost:.4f}\n\n", id=st.task.id,
                    title=st.task.title, status=st.status, effort=st.effort, cost=st.cost)
        write_private_text(out_dir / "tasks" / f"{st.task.id}.md", header + (st.notes_md or st.error) + "\n")

    async def _run_task(
        self, session: RunSession, st: TaskState, states: dict[str, TaskState], guard: BudgetGuard, gate: CacheGate,
        out_dir: Path,
    ) -> None:
        task = st.task
        if task.reuse_ref and task.reuse_ref in session.context.reusable:
            match = REUSE_RE.fullmatch(task.reuse_ref)
            prior = self.ws.read_task_artifact(match.group(1), match.group(2)) if match else None
            if prior:
                st.notes_md = prior
                st.verified_urls = set(urls_in(strip_unverified(prior)))
                st.handoff = session.context.reusable[task.reuse_ref]["handoff"]
                st.status, st.effort, st.result_status = "reused", "-", "ok"
                self._write_task(out_dir, st)
                self.bus.emit("task.reused", session.run_id, task.id, ref=task.reuse_ref)
                return
        role = self.settings.models.worker
        effort = task.effort
        max_tokens = role.max_tokens
        escalate = self.settings.orchestrator.escalate_on_failure
        st.status = "running"
        self.bus.emit("task.started", session.run_id, task.id, effort=effort)
        message = self._worker_message(session, st, states)
        trusted = fact_sources(session.facts_used.values())
        tools = web_tools(role.model, self.ws.config.web) if task.use_web else None
        notes = None
        for attempt in range(2 if escalate else 1):
            st.attempts += 1
            key = json.dumps([role.model, effort, tools], sort_keys=True)
            warm = await gate.enter(key)
            try:
                result = await self.llm.run(
                    ctx=CallContext(self.ws.name, session.run_id, task.id, "worker"),
                    role=role,
                    effort=effort,
                    system=self._system(self._worker_prompt(session)),
                    messages=[{"role": "user", "content": message}],
                    guard=guard,
                    tools=tools,
                    max_tokens=max_tokens,
                    on_warm=warm,
                )
            finally:
                warm()
            st.cost += result.cost
            notes = render(result.blocks, trusted)
            if attempt == 0 and escalate:
                if result.stop_reason in ("max_tokens", "pause_limit") and max_tokens < MAX_TOKENS_CEILING:
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CEILING)
                    self.bus.emit("task.retry", session.run_id, task.id, reason=result.stop_reason, effort=effort)
                    continue
                if notes.status == "insufficient":
                    higher = role.escalate(effort)
                    if higher:
                        self.bus.emit("task.retry", session.run_id, task.id, reason="insufficient", effort=higher)
                        effort = higher
                        continue
            break
        st.effort = effort
        st.notes_md = notes.as_markdown()
        st.handoff = notes.handoff or notes.body[:600]
        st.result_status = notes.status
        st.sources = notes.source_count
        st.verified_urls = set(notes.verified_urls)
        st.status = "done"
        self.ledger.record_task_cost(role="worker", model=role.model, effort=effort, web=task.use_web, cost=st.cost)
        self._write_task(out_dir, st)
        self.audit.append(
            "task.done", run_id=session.run_id, task_id=task.id, cost=round(st.cost, 6), effort=effort,
            attempts=st.attempts, sources=st.sources, result_status=st.result_status,
        )
        self.bus.emit("task.done", session.run_id, task.id, cost=st.cost, status=notes.status, sources=st.sources)

    def _synth_message(self, session: RunSession, states: dict[str, TaskState]) -> str:
        lines = [f"Today: {date.today().isoformat()}", f"<goal>\n{session.goal}\n</goal>"]
        if session.playbook:
            lines.append(session.playbook.synth_guidance)
        lines.append("Outline:\n" + "\n".join(f"{i}. {h}" for i, h in enumerate(session.plan.outline, start=1)))
        prev = session.context.previous_summary
        base = session.context.base
        if session.base_report and base:
            lines.append(f'<base_report run="{base["run_id"]}" date="{base["date"]}">\n'
                         f"{session.base_report}\n</base_report>")
        elif prev:
            lines.append(f'<previous_summary date="{prev["date"]}">\n{prev["summary"]}\n</previous_summary>')
        facts, _ = self._use_facts(session, session.goal, SYNTH_FACTS, SYNTH_FACT_CHARS)
        if facts:
            lines.append("Known facts from earlier runs (id, date, fact, sources):\n" + facts)
        notes = []
        gaps = []
        for st in states.values():
            if st.status in ("done", "reused"):
                notes.append(
                    f'<task_notes id="{st.task.id}" title="{st.task.title}" status="{st.result_status}">\n'
                    f"{st.notes_md}\n</task_notes>"
                )
            else:
                gaps.append(f"- {st.task.id} {st.task.title}: {st.status} {st.error}".rstrip())
        lines.append("<worker_notes>\n" + "\n".join(notes) + "\n</worker_notes>")
        if gaps:
            lines.append("Tasks without results (mention these gaps briefly):\n" + "\n".join(gaps))
        return "\n\n".join(lines)

    async def _synthesize(self, session: RunSession, states: dict[str, TaskState], guard: BudgetGuard) -> tuple[str, str, float]:
        role = self.settings.models.synthesizer
        self.bus.emit("synth.started", session.run_id)
        result = await self.llm.run(
            ctx=CallContext(self.ws.name, session.run_id, "synth", "synthesizer"),
            role=role,
            effort=session.plan.synth_effort,
            system=self._system(SYNTH_SYSTEM),
            messages=[{"role": "user", "content": self._synth_message(session, states)}],
            guard=guard,
        )
        rendered = render(result.blocks)
        self.ledger.record_task_cost(
            role="synthesizer", model=role.model, effort=session.plan.synth_effort, web=False, cost=result.cost
        )
        if result.stop_reason == "max_tokens":
            rendered_body = rendered.body + tr("\n\n> 주의: 출력 한도에 도달해 보고서가 잘렸을 수 있음\n")
        else:
            rendered_body = rendered.body
        self.bus.emit("synth.done", session.run_id, cost=result.cost)
        return rendered_body, rendered.handoff, result.cost

    def _fallback_report(self, session: RunSession, states: dict[str, TaskState], reason: str) -> str:
        parts = [f"# {session.plan.title}", "", tr("> 종합 단계를 실행하지 못해 작업별 노트를 그대로 묶음: {reason}", reason=reason), ""]
        for st in states.values():
            if st.notes_md:
                parts += [f"## {st.task.id}. {st.task.title}", "", st.notes_md, ""]
        return "\n".join(parts)

    async def _final_report(
        self, session: RunSession, states: dict[str, TaskState], completed: list[TaskState], guard: BudgetGuard,
        status: str, warnings: list[str],
    ) -> tuple[str, str, str]:
        try:
            body, summary, _ = await self._synthesize(session, states, guard)
        except (BudgetExceeded, RefusalError, anthropic.APIError) as exc:
            warnings.append(tr("종합 단계 실패: {error}", error=exc))
            return "partial", self._fallback_report(session, states, str(exc)), ""
        known: set[str] = set(fact_sources(session.facts_used.values()))
        for done in completed:
            known |= done.verified_urls
        if session.context.previous_summary:
            known |= set(urls_in(session.context.previous_summary.get("summary", "")))
        if session.base_report:
            known |= set(urls_in(session.base_report))
        suspicious = unknown_urls(body, known)
        if suspicious:
            warnings.append(tr("노트에 없는 링크 {count}개 발견, 보고서 하단에 표시함", count=len(suspicious)))
            body += tr("\n\n## 검증되지 않은 링크\n\n> 아래 링크는 작업 노트의 출처에 없던 주소임, 사용 전 확인 필요\n\n")
            body += "\n".join(f"- {u}" for u in suspicious) + "\n"
        return status, body, summary

    async def execute(self, session: RunSession) -> RunOutcome:
        plan = session.plan
        prior = session.prior_states
        if session.resumed:
            self.audit.append("run.resumed", run_id=session.run_id, reused_tasks=sorted(prior))
        self.audit.append("run.approved", run_id=session.run_id, estimate=round(session.estimate["total"], 4),
                          models=self.models_in_use())
        guard = self._guard(session.run_id, session.run_cap)
        out_dir = self.ws.output_dir(session.run_id)
        states = {t.id: prior.get(t.id) or TaskState(task=t, effort=t.effort) for t in plan.tasks}
        finished = {t.id: asyncio.Event() for t in plan.tasks}
        for task_id in prior:
            finished[task_id].set()
        sem = asyncio.Semaphore(self.settings.orchestrator.max_parallel)
        gate = CacheGate(self.settings.orchestrator.cache_warm_fanout)
        warnings: list[str] = []
        self._record(session, "running", states)
        self.bus.emit("run.started", session.run_id, tasks=len(plan.tasks), resumed=len(prior))

        async def runner(st: TaskState) -> None:
            try:
                for dep in st.task.depends_on:
                    await finished[dep].wait()
                failed = [d for d in st.task.depends_on if states[d].status not in ("done", "reused")]
                if failed:
                    st.status, st.error = "skipped", tr("선행 작업 실패: {tasks}", tasks=", ".join(failed))
                    self.bus.emit("task.skipped", session.run_id, st.task.id, reason=st.error)
                    return
                self.bus.emit("task.queued", session.run_id, st.task.id)
                async with sem:
                    await self._run_task(session, st, states, guard, gate, out_dir)
            except BudgetExceeded as exc:
                st.status, st.error = "blocked", str(exc)
                self.audit.append("budget.blocked", run_id=session.run_id, task_id=st.task.id, reason=str(exc))
                self.bus.emit("task.blocked", session.run_id, st.task.id, reason=str(exc))
            except RefusalError as exc:
                st.status, st.error = "failed", str(exc)
                self.audit.append("task.refused", run_id=session.run_id, task_id=st.task.id, category=exc.category)
                self.bus.emit("task.failed", session.run_id, st.task.id, reason=str(exc))
            except anthropic.APIError as exc:
                detail = getattr(exc, "message", "") or type(exc).__name__
                st.status, st.error = "failed", tr("API 오류 {kind}: {detail}", kind=type(exc).__name__,
                                                    detail=str(detail)[:200])
                self.audit.append("task.failed", run_id=session.run_id, task_id=st.task.id, error=st.error)
                self.bus.emit("task.failed", session.run_id, st.task.id, reason=st.error)
            except Exception as exc:
                st.status, st.error = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
                self.audit.append("task.failed", run_id=session.run_id, task_id=st.task.id, error=st.error)
                self.bus.emit("task.failed", session.run_id, st.task.id, reason=st.error)
            finally:
                finished[st.task.id].set()
                if st.status in ("failed", "blocked", "skipped"):
                    self._write_task(out_dir, st)
            self._record(session, "running", states)

        try:
            await asyncio.gather(*(runner(s) for s in states.values() if s.task.id not in prior))
            completed = [s for s in states.values() if s.status in ("done", "reused")]
            if not completed:
                self._record(session, "failed", states)
                self.audit.append("run.failed", run_id=session.run_id, reason="no completed tasks")
                self.bus.emit("run.failed", session.run_id, reason=tr("완료된 작업이 없음"))
                return RunOutcome(session.run_id, "failed", None, self.ledger.spent_run(session.run_id),
                                  [s.summary() for s in states.values()])
            status = "completed" if len(completed) == len(states) else "partial"
            if session.direct:
                single = completed[0]
                if single.result_status == "insufficient":
                    status = "partial"
                body, summary = single.notes_md, single.handoff
            else:
                status, body, summary = await self._final_report(session, states, completed, guard, status, warnings)
            total = self.ledger.spent_run(session.run_id)
            footer = tr("\n\n---\n실행 {run} | 작업 영역 {workspace} | 비용 ${cost:.4f}\n", run=session.run_id,
                        workspace=self.ws.name, cost=total)
            report_path = out_dir / "report.md"
            write_private_text(report_path, body.rstrip() + footer)
            self._record(session, status, states, summary=summary, report=str(report_path), warnings=warnings)
            self.audit.append(
                "run.completed", run_id=session.run_id, status=status, cost=round(total, 6),
                report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
            )
        except asyncio.CancelledError:
            self._record(session, "cancelled", states)
            self.audit.append("run.cancelled", run_id=session.run_id)
            self.bus.emit("run.cancelled", session.run_id)
            raise
        floor = self._floor(session)
        verified = set(fact_sources(session.facts_used.values()))
        for st in states.values():
            verified |= st.verified_urls
        sources = {u for u in urls_in(body) if u in verified}
        sources |= {name for t in session.plan.tasks for name in t.inputs}
        learned = await self._learn(session.run_id, session.goal, body, sources, list(session.context.facts), floor,
                                    guard, warnings)
        proposed = await self._propose(session.run_id, session.goal, body, floor, guard, warnings)
        total = self.ledger.spent_run(session.run_id)
        self.audit.append("run.finalized", run_id=session.run_id, cost=round(total, 6), knowledge=learned,
                          actions=len(proposed))
        if learned or proposed or warnings:
            self._record(session, status, states, summary=summary, report=str(report_path), warnings=warnings,
                         knowledge=learned, actions=proposed)
        self.bus.emit("run.done", session.run_id, status=status, report=str(report_path), cost=total)
        return RunOutcome(session.run_id, status, report_path, total, [s.summary() for s in states.values()],
                          warnings, learned, proposed)

    def _floor(self, session: RunSession) -> str:
        entries = {f.name: f.classification for f in session.context.sendable_inputs}
        ranks = [CLASS_RANK["internal"]]
        ranks += [CLASS_RANK[entries[n]] for t in session.plan.tasks for n in t.inputs if n in entries]
        ranks += [CLASS_RANK[f.classification] for f in session.facts_used.values()]
        if session.context.base:
            base = self.ws.load_run(session.context.base["run_id"]) or {}
            if base.get("allow_confidential"):
                ranks.append(CLASS_RANK["confidential"])
        top = max(ranks)
        return next(name for name, rank in CLASS_RANK.items() if rank == top)

    def _report_text(self, body: str) -> str:
        text = strip_report_footer(body)
        if self.ws.config.redact_pii:
            text, _ = redact_pii(text)
        if len(text) > HELPER_REPORT_CHARS:
            text = text[:HELPER_REPORT_CHARS] + tr("\n\n(이하 생략)")
        return text

    async def _helper(self, run_id: str, task_id: str, prompt: str, message: str, schema: dict,
                      guard: BudgetGuard) -> tuple[str, float]:
        role = self.settings.models.helper
        result = await self.llm.run(
            ctx=CallContext(self.ws.name, run_id, task_id, "helper"),
            role=role,
            effort=role.clamp("low"),
            system=self._system(prompt),
            messages=[{"role": "user", "content": message}],
            guard=guard,
            schema=schema,
        )
        if result.stop_reason != "end_turn":
            raise ValueError(tr("응답이 완료되지 않음 (stop_reason={reason})", reason=result.stop_reason))
        return plain_text(result.blocks), result.cost

    async def _learn(self, run_id: str, goal: str, body: str, sources: set[str], known: list[Fact], floor: str,
                     guard: BudgetGuard, warnings: list[str]) -> int:
        if not self.ws.config.knowledge or not sources:
            return 0
        candidates = select_facts(known, f"{goal}\n{body[:3000]}", LEARN_CANDIDATES)
        lines = [f"Today: {date.today().isoformat()}", f"<goal>\n{goal}\n</goal>"]
        if candidates:
            lines.append("Existing facts (id, date, fact, sources):\n" + facts_block(candidates, 8000, "existing_facts"))
        lines.append(f"<report>\n{self._report_text(body)}\n</report>")
        self.bus.emit("knowledge.started", run_id)
        try:
            raw, cost = await self._helper(run_id, "learn", KNOWLEDGE_SYSTEM, "\n\n".join(lines), EXTRACT_SCHEMA,
                                           guard)
            facts, replaces, dropped = parse_extraction(
                raw, run_id=run_id, classification=floor, allowed_sources=sources,
                candidates={f.id for f in candidates},
            )
            added, replaced = KnowledgeStore(self.ws).merge(facts, replaces)
        except (BudgetExceeded, RefusalError, anthropic.APIError, ValueError, ValidationError, OSError) as exc:
            warnings.append(tr("지식 정리 생략: {error}", error=str(exc)[:160]))
            self.bus.emit("knowledge.failed", run_id, reason=str(exc)[:160])
            return 0
        self.ledger.record_task_cost(role="helper", model=self.settings.models.helper.model, effort="low", web=False,
                                     cost=cost)
        self.audit.append("knowledge.updated", workspace=self.ws.name, run_id=run_id, added=added, replaced=replaced,
                          dropped=dropped, classification=floor)
        self.bus.emit("knowledge.updated", run_id, added=added, replaced=replaced, dropped=dropped, cost=cost)
        return added

    async def _propose(self, run_id: str, goal: str, body: str, floor: str, guard: BudgetGuard,
                       warnings: list[str]) -> list[str]:
        allowed = self.ws.config.actions
        if not allowed:
            return []
        lines = [
            f"Today: {date.today().isoformat()}",
            f"Allowed action types: {', '.join(allowed)}",
            f"<goal>\n{goal}\n</goal>",
            f"<report>\n{self._report_text(body)}\n</report>",
        ]
        self.bus.emit("actions.started", run_id)
        try:
            raw, cost = await self._helper(run_id, "actions", ACTIONS_SYSTEM, "\n\n".join(lines), PROPOSAL_SCHEMA,
                                           guard)
            actions, problems = parse_proposals(raw, run_id=run_id, allowed=allowed, classification=floor)
            store = ActionStore(self.ws)
            for action in actions:
                store.save(action)
        except (BudgetExceeded, RefusalError, anthropic.APIError, ValueError, ValidationError, OSError) as exc:
            warnings.append(tr("행동 제안 생략: {error}", error=str(exc)[:160]))
            self.bus.emit("actions.failed", run_id, reason=str(exc)[:160])
            return []
        warnings.extend(problems)
        for action in actions:
            self.audit.append("action.proposed", workspace=self.ws.name, run_id=run_id, action_id=action.id,
                              type=action.type, classification=action.classification)
        self.bus.emit("actions.proposed", run_id, count=len(actions), cost=cost)
        return [a.id for a in actions]

    async def propose_actions(self, run_id: str) -> list[str]:
        record = self.ws.load_run(run_id)
        report = self.ws.read_report(run_id)
        if record is None or not report or record.get("status") not in FOLLOWABLE:
            raise RunError(tr("보고서가 있는 완료된 실행에만 행동 제안을 받을 수 있음"))
        if not self.ws.config.actions:
            raise RunError(tr("이 작업 영역은 행동 제안을 쓰지 않도록 설정됨"))
        cap = self.ledger.spent_run(run_id) + MANUAL_HELPER_CAP
        guard = self._guard(run_id, cap)
        warnings: list[str] = []
        floor = "confidential" if record.get("allow_confidential") else "internal"
        proposed = await self._propose(run_id, str(record.get("goal", "")), report, floor, guard, warnings)
        if not proposed and warnings:
            raise RunError(warnings[0])
        record["actions"] = list(record.get("actions") or []) + proposed
        record["cost_total"] = round(self.ledger.spent_run(run_id), 6)
        self.ws.save_run(run_id, record)
        return proposed

    def cancel_planned(self, session: RunSession) -> None:
        if session.resumed:
            self.audit.append("run.resume_abandoned", run_id=session.run_id)
            return
        self._record(session, "rejected")
        self.audit.append("run.rejected", run_id=session.run_id)

    def _restore_task(self, run_id: str, task: PlannedTask, info: dict) -> TaskState | None:
        if info.get("status") not in ("done", "reused"):
            return None
        artifact = self.ws.read_task_artifact(run_id, task.id)
        if not artifact:
            return None
        parts = artifact.split("\n\n", 2)
        notes = parts[2] if len(parts) == 3 and parts[0].startswith(f"# {task.id}.") else artifact
        st = TaskState(
            task=task, status=info["status"], effort=str(info.get("effort") or task.effort),
            attempts=int(info.get("attempts") or 0), cost=float(info.get("cost") or 0.0), notes_md=notes.rstrip(),
            handoff=str(info.get("handoff") or ""), result_status=str(info.get("result_status") or "ok"),
            sources=int(info.get("sources") or 0),
        )
        st.verified_urls = set(urls_in(strip_unverified(notes)))
        if not st.handoff:
            st.handoff = notes[:600]
        return st

    def resume(self, run_id: str, allow_confidential: bool = False) -> RunSession:
        record = self.ws.load_run(run_id)
        if record is None:
            raise RunError(tr("실행 기록을 찾을 수 없음"))
        if record.get("status") not in RESUMABLE:
            raise RunError(tr("중단되었거나 실패한 실행만 재개할 수 있음"))
        allowed = bool(record.get("allow_confidential"))
        if allowed and not allow_confidential:
            raise RunError(tr("confidential 전송을 허용했던 실행이라 다시 명시적으로 허용해야 재개할 수 있음"))
        try:
            plan = Plan.model_validate(record["plan"])
            playbook = get_playbook(record.get("playbook"))
        except (KeyError, TypeError, ValidationError) as exc:
            raise RunError(tr("실행 기록을 재개할 수 없음: {error}", error=exc)) from None
        notes: list[str] = []
        base_record = None
        base_report = ""
        base_id = record.get("base_run")
        if base_id:
            base_record = self.ws.load_run(base_id)
            if base_record is None or base_record.get("status") not in FOLLOWABLE:
                base_record = None
                notes.append(tr("이어서 작업한 원래 실행이 없어져 그 보고서 없이 재개함"))
            else:
                base_report = self._base_report(base_id)
        ctx = build_context(self.ws, playbook, allowed, base_record)
        available = {f.name for f in ctx.sendable_inputs}
        infos = {str(t.get("id")): t for t in record.get("tasks") or [] if isinstance(t, dict)}
        prior: dict[str, TaskState] = {}
        tasks: list[PlannedTask] = []
        for task in plan.tasks:
            restored = self._restore_task(run_id, task, infos.get(task.id, {}))
            if restored is not None:
                prior[task.id] = restored
                tasks.append(task)
                continue
            missing = [name for name in task.inputs if name not in available]
            if missing:
                raise RunError(tr("재개에 필요한 입력 자료가 없거나 지금 정책으로 보낼 수 없음: {names}", names=", ".join(missing)))
            if task.use_web and not ctx.web_allowed:
                task = task.model_copy(update={"use_web": False, "max_searches": 0})
                notes.append(tr("{task}: 작업 영역 정책이 바뀌어 웹 조사 없이 진행함", task=task.id))
            tasks.append(task)
        plan = plan.model_copy(update={"tasks": tasks})
        for task_id, st in prior.items():
            st.task = next(t for t in tasks if t.id == task_id)
        direct = record.get("mode") == "direct"
        estimate = estimate_plan(plan, self.ws, self.settings, self.ledger, synthesize=not direct)
        for task_id in prior:
            estimate["tasks"][task_id] = 0.0
        finish_estimate(estimate)
        session = RunSession(
            run_id=run_id, goal=str(record.get("goal", "")), playbook=playbook, plan=plan,
            plan_notes=notes + list(record.get("plan_notes") or []), estimate=estimate,
            run_cap=float(record.get("run_cap") or self.settings.budget.run_default_usd), allow_confidential=allowed,
            context=ctx, created=str(record.get("created", "")), plan_cost=0.0, direct=direct,
            base_report=base_report, resumed=True, prior_states=prior,
        )
        self.audit.append("run.resume_requested", workspace=self.ws.name, run_id=run_id, previous=record.get("status"),
                          reused_tasks=sorted(prior), remaining=len(tasks) - len(prior))
        return session
