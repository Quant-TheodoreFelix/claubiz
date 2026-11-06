import ipaddress
import re
from typing import Literal

from .i18n import tr

Classification = Literal["public", "internal", "confidential"]
Scope = Literal["research", "inputs-only", "web-only"]

CLASS_RANK: dict[str, int] = {"public": 0, "internal": 1, "confidential": 2}
SCOPES: tuple[str, ...] = ("research", "inputs-only", "web-only")

HOST_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
BLOCKED_HOSTS = frozenset({"localhost", "localdomain", "local", "internal", "home.arpa"})


class PolicyError(ValueError):
    pass


def scope_allows_web(scope: str) -> bool:
    return scope in ("research", "web-only")


def scope_allows_inputs(scope: str) -> bool:
    return scope in ("research", "inputs-only")


def may_send(classification: str, max_classification: str, allow_confidential: bool) -> bool:
    rank = CLASS_RANK[classification]
    if classification == "confidential":
        return allow_confidential
    return rank <= CLASS_RANK[max_classification]


def validate_domains(domains: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for raw in domains:
        host = raw.strip().lower().rstrip(".")
        if not host:
            continue
        if "://" in host or "/" in host or ":" in host or "@" in host:
            raise PolicyError(tr("도메인에는 호스트 이름만 쓸 수 있음: {value}", value=raw))
        try:
            ipaddress.ip_address(host)
            raise PolicyError(tr("IP 주소는 허용되지 않음: {value}", value=raw))
        except ValueError:
            pass
        if host in BLOCKED_HOSTS or host.endswith((".local", ".localhost", ".internal", ".home.arpa")):
            raise PolicyError(tr("내부망 도메인은 허용되지 않음: {value}", value=raw))
        if not HOST_RE.fullmatch(host):
            raise PolicyError(tr("올바른 도메인이 아님: {value}", value=raw))
        if host not in cleaned:
            cleaned.append(host)
    if len(cleaned) > 64:
        raise PolicyError(tr("도메인 목록은 최대 64개까지 가능"))
    return tuple(cleaned)


RRN_RE = re.compile(r"(?<!\d)\d{6}\s?-\s?[1-8]\d{6}(?!\d)")
MOBILE_RE = re.compile(r"(?<!\d)01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")
LANDLINE_RE = re.compile(r"(?<!\d)0(?:2|[3-6][1-5])[-\s.]\d{3,4}[-\s.]\d{4}(?!\d)")
EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern[str], label: str, value: str, check=None) -> str:
        def repl(match: re.Match[str]) -> str:
            if check is not None and not check(match.group(0)):
                return match.group(0)
            counts[label] = counts.get(label, 0) + 1
            return tr("[{label} 가림]", label=label)

        return pattern.sub(repl, value)

    out = sub(RRN_RE, tr("주민등록번호"), text)
    out = sub(CARD_RE, tr("카드번호"), out, lambda s: _luhn_ok(re.sub(r"\D", "", s)))
    out = sub(MOBILE_RE, tr("전화번호"), out)
    out = sub(LANDLINE_RE, tr("전화번호"), out)
    out = sub(EMAIL_RE, tr("이메일"), out)
    return out, counts
