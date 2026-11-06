import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import hostos
from .paths import confine, ensure_private_dir, home, read_bytes_nofollow, write_private_text

PLAYBOOK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
MAX_PLAYBOOK_BYTES = 64 * 1024


class PlaybookError(Exception):
    pass


@dataclass(frozen=True)
class Playbook:
    id: str
    title: str
    description: str
    goal_hint: str
    planner_guidance: str
    synth_guidance: str
    needs_web: bool
    needs_inputs: bool
    uses_history: bool
    builtin: bool = True


class PlaybookSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str
    title: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=300)
    goal_hint: str = Field("", max_length=200)
    planner_guidance: str = Field(min_length=1, max_length=2000)
    synth_guidance: str = Field(min_length=1, max_length=2000)
    needs_web: bool = True
    needs_inputs: bool = False
    uses_history: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not PLAYBOOK_ID_RE.fullmatch(value):
            raise ValueError("플레이북 ID는 소문자, 숫자, - 만 쓸 수 있음 (2~40자)")
        return value

    def to_playbook(self) -> Playbook:
        return Playbook(**self.model_dump(), builtin=False)


BUILTIN_PLAYBOOKS: dict[str, Playbook] = {
    p.id: p
    for p in (
        Playbook(
            id="market-research",
            title="시장·경쟁사 조사 보고서",
            description="주제를 받아 시장 현황, 주요 경쟁사, 기회와 위험을 웹에서 조사하고 출처가 달린 보고서를 만든다",
            goal_hint="예: 국내 B2B SaaS 전자계약 시장과 주요 경쟁사 조사",
            planner_guidance=(
                "Playbook: market and competitor research report. Typical decomposition: (a) market definition, size, "
                "growth and key trends; (b) one task per one or two major competitors covering offering, pricing, "
                "positioning, recent moves; (c) customer demand and buying criteria; (d) regulation, risks and entry "
                "barriers when relevant. Merge or drop parts that the goal does not need. All research tasks need web."
            ),
            synth_guidance=(
                "Deliverable: market and competitor research report. Include an executive summary, market overview, "
                "a competitor comparison table, opportunities and risks, and concrete recommendations."
            ),
            needs_web=True,
            needs_inputs=False,
            uses_history=False,
        ),
        Playbook(
            id="trend-monitor",
            title="정기 동향 모니터링",
            description="키워드나 경쟁사의 최근 동향을 조사하고 이전 실행 결과와 비교해 변화점만 보고한다",
            goal_hint="예: 국내 생성형 AI 규제와 주요 빅테크 발표 동향",
            planner_guidance=(
                "Playbook: periodic trend monitoring. Focus only on developments since the previous run date given in "
                "the context (or the last 30 days if there is no previous run). Use 2 to 4 tasks split by topic or "
                "entity, low effort, and few searches each. Prefer news, official announcements and filings."
            ),
            synth_guidance=(
                "Deliverable: change-focused monitoring brief. Lead with what is new or changed compared to the "
                "previous summary, then signals to watch. Do not repeat unchanged facts from the previous summary. "
                "If nothing material changed, say so in a short brief."
            ),
            needs_web=True,
            needs_inputs=False,
            uses_history=True,
        ),
        Playbook(
            id="doc-digest",
            title="자료 요약·정리",
            description="작업 영역에 가져온 문서를 읽고 핵심 요약, 결정 사항, 할 일, 남은 질문으로 정리한다",
            goal_hint="예: 이번 주 회의록과 제안서 초안을 요약하고 할 일 정리",
            planner_guidance=(
                "Playbook: document digest. Assign the workspace input files to tasks, usually one task per document "
                "or per group of small related documents. Do not use web unless the goal explicitly asks to verify or "
                "enrich facts externally."
            ),
            synth_guidance=(
                "Deliverable: document digest. Include an overall summary, key points per document, decisions made, "
                "action items with owners and dates when stated, and open questions."
            ),
            needs_web=False,
            needs_inputs=True,
            uses_history=False,
        ),
    )
}


def playbooks_dir() -> Path:
    return ensure_private_dir(home() / "playbooks")


def load_custom_playbooks() -> tuple[dict[str, Playbook], list[str]]:
    found: dict[str, Playbook] = {}
    errors: list[str] = []
    for path in sorted(playbooks_dir().glob("*.toml")):
        if hostos.is_link(path):
            errors.append(f"{path.name}: 심볼릭 링크는 무시함")
            continue
        try:
            raw = read_bytes_nofollow(path, MAX_PLAYBOOK_BYTES).decode("utf-8")
            spec = PlaybookSpec.model_validate(tomllib.loads(raw))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        if spec.id != path.stem or spec.id in BUILTIN_PLAYBOOKS:
            errors.append(f"{path.name}: ID가 파일 이름과 다르거나 기본 플레이북과 겹침")
            continue
        found[spec.id] = spec.to_playbook()
    return found, errors


def all_playbooks() -> dict[str, Playbook]:
    custom, _ = load_custom_playbooks()
    return {**BUILTIN_PLAYBOOKS, **custom}


def render_playbook_toml(spec: PlaybookSpec) -> str:
    lines = []
    for key, value in spec.model_dump().items():
        rendered = ("true" if value else "false") if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def save_custom_playbook(spec: PlaybookSpec) -> Playbook:
    if spec.id in BUILTIN_PLAYBOOKS:
        raise PlaybookError(f"기본 플레이북과 같은 ID는 쓸 수 없음: {spec.id}")
    target = confine(playbooks_dir(), f"{spec.id}.toml")
    if target.exists():
        raise PlaybookError(f"이미 있는 플레이북 ID: {spec.id}")
    write_private_text(target, render_playbook_toml(spec))
    return spec.to_playbook()


def get_playbook(playbook_id: str | None) -> Playbook | None:
    if not playbook_id:
        return None
    playbooks = all_playbooks()
    if playbook_id not in playbooks:
        raise KeyError(f"알 수 없는 플레이북: {playbook_id} (사용 가능: {', '.join(playbooks)})")
    return playbooks[playbook_id]
