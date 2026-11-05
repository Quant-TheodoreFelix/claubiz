import hashlib

from .budget import BudgetGuard
from .llm import CallContext
from .policy import redact_pii
from .prompts import BRIEF_SYSTEM, system_blocks
from .render import plain_text
from .workspace import MAX_BRIEF_CHARS, new_run_id

BRIEF_RUN_CAP_USD = 0.3
BRIEF_MAX_TOKENS = 4000
MAX_HINT_CHARS = 4000
SCOPE_NOTES = {
    "research": "web research plus the operator's local documents",
    "web-only": "web research only, no local documents",
    "inputs-only": "only the operator's local documents, no web access",
}


def brief_message(name: str, description: str, scope: str, web_note: str, hints: str,
                  profile: list[str] | None = None) -> str:
    lines = [
        f"Workspace name: {name}",
        f"Description: {description.strip() or '(none)'}",
        f"Activity scope: {SCOPE_NOTES.get(scope, scope)}",
        f"Web source policy: {web_note}",
    ]
    if profile:
        lines.append("<business_profile>\n" + "\n".join(profile) + "\n</business_profile>")
    if hints.strip():
        lines.append(f"<operator_notes>\n{hints.strip()[:MAX_HINT_CHARS]}\n</operator_notes>")
    return "\n".join(lines)


async def generate_brief(
    runtime, *, name: str, description: str, scope: str, web_note: str, hints: str = "", ws_budget: float = 0.0,
    redact: bool = True, profile: list[str] | None = None,
) -> tuple[str, float]:
    settings = runtime.settings
    role = settings.models.planner
    run_id = new_run_id()
    message = brief_message(name, description, scope, web_note, hints, profile)
    if redact:
        message, _ = redact_pii(message)
    guard = BudgetGuard(runtime.ledger, settings, name, ws_budget, run_id, BRIEF_RUN_CAP_USD,
                        getattr(runtime, "pool", None))
    result = await runtime.llm.run(
        ctx=CallContext(name, run_id, "brief", "planner"),
        role=role,
        effort=role.clamp("low"),
        system=system_blocks(BRIEF_SYSTEM, settings.orchestrator.output_language),
        messages=[{"role": "user", "content": message}],
        guard=guard,
        max_tokens=BRIEF_MAX_TOKENS,
    )
    text = plain_text(result.blocks).strip()[:MAX_BRIEF_CHARS]
    runtime.audit.append(
        "workspace.brief_generated", workspace=name, run_id=run_id, cost=round(result.cost, 6),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), stop_reason=result.stop_reason,
    )
    return text, result.cost
