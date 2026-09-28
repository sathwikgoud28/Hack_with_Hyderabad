"""Memory layer.

`HindsightMemory` is the real backend used in the hackathon demo. It stores
postmortems and agent feedback in a Hindsight memory bank and recalls them with
Hindsight's semantic + keyword + temporal search.

`LocalMemory` is a small offline stand-in (keyword overlap scoring, JSON file on
disk) so the app and tests run without API keys. It is NOT a replacement for
Hindsight; the UI clearly shows which backend is active.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import DATA_DIR, settings

log = logging.getLogger(__name__)

BANK_MISSION = (
    "You are the long-term memory of an on-call incident response assistant for an "
    "e-commerce platform. Remember production incidents: affected service, symptoms and "
    "error messages, root cause, what triggered it (deploys, config changes, traffic, bad data), "
    "the fix that worked, fixes that did NOT work, and time to resolve. Also remember engineer "
    "feedback on whether the assistant's recommendations helped."
)


_INCIDENT_ID_RE = re.compile(r"INC-\d{4}")

# A recall counts as a real match only if at least one memory scores this high.
# Hindsight's reranked `final` score is ~0.2-0.5 for true matches and <0.01 for unrelated
# alerts; LocalMemory only returns items scoring >= 1.5, so they always qualify.
STRONG_MATCH_SCORE = 0.05


def is_strong_match(memories: list["MemoryItem"]) -> bool:
    return any(m.score is None or m.score >= STRONG_MATCH_SCORE for m in memories)


@dataclass
class MemoryItem:
    id: str
    text: str
    type: str = "experience"
    context: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    occurred_at: str | None = None
    score: float | None = None  # Hindsight's reranked relevance (can exceed 1; used for the match gate)
    similarity: float | None = None  # Hindsight's semantic similarity, 0-1 (shown to users as a percentage)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryItem":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class RetainItem:
    content: str
    context: str
    metadata: dict[str, str]
    timestamp: datetime | None = None
    document_id: str | None = None
    tags: list[str] = field(default_factory=list)


class MemoryStore(Protocol):
    name: str

    def ensure_bank(self) -> None: ...
    def retain(self, items: list[RetainItem]) -> None: ...
    def recall(self, query: str, limit: int = 8) -> list[MemoryItem]: ...
    def reflect(self, query: str) -> str | None: ...
    def reset(self) -> None: ...
    def count(self) -> int | None: ...


# --------------------------------------------------------------------------- #
# Hindsight
# --------------------------------------------------------------------------- #
class HindsightMemory:
    name = "hindsight"

    def __init__(self, base_url: str, api_key: str, bank_id: str):
        from hindsight_client import Hindsight  # imported lazily so tests don't need it

        self.bank_id = bank_id
        # The sync SDK runs each call on the calling thread's asyncio loop, and its aiohttp session stays
        # bound to the loop it was first used on. FastAPI serves sync handlers from a thread pool, so a call
        # from a second thread fails ("attached to a different loop"). Pin every SDK call to one thread.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hindsight")
        self.client = self._call(Hindsight, base_url=base_url, api_key=api_key or None, timeout=120.0)
        self._bank_ready = False

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return self._executor.submit(fn, *args, **kwargs).result()

    def ensure_bank(self) -> None:
        if self._bank_ready:
            return
        try:
            self._call(
                self.client.create_bank,
                bank_id=self.bank_id,
                name="Incident Response Memory",
                mission=BANK_MISSION,
            )
            log.info("Created Hindsight bank %s", self.bank_id)
        except Exception as exc:  # bank most likely exists already
            log.info("create_bank skipped for %s: %s", self.bank_id, exc)
        self._bank_ready = True

    def retain(self, items: list[RetainItem]) -> None:
        self.ensure_bank()
        payload = []
        for it in items:
            entry: dict[str, Any] = {
                "content": it.content,
                "context": it.context,
                "metadata": it.metadata,
            }
            if it.timestamp:
                entry["timestamp"] = it.timestamp
            if it.document_id:
                entry["document_id"] = it.document_id
            if it.tags:
                entry["tags"] = it.tags
            payload.append(entry)
        self._call(self.client.retain_batch, bank_id=self.bank_id, items=payload)

    def recall(self, query: str, limit: int = 8) -> list[MemoryItem]:
        self.ensure_bank()
        resp = self._call(self.client.recall, bank_id=self.bank_id, query=query, budget="mid", max_tokens=4096)
        out: list[MemoryItem] = []
        for r in (resp.results or [])[:limit]:
            occurred = r.occurred_start or r.mentioned_at
            metadata = {k: str(v) for k, v in (r.metadata or {}).items()}
            # Observations are consolidated by Hindsight and carry no retain metadata; recover the ID from the text.
            if "incident_id" not in metadata and (m := _INCIDENT_ID_RE.search(r.text or "")):
                metadata["incident_id"] = m.group(0)
            final = getattr(r.scores, "final", None)
            semantic = getattr(r.scores, "semantic", None)
            out.append(
                MemoryItem(
                    id=str(r.id),
                    text=r.text,
                    type=str(r.type or "experience"),
                    context=r.context,
                    metadata=metadata,
                    occurred_at=str(occurred) if occurred else None,
                    score=round(final, 4) if final is not None else None,
                    similarity=round(max(0.0, min(1.0, semantic)), 4) if semantic is not None else None,
                )
            )
        return out

    def reflect(self, query: str) -> str | None:
        self.ensure_bank()
        resp = self._call(self.client.reflect, bank_id=self.bank_id, query=query, budget="mid")
        return resp.text

    def reset(self) -> None:
        try:
            self._call(self.client.delete_bank, self.bank_id)
        except Exception as exc:
            log.warning("delete_bank failed: %s", exc)
        self._bank_ready = False
        self.ensure_bank()

    def count(self) -> int | None:
        try:
            resp = self._call(self.client.list_memories, bank_id=self.bank_id, limit=1)
            return getattr(resp, "total", None)
        except Exception as exc:
            log.warning("Hindsight list_memories failed: %s", exc)
            return None

    def close(self) -> None:
        try:
            self._call(self.client.close)
        except Exception:
            pass
        self._executor.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# Local offline fallback
# --------------------------------------------------------------------------- #
_STOP = set(
    "a an the and or of to in on for with at by from is are was were be been it this that "
    "as after before into over under per not no we our us i you they them his her its "
    "than then so if but when while has have had do does did 5m 1h".split()
)


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-\.]{1,}", text.lower())
    out = []
    for w in words:
        w = w.strip(".-")
        if len(w) > 1 and w not in _STOP:
            out.append(w)
    return out


class LocalMemory:
    """Tiny TF-IDF style store persisted to a JSON file."""

    name = "local"
    # Below this, keyword overlap is incidental (e.g. both alerts mention "errors").
    MIN_SCORE = 1.5

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._docs: list[dict[str, Any]] = []
        if path.exists():
            try:
                self._docs = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("Corrupt local memory file %s; starting empty", path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._docs, indent=2), encoding="utf-8")

    def ensure_bank(self) -> None:
        return None

    def retain(self, items: list[RetainItem]) -> None:
        with self._lock:
            for it in items:
                ts = it.timestamp or datetime.now(timezone.utc)
                self._docs.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "text": it.content,
                        "type": "observation" if it.context == "agent_feedback" else "experience",
                        "context": it.context,
                        "metadata": it.metadata,
                        "occurred_at": ts.isoformat(),
                    }
                )
            self._save()

    def recall(self, query: str, limit: int = 8) -> list[MemoryItem]:
        q = Counter(_tokens(query))
        if not q or not self._docs:
            return []
        doc_tokens = [Counter(_tokens(d["text"])) for d in self._docs]
        n = len(self._docs)
        df: Counter = Counter()
        for dt in doc_tokens:
            df.update(dt.keys())
        scored = []
        for d, dt in zip(self._docs, doc_tokens):
            s = 0.0
            for term, qc in q.items():
                if term in dt:
                    idf = math.log(1 + n / df[term])
                    s += qc * idf * (1 + math.log(dt[term]))
            if s > 0:
                s /= math.sqrt(sum(dt.values()))
                scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            MemoryItem(
                id=d["id"],
                text=d["text"],
                type=d.get("type", "experience"),
                context=d.get("context"),
                metadata=d.get("metadata", {}),
                occurred_at=d.get("occurred_at"),
                score=round(s, 3),
            )
            for s, d in scored[:limit]
            if s >= self.MIN_SCORE
        ]

    def reflect(self, query: str) -> str | None:
        return None  # the agent synthesises insights itself when reflect is unavailable

    def all_docs(self) -> list[dict[str, Any]]:
        return list(self._docs)

    def reset(self) -> None:
        with self._lock:
            self._docs = []
            self._save()

    def count(self) -> int | None:
        return len(self._docs)


def build_memory() -> MemoryStore:
    if settings.use_hindsight:
        return HindsightMemory(settings.hindsight_base_url, settings.hindsight_api_key, settings.hindsight_bank_id)
    return LocalMemory(DATA_DIR / "local_memory.json")
