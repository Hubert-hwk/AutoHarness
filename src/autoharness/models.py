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


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    PROMOTED = "promoted"
    LEARNED = "learned"
    FAILED = "failed"


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


class EvaluationSnapshot(BaseModel):
    """Observed metrics and tests for one harness or repair candidate."""

    run_id: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    tests_passed: bool = True
    failed_tests: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricRule(BaseModel):
    """Acceptance rule for one metric.

    ``threshold`` is a minimum for higher-is-better metrics and a maximum for
    lower-is-better metrics. Regression tolerances are relative to the baseline.
    """

    metric: str
    direction: MetricDirection
    required: bool = True
    threshold: float | None = None
    max_regression_ratio: float = Field(default=0.0, ge=0)

    @field_validator("metric")
    @classmethod
    def metric_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("metric must not be blank")
        return value.strip()


class EvaluationPolicy(BaseModel):
    rules: list[MetricRule] = Field(min_length=1)
    require_tests: bool = True


class EvaluationRequest(BaseModel):
    baseline: EvaluationSnapshot
    candidate: EvaluationSnapshot
    policy: EvaluationPolicy


class MetricComparison(BaseModel):
    metric: str
    direction: MetricDirection
    baseline: float | None = None
    candidate: float | None = None
    delta: float | None = None
    improvement_ratio: float | None = None
    accepted: bool
    reasons: list[str] = Field(default_factory=list)


class EvaluationResult(BaseModel):
    accepted: bool
    summary: str
    comparisons: list[MetricComparison]
    rejection_reasons: list[str] = Field(default_factory=list)


class BenchmarkCommand(BaseModel):
    """One trusted command executed without a shell inside an isolated copy."""

    name: str
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def command_name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command name must not be blank")
        return value.strip()

    @field_validator("argv")
    @classmethod
    def arguments_must_not_be_blank(cls, value: list[str]) -> list[str]:
        if any(not argument for argument in value):
            raise ValueError("command arguments must not be blank")
        return value


class PatchVerificationPlan(BaseModel):
    """Commands, metrics, and policy used to verify a patch candidate."""

    commands: list[BenchmarkCommand] = Field(min_length=1)
    metrics_file: str = ".autoharness-metrics.json"
    policy: EvaluationPolicy
    stop_on_failure: bool = True
    protected_paths: list[str] = Field(default_factory=lambda: [".autoharness", ".github", "tests"])

    @field_validator("metrics_file")
    @classmethod
    def metrics_path_must_be_safe(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise ValueError("metrics_file must be a safe relative file path")
        return path.as_posix()

    @field_validator("protected_paths")
    @classmethod
    def protected_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            path = Path(item)
            if path.is_absolute() or ".." in path.parts or item in {"", "."}:
                raise ValueError("protected_paths must contain safe relative paths")
            normalized.append(path.as_posix().rstrip("/"))
        return normalized


class CommandExecution(BaseModel):
    name: str
    argv: list[str]
    exit_code: int
    duration_ms: float = Field(ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    timed_out: bool = False


class BenchmarkRun(BaseModel):
    snapshot: EvaluationSnapshot
    commands: list[CommandExecution]


class SourceFileFingerprint(BaseModel):
    path: str
    existed: bool
    sha256: str | None = None


class VerificationAttestation(BaseModel):
    patch_sha256: str
    source_files: list[SourceFileFingerprint]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PatchVerificationResult(BaseModel):
    accepted: bool
    changed_paths: list[str]
    baseline: BenchmarkRun
    candidate: BenchmarkRun
    evaluation: EvaluationResult
    attestation: VerificationAttestation


class PatchPromotionResult(BaseModel):
    applied: bool
    changed_paths: list[str]
    patch_sha256: str
    backup_path: str
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RepairPipelineResult(BaseModel):
    verification: PatchVerificationResult
    promotion: PatchPromotionResult | None = None


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


class RepairCandidateRecord(BaseModel):
    candidate_id: str
    title: str
    repository_path: str
    patch_sha256: str
    failure_type: FailureType
    status: CandidateStatus
    changed_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class RepairCandidateEvent(BaseModel):
    sequence: int
    candidate_id: str
    status: CandidateStatus
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class EvolutionPipelineResult(BaseModel):
    candidate: RepairCandidateRecord
    repair: RepairPipelineResult
    skill: Skill | None = None
    skill_path: str | None = None
