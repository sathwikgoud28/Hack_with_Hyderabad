"""Request / response schemas."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class IncidentIn(BaseModel):
    """What the engineer types: which service, what is wrong, the error, and optionally logs / recent changes."""

    service: str = Field(..., max_length=80)
    problem: str = Field(..., max_length=300)
    error: str = Field(..., max_length=2000)
    details: str = Field("", max_length=4000)

    @field_validator("service", "problem", "error")
    @classmethod
    def _required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("details")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class MatchedIncident(BaseModel):
    id: str
    why: str = ""


class Step(BaseModel):
    step: str
    source: str = "general"


class Avoid(BaseModel):
    action: str
    reason: str = ""


class Recommendation(BaseModel):
    summary: str = ""
    root_cause: str = "Unknown - gather more data."
    suggested_fix: str = ""
    evidence: list[str] = []
    steps: list[Step] = []
    avoid: list[Avoid] = []
    matched_incidents: list[MatchedIncident] = []
    confidence: Literal["low", "medium", "high"] = "low"
    estimated_time_to_resolve_min: int | None = None
    generated_by: str = "llm"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        v = str(v or "low").lower()
        return v if v in {"low", "medium", "high"} else "low"

    @field_validator("estimated_time_to_resolve_min", mode="before")
    @classmethod
    def _eta(cls, v):
        try:
            return int(float(v)) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v):
        # Models sometimes return objects instead of plain strings.
        if not isinstance(v, list):
            return []
        return [x if isinstance(x, str) else " - ".join(str(y) for y in x.values()) if isinstance(x, dict) else str(x) for x in v]


class FailIn(BaseModel):
    note: str = Field("", max_length=1000)


class ResolveIn(BaseModel):
    root_cause: str = Field(..., max_length=2000)
    solution: str = Field(..., max_length=2000)
    time_to_resolve_min: int | None = Field(None, ge=1, le=10000)

    @field_validator("root_cause", "solution")
    @classmethod
    def _required(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("please describe it in a few words")
        return v
