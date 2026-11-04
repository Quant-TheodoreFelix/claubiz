import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .audit import AuditLog
from .budget import BudgetGuard, Ledger, UsageRecord, cost_of, usage_from_message
from .config import DYNAMIC_WEB_MODELS, OUTPUT_TOKENS_BY_EFFORT, RoleConfig, Settings
from .events import EventBus
from .workspace import WebPolicy

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_INPUT_TOKENS_PER_SEARCH = 12000
OUTPUT_RESERVE_FACTOR = 2
ECHO_UNSAFE_BEFORE_FALLBACK = frozenset({"thinking", "redacted_thinking", "tool_use", "server_tool_use"})


class RefusalError(Exception):
    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"모델이 요청을 거절함 (분류: {category or '미상'})")
        self.category = category
        self.explanation = explanation


@dataclass(frozen=True)
class CallContext:
    workspace: str
    run_id: str
    task_id: str
    role: str


@dataclass
class AgentResult:
    blocks: list[Any]
    stop_reason: str
    cost: float
    usage: list[UsageRecord]
    served_model: str
    request_ids: list[str] = field(default_factory=list)


def web_tools(model: str, policy: WebPolicy, searches: int | None = None) -> list[dict]:
    dynamic = model in DYNAMIC_WEB_MODELS
    domain: dict[str, list[str]] = {}
    if policy.allowed_domains:
        domain = {"allowed_domains": list(policy.allowed_domains)}
    elif policy.blocked_domains:
        domain = {"blocked_domains": list(policy.blocked_domains)}
    tools: list[dict] = []
    max_searches = policy.max_searches_per_task if searches is None else searches
    if max_searches > 0:
        tools.append(
            {
                "type": "web_search_20260209" if dynamic else "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
                **domain,
            }
        )
    if policy.max_fetches_per_task > 0 and max_searches > 0:
        tools.append(
            {
                "type": "web_fetch_20260209" if dynamic else "web_fetch_20250910",
                "name": "web_fetch",
                "max_uses": policy.max_fetches_per_task,
                "max_content_tokens": policy.fetch_max_content_tokens,
                "citations": {"enabled": True},
                **domain,
            }
        )
    return tools


def echo_safe(content: list[Any]) -> list[Any]:
    last_fallback = max((i for i, b in enumerate(content) if getattr(b, "type", "") == "fallback"), default=-1)
    if last_fallback < 0:
        return content
    head = [b for b in content[:last_fallback] if getattr(b, "type", "") not in ECHO_UNSAFE_BEFORE_FALLBACK]
    return head + content[last_fallback:]


class LLM:
    def __init__(
        self, client: anthropic.AsyncAnthropic, settings: Settings, ledger: Ledger, bus: EventBus, audit: AuditLog
    ) -> None:
        self.client = client
        self.settings = settings
        self.ledger = ledger
        self.bus = bus
        self.audit = audit
        self._fallback_disabled: set[str] = set()

    def _kwargs(
        self, role: RoleConfig, effort: str, system: list[dict], messages: list[dict], tools: list[dict] | None,
        max_tokens: int, schema: dict | None,
    ) -> dict:
        output_config: dict[str, Any] = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        kwargs: dict[str, Any] = {
            "model": role.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if tools:
            kwargs["tools"] = tools
        if role.uses_fallbacks() and role.model not in self._fallback_disabled:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    async def estimate(self, kwargs: dict, tools: list[dict] | None) -> float:
        count_args = {k: v for k, v in kwargs.items() if k not in ("betas", "fallbacks", "max_tokens")}
        try:
            counted = await self.client.messages.count_tokens(**count_args)
            input_tokens = counted.input_tokens
        except anthropic.APIError:
            input_tokens = len(json.dumps([kwargs["system"], kwargs["messages"]], ensure_ascii=False, default=str)) // 2
        searches = 0
        fetch_tokens = 0
        for tool in tools or []:
            if tool.get("name") == "web_search":
                searches = int(tool.get("max_uses", 0))
            elif tool.get("name") == "web_fetch":
                fetch_tokens = int(tool.get("max_uses", 0)) * int(tool.get("max_content_tokens", 0))
        price = self.settings.price_for(kwargs["model"])
        prompt = input_tokens + searches * WEB_INPUT_TOKENS_PER_SEARCH + fetch_tokens
        effort = kwargs.get("output_config", {}).get("effort", "high")
        output = min(kwargs["max_tokens"], OUTPUT_TOKENS_BY_EFFORT.get(effort, 9000) * OUTPUT_RESERVE_FACTOR)
        return (
            prompt * price.input / 1_000_000
            + output * price.output / 1_000_000
            + searches * self.settings.web_search_usd_per_1k / 1000
        )

    async def _stream(self, kwargs: dict, ctx: CallContext, on_warm: Callable[[], None] | None) -> Any:
        warmed = False
        async with self.client.beta.messages.stream(**kwargs) as stream:
            async for event in stream:
                etype = getattr(event, "type", "")
                if not warmed and etype in ("content_block_start", "content_block_delta"):
                    warmed = True
                    if on_warm is not None:
                        on_warm()
                if etype == "content_block_stop":
                    block = getattr(event, "content_block", None)
                    btype = getattr(block, "type", "")
                    if btype == "server_tool_use":
                        tool_input = getattr(block, "input", {}) or {}
                        if block.name == "code_execution":
                            detail = "검색 결과 필터링"
                        else:
                            detail = tool_input.get("query") or tool_input.get("url") or ""
                        self.bus.emit("task.tool", ctx.run_id, ctx.task_id, tool=block.name, detail=str(detail)[:200])
                    elif btype == "fallback":
                        self.bus.emit("task.fallback", ctx.run_id, ctx.task_id)
            message = await stream.get_final_message()
        if not warmed and on_warm is not None:
            on_warm()
        return message

    async def run(
        self,
        *,
        ctx: CallContext,
        role: RoleConfig,
        effort: str,
        system: list[dict],
        messages: list[dict],
        guard: BudgetGuard,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        schema: dict | None = None,
        on_warm: Callable[[], None] | None = None,
    ) -> AgentResult:
        convo = list(messages)
        blocks: list[Any] = []
        records: list[UsageRecord] = []
        request_ids: list[str] = []
        total = 0.0
        served = role.model
        limit = max_tokens or role.max_tokens
        continuations = self.settings.orchestrator.max_pause_continuations
        for turn in range(continuations + 1):
            kwargs = self._kwargs(role, effort, system, convo, tools, limit, schema)
            estimate = await self.estimate(kwargs, tools)
            reservation = await guard.reserve(estimate, f"{ctx.role}:{ctx.task_id}")
            try:
                try:
                    message = await self._stream(kwargs, ctx, on_warm if turn == 0 else None)
                except anthropic.BadRequestError as exc:
                    if "fallbacks" in kwargs and "fallback" in str(exc.message).lower():
                        self._fallback_disabled.add(role.model)
                        self.audit.append("llm.fallback_disabled", model=role.model, reason=str(exc.message)[:300])
                        kwargs = self._kwargs(role, effort, system, convo, tools, limit, schema)
                        message = await self._stream(kwargs, ctx, on_warm if turn == 0 else None)
                    else:
                        raise
                request_id = getattr(message, "_request_id", None) or ""
                recs = usage_from_message(message)
                cost = 0.0
                for rec in recs:
                    rec_cost = cost_of(rec, self.settings)
                    cost += rec_cost
                    self.ledger.record(
                        workspace=ctx.workspace, run_id=ctx.run_id, task_id=ctx.task_id, role=ctx.role, effort=effort,
                        rec=rec, cost=rec_cost, request_id=request_id,
                    )
            finally:
                await guard.release(reservation)
            records.extend(recs)
            request_ids.append(request_id)
            total += cost
            served = message.model
            self.bus.emit(
                "llm.usage", ctx.run_id, ctx.task_id, role=ctx.role, model=served, cost=cost,
                input_tokens=sum(r.input_tokens for r in recs), output_tokens=sum(r.output_tokens for r in recs),
                cache_read=sum(r.cache_read for r in recs),
                cache_write=sum(r.cache_write_5m + r.cache_write_1h for r in recs),
                web_searches=sum(r.web_searches for r in recs),
            )
            if guard.soft_cap_crossed():
                snap = guard.snapshot()
                self.bus.emit("budget.soft_cap", ctx.run_id, None, spent=snap["cycle_spent"], soft=snap["soft_cap"])
                self.audit.append("budget.soft_cap", spent=snap["cycle_spent"], soft=snap["soft_cap"])
            content = list(message.content)
            blocks.extend(content)
            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                raise RefusalError(getattr(details, "category", None), getattr(details, "explanation", None))
            if message.stop_reason == "pause_turn" and turn < continuations:
                convo.append({"role": "assistant", "content": echo_safe(content)})
                continue
            return AgentResult(blocks, message.stop_reason or "", total, records, served, request_ids)
        return AgentResult(blocks, "pause_limit", total, records, served, request_ids)
