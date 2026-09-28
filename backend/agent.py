"""DejaFix incident agent.

Two modes, decided by what Hindsight remembers:
  known problem   -> recall the fix that worked before and recommend it
  unknown problem -> fresh investigation from the current error, logs and recent changes
A fix that fails is remembered and the agent investigates again; a resolved incident is
retained, so the next similar incident becomes a known problem.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .llm import LLM
from .memory import STRONG_MATCH_SCORE, MemoryItem, MemoryStore, RetainItem, is_strong_match
from .models import FailIn, IncidentIn, Recommendation, ResolveIn

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are DejaFix, an incident investigator for on-call engineers at an e-commerce company.
You get a live incident and, when available, memories of similar past incidents from long-term memory.
Find the most likely root cause and the single best fix to try now.

MODE "MEMORY" (similar past incidents were found):
- Recommend the fix that worked in the matching past incidents and cite their IDs (e.g. INC-1031) in
  `source`, `evidence` and `matched_incidents`.
- Anything that did NOT work before goes in `avoid`, never in `steps`.
- Base `estimated_time_to_resolve_min` on how long the matching incidents took.
- `root_cause` = the root cause confirmed in the matching incident, e.g. "Likely the same as INC-1031: ...".
  Still explain in `evidence` which current signals match that incident.

MODE "FRESH INVESTIGATION" (nothing similar in memory):
- Do not mention or invent past incidents; `matched_incidents` must be empty.
- Reason only from the current service, problem, error and logs/recent changes; `evidence` must point to those details.
- `root_cause` is a HYPOTHESIS, never a confirmed fact: phrase it as "Possible <cause>, based on <evidence>".
- Confidence is "low" or "medium".

Always:
- If fixes were already tried in THIS incident and failed, never suggest them again; move on to the next most likely cause.
- Be concrete (commands, config keys, dashboards) but short. `steps` = how to apply the fix, max 5.

Respond with ONLY a JSON object:
{
  "summary": "1-2 plain-English sentences for the engineer",
  "root_cause": "most likely root cause (a hypothesis in FRESH mode)",
  "suggested_fix": "the one fix to try now, in one sentence",
  "evidence": ["why you believe this, max 4 short points"],
  "steps": [{"step": "action", "source": "INC-xxxx, current-data or general"}],
  "avoid": [{"action": "what not to do", "reason": "why"}],
  "matched_incidents": [{"id": "INC-xxxx", "why": "why it matches"}],
  "confidence": "low" | "medium" | "high",
  "estimated_time_to_resolve_min": number or null
}"""

MATCH_PROMPT = """You decide whether a new production incident is the SAME kind of failure as past incidents.
Same kind means the same failure mechanism / root-cause pattern (for example "database connection pool exhausted
after a deploy"). Sharing only the service, the business area (payments), or a symptom word like "timeout" or
"slow" is NOT enough.
Judge from what the new incident states: symptoms that point to the same mechanism count, but do not invent details
the new incident does not mention (for example, do not assume a key rotation just because a token is invalid).
Respond with ONLY JSON: {"same_as": ["INC-xxxx"], "reason": "one short sentence"} - an empty list if none match."""

INSIGHTS_SECTIONS = [
    "Recurring incident patterns",
    "Most affected services",
    "Common triggers",
    "Successful fixes",
    "Failed fixes / ineffective responses",
    "Preventive actions",
]
INSIGHTS_QUESTION = (
    "Analyse all past incidents and failed fixes in memory for an SRE team. Answer in Markdown using exactly these "
    "six '## ' headings, in this order: "
    + ", ".join(f"'## {s}'" for s in INSIGHTS_SECTIONS)
    + ". Under each heading give 2-5 short bullet points that cite incident IDs. "
    "Only state what the memories support."
)

# Fresh-investigation checklist used when no LLM is available.
GENERIC_STEPS = [
    "Check what changed in the last 2 hours: deploys, feature flags, config and infrastructure changes.",
    "Open the service dashboard: error rate, latency, saturation (CPU, memory, connections).",
    "Read the most recent error logs and group them by exception type.",
    "If a deploy happened recently and errors started after it, roll back.",
    "Check downstream dependencies (database, cache, queues, third-party APIs).",
    "Escalate to the owning team if not mitigated within 30 minutes.",
]


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def postmortem_text(inc: dict[str, Any]) -> str:
    failed = inc.get("failed_attempts") or []
    failed_txt = "; ".join(failed) if failed else "none recorded"
    date = str(inc.get("date", ""))[:10]
    severity = f" ({inc['severity']})" if inc.get("severity") else ""
    lines = [
        f"Incident {inc['id']}{severity} on service {inc['service']}, {date}: {inc['title']}.",
        f"Alert/symptoms: {inc['alert']}",
        f"Root cause: {inc['root_cause']}",
    ]
    if inc.get("trigger"):
        lines.append(f"Trigger: {inc['trigger']}.")
    lines += [f"Fix that worked: {inc['fix']}", f"Things that did NOT work: {failed_txt}."]
    tail = f"Time to resolve: {inc['time_to_resolve_min']} minutes." if inc.get("time_to_resolve_min") else ""
    if inc.get("responder"):
        tail += f" Responder: {inc['responder']}."
    if tail.strip():
        lines.append(tail.strip())
    return "\n".join(lines)


def incident_to_retain_item(inc: dict[str, Any]) -> RetainItem:
    ts = None
    if inc.get("date"):
        ts = datetime.fromisoformat(str(inc["date"]).replace("Z", "+00:00"))
    metadata = {"incident_id": inc["id"], "service": inc["service"]}
    for key in ("severity", "trigger"):
        if inc.get(key):
            metadata[key] = inc[key]
    if inc.get("time_to_resolve_min"):
        metadata["time_to_resolve_min"] = str(inc["time_to_resolve_min"])
    # Structured summary, so a recalled memory can be shown as "previous problem / solution / outcome"
    # even on a machine whose local incident log doesn't have this incident.
    metadata.update(
        {
            "problem": _clip(inc["title"]),
            "error": _clip(inc["alert"]),
            "root_cause": _clip(inc["root_cause"]),
            "solution": _clip(inc["fix"]),
            "outcome": "resolved",
        }
    )
    return RetainItem(
        content=postmortem_text(inc),
        context="incident_postmortem",
        metadata=metadata,
        timestamp=ts,
        document_id=inc["id"],
        tags=[f"service:{inc['service']}"],
    )


def _format_memories(memories: list[MemoryItem]) -> str:
    if not memories:
        return "(no similar past incidents in memory)"
    lines = []
    for i, m in enumerate(memories, 1):
        inc = m.metadata.get("incident_id", "?")
        when = (m.occurred_at or "")[:10]
        score = f", relevance {m.score:.3f}" if m.score is not None else ""
        lines.append(f"[{i}] ({m.context or m.type}, {inc}, {when}{score}) {m.text}")
    return "\n".join(lines)


def _query(inp: IncidentIn) -> str:
    return f"{inp.service}: {inp.problem}. Error: {inp.error}. {inp.details}".strip()


def _key(text: str) -> str:
    return text.lower().strip()[:50]


_FIX_RE = re.compile(r"\b(resolved by|fixed by|was resolved|mitigated by)\b", re.I)
_FAIL_RE = re.compile(r"\b(fail(ed|ing)? to|did not|didn't|ineffective|made (it|things) worse|wasted|only delayed|temporary relief)\b", re.I)


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"{label}:\s*(.+)", text)
    return m.group(1).strip().rstrip(".") if m else None


def _clip(value: Any, n: int = 300) -> str:
    text = str(value or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# Matching signals: why a recalled incident counts as "the same problem"
# --------------------------------------------------------------------------- #
# Error codes like ORDER_QUEUE_TIMEOUT or exception names like MismatchedInputException.
_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b|\b[A-Z][A-Za-z0-9]+(?:Exception|Error)\b")
_DEPLOY_RE = re.compile(r"\b(deploy\w*|releas\w*|rollout|rolled out|roll(?:ed)?[ -]?back|v\d+\.\d+)", re.I)
_SYMPTOMS = [
    (r"time[ds]? ?out|timed out", "timeouts"),
    (r"\bqueue", "queue processing"),
    (r"latency|\bslow", "slow responses"),
    (r"\b5\d\d\b|\b5xx\b", "5xx errors"),
    (r"connection pool|hikari|\bpool\b", "connection pool"),
    (r"\boom|out of memory|memory limit", "memory exhaustion"),
    (r"\bdisk\b|no space", "disk space"),
    (r"certificate|\btls\b|x509", "certificates"),
    (r"\b401\b|unauthori[sz]ed|\blog ?in\b|\btoken", "authentication"),
    (r"rate limit|\b429\b|too many requests", "rate limiting"),
    (r"\bconsumer|\bkafka\b|\boffset", "message consumers"),
    (r"deseriali[sz]|poison|malformed", "bad messages"),
    (r"\bredis\b|\bcache|evict", "cache"),
    (r"\bdns\b|no such host", "DNS"),
    (r"\bstuck\b|\bpending\b", "stuck work"),
]


def has_concrete_signal(signals: list[str]) -> bool:
    """A recalled incident only counts as the same problem if something checkable matches:
    the same service, the same error code, or at least two shared kinds of symptom."""
    for s in signals:
        if s.startswith(("Same service", "Same error")):
            return True
        if s.startswith("Similar symptoms:") and len(s.split(":", 1)[1].split(",")) >= 2:
            return True
    return False


def is_same_service_and_error(signals: list[str]) -> bool:
    return any(s.startswith("Same service") for s in signals) and any(s.startswith("Same error") for s in signals)


def _norm_service(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _same_error_text(current: str, previous: str | None) -> bool:
    a = " ".join(re.findall(r"[a-z0-9]+", current.lower()))
    b = " ".join(re.findall(r"[a-z0-9]+", (previous or "").lower()))
    return len(a) >= 10 and len(b) >= 10 and (a == b or a in b or b in a)


def matching_signals(inp: IncidentIn, prev_service: str | None, prev_text: str, prev_error: str | None = None) -> list[str]:
    """Concrete, checkable reasons the current incident resembles a past one (only true ones are returned)."""
    cur_text = f"{inp.problem} {inp.error} {inp.details}"
    signals = []
    if prev_service and _norm_service(prev_service) == _norm_service(inp.service):
        signals.append(f"Same service ({inp.service})")
    codes = set(_CODE_RE.findall(f"{inp.problem} {inp.error}")) & set(_CODE_RE.findall(prev_text))
    if codes:
        signals.append("Same error (" + ", ".join(sorted(codes)) + ")")
    elif _same_error_text(inp.error, prev_error):
        signals.append(f'Same error message ("{_clip(inp.error, 60)}")')
    if _DEPLOY_RE.search(cur_text) and _DEPLOY_RE.search(prev_text):
        signals.append("Both happened around a deployment / release")
    shared = [label for pattern, label in _SYMPTOMS if re.search(pattern, cur_text, re.I) and re.search(pattern, prev_text, re.I)]
    if shared:
        signals.append("Similar symptoms: " + ", ".join(dict.fromkeys(shared[:4])))
    return signals


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Incident log (the app's own record of live incidents)
# --------------------------------------------------------------------------- #
class IncidentLog:
    def __init__(self, path: Path, first_number: int = 2001):
        self.path = path
        self._lock = threading.Lock()
        self._first = first_number
        self.items: list[dict[str, Any]] = []
        if path.exists():
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
                self.items = [i for i in items if "input" in i]
                if len(self.items) != len(items):
                    log.warning("Ignored %d incidents saved by an older version", len(items) - len(self.items))
            except json.JSONDecodeError:
                self.items = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2), encoding="utf-8")

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record["id"] = f"INC-{self._first + len(self.items)}"
            self.items.append(record)
            self._save()
            return record

    def get(self, incident_id: str) -> dict[str, Any] | None:
        return next((i for i in self.items if i["id"] == incident_id), None)

    def update(self, incident_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            rec = self.get(incident_id)
            if rec is None:
                raise KeyError(incident_id)
            rec.update(changes)
            self._save()
            return rec

    def clear(self) -> None:
        with self._lock:
            self.items = []
            self._save()


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class IncidentAgent:
    def __init__(self, memory: MemoryStore, llm: LLM, incident_log: IncidentLog, seed: list[dict[str, Any]] | None = None):
        self.memory = memory
        self.llm = llm
        self.log = incident_log
        self.seed = seed or []  # historical postmortems (the same ones loaded into Hindsight)

    # ---- recall ----------------------------------------------------------- #
    def recall(self, inp: IncidentIn, exclude_id: str | None = None) -> tuple[list[MemoryItem], str | None]:
        """Returns (memories, error). On failure the investigation still runs, but the error is shown to the user."""
        try:
            memories = self.memory.recall(_query(inp), limit=10)
        except Exception as exc:
            log.exception("Memory recall failed")
            return [], f"{type(exc).__name__}: {exc}"[:300]
        if exclude_id:  # the incident's own failed-fix memories are passed separately
            memories = [m for m in memories if m.metadata.get("incident_id") != exclude_id]
        return memories, None

    def same_failure(self, inp: IncidentIn, memories: list[MemoryItem]) -> tuple[list[MemoryItem], str, list[str]]:
        """Keep only memories of past incidents that are the same kind of failure.

        Returns (relevant memories, the check's reason, IDs of look-alike incidents that were rejected).
        Hindsight's score alone can't tell "payment gateway timeout" from "payments DB pool timeout" (both score
        ~0.3), so candidates that pass the score gate are confirmed by the LLM. Without an LLM the score gate decides.
        """
        if not memories or not is_strong_match(memories):
            return [], "", []
        by_id: dict[str, list[str]] = {}
        for m in memories:
            if iid := m.metadata.get("incident_id"):
                by_id.setdefault(iid, []).append(m.text.split(" | ")[0])
        if not by_id:
            return [], "", []
        candidates = "\n".join(f"{iid}: " + " / ".join(t[:220] for t in texts[:3]) for iid, texts in by_id.items())
        raw = self.llm.complete_json(MATCH_PROMPT, f"NEW INCIDENT\n{_query(inp)}\n\nPAST INCIDENTS\n{candidates}")
        if raw is None or not isinstance(raw.get("same_as"), list):
            strong_ids = {m.metadata.get("incident_id") for m in memories if m.score is None or m.score >= STRONG_MATCH_SCORE}
            return [m for m in memories if m.metadata.get("incident_id") in strong_ids], "", []
        same = {str(x).strip() for x in raw["same_as"]}
        reason = _clip(raw.get("reason"), 300)
        return [m for m in memories if m.metadata.get("incident_id") in same], reason, [i for i in by_id if i not in same]

    # ---- recommend -------------------------------------------------------- #
    def recommend(self, inp: IncidentIn, memories: list[MemoryItem], failed: list[dict[str, str]], mode: str) -> Recommendation:
        raw = self.llm.complete_json(SYSTEM_PROMPT, self._prompt(inp, memories, failed, mode))
        if raw is not None:
            try:
                rec = Recommendation.model_validate(raw)
                rec.generated_by = "llm"
                if mode == "fresh":
                    rec.matched_incidents = []
                    if rec.confidence == "high":
                        rec.confidence = "medium"
                if not rec.suggested_fix and rec.steps:
                    rec.suggested_fix = rec.steps[0].step
                return rec
            except Exception as exc:
                log.warning("LLM recommendation failed validation, using rule-based one: %s", exc)
        return self.rule_based(inp, memories if mode == "memory" else [], failed)

    @staticmethod
    def _prompt(inp: IncidentIn, memories: list[MemoryItem], failed: list[dict[str, str]], mode: str) -> str:
        parts = [
            f"MODE: {'MEMORY' if mode == 'memory' else 'FRESH INVESTIGATION'}",
            "",
            "LIVE INCIDENT",
            f"Service: {inp.service}",
            f"Problem: {inp.problem}",
            f"Error: {inp.error}",
            f"Logs / recent changes: {inp.details or '(none provided)'}",
        ]
        if failed:
            parts += ["", "ALREADY TRIED IN THIS INCIDENT - DID NOT WORK (do not suggest again):"]
            parts += [f"- {f['fix']}" + (f" (engineer: {f['note']})" if f.get("note") else "") for f in failed]
        parts += ["", "MEMORIES FROM PAST INCIDENTS", _format_memories(memories if mode == "memory" else [])]
        return "\n".join(parts)

    @staticmethod
    def rule_based(inp: IncidentIn, memories: list[MemoryItem], failed: list[dict[str, str]]) -> Recommendation:
        """Deterministic recommendation used when no LLM is configured or all LLM calls fail.

        Understands both full postmortems (local store) and the atomic facts Hindsight extracts from them.
        """
        tried = {_key(f["fix"]) for f in failed}
        already_failed = [{"action": f["fix"], "reason": "Already tried in this incident - did not work"} for f in failed]
        incidents = [m for m in memories if m.metadata.get("incident_id")]

        if not incidents or not is_strong_match(memories):
            steps = [s for s in GENERIC_STEPS if _key(s) not in tried]
            evidence = [f"Error reported: {inp.error}", f"Problem: {inp.problem} on {inp.service}"]
            if inp.details:
                evidence.append(f"Logs / recent changes: {inp.details[:200]}")
            return Recommendation(
                summary="No similar incident in memory, so this is a fresh investigation based on the current error.",
                root_cause="Unknown - not enough data yet; follow the checklist to narrow it down.",
                suggested_fix=steps[0] if steps else "Escalate to the owning team.",
                evidence=evidence,
                steps=[{"step": s, "source": "general"} for s in steps[:5]],
                avoid=already_failed,
                confidence="low",
                generated_by="rules",
            )

        strong = [m for m in incidents if m.score is None or m.score >= STRONG_MATCH_SCORE] or incidents
        strong_ids = list(dict.fromkeys(m.metadata["incident_id"] for m in strong))
        related = [m for m in incidents if m.metadata["incident_id"] in strong_ids]

        fixes: list[tuple[str, str]] = []
        avoid: list[dict[str, str]] = []
        root_causes: dict[str, str] = {}
        seen: set[str] = set()

        def first_time(text: str) -> bool:
            if _key(text) in seen:
                return False
            seen.add(_key(text))
            return True

        for m in related:
            iid = m.metadata["incident_id"]
            text = m.text.split(" | ")[0].strip()  # drop Hindsight's "| When: ... | Involving: ..." suffix
            failed_txt = _field(m.text, "Things that did NOT work")
            if fix := _field(m.text, "Fix that worked"):
                if first_time(fix):
                    fixes.append((fix, iid))
            elif _FIX_RE.search(text) and not _FAIL_RE.search(text) and first_time(text):
                fixes.append((text, iid))
            if failed_txt and failed_txt != "none recorded":
                for f in (x.strip() for x in failed_txt.split(";")):
                    if f and first_time(f):
                        avoid.append({"action": f, "reason": f"Did not work during {iid}"})
            elif failed_txt is None and _FAIL_RE.search(text) and first_time(text):
                avoid.append({"action": text, "reason": f"Did not work during {iid}"})
            root_causes.setdefault(iid, _field(m.text, "Root cause") or text)

        fixes = [(f, i) for f, i in fixes if _key(f) not in tried]
        times = [int(m.metadata["time_to_resolve_min"]) for m in related if m.metadata.get("time_to_resolve_min", "").isdigit()]
        top_id = strong_ids[0]
        return Recommendation(
            summary=f"Seen before: this looks like {top_id}. Start with the fix that worked then.",
            root_cause=root_causes.get(top_id, "See the matching incidents."),
            suggested_fix=fixes[0][0] if fixes else "Every fix from memory has been tried - check recent changes and escalate.",
            evidence=[f"{i}: {root_causes.get(i, '')[:160]}" for i in strong_ids[:3]],
            steps=[{"step": f, "source": i} for f, i in fixes[:5]],
            avoid=(already_failed + avoid)[:5],
            matched_incidents=[{"id": i, "why": root_causes.get(i, "")[:180]} for i in strong_ids[:4]],
            confidence="high" if len(strong_ids) >= 2 else "medium",
            estimated_time_to_resolve_min=min(times) if times else None,
            generated_by="rules",
        )

    # ---- incident lifecycle ---------------------------------------------- #
    def _attempt(self, inp: IncidentIn, failed: list[dict[str, str]], n: int, exclude_id: str | None = None) -> dict[str, Any]:
        started_at = _now().isoformat()
        memories, memory_error = self.recall(inp, exclude_id)
        relevant, match_reason, judged_different = self.same_failure(inp, memories)
        # Same service + same error is the same problem even when the LLM check hesitates on a terse report.
        if judged_different:
            llm_matched_any = bool(relevant)
            candidates = [m for m in memories if m.metadata.get("incident_id") in judged_different]
            restored = [c["id"] for c in self.matched_incidents(inp, candidates) if is_same_service_and_error(c["signals"])]
            if restored:
                relevant += [m for m in candidates if m.metadata.get("incident_id") in restored]
                judged_different = [i for i in judged_different if i not in restored]
                if not llm_matched_any:
                    match_reason = f"Same service and same error as {', '.join(restored)}"
        matched = self.matched_incidents(inp, relevant)
        # Guard against over-matching: the LLM check can link a vague incident to a past one by assuming a cause.
        no_signal = [c["id"] for c in matched if not has_concrete_signal(c["signals"])]
        if no_signal:
            relevant = [m for m in relevant if m.metadata.get("incident_id") not in no_signal]
            matched = [c for c in matched if c["id"] not in no_signal]
        searched_at = _now().isoformat()
        mode = "memory" if relevant else "fresh"
        if mode == "fresh":
            match_reason = match_reason if not no_signal else ""
        rec = self.recommend(inp, relevant, failed, mode)
        best_similarity: dict[str, float] = {}
        for m in memories:
            iid = m.metadata.get("incident_id")
            if m.similarity is not None:
                best_similarity[iid] = max(best_similarity.get(iid, 0.0), m.similarity)
        rejected = [
            {"id": i, "similarity": best_similarity.get(i), "reason": "judged a different failure by the relevance check"}
            for i in judged_different
        ] + [
            {"id": i, "similarity": best_similarity.get(i), "reason": "no concrete matching signal (different service, different error)"}
            for i in no_signal
        ]
        return {
            "n": n,
            "mode": mode,
            "memories": [m.to_dict() for m in relevant],
            "matched": matched,
            "match_reason": match_reason,
            "rejected": rejected,
            "memory_error": memory_error,
            "recommendation": rec.model_dump(),
            # A fresh investigation's root cause is a hypothesis until the engineer confirms the fix worked.
            "root_cause_status": "historical" if mode == "memory" else "hypothesis",
            "outcome": None,
            "note": "",
            "started_at": started_at,
            "searched_at": searched_at,
            "recommended_at": _now().isoformat(),
        }

    def investigate(self, inp: IncidentIn) -> dict[str, Any]:
        detected_at = _now().isoformat()
        attempt = self._attempt(inp, failed=[], n=1)
        return self.log.create(
            {
                "created_at": detected_at,
                "detected_at": detected_at,
                "status": "investigating",
                "input": inp.model_dump(),
                "attempts": [attempt],
                "memory_backend": self.memory.name,
            }
        )

    def _open(self, incident_id: str) -> dict[str, Any]:
        rec = self.log.get(incident_id)
        if rec is None:
            raise KeyError(incident_id)
        if rec["status"] != "investigating":
            raise ValueError(f"{incident_id} is already resolved")
        return rec

    @staticmethod
    def _failed(rec: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"fix": a["recommendation"]["suggested_fix"], "note": a.get("note", "")}
            for a in rec["attempts"]
            if a["outcome"] == "failed"
        ]

    def fail(self, incident_id: str, body: FailIn) -> dict[str, Any]:
        """The suggested fix did not work (or was rejected): remember that, then investigate again."""
        rec = self._open(incident_id)
        inp = IncidentIn(**rec["input"])
        current = rec["attempts"][-1]
        fix = current["recommendation"]["suggested_fix"]
        note = body.note.strip()
        outcome_at = _now()
        # Retain first: if memory is unreachable nothing changes and the user can retry.
        self.memory.retain(
            [
                RetainItem(
                    content=(
                        f"Failed fix during incident {incident_id} on service {inp.service} ({inp.problem}; error: {inp.error}): "
                        f"tried '{fix}' and it did NOT work." + (f" Engineer note: {note}" if note else "")
                    ),
                    context="failed_fix",
                    metadata={
                        "incident_id": incident_id,
                        "service": inp.service,
                        "problem": _clip(inp.problem),
                        "error": _clip(inp.error),
                        "failed_fix": _clip(fix),
                        "outcome": "failed",
                    },
                    timestamp=outcome_at,
                    document_id=f"{incident_id}-failed-{current['n']}",
                    tags=[f"service:{inp.service}"],
                )
            ]
        )
        current["outcome"] = "failed"
        current["note"] = note
        current["outcome_at"] = outcome_at.isoformat()
        current["failed_saved_at"] = _now().isoformat()
        next_attempt = self._attempt(inp, self._failed(rec), n=current["n"] + 1, exclude_id=incident_id)
        return self.log.update(incident_id, attempts=rec["attempts"] + [next_attempt])

    def resolve(self, incident_id: str, body: ResolveIn) -> dict[str, Any]:
        """The fix worked: store what happened in Hindsight so the next similar incident is a known problem."""
        rec = self._open(incident_id)
        inp = IncidentIn(**rec["input"])
        now = _now()
        didnt_work = [f["fix"] + (f" ({f['note']})" if f["note"] else "") for f in self._failed(rec)]
        minutes = body.time_to_resolve_min
        self.memory.retain(
            [
                incident_to_retain_item(
                    {
                        "id": incident_id,
                        "date": now.isoformat(),
                        "service": inp.service,
                        "title": inp.problem,
                        "alert": inp.error + (f" | {inp.details}" if inp.details else ""),
                        "root_cause": body.root_cause,
                        "fix": body.solution,
                        "failed_attempts": didnt_work,
                        "time_to_resolve_min": minutes,
                    }
                )
            ]
        )
        saved_at = _now()
        rec["attempts"][-1]["outcome"] = "worked"
        rec["attempts"][-1]["outcome_at"] = now.isoformat()
        new_memory = {
            "problem": f"{inp.problem} ({inp.error})",
            "service": inp.service,
            "root_cause": body.root_cause,
            "solution": body.solution,
            "didnt_work": didnt_work,
            "outcome": "Resolved" + (f" in {minutes} min" if minutes else ""),
        }
        return self.log.update(
            incident_id,
            status="resolved",
            resolved_at=now.isoformat(),
            attempts=rec["attempts"],
            resolution={**body.model_dump(), "new_memory": new_memory, "saved_at": saved_at.isoformat()},
        )

    # ---- memory context shown to the engineer ------------------------------ #
    def _incident_facts(self, incident_id: str) -> dict[str, Any]:
        """The recorded details of a past incident: resolved in this app, or a historical postmortem."""
        rec = self.log.get(incident_id)
        if rec and rec["status"] == "resolved":
            res = rec["resolution"]
            return {
                "service": rec["input"]["service"],
                "problem": rec["input"]["problem"],
                "error": rec["input"]["error"],
                "details": rec["input"].get("details", ""),
                "root_cause": res["root_cause"],
                "solution": res["solution"],
                "minutes": res.get("time_to_resolve_min"),
                "didnt_work": res.get("new_memory", {}).get("didnt_work", []),
                "date": rec.get("resolved_at"),
                "resolved": True,
            }
        s = next((x for x in self.seed if x["id"] == incident_id), None)
        if s:
            return {
                "service": s["service"],
                "problem": s["title"],
                "error": s["alert"],
                "details": "",
                "root_cause": s["root_cause"],
                "solution": s["fix"],
                "minutes": s.get("time_to_resolve_min"),
                "didnt_work": s.get("failed_attempts", []),
                "date": s["date"],
                "resolved": True,
            }
        return {}

    def matched_incidents(self, inp: IncidentIn, memories: list[MemoryItem]) -> list[dict[str, Any]]:
        """One card per past incident that Hindsight recalled, in relevance order, built only from real data:
        the recalled memories themselves, their retain metadata, and the incident's own record."""
        groups: dict[str, list[MemoryItem]] = {}
        for m in memories:
            if iid := m.metadata.get("incident_id"):
                groups.setdefault(iid, []).append(m)
        cards = []
        for iid, mems in groups.items():
            rec = self._incident_facts(iid)
            meta = next((m.metadata for m in mems if m.metadata.get("problem") or m.metadata.get("solution")), {})

            def pick(key: str, meta_key: str | None = None) -> Any:
                return rec.get(key) or meta.get(meta_key or key) or None

            minutes = pick("minutes", "time_to_resolve_min")
            try:
                minutes = int(minutes) if minutes not in (None, "") else None
            except (TypeError, ValueError):
                minutes = None
            if rec.get("resolved") or meta.get("outcome") == "resolved" or any(m.context == "incident_postmortem" for m in mems):
                outcome = {"status": "resolved", "minutes": minutes}
            elif all(m.context == "failed_fix" for m in mems):
                outcome = {"status": "failed_fix", "minutes": None}
            else:
                outcome = {"status": "unknown", "minutes": None}
            facts = list(dict.fromkeys(m.text.split(" | ")[0].strip() for m in mems))[:4]
            service = pick("service") or next((m.metadata.get("service") for m in mems if m.metadata.get("service")), None)
            prev_text = " ".join(str(x) for x in [pick("problem"), pick("error"), pick("root_cause"), pick("details"), *facts] if x)
            similarities = [m.similarity for m in mems if m.similarity is not None]
            cards.append(
                {
                    "id": iid,
                    "service": service,
                    "date": rec.get("date") or mems[0].occurred_at,
                    "similarity": max(similarities) if similarities else None,
                    "problem": pick("problem"),
                    "error": pick("error"),
                    "root_cause": pick("root_cause"),
                    "solution": pick("solution"),
                    "didnt_work": rec.get("didnt_work") or [],
                    "outcome": outcome,
                    "facts": facts,
                    "memories_recalled": len(mems),
                    "signals": matching_signals(inp, service, prev_text, pick("error")),
                }
            )
        return cards

    def view(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Fill in display fields for incidents saved before those fields existed. Nothing is persisted."""
        if all("matched" in a for a in rec["attempts"]):
            return rec
        out = copy.deepcopy(rec)
        inp = IncidentIn(**out["input"])
        for a in out["attempts"]:
            if "matched" not in a:
                memories = [MemoryItem.from_dict(m) for m in a["memories"]] if a["mode"] == "memory" else []
                a["matched"] = self.matched_incidents(inp, memories)
                a.setdefault("match_reason", "")
                a.setdefault("rejected", [])
                a.setdefault("root_cause_status", "historical" if a["mode"] == "memory" else "hypothesis")
        return out

    # ---- history & insights ---------------------------------------------- #
    def history(self, seed: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Resolved incidents from the app plus the historical postmortems, newest first."""
        seed = self.seed if seed is None else seed
        out = []
        for r in self.log.items:
            if r["status"] != "resolved":
                continue
            res = r["resolution"]
            out.append(
                {
                    "id": r["id"],
                    "date": r["resolved_at"],
                    "service": r["input"]["service"],
                    "problem": r["input"]["problem"],
                    "root_cause": res["root_cause"],
                    "solution": res["solution"],
                    "didnt_work": res["new_memory"]["didnt_work"],
                    "minutes": res.get("time_to_resolve_min"),
                    "source": "dejafix",
                }
            )
        for s in seed:
            out.append(
                {
                    "id": s["id"],
                    "date": s["date"],
                    "service": s["service"],
                    "problem": s["title"],
                    "root_cause": s["root_cause"],
                    "solution": s["fix"],
                    "didnt_work": s.get("failed_attempts", []),
                    "minutes": s.get("time_to_resolve_min"),
                    "source": "history",
                }
            )
        return sorted(out, key=lambda x: str(x["date"]), reverse=True)

    def summary(self) -> dict[str, Any]:
        """Numbers and lists for the Memory insights page, computed from real data (no LLM involved)."""
        history = self.history()
        apps = self.log.items
        rows = {h["id"]: h for h in history}
        for a in apps:  # active incidents take part in patterns too
            rows.setdefault(
                a["id"],
                {"id": a["id"], "date": a["created_at"], "service": a["input"]["service"], "problem": a["input"]["problem"], "solution": None},
            )

        # Recurring patterns = groups of incidents that are the same failure. Links come from real data only:
        # historical postmortems with the same service and trigger, and incidents where Hindsight recalled
        # a past incident as the same failure.
        parent = {i: i for i in rows}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            if a in parent and b in parent:
                parent[find(a)] = find(b)

        by_key: dict[tuple[str, str], list[str]] = {}
        for s in self.seed:
            if s.get("trigger"):
                by_key.setdefault((s["service"], s["trigger"]), []).append(s["id"])
        for ids in by_key.values():
            for other in ids[1:]:
                union(ids[0], other)
        recognised = 0
        for a in apps:
            first = a["attempts"][0]
            if first["mode"] == "memory":
                recognised += 1
                for m in first["memories"]:
                    if (iid := m["metadata"].get("incident_id")) and iid != a["id"]:
                        union(a["id"], iid)

        groups: dict[str, list[str]] = {}
        for i in rows:
            groups.setdefault(find(i), []).append(i)
        patterns = []
        for members in groups.values():
            if len(members) < 2:
                continue
            items = sorted((rows[i] for i in members), key=lambda r: str(r["date"]))
            solved = [r for r in items if r.get("solution")]
            patterns.append(
                {
                    "count": len(items),
                    "incidents": [r["id"] for r in items],
                    "services": sorted({r["service"] for r in items}),
                    "problem": items[-1]["problem"],
                    "fix": solved[-1]["solution"] if solved else None,
                    "fix_from": solved[-1]["id"] if solved else None,
                }
            )
        patterns.sort(key=lambda p: (-p["count"], p["incidents"][-1]), reverse=False)

        failed = [{"id": h["id"], "action": f} for h in history for f in h.get("didnt_work") or []]
        ineffective = Counter()
        for f in failed:
            text = f["action"].lower()
            for label, words in (
                ("Restarting pods / services / consumers", ("restart",)),
                ("Scaling up replicas or consumers", ("scal", "more consumer", "replicas")),
                ("Raising pool sizes or memory limits", ("pool", "memory limit", "maximumpoolsize")),
                ("Clearing caches or deleting data", ("cache", "deleting", "flush")),
                ("Rolling back when nothing was deployed", ("rolling back", "roll back", "rollback")),
            ):
                if any(w in text for w in words):
                    ineffective[label] += 1
        new_learned = [a for a in apps if a["status"] == "resolved" and a["attempts"][0]["mode"] == "fresh"]

        return {
            "cards": {
                "total_memories": self.memory.count(),
                "resolved_incidents": len(history),
                "recurring_patterns": len(patterns),
                "recognised_by_memory": recognised,
                "new_problems_learned": len(new_learned),
            },
            "patterns": patterns[:8],
            "services": Counter(r["service"] for r in rows.values()).most_common(6),
            "triggers": Counter(s["trigger"] for s in self.seed if s.get("trigger")).most_common(6),
            "successful_fixes": [
                {"id": h["id"], "service": h["service"], "problem": h["problem"], "solution": h["solution"], "minutes": h.get("minutes"), "source": h["source"]}
                for h in history[:6]
            ],
            "failed_fixes": failed[:8],
            "ineffective": ineffective.most_common(),
            "progression": self._progression(apps),
        }

    def _progression(self, apps: list[dict[str, Any]]) -> dict[str, Any] | None:
        """The newest real example of learning: an unknown incident resolved in this app, then recalled for a later one."""
        by_id = {a["id"]: a for a in apps}
        for later in reversed(apps):
            first = later["attempts"][0]
            if first["mode"] != "memory":
                continue
            for m in first["memories"]:
                earlier = by_id.get(m["metadata"].get("incident_id"))
                if earlier and earlier["status"] == "resolved" and earlier["attempts"][0]["mode"] == "fresh":
                    sims = [x.get("similarity") for x in first["memories"] if x["metadata"].get("incident_id") == earlier["id"] and x.get("similarity") is not None]
                    return {
                        "first": {
                            "id": earlier["id"],
                            "service": earlier["input"]["service"],
                            "problem": earlier["input"]["problem"],
                            "error": earlier["input"]["error"],
                            "attempts": len(earlier["attempts"]),
                            "confidence": earlier["attempts"][0]["recommendation"]["confidence"],
                            "solution": earlier["resolution"]["solution"],
                            "minutes": earlier["resolution"].get("time_to_resolve_min"),
                            "resolved_at": earlier.get("resolved_at"),
                        },
                        "second": {
                            "id": later["id"],
                            "problem": later["input"]["problem"],
                            "error": later["input"]["error"],
                            "created_at": later["created_at"],
                            "confidence": first["recommendation"]["confidence"],
                            "suggested_fix": first["recommendation"]["suggested_fix"],
                            "similarity": max(sims) if sims else None,
                        },
                    }
        return None

    def insights(self) -> dict[str, Any]:
        try:
            text = self.memory.reflect(INSIGHTS_QUESTION)
            if text:
                return {"source": "hindsight_reflect", "text": text}
        except Exception as exc:
            log.warning("Hindsight reflect failed: %s", exc)

        docs = getattr(self.memory, "all_docs", lambda: [])()
        if docs and self.llm.available:
            corpus = "\n\n".join(d["text"] for d in docs[-60:])
            text = self.llm.complete_text(
                "You analyse incident history for an SRE team. Be concise, use short bullet lists.",
                f"{INSIGHTS_QUESTION}\n\nINCIDENT MEMORY:\n{corpus}",
            )
            if text:
                return {"source": "llm_over_memory", "text": text}
        return {"source": "stats", "text": self._stats_summary(docs)}

    @staticmethod
    def _stats_summary(docs: list[dict[str, Any]]) -> str:
        pm = [d for d in docs if d.get("context") == "incident_postmortem"]
        if not pm:
            return "No incidents in memory yet."
        by_service: dict[str, int] = {}
        by_trigger: dict[str, int] = {}
        restarts_failed = 0
        for d in pm:
            md = d.get("metadata", {})
            by_service[md.get("service", "?")] = by_service.get(md.get("service", "?"), 0) + 1
            by_trigger[md.get("trigger", "unknown")] = by_trigger.get(md.get("trigger", "unknown"), 0) + 1
            failed = _field(d["text"], "Things that did NOT work") or ""
            if "restart" in failed.lower():
                restarts_failed += 1
        svc = ", ".join(f"{k} ({v})" for k, v in sorted(by_service.items(), key=lambda x: -x[1])[:5])
        trg = ", ".join(f"{k} ({v})" for k, v in sorted(by_trigger.items(), key=lambda x: -x[1]))
        return (
            f"- {len(pm)} incidents in memory.\n"
            f"- Most incident-prone services: {svc}.\n"
            f"- Triggers: {trg}.\n"
            f"- Restarting pods was tried and failed in {restarts_failed} incidents; it rarely fixes the root cause."
        )
