from datetime import datetime

from rich.markup import escape

from .config import model_label
from .events import Event
from .i18n import tr

STATUS_LABELS = {
    "pending": tr("대기"),
    "queued": tr("대기열"),
    "running": tr("실행 중"),
    "done": tr("완료"),
    "reused": tr("재사용"),
    "failed": tr("실패"),
    "blocked": tr("예산 차단"),
    "skipped": tr("건너뜀"),
}


def describe(event: Event) -> str | None:
    d = {k: escape(v) if isinstance(v, str) else v for k, v in event.data.items()}
    kind = event.kind
    if kind == "task.started":
        return tr("시작 (effort {effort})", effort=d.get("effort"))
    if kind == "task.tool":
        return f"{d.get('tool')}: {d.get('detail')}"
    if kind == "task.retry":
        return tr("재시도 ({reason}, effort {effort})", reason=d.get("reason"), effort=d.get("effort"))
    if kind == "task.done":
        return tr("완료 (${cost:.4f}, 출처 {sources}개, {status})", cost=d.get("cost", 0),
                  sources=d.get("sources"), status=d.get("status"))
    if kind == "task.reused":
        return tr("이전 결과 재사용 ({ref})", ref=d.get("ref"))
    if kind == "task.failed":
        return tr("[red]실패: {reason}[/]", reason=d.get("reason"))
    if kind == "task.blocked":
        return tr("[red]예산 차단: {reason}[/]", reason=d.get("reason"))
    if kind == "task.skipped":
        return tr("[yellow]건너뜀: {reason}[/]", reason=d.get("reason"))
    if kind == "task.fallback":
        return tr("[yellow]안전 분류기 거절로 대체 모델 사용[/]")
    if kind == "llm.usage":
        return tr(
            "[dim]{role} {model} 입력 {input_tokens} 캐시읽기 {cache_read} 캐시쓰기 {cache_write} "
            "출력 {output_tokens} 검색 {web_searches} ${cost:.4f}[/]",
            role=d.get("role"), model=d.get("model"), input_tokens=d.get("input_tokens"),
            cache_read=d.get("cache_read"), cache_write=d.get("cache_write"),
            output_tokens=d.get("output_tokens"), web_searches=d.get("web_searches"), cost=d.get("cost", 0),
        )
    if kind == "budget.soft_cap":
        return tr("[yellow]월 경고 기준 도달: ${spent:.2f} / ${soft:.2f}[/]", spent=d.get("spent", 0),
                  soft=d.get("soft", 0))
    if kind == "run.model_changed":
        model = str(event.data.get("model") or "config")
        label = tr("설정 기본값") if model == "config" else model_label(model)
        return tr("[yellow]모델 변경: {label} (다음 호출부터 적용)[/]", label=escape(label))
    if kind == "run.planning":
        return tr("계획 수립 중")
    if kind == "run.started":
        if d.get("resumed"):
            return tr("실행 재개 (작업 {tasks}개 중 {resumed}개는 이전 결과 사용)", tasks=d.get("tasks"),
                      resumed=d.get("resumed"))
        return tr("실행 시작 (작업 {tasks}개)", tasks=d.get("tasks"))
    if kind == "synth.started":
        return tr("종합 보고서 작성 시작")
    if kind == "synth.done":
        return tr("종합 완료 (${cost:.4f})", cost=d.get("cost", 0))
    if kind == "knowledge.started":
        return tr("지식 정리 중")
    if kind == "knowledge.updated":
        if d.get("dropped"):
            return tr("지식 {added}개 추가, {replaced}개 갱신, 출처 없는 {dropped}개 제외 (${cost:.4f})",
                      added=d.get("added"), replaced=d.get("replaced"), dropped=d.get("dropped"),
                      cost=d.get("cost", 0))
        return tr("지식 {added}개 추가, {replaced}개 갱신 (${cost:.4f})", added=d.get("added"),
                  replaced=d.get("replaced"), cost=d.get("cost", 0))
    if kind == "knowledge.failed":
        return tr("[yellow]지식 정리 생략: {reason}[/]", reason=d.get("reason"))
    if kind == "actions.started":
        return tr("행동 제안 정리 중")
    if kind == "actions.proposed":
        count = d.get("count") or 0
        if count:
            return tr("행동 제안 {count}건 (${cost:.4f}) · 제안 탭에서 승인", count=count, cost=d.get("cost", 0))
        return tr("행동 제안 {count}건 (${cost:.4f})", count=count, cost=d.get("cost", 0))
    if kind == "actions.failed":
        return tr("[yellow]행동 제안 생략: {reason}[/]", reason=d.get("reason"))
    if kind == "run.done":
        return tr("[bold]실행 종료: {status} (${cost:.4f})[/]", status=d.get("status"), cost=d.get("cost", 0))
    if kind == "run.failed":
        return tr("[red]실행 실패: {reason}[/]", reason=d.get("reason"))
    if kind == "run.cancelled":
        return tr("[yellow]실행 취소됨[/]")
    return None


def line(event: Event) -> str | None:
    text = describe(event)
    if text is None:
        return None
    ts = datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")
    tid = escape(f"[{event.task_id}] ") if event.task_id else ""
    return f"[dim]{ts}[/] {tid}{text}"
