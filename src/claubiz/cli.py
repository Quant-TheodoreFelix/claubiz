import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, eventfmt, hostos, i18n
from .actions import TYPE_LABELS, ActionError, ActionStore
from .agent import AgentError
from .audit import AuditLog
from .budget import BudgetExceeded, Ledger, cycle_start
from .config import MODEL_CHOICES, ConfigError, load_settings, model_label, write_default_config
from .digest import DEFAULT_DIGEST_CAP, DEFAULT_DIGEST_MODEL, DigestError, DigestStore, make_spec
from .events import Event
from .i18n import tr
from .knowledge import KnowledgeStore
from .orchestrator import RunError, RunSession
from .paths import DEFAULT_DIR, HOME_ENV, UnsafePathError, home, read_bytes_nofollow
from .planner import PlanError
from .playbooks import all_playbooks, load_custom_playbooks
from .policy import SCOPES, PolicyError
from .schedule import ScheduleError
from .secrets import (
    KeychainStore,
    SecretError,
    backend_name,
    install_log_redaction,
    scrub_credential_env,
    secure_unlink,
    validate_key,
)
from .transport import build_client
from .workspace import (
    ACTION_TYPES,
    BUSINESS_TYPES,
    PROFILE_FIELDS,
    BusinessProfile,
    WebPolicy,
    Workspace,
    WorkspaceConfig,
    WorkspaceError,
)

console = Console(highlight=False)


def _store():
    settings = load_settings()
    return settings, KeychainStore(settings.api.keychain_service, settings.api.keychain_account)


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


async def _list_models(settings, key) -> list[str]:
    client = build_client(settings, key)
    try:
        page = await client.models.list(limit=100)
        return [m.id for m in page.data]
    finally:
        await client.close()


async def _list_workspaces(settings, key) -> list[tuple[str, str]]:
    client = build_client(settings, key)
    try:
        items = []
        async for w in client.beta.organization.workspaces.list(include_default=True, limit=100):
            items.append((w.id, getattr(w, "name", "")))
        return items
    finally:
        await client.close()


def cmd_init(args) -> int:
    home()
    path = write_default_config(overwrite=args.force, workspace_id=args.workspace_id or "")
    load_settings()
    console.print(tr("설정 파일: {path}", path=path))
    access = tr("현재 사용자 전용") if os.name == "nt" else tr("권한 700")
    console.print(tr("데이터 폴더: {path} ({access})", path=home(), access=access))
    return 0


def cmd_auth_set(args) -> int:
    _, store = _store()
    value = validate_key(getpass.getpass(tr("API 키 입력(화면에 표시되지 않음): ")))
    store.set(value)
    AuditLog().append("auth.key_set", fingerprint=value.fingerprint(), backend=backend_name())
    console.print(tr("키체인 저장 완료 (지문 {fingerprint})", fingerprint=value.fingerprint()))
    return 0


def cmd_auth_import(args) -> int:
    settings, store = _store()
    path = Path(args.path).expanduser()
    value = store.import_file(path)
    AuditLog().append("auth.key_imported", fingerprint=value.fingerprint(), backend=backend_name())
    console.print(tr("키체인 저장 및 재확인 완료 (지문 {fingerprint})", fingerprint=value.fingerprint()))
    if not args.skip_check:
        models = asyncio.run(_list_models(settings, store.require()))
        console.print(tr("API 확인 완료: 사용 가능 모델 {count}개", count=len(models)))
    if args.keep:
        console.print(tr("[yellow]원본 파일을 유지함, 평문 보관은 권장하지 않음[/]"))
    else:
        secure_unlink(path)
        AuditLog().append("auth.plaintext_removed", path=str(path))
        console.print(tr("평문 원본 삭제 완료: {path}", path=path))
    return 0


def cmd_auth_status(args) -> int:
    settings, store = _store()
    console.print(tr("키링 백엔드: {backend}", backend=backend_name()))
    value = store.get()
    if value is None:
        console.print(tr("[red]키체인에 API 키 없음[/]"))
        return 1
    console.print(tr("API 키: 등록됨 (지문 {fingerprint})", fingerprint=value.fingerprint()))
    console.print(tr("워크스페이스 ID: {workspace_id}", workspace_id=settings.api.workspace_id or tr("(미설정)")))
    if args.check:
        models = asyncio.run(_list_models(settings, value))
        console.print(tr("API 확인 완료: 사용 가능 모델 {count}개", count=len(models)))
    return 0


def cmd_auth_workspaces(args) -> int:
    settings, store = _store()
    items = asyncio.run(_list_workspaces(settings, store.require()))
    table = Table("ID", tr("이름"))
    for wid, name in items:
        table.add_row(wid, name)
    console.print(table)
    return 0


def cmd_auth_remove(args) -> int:
    _, store = _store()
    if not args.yes and not _confirm(tr("키체인에서 API 키를 삭제할까?")):
        return 1
    removed = store.delete()
    AuditLog().append("auth.key_removed", removed=removed)
    console.print(tr("삭제 완료") if removed else tr("삭제할 키가 없음"))
    return 0


def _profile_from(args, base: BusinessProfile | None = None) -> BusinessProfile:
    data = (base or BusinessProfile()).model_dump()
    for name in ("business_type", *dict(PROFILE_FIELDS)):
        value = getattr(args, name, None)
        if value is not None:
            data[name] = value
    return BusinessProfile.model_validate(data)


def _action_types(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(a.strip() for a in raw.split(",") if a.strip() and a.strip() != "none")


def cmd_ws_create(args) -> int:
    web = WebPolicy(
        allowed_domains=tuple(args.allow_domain or ()),
        blocked_domains=tuple(args.block_domain or ()),
        max_searches_per_task=args.max_searches,
        max_fetches_per_task=args.max_fetches,
    )
    cfg = WorkspaceConfig(
        name=args.name,
        description=args.desc or "",
        scope=args.scope,
        max_send_classification=args.max_class,
        redact_pii=not args.no_redact,
        budget_monthly_usd=args.budget,
        knowledge=not args.no_knowledge,
        actions=_action_types(args.actions, ACTION_TYPES),
        web=web,
        profile=_profile_from(args),
    )
    ws = Workspace.create(cfg)
    AuditLog().append("workspace.created", name=ws.name, scope=cfg.scope, web=web.model_dump())
    console.print(tr("작업 영역 생성: {path}", path=ws.root))
    return 0


def cmd_ws_list(args) -> int:
    table = Table(tr("이름"), tr("활동 범위"), tr("입력 자료"), tr("최근 실행"), tr("설명"))
    for name in Workspace.list_names():
        ws = Workspace.open(name)
        runs = ws.recent_runs(limit=1)
        table.add_row(
            name, ws.config.scope, str(len(ws.inputs())), runs[0]["created"] if runs else "-", ws.config.description
        )
    console.print(table)
    return 0


def cmd_ws_show(args) -> int:
    ws = Workspace.open(args.name)
    c = ws.config
    console.print(f"[bold]{c.name}[/] {c.description}")
    console.print(tr("경로: {path}", path=ws.root))
    console.print(tr("활동 범위: {scope} | 전송 허용 등급: {classification} 이하 | 개인정보 가림: {redact}",
                     scope=c.scope, classification=c.max_send_classification, redact=c.redact_pii))
    budget = "$" + str(c.budget_monthly_usd) if c.budget_monthly_usd else tr("전체 예산 공유")
    console.print(tr("작업 영역 월 예산: {budget}", budget=budget))
    domains = c.web.allowed_domains or c.web.blocked_domains
    if c.web.allowed_domains:
        mode = tr("허용 목록")
    else:
        mode = tr("차단 목록") if c.web.blocked_domains else tr("제한 없음")
    console.print(tr("웹 출처 정책: {mode} {domains}", mode=mode, domains=", ".join(domains)))
    console.print(tr("작업당 검색/페치 상한: {searches}/{fetches}", searches=c.web.max_searches_per_task,
                     fetches=c.web.max_fetches_per_task))
    console.print(tr("지침(brief.md): 있음") if ws.has_brief() else tr("지침(brief.md): 없음"))
    table = Table(tr("입력 파일"), tr("등급"), tr("글자 수"), tr("가져온 시각"))
    for f in ws.inputs():
        table.add_row(f.name, f.classification, str(f.chars), f.imported_at)
    console.print(table)
    runs = Table(tr("실행"), tr("상태"), tr("플레이북"), tr("비용"), tr("목표"))
    for r in ws.recent_runs(limit=10):
        runs.add_row(r["run_id"], r["status"], r.get("playbook") or "-", f"${r.get('cost_total', 0):.4f}",
                     escape(r["goal"][:50]))
    console.print(runs)
    return 0


def cmd_ws_profile(args) -> int:
    ws = Workspace.open(args.name)
    updates: dict = {"profile": _profile_from(args, ws.config.profile)}
    if args.knowledge is not None:
        updates["knowledge"] = args.knowledge == "on"
    if args.actions is not None:
        updates["actions"] = _action_types(args.actions, ws.config.actions)
    config = WorkspaceConfig.model_validate({**ws.config.model_dump(), **updates})
    ws.save_config(config)
    AuditLog().append("workspace.profile_set", workspace=ws.name, business_type=config.profile.business_type,
                      knowledge=config.knowledge, actions=list(config.actions))
    lines = config.profile.lines() or [tr("(업무 프로필 비어 있음)")]
    for line in lines:
        console.print(escape(line))
    console.print(tr("누적 지식: {knowledge} | 행동 제안: {actions}",
                     knowledge=tr("켜짐") if config.knowledge else tr("꺼짐"),
                     actions=", ".join(TYPE_LABELS[a] for a in config.actions) or tr("꺼짐")))
    return 0


def cmd_ws_import(args) -> int:
    ws = Workspace.open(args.name)
    audit = AuditLog()
    for raw in args.files:
        entry = ws.import_file(Path(raw), args.cls)
        audit.append("input.imported", workspace=ws.name, name=entry.name, classification=entry.classification,
                     sha256=entry.sha256)
        console.print(tr("가져옴: {name} ({classification}, {chars}자)", name=entry.name,
                         classification=entry.classification, chars=entry.chars))
    return 0


def cmd_ws_brief(args) -> int:
    ws = Workspace.open(args.name)
    text = read_bytes_nofollow(Path(args.file).expanduser(), 80 * 1024).decode("utf-8")
    chars = ws.write_brief(text)
    AuditLog().append("workspace.brief_set", workspace=ws.name, chars=chars)
    console.print(tr("지침 저장: {path} ({chars}자)", path=ws.root / "brief.md", chars=chars) if chars
                  else tr("지침 삭제됨 (빈 파일)"))
    return 0


def cmd_digest_list(args) -> int:
    ws = Workspace.open(args.workspace)
    table = Table("ID", tr("이름"), tr("일정"), tr("다음 실행"), tr("모델"), tr("상한"), tr("상태"), tr("최근 결과"))
    for spec in DigestStore(ws).load():
        table.add_row(
            spec.id, escape(spec.title), spec.schedule, spec.next_due().strftime("%m-%d %H:%M"),
            model_label(spec.model) if spec.model else tr("기본"), f"${spec.run_cap:.2f}",
            tr("켜짐") if spec.enabled else tr("꺼짐"), spec.last_status or "-",
        )
    console.print(table)
    return 0


def cmd_digest_add(args) -> int:
    ws = Workspace.open(args.workspace)
    spec = make_spec(ws, title=args.title, request=args.request, schedule=args.schedule, model=args.model,
                     max_searches=args.searches, run_cap=args.cap)
    DigestStore(ws).add(spec)
    AuditLog().append("digest.created", workspace=ws.name, digest_id=spec.id, schedule=spec.schedule,
                      model=spec.model or "config", run_cap=spec.run_cap)
    console.print(tr("정기 정리 추가: {id} | {schedule} | 다음 실행 {next_run}", id=spec.id, schedule=spec.schedule,
                     next_run=f"{spec.next_due():%Y-%m-%d %H:%M}"))
    return 0


def cmd_digest_remove(args) -> int:
    ws = Workspace.open(args.workspace)
    if not DigestStore(ws).remove(args.id):
        raise DigestError(tr("정기 정리를 찾을 수 없음"))
    AuditLog().append("digest.removed", workspace=ws.name, digest_id=args.id)
    console.print(tr("제거 완료"))
    return 0


def cmd_digest_toggle(args) -> int:
    ws = Workspace.open(args.workspace)
    enabled = args.digest_cmd == "enable"
    changes = {"enabled": enabled, "failures": 0} if enabled else {"enabled": False}
    spec = DigestStore(ws).update(args.id, **changes)
    AuditLog().append("digest.updated", workspace=ws.name, digest_id=spec.id, enabled=enabled)
    state = tr("켜짐") if enabled else tr("꺼짐")
    console.print(f"{spec.title}: {state}")
    return 0


async def _digest_async(args) -> int:
    from .digest import all_due, run_claimed, run_due
    from .runtime import Runtime

    now = datetime.now()
    if args.digest_cmd == "run-due" and not all_due(now):
        console.print(tr("[dim]{time} 실행할 정기 정리 없음[/]", time=f"{now:%Y-%m-%d %H:%M}"))
        return 0
    runtime = Runtime.create()
    try:
        if args.verbose:
            runtime.bus.listen(_print_event)
        if args.digest_cmd == "run-due":
            results = await run_due(runtime, now)
            for name, digest_id, status in results:
                console.print(f"{now:%Y-%m-%d %H:%M} {name}/{digest_id}: {escape(status)}")
            return 0 if all(not s.startswith("failed") for _, _, s in results) else 1
        ws = Workspace.open(args.workspace)
        store = DigestStore(ws)
        spec = store.get(args.id)
        if spec is None:
            raise DigestError(tr("정기 정리를 찾을 수 없음"))
        claim = store.claim(spec.id, now, force=True)
        if claim is None:
            raise DigestError(tr("이 정기 정리는 다른 곳에서 실행 중임"))
        outcome = await run_claimed(runtime, ws, spec, claim)
        console.print(tr("[bold]결과: {status}[/] | 비용 ${cost:.4f} | 지식 {learned}개 추가 | 행동 제안 {proposed}건",
                         status=outcome.status, cost=outcome.cost, learned=outcome.learned,
                         proposed=len(outcome.proposed)))
        if outcome.report_path:
            console.print(tr("보고서: {path}", path=outcome.report_path))
        return 0 if outcome.status in ("completed", "partial") else 1
    finally:
        await runtime.aclose()


def cmd_digest_run(args) -> int:
    return asyncio.run(_digest_async(args))


def cmd_agent(args) -> int:
    from . import agent

    windows = sys.platform == "win32"
    if args.agent_cmd == "install":
        if windows:
            question = tr("작업 스케줄러에 {task} 작업을 등록해 {interval}분마다 정기 정리를 확인할까?",
                          task=agent.TASK_NAME, interval=args.interval)
        else:
            question = tr("~/Library/LaunchAgents에 {label}을 등록해 {interval}분마다 정기 정리를 확인할까?",
                          label=agent.LABEL, interval=args.interval)
        if not args.yes and not _confirm(question):
            return 1
        path = agent.install(args.interval)
        AuditLog().append("agent.installed", path=str(path), interval=args.interval)
        console.print(tr("등록 완료: {path}", path=path))
        console.print(tr("로그: {path}", path=agent.log_path()))
        if not windows:
            console.print(tr("[yellow]처음 실행될 때 macOS가 키체인 접근 허용을 물을 수 있음[/]"))
        return 0
    if args.agent_cmd == "uninstall":
        removed = agent.uninstall()
        AuditLog().append("agent.removed", removed=removed)
        console.print(tr("등록 해제 완료") if removed else tr("등록된 항목이 없음"))
        return 0
    installed, loaded = agent.status()
    path = agent.registration_path()
    console.print(tr("등록 파일: 있음 ({path})", path=path) if installed else tr("등록 파일: 없음 ({path})", path=path))
    if windows:
        console.print(tr("작업 스케줄러 등록: 됨") if loaded else tr("작업 스케줄러 등록: 안 됨"))
    else:
        console.print(tr("launchd 등록: 됨") if loaded else tr("launchd 등록: 안 됨"))
    return 0


def cmd_actions(args) -> int:
    ws = Workspace.open(args.workspace)
    store = ActionStore(ws)
    audit = AuditLog()
    if args.actions_cmd == "list":
        table = Table("ID", tr("생성"), tr("유형"), tr("제목"), tr("상태"), tr("파일"))
        for action in store.list():
            table.add_row(action.id, action.created[:16].replace("T", " "), TYPE_LABELS.get(action.type, action.type),
                          escape(action.title), action.status, escape(action.output) or "-")
        console.print(table)
        return 0
    if args.actions_cmd == "approve":
        action = store.get(args.id)
        if action is None:
            raise ActionError(tr("제안을 찾을 수 없음"))
        if (action.classification == "confidential" and not args.yes
                and not _confirm(tr("기밀 등급 제안임. 파일을 만들까?"))):
            return 1
        done = store.approve(args.id)
        audit.append("action.approved", workspace=ws.name, action_id=done.id, type=done.type, output=done.output,
                     sha256=done.sha256, via="cli")
        console.print(tr("만듦: {path}", path=store.output_path(done)))
        return 0
    done = store.reject(args.id)
    audit.append("action.rejected", workspace=ws.name, action_id=done.id, via="cli")
    console.print(tr("거절함"))
    return 0


def cmd_knowledge(args) -> int:
    ws = Workspace.open(args.workspace)
    store = KnowledgeStore(ws)
    if args.knowledge_cmd == "remove":
        removed = store.remove(args.ids)
        AuditLog().append("knowledge.removed", workspace=ws.name, ids=args.ids, removed=removed, via="cli")
        console.print(tr("{count}개 제거", count=removed))
        return 0
    table = Table("ID", tr("날짜"), tr("주제"), tr("내용"), tr("출처"))
    for fact in sorted(store.facts(), key=lambda f: f.as_of, reverse=True):
        table.add_row(fact.id, fact.as_of, escape(fact.topic), escape(fact.text), escape(" ".join(fact.sources[:2])))
    console.print(table)
    return 0


def cmd_playbooks(args) -> int:
    table = Table("ID", tr("이름"), tr("종류"), tr("웹"), tr("입력 자료"), tr("설명"))
    for p in all_playbooks().values():
        table.add_row(p.id, escape(p.title), tr("기본") if p.builtin else tr("사용자"),
                      tr("필요") if p.needs_web else "-", tr("필요") if p.needs_inputs else "-",
                      escape(p.description))
    console.print(table)
    _, errors = load_custom_playbooks()
    for error in errors:
        console.print(tr("[yellow]무시한 플레이북 파일 {error}[/]", error=escape(error)))
    return 0


def _plan_table(session: RunSession) -> Table:
    table = Table("ID", tr("작업"), "effort", tr("웹"), tr("입력"), tr("선행"), tr("예상 비용"),
                  title=escape(session.plan.title))
    for t in session.plan.tasks:
        table.add_row(
            t.id,
            escape(t.title) + (tr(" [dim](재사용 {ref})[/]", ref=escape(t.reuse_ref)) if t.reuse_ref else ""),
            t.effort,
            tr("{count}회", count=t.max_searches) if t.use_web else "-",
            escape(", ".join(t.inputs)) or "-",
            ", ".join(t.depends_on) or "-",
            f"${session.estimate['tasks'][t.id]:.3f}",
        )
    return table


def _print_event(event: Event) -> None:
    text = eventfmt.line(event)
    if text:
        console.print(text)


def _print_outcome(outcome) -> None:
    console.print(tr("[bold]결과: {status}[/] | 총 비용 ${cost:.4f}", status=outcome.status, cost=outcome.cost))
    if outcome.report_path:
        console.print(tr("보고서: {path}", path=outcome.report_path))
    if outcome.learned:
        console.print(tr("지식 {count}개 추가 (`claubiz knowledge list`)", count=outcome.learned))
    if outcome.proposed:
        console.print(tr("행동 제안 {count}건 (`claubiz actions list`로 확인 후 approve)",
                         count=len(outcome.proposed)))


async def _run_async(args) -> int:
    from .runtime import Runtime

    runtime = Runtime.create()
    try:
        ws = Workspace.open(args.workspace)
        orch = runtime.orchestrator(ws)
        runtime.bus.listen(_print_event)
        if args.model:
            orch.set_model(args.model)
        if args.resume:
            session = orch.resume(args.resume, args.allow_confidential)
            done = len(session.prior_states)
            console.print(tr("재개: 완료된 작업 {done}개는 그대로 쓰고 {rerun}개를 다시 실행 | 남은 예상 비용 약 ${estimate:.3f}"
                             " | 실행 상한 ${cap:.2f} (이미 사용 ${spent:.2f})",
                             done=done, rerun=len(session.plan.tasks) - done,
                             estimate=session.estimate["total"],
                             cap=session.run_cap, spent=runtime.ledger.spent_run(session.run_id)))
            for note in session.plan_notes:
                console.print(tr("[yellow]조정: {note}[/]", note=escape(note)))
            if not args.yes and not _confirm(tr("이 실행을 재개할까?")):
                orch.cancel_planned(session)
                return 1
            outcome = await orch.execute(session)
            _print_outcome(outcome)
            return 0 if outcome.status in ("completed", "partial") else 1
        goal = args.goal
        if not goal:
            console.print(tr("목표를 입력하고 빈 줄에서 Enter (Ctrl+D 종료)"))
            goal = sys.stdin.read()
        if args.direct:
            session = orch.direct(goal, args.playbook, args.budget, args.allow_confidential, args.base)
            console.print(tr("바로 실행: 에이전트 1개, 예상 비용 약 ${estimate:.3f} | 실행 상한 ${cap:.2f}",
                             estimate=session.estimate["total"], cap=session.run_cap))
            for note in session.plan_notes:
                console.print(tr("[yellow]조정: {note}[/]", note=escape(note)))
            outcome = await orch.execute(session)
            _print_outcome(outcome)
            return 0 if outcome.status in ("completed", "partial") else 1
        console.print(tr("계획 수립 중..."))
        session = await orch.plan(goal, args.playbook, args.budget, args.allow_confidential, args.base)
        console.print(_plan_table(session))
        console.print(tr("접근 방식: {approach}", approach=escape(session.plan.approach)))
        for note in session.plan_notes:
            console.print(tr("[yellow]조정: {note}[/]", note=escape(note)))
        snap = orch._guard(session.run_id, session.run_cap).snapshot()
        console.print(tr(
            "계획 비용 ${plan_cost:.4f} | 예상 실행 비용 약 ${estimate:.3f} (종합 ${synth:.3f}, effort {effort} 포함)"
            " | 실행 상한 ${cap:.2f} | 이번 주기 사용 ${spent:.2f} / ${hard:.2f}",
            plan_cost=session.plan_cost, estimate=session.estimate["total"],
            synth=session.estimate["synthesizer"], effort=session.plan.synth_effort, cap=session.run_cap,
            spent=snap["cycle_spent"], hard=snap["hard_cap"],
        ))
        if args.plan_only:
            orch.cancel_planned(session)
            return 0
        if not args.yes and not _confirm(tr("이 계획으로 실행할까?")):
            orch.cancel_planned(session)
            console.print(tr("실행 취소"))
            return 1
        outcome = await orch.execute(session)
        for w in outcome.warnings:
            console.print(f"[yellow]{escape(w)}[/]")
        _print_outcome(outcome)
        return 0 if outcome.status in ("completed", "partial") else 1
    finally:
        await runtime.aclose()


def cmd_run(args) -> int:
    return asyncio.run(_run_async(args))


def cmd_budget(args) -> int:
    settings = load_settings()
    ledger = Ledger()
    start = cycle_start(datetime.now(), settings.budget.cycle_anchor_day)
    spent = ledger.spent_since(start)
    b = settings.budget
    console.print(tr("결제 주기 시작: {start} (기준일 {anchor}일)", start=start.date(), anchor=b.cycle_anchor_day))
    console.print(tr("사용: ${spent:.4f} | 경고 기준 ${soft:.2f} | 상한 ${hard:.2f}", spent=spent,
                     soft=b.monthly_soft_usd, hard=b.monthly_hard_usd))
    table = Table(tr("작업 영역"), tr("모델"), tr("호출"), tr("입력"), tr("출력"), tr("캐시 읽기"), tr("캐시 쓰기"),
                  tr("검색"), tr("비용"))
    for ws, model, calls, inp, out, cr, cw, web, cost in ledger.summary_since(start):
        table.add_row(ws, model, str(calls), str(inp), str(out), str(cr), str(cw), str(web), f"${cost:.4f}")
    console.print(table)
    console.print(tr("[dim]로컬 장부 기준 추정치임, 실제 청구는 Console의 Cost 페이지에서 확인[/]"))
    ledger.close()
    return 0


def cmd_audit_verify(args) -> int:
    ok, count, message = AuditLog().verify()
    console.print(tr("감사 로그 {count}건: {message}", count=count, message=message))
    return 0 if ok else 2


def cmd_tui(args) -> int:
    from .tui import ClaubizApp

    ClaubizApp(initial_workspace=args.workspace).run()
    return 0


def _profile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--type", dest="business_type", choices=["", *dict(BUSINESS_TYPES)], help=tr("비즈니스 유형"))
    labels = {"industry": tr("업종·세부 분야"), "offering": tr("주요 제품·서비스"), "customers": tr("주요 고객"),
              "competitors": tr("경쟁사·비교 대상"), "markets": tr("관심 지역·시장"), "audience": tr("보고서 독자"),
              "focus": tr("반복 업무·관심 이슈")}
    for name, _ in PROFILE_FIELDS:
        p.add_argument(f"--{name}", help=labels[name])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claubiz",
        description=tr("Claude API 기반 비즈니스 자동화 멀티 에이전트 (하위 명령 없이 실행하면 TUI가 열림)"),
    )
    parser.add_argument("--version", action="version", version=f"claubiz {__version__}")
    parser.add_argument("--home", help=tr("데이터 폴더 (기본 ~/{folder}, 환경 변수 {env}로도 지정)", folder=DEFAULT_DIR,
                                          env=HOME_ENV))
    parser.add_argument("--lang", choices=i18n.SUPPORTED, help=tr("화면 언어 (ko, en, 환경 변수 CLAUBIZ_LANG로도 지정)"))
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help=tr("데이터 폴더와 설정 파일 생성"))
    p.add_argument("--workspace-id", help=tr("Console 워크스페이스 ID (조직 키 사용 시 필요)"))
    p.add_argument("--force", action="store_true", help=tr("기존 설정 덮어쓰기"))
    p.set_defaults(func=cmd_init)

    auth = sub.add_parser("auth", help=tr("API 키 관리 (macOS 키체인)"))
    auth = auth.add_subparsers(dest="auth_cmd", required=True)
    p = auth.add_parser("set", help=tr("키를 직접 입력해 저장"))
    p.set_defaults(func=cmd_auth_set)
    p = auth.add_parser("import-file", help=tr("평문 키 파일을 키체인으로 옮기고 원본 삭제"))
    p.add_argument("path")
    p.add_argument("--keep", action="store_true", help=tr("원본 파일 유지"))
    p.add_argument("--skip-check", action="store_true", help=tr("API 확인 호출 생략"))
    p.set_defaults(func=cmd_auth_import)
    p = auth.add_parser("status", help=tr("키 등록 상태 확인"))
    p.add_argument("--check", action="store_true", help=tr("무료 API 호출로 키 동작 확인"))
    p.set_defaults(func=cmd_auth_status)
    p = auth.add_parser("workspaces", help=tr("Console 워크스페이스 목록"))
    p.set_defaults(func=cmd_auth_workspaces)
    p = auth.add_parser("remove", help=tr("키체인에서 키 삭제"))
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_auth_remove)

    ws = sub.add_parser("ws", help=tr("작업 영역 관리")).add_subparsers(dest="ws_cmd", required=True)
    p = ws.add_parser("create", help=tr("작업 영역 생성"))
    p.add_argument("name")
    p.add_argument("--desc", default="")
    p.add_argument("--scope", choices=SCOPES, default="research")
    p.add_argument("--max-class", choices=("public", "internal"), default="internal", help=tr("기본 전송 허용 등급"))
    p.add_argument("--allow-domain", action="append", help=tr("웹 출처 허용 도메인 (반복 가능)"))
    p.add_argument("--block-domain", action="append", help=tr("웹 출처 차단 도메인 (반복 가능)"))
    p.add_argument("--max-searches", type=int, default=5, help=tr("작업당 최대 웹 검색 수"))
    p.add_argument("--max-fetches", type=int, default=3, help=tr("작업당 최대 웹 페이지 페치 수"))
    p.add_argument("--budget", type=float, default=0.0, help=tr("작업 영역 월 예산 (0이면 전체 예산 공유)"))
    p.add_argument("--no-redact", action="store_true", help=tr("개인정보 자동 가림 끄기"))
    p.add_argument("--no-knowledge", action="store_true", help=tr("실행 결과를 누적 지식으로 쌓지 않음"))
    p.add_argument("--actions", help=tr("행동 제안 유형 (쉼표 구분, 기본 {types}, none이면 끔)",
                                        types=",".join(ACTION_TYPES)))
    _profile_args(p)
    p.set_defaults(func=cmd_ws_create)
    p = ws.add_parser("profile", help=tr("업무 프로필과 자동화 설정 변경 (준 항목만 바뀜)"))
    p.add_argument("name")
    p.add_argument("--knowledge", choices=("on", "off"))
    p.add_argument("--actions", help=tr("행동 제안 유형 (쉼표 구분: {types}, none이면 끔)", types=",".join(ACTION_TYPES)))
    _profile_args(p)
    p.set_defaults(func=cmd_ws_profile)
    p = ws.add_parser("list", help=tr("작업 영역 목록"))
    p.set_defaults(func=cmd_ws_list)
    p = ws.add_parser("show", help=tr("작업 영역 상세"))
    p.add_argument("name")
    p.set_defaults(func=cmd_ws_show)
    p = ws.add_parser("import", help=tr("로컬 자료 가져오기"))
    p.add_argument("name")
    p.add_argument("files", nargs="+")
    p.add_argument("--class", dest="cls", choices=("public", "internal", "confidential"), default="internal")
    p.set_defaults(func=cmd_ws_import)
    p = ws.add_parser("brief", help=tr("작업 영역 지침(회사 소개, 용어, 작성 방식 등) 설정"))
    p.add_argument("name")
    p.add_argument("file")
    p.set_defaults(func=cmd_ws_brief)

    p = sub.add_parser("playbooks", help=tr("플레이북 목록"))
    p.set_defaults(func=cmd_playbooks)

    digest = sub.add_parser("digest", help=tr("정기 이슈 정리")).add_subparsers(dest="digest_cmd", required=True)
    p = digest.add_parser("list", help=tr("정기 정리 목록"))
    p.add_argument("-w", "--workspace", required=True)
    p.set_defaults(func=cmd_digest_list)
    p = digest.add_parser("add", help=tr("정기 정리 추가"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--request", required=True, help=tr("무엇을 찾아 어떻게 정리할지"))
    p.add_argument("--schedule", required=True, help=tr("예: 매일 09:00, 평일 08:30, 월,목 10:00, 6시간마다"))
    p.add_argument("--model", default=DEFAULT_DIGEST_MODEL, choices=["", *dict(MODEL_CHOICES)],
                   help=tr("빈 문자열이면 설정 기본 모델"))
    p.add_argument("--searches", type=int, default=4, help=tr("실행마다 최대 웹 검색 수"))
    p.add_argument("--cap", type=float, default=DEFAULT_DIGEST_CAP, help=tr("실행마다 예산 상한 (USD)"))
    p.set_defaults(func=cmd_digest_add)
    p = digest.add_parser("remove", help=tr("정기 정리 제거"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("id")
    p.set_defaults(func=cmd_digest_remove)
    for name, text in (("enable", tr("정기 정리 켜기")), ("disable", tr("정기 정리 끄기"))):
        p = digest.add_parser(name, help=text)
        p.add_argument("-w", "--workspace", required=True)
        p.add_argument("id")
        p.set_defaults(func=cmd_digest_toggle)
    p = digest.add_parser("run", help=tr("정기 정리를 지금 실행"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("id")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_digest_run)
    p = digest.add_parser("run-due", help=tr("모든 작업 영역에서 때가 된 정기 정리 실행 (launchd, cron용)"))
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_digest_run)

    agent_parser = sub.add_parser("agent", help=tr("정기 정리 백그라운드 실행 (macOS launchd, Windows 작업 스케줄러)"))
    agent_cmds = agent_parser.add_subparsers(dest="agent_cmd", required=True)
    p = agent_cmds.add_parser("install", help=tr("백그라운드 실행 등록"))
    p.add_argument("--interval", type=int, default=15, help=tr("확인 간격 (분, 5~240)"))
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_agent)
    p = agent_cmds.add_parser("uninstall", help=tr("백그라운드 실행 해제"))
    p.set_defaults(func=cmd_agent)
    p = agent_cmds.add_parser("status", help=tr("등록 상태"))
    p.set_defaults(func=cmd_agent)

    actions = sub.add_parser("actions", help=tr("승인형 행동 제안"))
    actions = actions.add_subparsers(dest="actions_cmd", required=True)
    p = actions.add_parser("list", help=tr("제안 목록"))
    p.add_argument("-w", "--workspace", required=True)
    p.set_defaults(func=cmd_actions)
    p = actions.add_parser("approve", help=tr("제안 승인 (작업 영역 outbox에 파일 생성)"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_actions)
    p = actions.add_parser("reject", help=tr("제안 거절"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("id")
    p.set_defaults(func=cmd_actions)

    knowledge = sub.add_parser("knowledge", help=tr("누적 지식"))
    knowledge = knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    p = knowledge.add_parser("list", help=tr("지식 목록"))
    p.add_argument("-w", "--workspace", required=True)
    p.set_defaults(func=cmd_knowledge)
    p = knowledge.add_parser("remove", help=tr("지식 제거"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_knowledge)

    p = sub.add_parser("run", help=tr("목표 실행 (계획 -> 승인 -> 병렬 실행 -> 종합)"))
    p.add_argument("-w", "--workspace", required=True)
    p.add_argument("-p", "--playbook", help=tr("플레이북 ID (`claubiz playbooks`로 확인)"))
    p.add_argument("-g", "--goal")
    p.add_argument("--budget", type=float, help=tr("이번 실행 예산 상한 (USD)"))
    p.add_argument("--yes", action="store_true", help=tr("계획 승인 확인 생략"))
    p.add_argument("--plan-only", action="store_true", help=tr("계획과 예상 비용만 보기"))
    p.add_argument("--allow-confidential", action="store_true", help=tr("confidential 등급 자료 전송 허용"))
    p.add_argument("--direct", action="store_true", help=tr("계획 없이 에이전트 하나로 바로 실행"))
    p.add_argument("--from", dest="base", metavar="RUN_ID", help=tr("이 실행 결과를 바탕으로 이어서 작업"))
    p.add_argument("--resume", metavar="RUN_ID", help=tr("중단되거나 실패한 실행을 남은 작업부터 재개"))
    p.add_argument("--model", choices=[m for m, _ in MODEL_CHOICES], help=tr("이번 실행의 모든 역할에 쓸 모델"))
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("budget", help=tr("이번 결제 주기 사용량"))
    p.set_defaults(func=cmd_budget)

    audit = sub.add_parser("audit", help=tr("감사 로그")).add_subparsers(dest="audit_cmd", required=True)
    p = audit.add_parser("verify", help=tr("해시 체인 무결성 검증"))
    p.set_defaults(func=cmd_audit_verify)

    p = sub.add_parser("tui", help=tr("터미널 UI 실행"))
    p.add_argument("-w", "--workspace")
    p.set_defaults(func=cmd_tui)
    return parser


def prepare_stdio() -> None:
    if sys.platform != "win32":
        return
    if sys.stdout is None or sys.stderr is None:
        from .agent import log_path

        fd = hostos.open_fd(log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        stream = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    install_log_redaction()
    scrub_credential_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home:
        os.environ[HOME_ENV] = args.home
    prepare_stdio()
    if args.command is None:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.print_help()
            return 0
        args = parser.parse_args([*(["--home", args.home] if args.home else []),
                                  *(["--lang", args.lang] if args.lang else []), "tui"])
    try:
        return args.func(args)
    except (ConfigError, SecretError, WorkspaceError, PolicyError, RunError, PlanError, BudgetExceeded, KeyError,
            UnsafePathError, DigestError, ActionError, ScheduleError, AgentError) as exc:
        console.print(tr("[red]오류:[/] {error}", error=escape(str(exc))))
        return 2
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        console.print(tr("[red]입력 오류:[/] {error}", error=escape(errors)))
        return 2
    except anthropic.AuthenticationError:
        console.print(tr("[red]오류:[/] API 키 인증 실패 (`claubiz auth status --check`)"))
        return 2
    except anthropic.APIStatusError as exc:
        console.print(tr("[red]API 오류 {status}:[/] {message}", status=exc.status_code,
                         message=escape(str(exc.message)[:300])))
        return 2
    except anthropic.APIConnectionError as exc:
        console.print(tr("[red]연결 오류:[/] {error}", error=type(exc).__name__))
        return 2
    except KeyboardInterrupt:
        console.print(tr("중단됨"))
        return 130


if __name__ == "__main__":
    sys.exit(main())
