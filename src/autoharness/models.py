"""Domain models shared by the AutoHarness core, CLI, and API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventKind(StrEnum):
    """Observable steps emitted by an agent runtime."""

    INPUT = "input"
    PLAN = "plan"
    RETRIEVAL = "retrieval"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    MEMORY = "memory"
    RESPONSE = "response"
    VALIDATION = "validation"
    FEEDBACK = "feedback"
    ERROR = "error"


class EventStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class FailureType(StrEnum):
    PLANNING = "planning_failure"
    RETRIEVAL = "retrieval_failure"
    TOOL = "tool_failure"
    MEMORY = "memory_failure"
    REASONING = "reasoning_failure"
    VALIDATION = "validation_failure"
    UNKNOWN = "unknown_failure"


class TraceEvent(BaseModel):
    """One structured observation from an agent execution."""

    model_config = ConfigDict(extra="allow")

    kind: EventKind
    name: str = ""
    status: EventStatus = EventStatus.UNKNOWN
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    input: Any | None = None
    output: Any | None = None
    error: str | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTrace(BaseModel):
    """A complete trace plus explicit user feedback and runtime logs."""

    model_config = ConfigDict(extra="allow")

    trace_id: str | None = None
    task: str
    events: list[TraceEvent] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    feedback: str | None = None
    expected: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task")
    @classmethod
    def task_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must not be blank")
        return value.strip()


class Evidence(BaseModel):
    source: str
    excerpt: str
    signal: str


class FailureDiagnosis(BaseModel):
    failure_type: FailureType
    confidence: float = Field(ge=0, le=1)
    summary: str
    likely_causes: list[str]
    evidence: list[Evidence]
    recommended_checks: list[str]
    search_terms: list[str]


class CodeLocation(BaseModel):
    path: str
    symbol: str
    kind: str
    line: int = Field(ge=1)
    score: float = Field(ge=0)
    reasons: list[str]


class AnalysisRequest(BaseModel):
    trace: AgentTrace
    repository_path: str | None = None
    candidate_limit: int = Field(default=8, ge=1, le=50)

    @field_validator("repository_path")
    @classmethod
    def normalize_repository_path(cls, value: str | None) -> str | None:
        return str(Path(value).expanduser().resolve()) if value else None


class AnalysisResult(BaseModel):
    diagnosis: FailureDiagnosis
    code_locations: list[CodeLocation] = Field(default_factory=list)
    indexed_files: int = 0
    indexed_symbols: int = 0


class RepairExperience(BaseModel):
    title: str
    failure_type: FailureType
    trigger_terms: list[str]
    root_cause: str
    repair_steps: list[str] = Field(min_length=1)
    validation_steps: list[str] = Field(min_length=1)
    affected_components: list[str] = Field(default_factory=list)
    success_metrics: dict[str, float] = Field(default_factory=dict)


class Skill(BaseModel):
    name: str
    description: str
    failure_type: FailureType
    triggers: list[str]
    context: dict[str, Any]
    workflow: list[str]
    evaluation: list[str]
    version: int = 1
