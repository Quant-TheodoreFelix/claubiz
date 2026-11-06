import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import i18n
from .paths import home, read_bytes_nofollow, write_private_text

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORT_ORDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
OUTPUT_TOKENS_BY_EFFORT: dict[str, int] = {"low": 2500, "medium": 5000, "high": 9000, "xhigh": 14000, "max": 20000}

FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"})
DYNAMIC_WEB_MODELS = frozenset(
    {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    }
)


MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("claude-opus-5-5", "Opus 5.5"),
    ("claude-sonnet-5-5", "Sonnet 5.5"),
    ("claude-haiku-5-5", "Haiku 5.5"),
    ("claude-fable-5-1", "Fable 5.1"),
    ("claude-opus-5", "Opus 5"),
)
MODEL_ROLES: tuple[str, ...] = ("planner", "worker", "synthesizer")


class ConfigError(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApiConfig(Strict):
    base_url: str = "https://api.anthropic.com"
    egress_allowlist: tuple[str, ...] = ("api.anthropic.com",)
    ca_bundle: str = ""
    proxy: str = ""
    timeout_seconds: float = Field(600.0, ge=10, le=3600)
    max_retries: int = Field(2, ge=0, le=6)
    keychain_service: str = "claubiz"
    keychain_account: str = "anthropic-api-key"
    workspace_id: str = ""

    @model_validator(mode="after")
    def _check(self) -> "ApiConfig":
        if self.workspace_id and not re.fullmatch(r"wrkspc_[A-Za-z0-9]{8,64}", self.workspace_id):
            raise ValueError("api.workspace_id 형식이 올바르지 않음 (wrkspc_...)")
        parts = urlsplit(self.base_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("api.base_url은 https URL이어야 함")
        if parts.hostname not in self.egress_allowlist:
            raise ValueError("api.base_url 호스트가 egress_allowlist에 없음")
        if self.proxy and urlsplit(self.proxy).scheme not in ("http", "https"):
            raise ValueError("api.proxy는 http 또는 https URL이어야 함")
        return self


class BudgetConfig(Strict):
    monthly_soft_usd: float = Field(80.0, gt=0)
    monthly_hard_usd: float = Field(90.0, gt=0)
    cycle_anchor_day: int = Field(1, ge=1, le=28)
    run_default_usd: float = Field(3.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "BudgetConfig":
        if self.monthly_soft_usd > self.monthly_hard_usd:
            raise ValueError("monthly_soft_usd는 monthly_hard_usd 이하여야 함")
        if self.run_default_usd > self.monthly_hard_usd:
            raise ValueError("run_default_usd는 monthly_hard_usd 이하여야 함")
        return self


class RoleConfig(Strict):
    model: str
    effort: Effort
    max_tokens: int = Field(ge=1024, le=128000)
    effort_min: Effort = "low"
    effort_max: Effort = "high"
    fallbacks: bool = True

    @model_validator(mode="after")
    def _check(self) -> "RoleConfig":
        lo, hi = EFFORT_ORDER.index(self.effort_min), EFFORT_ORDER.index(self.effort_max)
        if lo > hi:
            raise ValueError("effort_min은 effort_max보다 높을 수 없음")
        return self

    def clamp(self, effort: str) -> str:
        idx = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else EFFORT_ORDER.index(self.effort)
        lo, hi = EFFORT_ORDER.index(self.effort_min), EFFORT_ORDER.index(self.effort_max)
        return EFFORT_ORDER[min(max(idx, lo), hi)]

    def escalate(self, effort: str) -> str | None:
        idx = EFFORT_ORDER.index(effort)
        hi = EFFORT_ORDER.index(self.effort_max)
        return EFFORT_ORDER[idx + 1] if idx < hi else None

    def uses_fallbacks(self) -> bool:
        return self.fallbacks and self.model in FALLBACK_MODELS


class ModelsConfig(Strict):
    planner: RoleConfig = RoleConfig(model="claude-opus-5-5", effort="medium", max_tokens=16000, effort_max="high")
    worker: RoleConfig = RoleConfig(model="claude-opus-5-5", effort="low", max_tokens=16000, effort_max="high")
    synthesizer: RoleConfig = RoleConfig(model="claude-opus-5-5", effort="medium", max_tokens=32000, effort_max="high")
    helper: RoleConfig = RoleConfig(
        model="claude-haiku-5-5", effort="low", max_tokens=8000, effort_max="medium", fallbacks=False
    )


class OrchestratorConfig(Strict):
    max_parallel: int = Field(4, ge=1, le=16)
    max_tasks: int = Field(6, ge=1, le=12)
    cache_warm_fanout: bool = True
    escalate_on_failure: bool = True
    max_pause_continuations: int = Field(4, ge=0, le=10)
    output_language: str = Field("Korean", min_length=2, max_length=40)


class UiConfig(Strict):
    theme: str = Field("claubiz", pattern=r"^[a-z0-9-]{2,40}$")
    language: Literal["ko", "en"] = "ko"


class Price(Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write_5m_multiplier: float = 1.25
    cache_write_1h_multiplier: float = 2.0
    long_context_threshold: int = 0
    long_input: float = 0.0
    long_output: float = 0.0


DEFAULT_PRICING: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.20),
    "claude-haiku-5-5": Price(
        input=0.10, output=0.50, cache_read=0.01, long_context_threshold=100000, long_input=0.50, long_output=2.50
    ),
    "claude-opus-5": Price(input=5.0, output=25.0, cache_read=0.50),
    "claude-opus-4-8": Price(input=5.0, output=25.0, cache_read=0.50),
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_read=0.25),
}


class Settings(Strict):
    api: ApiConfig = ApiConfig()
    budget: BudgetConfig = BudgetConfig()
    models: ModelsConfig = ModelsConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    ui: UiConfig = UiConfig()
    pricing: dict[str, Price] = Field(default_factory=lambda: dict(DEFAULT_PRICING))
    web_search_usd_per_1k: float = Field(10.0, ge=0)

    @field_validator("pricing")
    @classmethod
    def _merge_pricing(cls, value: dict[str, Price]) -> dict[str, Price]:
        merged = dict(DEFAULT_PRICING)
        merged.update(value)
        return merged

    def price_for(self, model: str) -> Price:
        if model in self.pricing:
            return self.pricing[model]
        return max(self.pricing.values(), key=lambda p: p.output)


def model_label(model: str) -> str:
    return dict(MODEL_CHOICES).get(model, model)


def with_model(settings: "Settings", model: str | None) -> "Settings":
    if not model:
        return settings
    if model not in settings.pricing:
        raise ConfigError(f"가격 정보가 없는 모델이라 쓸 수 없음: {model}")
    models = settings.models
    roles = {role: getattr(models, role).model_copy(update={"model": model}) for role in MODEL_ROLES}
    return settings.model_copy(update={"models": models.model_copy(update=roles)})


def config_path() -> Path:
    return home() / "config.toml"


def load_settings(path: Path | None = None) -> Settings:
    target = path or config_path()
    if not target.exists():
        return Settings()
    try:
        data = tomllib.loads(read_bytes_nofollow(target, 256 * 1024).decode("utf-8"))
        return Settings.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, UnicodeDecodeError) as exc:
        raise ConfigError(f"설정 파일 오류 ({target}): {exc}") from None


DEFAULT_CONFIG_TOML = """\
[api]
base_url = "https://api.anthropic.com"
egress_allowlist = ["api.anthropic.com"]
ca_bundle = ""
proxy = ""
timeout_seconds = 600
max_retries = 2
keychain_service = "claubiz"
keychain_account = "anthropic-api-key"
workspace_id = "{workspace_id}"

[budget]
monthly_soft_usd = 80.0
monthly_hard_usd = 90.0
cycle_anchor_day = 1
run_default_usd = 3.0

[models.planner]
model = "claude-opus-5-5"
effort = "medium"
max_tokens = 16000
effort_max = "high"

[models.worker]
model = "claude-opus-5-5"
effort = "low"
max_tokens = 16000
effort_min = "low"
effort_max = "high"

[models.synthesizer]
model = "claude-opus-5-5"
effort = "medium"
max_tokens = 32000
effort_max = "high"

[models.helper]
model = "claude-haiku-5-5"
effort = "low"
max_tokens = 8000
effort_max = "medium"
fallbacks = false

[orchestrator]
max_parallel = 4
max_tasks = 6
cache_warm_fanout = true
escalate_on_failure = true
max_pause_continuations = 4
output_language = "{output_language}"

[ui]
theme = "claubiz"
language = "{language}"
"""


def write_default_config(path: Path | None = None, overwrite: bool = False, workspace_id: str = "") -> Path:
    target = path or config_path()
    if target.exists() and not overwrite:
        return target
    lang = i18n.language()
    text = (DEFAULT_CONFIG_TOML.replace("{workspace_id}", workspace_id)
            .replace("{output_language}", i18n.OUTPUT_LANGUAGES[lang]).replace("{language}", lang))
    Settings.model_validate(tomllib.loads(text))
    write_private_text(target, text)
    return target
