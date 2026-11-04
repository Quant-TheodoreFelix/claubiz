import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, ValidationError

from .budget import Ledger
from .config import OUTPUT_TOKENS_BY_EFFORT, Settings
from .playbooks import Playbook
from .policy import may_send, scope_allows_inputs, scope_allows_web
from .workspace import InputFile, Workspace

TASK_ID_RE = re.compile(r"^t\d{1,2}$")
REUSE_RE = re.compile(r"^(r\d{8}-\d{6}-[0-9a-f]{6})/(t\d{1,2})$")
CHARS_PER_TOKEN = 2.0


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    objective: str
    use_web: bool
    inputs: list[str]
    depends_on: list[str]
    effort: str
    max_searches: int
    reuse_ref: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    approach: str
    tasks: list[PlannedTask]
    outline: list[str]
    synth_effort: str = "medium"


PLAN_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "approach", "tasks", "outline", "synth_effort"],
    "properties": {
        "title": {"type": "string"},
        "approach": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id", "title", "objective", "use_web", "inputs", "depends_on", "effort", "max_searches", "reuse_ref",
                ],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "use_web": {"type": "boolean"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                    "max_searches": {"type": "integer"},
                    "reuse_ref": {"type": "string"},
                },
            },
        },
        "outline": {"type": "array", "items": {"type": "string"}},
        "synth_effort": {"type": "string", "enum": ["low", "medium", "high"]},
    },
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class PlanContext:
    sendable_inputs: list[InputFile]
    reusable: dict[str, dict]
    previous_summary: dict | None
    web_allowed: bool
    base: dict | None = None


def sendable_inputs(ws: Workspace, allow_confidential: bool) -> list[InputFile]:
    if not scope_allows_inputs(ws.config.scope):
        return []
    return [
        f for f in ws.inputs() if may_send(f.classification, ws.config.max_send_classification, allow_confidential)
    ]


def _reusable_tasks(record: dict) -> dict[str, dict]:
    found = {}
    for task in record.get("tasks", []):
        if task.get("status") == "done" and task.get("handoff"):
            found[f"{record['run_id']}/{task['id']}"] = {
                "date": record["created"][:10],
                "title": task["title"],
                "handoff": task["handoff"][:800],
            }
    return found


def build_context(
    ws: Workspace, playbook: Playbook | None, allow_confidential: bool, base_record: dict | None = None
) -> PlanContext:
    max_age = ws.config.reuse_max_age_days
    cutoff = datetime.now() - timedelta(days=max_age)
    reusable: dict[str, dict] = {}
    previous = None
    base = None
    if base_record is not None:
        base = {
            "run_id": base_record["run_id"],
            "date": base_record["created"][:10],
            "goal": base_record.get("goal", ""),
            "title": (base_record.get("plan") or {}).get("title", ""),
            "summary": base_record.get("summary", ""),
        }
        reusable.update(_reusable_tasks(base_record))
    for record in ws.recent_runs(limit=20):
        if record.get("status") != "completed":
            continue
        created = datetime.fromisoformat(record["created"])
        if playbook and playbook.uses_history and previous is None and record.get("playbook") == playbook.id:
            previous = {"run_id": record["run_id"], "date": record["created"][:10], "summary": record.get("summary", "")}
        if max_age and created >= cutoff:
            for ref, info in _reusable_tasks(record).items():
                reusable.setdefault(ref, info)
    web_allowed = scope_allows_web(ws.config.scope) and ws.config.web.max_searches_per_task > 0
    return PlanContext(sendable_inputs(ws, allow_confidential), reusable, previous, web_allowed, base)


def planner_message(goal: str, ws: Workspace, playbook: Playbook | None, ctx: PlanContext, settings: Settings) -> str:
    worker = settings.models.worker
    efforts = [e for e in ("low", "medium", "high") if worker.clamp(e) == e]
    lines = [
        f"Today: {date.today().isoformat()}",
        f"Task limit: {settings.orchestrator.max_tasks}",
        f"Allowed effort levels: {', '.join(efforts)}",
        f"Web access: {'allowed, at most ' + str(ws.config.web.max_searches_per_task) + ' searches per task' if ctx.web_allowed else 'not allowed'}",
    ]
    if ws.config.web.allowed_domains:
        lines.append(f"Web sources restricted to: {', '.join(ws.config.web.allowed_domains)}")
    if playbook:
        lines.append(f"\n{playbook.planner_guidance}")
    if ctx.sendable_inputs:
        lines.append("\nWorkspace input files (name | approx tokens):")
        lines += [f"- {f.name} | {int(f.chars / CHARS_PER_TOKEN)}" for f in ctx.sendable_inputs]
    else:
        lines.append("\nWorkspace input files: none available")
    if ctx.previous_summary:
        lines.append(f"\nPrevious run of this playbook: {ctx.previous_summary['date']}")
    if ctx.base:
        lines += [
            f"\nThis run continues an earlier run ({ctx.base['run_id']}, {ctx.base['date']}).",
            f"<earlier_goal>\n{ctx.base['goal'].strip()}\n</earlier_goal>",
            f"<earlier_findings>\n{ctx.base['summary'].strip() or '(none recorded)'}\n</earlier_findings>",
            (
                "Plan only the additional work the new goal needs. When an earlier task artifact below already "
                "answers a task, reuse it with reuse_ref instead of researching again."
            ),
        ]
    if ctx.reusable:
        lines.append("\nPrior research artifacts available for reuse (reuse_ref | date | title | summary):")
        for ref, info in list(ctx.reusable.items())[:15]:
            summary = info["handoff"].replace("\n", " ")[:300]
            lines.append(f"- {ref} | {info['date']} | {info['title']} | {summary}")
    lines.append(f"\n<goal>\n{goal.strip()}\n</goal>")
    return "\n".join(lines)


def _topo_order(tasks: list[PlannedTask]) -> list[str]:
    deps = {t.id: set(t.depends_on) for t in tasks}
    order: list[str] = []
    ready = [t.id for t in tasks if not deps[t.id]]
    while ready:
        node = ready.pop(0)
        order.append(node)
        for other, ds in deps.items():
            if node in ds:
                ds.discard(node)
                if not ds and other not in order and other not in ready:
                    ready.append(other)
    if len(order) != len(tasks):
        raise PlanError("작업 의존성에 순환이 있음")
    return order


def validate_plan(raw: str, ws: Workspace, ctx: PlanContext, settings: Settings) -> tuple[Plan, list[str]]:
    try:
        plan = Plan.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise PlanError(f"계획 형식 오류: {exc}") from None
    notes: list[str] = []
    if not plan.tasks:
        raise PlanError("계획에 작업이 없음")
    if len(plan.tasks) > settings.orchestrator.max_tasks:
        notes.append(f"작업 수 제한으로 {len(plan.tasks) - settings.orchestrator.max_tasks}개 작업을 제외함")
        plan.tasks = plan.tasks[: settings.orchestrator.max_tasks]
    ids = [t.id for t in plan.tasks]
    if len(set(ids)) != len(ids) or not all(TASK_ID_RE.fullmatch(i) for i in ids):
        raise PlanError("작업 ID가 중복되었거나 형식이 올바르지 않음")
    known_inputs = {f.name for f in ctx.sendable_inputs}
    worker = settings.models.worker
    limit = ws.config.web.max_searches_per_task
    for task in plan.tasks:
        task.depends_on = [d for d in dict.fromkeys(task.depends_on) if d in ids and d != task.id]
        bad_inputs = [i for i in task.inputs if i not in known_inputs]
        if bad_inputs:
            notes.append(f"{task.id}: 허용되지 않거나 없는 입력 파일 제외 ({', '.join(bad_inputs)})")
        task.inputs = [i for i in dict.fromkeys(task.inputs) if i in known_inputs]
        if task.use_web and not ctx.web_allowed:
            notes.append(f"{task.id}: 작업 영역 정책상 웹 사용 불가, 웹 비활성화")
            task.use_web = False
        task.max_searches = min(max(task.max_searches, 1), limit) if task.use_web else 0
        clamped = worker.clamp(task.effort)
        if clamped != task.effort:
            notes.append(f"{task.id}: effort {task.effort} -> {clamped} (설정 범위)")
            task.effort = clamped
        if task.reuse_ref and task.reuse_ref not in ctx.reusable:
            notes.append(f"{task.id}: 재사용 대상이 유효하지 않아 새로 조사함")
            task.reuse_ref = ""
        if task.reuse_ref and not REUSE_RE.fullmatch(task.reuse_ref):
            task.reuse_ref = ""
        if not task.use_web and not task.inputs and not task.reuse_ref and not task.depends_on:
            notes.append(f"{task.id}: 웹과 입력 자료가 모두 없음, 모델 지식만으로 작성됨")
    synth = settings.models.synthesizer
    clamped_synth = synth.clamp(plan.synth_effort)
    if clamped_synth != plan.synth_effort:
        notes.append(f"종합 effort {plan.synth_effort} -> {clamped_synth} (설정 범위)")
        plan.synth_effort = clamped_synth
    _topo_order(plan.tasks)
    return plan, notes


def estimate_task(task: PlannedTask, ws: Workspace, settings: Settings, ledger: Ledger) -> float:
    if task.reuse_ref:
        return 0.0
    worker = settings.models.worker
    history = ledger.task_cost_history("worker", worker.model, task.effort, task.use_web)
    if len(history) >= 3:
        return sum(history) / len(history)
    price = settings.price_for(worker.model)
    chars = sum(f.chars for f in ws.inputs() if f.name in task.inputs)
    prompt = 2500 + chars / CHARS_PER_TOKEN + task.max_searches * 9000 + (5500 if task.use_web else 0)
    if task.use_web:
        prompt += ws.config.web.max_fetches_per_task * ws.config.web.fetch_max_content_tokens * 0.5
    output = OUTPUT_TOKENS_BY_EFFORT.get(task.effort, 5000)
    return (prompt * price.input + output * price.output) / 1_000_000 + task.max_searches * settings.web_search_usd_per_1k / 1000


def estimate_plan(plan: Plan, ws: Workspace, settings: Settings, ledger: Ledger, synthesize: bool = True) -> dict:
    per_task = {t.id: estimate_task(t, ws, settings, ledger) for t in plan.tasks}
    if not synthesize:
        return {"tasks": per_task, "synthesizer": 0.0, "total": sum(per_task.values())}
    synth = settings.models.synthesizer
    history = ledger.task_cost_history("synthesizer", synth.model, plan.synth_effort, False)
    if len(history) >= 3:
        synth_cost = sum(history) / len(history)
    else:
        price = settings.price_for(synth.model)
        prompt = 3000 + 2500 * len(plan.tasks)
        output = OUTPUT_TOKENS_BY_EFFORT.get(plan.synth_effort, 5000) * 1.5
        synth_cost = (prompt * price.input + output * price.output) / 1_000_000
    return {"tasks": per_task, "synthesizer": synth_cost, "total": sum(per_task.values()) + synth_cost}
