from datetime import datetime

from rich.markup import escape

from .config import model_label
from .events import Event

STATUS_LABELS = {
    "pending": "대기",
    "queued": "대기열",
    "running": "실행 중",
    "done": "완료",
    "reused": "재사용",
    "failed": "실패",
    "blocked": "예산 차단",
    "skipped": "건너뜀",
}


def describe(event: Event) -> str | None:
    d = {k: escape(v) if isinstance(v, str) else v for k, v in event.data.items()}
    kind = event.kind
    if kind == "task.started":
        return f"시작 (effort {d.get('effort')})"
    if kind == "task.tool":
        return f"{d.get('tool')}: {d.get('detail')}"
    if kind == "task.retry":
        return f"재시도 ({d.get('reason')}, effort {d.get('effort')})"
    if kind == "task.done":
        return f"완료 (${d.get('cost', 0):.4f}, 출처 {d.get('sources')}개, {d.get('status')})"
    if kind == "task.reused":
        return f"이전 결과 재사용 ({d.get('ref')})"
    if kind == "task.failed":
        return f"[red]실패: {d.get('reason')}[/]"
    if kind == "task.blocked":
        return f"[red]예산 차단: {d.get('reason')}[/]"
    if kind == "task.skipped":
        return f"[yellow]건너뜀: {d.get('reason')}[/]"
    if kind == "task.fallback":
        return "[yellow]안전 분류기 거절로 대체 모델 사용[/]"
    if kind == "llm.usage":
        return (
            f"[dim]{d.get('role')} {d.get('model')} 입력 {d.get('input_tokens')} 캐시읽기 {d.get('cache_read')}"
            f" 캐시쓰기 {d.get('cache_write')} 출력 {d.get('output_tokens')} 검색 {d.get('web_searches')}"
            f" ${d.get('cost', 0):.4f}[/]"
        )
    if kind == "budget.soft_cap":
        return f"[yellow]월 경고 기준 도달: ${d.get('spent', 0):.2f} / ${d.get('soft', 0):.2f}[/]"
    if kind == "run.model_changed":
        model = str(event.data.get("model") or "config")
        label = "설정 기본값" if model == "config" else model_label(model)
        return f"[yellow]모델 변경: {escape(label)} (다음 호출부터 적용)[/]"
    if kind == "run.planning":
        return "계획 수립 중"
    if kind == "run.started":
        if d.get("resumed"):
            return f"실행 재개 (작업 {d.get('tasks')}개 중 {d.get('resumed')}개는 이전 결과 사용)"
        return f"실행 시작 (작업 {d.get('tasks')}개)"
    if kind == "synth.started":
        return "종합 보고서 작성 시작"
    if kind == "synth.done":
        return f"종합 완료 (${d.get('cost', 0):.4f})"
    if kind == "run.done":
        return f"[bold]실행 종료: {d.get('status')} (${d.get('cost', 0):.4f})[/]"
    if kind == "run.failed":
        return f"[red]실행 실패: {d.get('reason')}[/]"
    if kind == "run.cancelled":
        return "[yellow]실행 취소됨[/]"
    return None


def line(event: Event) -> str | None:
    text = describe(event)
    if text is None:
        return None
    ts = datetime.fromtimestamp(event.ts).strftime("%H:%M:%S")
    tid = escape(f"[{event.task_id}] ") if event.task_id else ""
    return f"[dim]{ts}[/] {tid}{text}"
