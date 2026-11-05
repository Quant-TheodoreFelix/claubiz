import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

HANDOFF_RE = re.compile(r"<handoff>(.*?)</handoff>", re.DOTALL)
STATUS_RE = re.compile(r"<status>\s*(ok|partial|insufficient)\s*</status>", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s)\]>\"'<`]+")


@dataclass(frozen=True)
class Source:
    index: int
    url: str
    title: str


@dataclass(frozen=True)
class RenderedNotes:
    body: str
    handoff: str
    status: str
    sources: tuple[Source, ...]
    consulted: tuple[Source, ...] = ()
    unverified: tuple[str, ...] = ()
    verified_urls: frozenset[str] = frozenset()

    @property
    def source_count(self) -> int:
        return len(self.sources) + len(self.consulted)

    def as_markdown(self) -> str:
        lines = [self.body.rstrip()]
        if self.sources:
            lines += ["", "### 출처"]
            lines += [f"{s.index}. [{_md_escape(s.title)}]({s.url})" for s in self.sources]
        if self.consulted:
            lines += ["", "### 참고한 페이지"]
            lines += [f"- [{_md_escape(s.title)}]({s.url})" for s in self.consulted]
        if self.unverified:
            lines += ["", "### 확인되지 않은 링크", "", "> 검색이나 페치 결과에 없던 주소임, 사용 전 확인 필요", ""]
            lines += [f"- {u}" for u in self.unverified]
        return "\n".join(lines) + "\n"


def _md_escape(text: str) -> str:
    return text.replace("[", "(").replace("]", ")").replace("\n", " ").strip() or "출처"


def _safe_url(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url.replace(")", "%29").replace(" ", "%20")


def norm_url(url: str) -> str:
    return url.rstrip(".,;:!?").rstrip("/")


def urls_in(text: str) -> list[str]:
    seen: list[str] = []
    for raw in URL_RE.findall(text):
        url = norm_url(raw)
        if url not in seen:
            seen.append(url)
    return seen


def plain_text(blocks: list[Any]) -> str:
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")


def _web_results(blocks: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    fetched: dict[str, str] = {}
    searched: dict[str, str] = {}
    for block in blocks:
        btype = getattr(block, "type", "")
        content = getattr(block, "content", None)
        if btype == "web_fetch_tool_result":
            url = getattr(content, "url", None)
            safe = _safe_url(url) if url else None
            if safe:
                doc = getattr(content, "content", None)
                fetched[norm_url(safe)] = getattr(doc, "title", None) or safe
        elif btype == "web_search_tool_result" and isinstance(content, list):
            for item in content:
                url = getattr(item, "url", None)
                safe = _safe_url(url) if url else None
                if safe:
                    searched[norm_url(safe)] = getattr(item, "title", None) or safe
    return fetched, searched


def render(blocks: list[Any], trusted: Iterable[str] = ()) -> RenderedNotes:
    sources: dict[str, Source] = {}
    pieces: list[str] = []
    for block in blocks:
        if getattr(block, "type", "") != "text":
            continue
        marks: list[int] = []
        for cite in getattr(block, "citations", None) or []:
            url = getattr(cite, "url", None)
            safe = _safe_url(url) if url else None
            if not safe:
                continue
            if safe not in sources:
                sources[safe] = Source(len(sources) + 1, safe, getattr(cite, "title", None) or safe)
            idx = sources[safe].index
            if idx not in marks:
                marks.append(idx)
        text = block.text
        if marks:
            stripped = text.rstrip()
            tail = text[len(stripped) :]
            by_index = {s.index: s.url for s in sources.values()}
            text = stripped + "".join(f" [[{i}]]({by_index[i]})" for i in marks) + tail
        pieces.append(text)
    rendered = "".join(pieces)
    raw = plain_text(blocks)
    handoff_match = HANDOFF_RE.search(raw)
    status_match = STATUS_RE.search(raw)
    body = HANDOFF_RE.sub("", rendered)
    body = STATUS_RE.sub("", body).strip()

    fetched, searched = _web_results(blocks)
    cited = {norm_url(u) for u in sources}
    mentioned = urls_in(body)
    consulted: dict[str, str] = {}
    for url, title in fetched.items():
        if url not in cited:
            consulted[url] = title
    for url in mentioned:
        if url in searched and url not in cited and url not in consulted:
            consulted[url] = searched[url]
    verified = cited | set(fetched) | set(searched) | {norm_url(u) for u in trusted}
    unverified = tuple(u for u in mentioned if u not in verified)
    return RenderedNotes(
        body=body,
        handoff=handoff_match.group(1).strip() if handoff_match else "",
        status=status_match.group(1).lower() if status_match else "ok",
        sources=tuple(sorted(sources.values(), key=lambda s: s.index)),
        consulted=tuple(Source(0, url, title) for url, title in consulted.items()),
        unverified=unverified,
        verified_urls=frozenset(verified),
    )


def unknown_urls(report: str, known: Iterable[str]) -> list[str]:
    allowed = {norm_url(u) for u in known}
    return [u for u in urls_in(report) if u not in allowed]
