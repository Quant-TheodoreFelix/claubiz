import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from . import hostos
from .paths import confine, read_bytes_nofollow, write_private_text
from .policy import Classification
from .render import norm_url
from .workspace import RUN_ID_RE, Workspace

FACT_ID_RE = re.compile(r"^k[0-9a-f]{10}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TERM_RE = re.compile(r"[0-9a-z가-힣]+")
HANGUL_RE = re.compile(r"[가-힣]")
MAX_FACTS = 1000
MAX_FACT_CHARS = 400
MAX_TOPIC_CHARS = 40
MAX_SOURCES = 4
MAX_NEW_FACTS = 12
MAX_FILE_BYTES = 4 * 1024 * 1024

EXTRACT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts", "replaces"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "topic", "sources", "as_of"],
                "properties": {
                    "text": {"type": "string"},
                    "topic": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "as_of": {"type": "string"},
                },
            },
        },
        "replaces": {"type": "array", "items": {"type": "string"}},
    },
}


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    topic: str = ""
    sources: list[str]
    as_of: str
    added: str
    run_id: str
    classification: Classification


def new_fact_id(seed: str) -> str:
    return "k" + hashlib.sha256(f"{seed}{time.time_ns()}".encode()).hexdigest()[:10]


def _norm_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def terms(text: str) -> set[str]:
    found: set[str] = set()
    for word in TERM_RE.findall(text.lower()):
        if len(word) >= 2:
            found.add(word)
        if HANGUL_RE.search(word) and len(word) > 2:
            found.update(word[i : i + 2] for i in range(len(word) - 1))
    return found


class KnowledgeStore:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws

    def _path(self):
        return confine(self.ws.root, "knowledge.json")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = hostos.open_fd(confine(self.ws.root, "knowledge.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            hostos.lock(fd)
            yield
        finally:
            hostos.unlock(fd)
            os.close(fd)

    def facts(self) -> list[Fact]:
        path = self._path()
        if not path.exists():
            return []
        data = json.loads(read_bytes_nofollow(path, MAX_FILE_BYTES).decode("utf-8"))
        out = []
        for item in data.get("facts", []):
            try:
                out.append(Fact.model_validate(item))
            except ValidationError:
                continue
        return out

    def _save(self, facts: list[Fact]) -> None:
        payload = {"facts": [f.model_dump() for f in facts]}
        write_private_text(self._path(), json.dumps(payload, ensure_ascii=False, indent=1))

    def merge(self, new: list[Fact], replaces: Iterable[str] = ()) -> tuple[int, int]:
        with self._locked():
            return self._merge(new, replaces)

    def _merge(self, new: list[Fact], replaces: Iterable[str]) -> tuple[int, int]:
        drop = set(replaces)
        current = self.facts()
        kept = [f for f in current if f.id not in drop]
        replaced = len(current) - len(kept)
        seen = {_norm_text(f.text): f for f in kept}
        added = 0
        for fact in new:
            key = _norm_text(fact.text)
            existing = seen.get(key)
            if existing is not None:
                existing.as_of = max(existing.as_of, fact.as_of)
                existing.sources = list(dict.fromkeys(existing.sources + fact.sources))[:MAX_SOURCES]
                continue
            kept.append(fact)
            seen[key] = fact
            added += 1
        if len(kept) > MAX_FACTS:
            kept.sort(key=lambda f: (f.as_of, f.added))
            kept = kept[len(kept) - MAX_FACTS :]
        self._save(kept)
        return added, replaced

    def remove(self, ids: Iterable[str]) -> int:
        drop = set(ids)
        with self._locked():
            current = self.facts()
            kept = [f for f in current if f.id not in drop]
            if len(kept) != len(current):
                self._save(kept)
        return len(current) - len(kept)

    def remove_run(self, run_id: str) -> int:
        if not RUN_ID_RE.fullmatch(run_id):
            return 0
        return self.remove(f.id for f in self.facts() if f.run_id == run_id)

    def count_run(self, run_id: str) -> int:
        return sum(1 for f in self.facts() if f.run_id == run_id)


def select_facts(facts: list[Fact], query: str, limit: int, allowed: Callable[[str], bool] | None = None,
                 today: date | None = None) -> list[Fact]:
    wanted = terms(query)
    if not wanted or limit <= 0:
        return []
    now = today or date.today()
    scored: list[tuple[float, Fact]] = []
    for fact in facts:
        if allowed is not None and not allowed(fact.classification):
            continue
        have = terms(f"{fact.topic} {fact.text}")
        overlap = len(wanted & have)
        if not overlap:
            continue
        score = overlap / math.sqrt(len(have) or 1)
        try:
            age = (now - date.fromisoformat(fact.as_of)).days
        except ValueError:
            age = 365
        score += 0.3 if age <= 30 else (0.1 if age <= 180 else 0.0)
        scored.append((score, fact))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = [fact for _, fact in scored[:limit]]
    chosen.sort(key=lambda f: f.as_of, reverse=True)
    return chosen


def facts_block(facts: list[Fact], max_chars: int, tag: str = "known_facts") -> str:
    if not facts:
        return ""
    lines = [f"<{tag}>"]
    used = 0
    for fact in facts:
        sources = " ".join(fact.sources[:2])
        line = f"- [{fact.id}] ({fact.as_of}) {fact.text}" + (f" | {sources}" if sources else "")
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    lines.append(f"</{tag}>")
    return "\n".join(lines) if len(lines) > 2 else ""


def fact_sources(facts: Iterable[Fact]) -> set[str]:
    return {s for f in facts for s in f.sources if s.startswith(("http://", "https://"))}


def parse_extraction(raw: str, *, run_id: str, classification: str, allowed_sources: set[str],
                     candidates: set[str], today: date | None = None) -> tuple[list[Fact], list[str], int]:
    data = json.loads(raw)
    now = today or date.today()
    allowed = {norm_url(s) if s.startswith(("http://", "https://")) else s for s in allowed_sources}
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    facts: list[Fact] = []
    dropped = 0
    for item in (data.get("facts") or [])[: MAX_NEW_FACTS * 2]:
        text = " ".join(str(item.get("text", "")).split())
        if not 8 <= len(text) <= MAX_FACT_CHARS:
            dropped += 1
            continue
        sources = []
        for raw_source in item.get("sources") or []:
            source = str(raw_source).strip()
            key = norm_url(source) if source.startswith(("http://", "https://")) else source
            if key in allowed and key not in sources:
                sources.append(key)
        if not sources:
            dropped += 1
            continue
        as_of = str(item.get("as_of", "")).strip()[:10]
        if not DATE_RE.fullmatch(as_of):
            as_of = now.isoformat()
        else:
            try:
                parsed = date.fromisoformat(as_of)
            except ValueError:
                parsed = now
            as_of = min(parsed, now).isoformat()
        topic = " ".join(str(item.get("topic", "")).split())[:MAX_TOPIC_CHARS]
        facts.append(Fact(
            id=new_fact_id(text), text=text, topic=topic, sources=sources[:MAX_SOURCES], as_of=as_of, added=stamp,
            run_id=run_id, classification=classification,
        ))
        if len(facts) >= MAX_NEW_FACTS:
            break
    replaces = [r for r in dict.fromkeys(str(x) for x in data.get("replaces") or []) if r in candidates]
    return facts, replaces, dropped
