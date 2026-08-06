"""Domain models shared by the AutoHarness core, CLI, and API."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

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


class AutoFixPhase(StrEnum):
    GENERATION = "generation"
    DEDUPLICATION = "deduplication"
    VERIFICATION = "verification"
    EVALUATION = "evaluation"
    PROMOTION = "promotion"
    LEARNING = "learning"
    COMPLETE = "complete"


class AutoFixRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class SkillHealthStatus(StrEnum):
    UNOBSERVED = "unobserved"
    LEARNING = "learning"
    HEALTHY = "healthy"
    QUARANTINED = "quarantined"


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
    repair: RepairPipelineResult | None = None
    skill: Skill | None = None
    skill_path: str | None = None
    error_type: str | None = None
    error: str | None = None


class SkillQuery(BaseModel):
    failure_type: FailureType | None = None
    text: str = ""
    components: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=50)
    same_failure_only: bool = True
    include_quarantined: bool = False
    quarantine_probe_index: int | None = Field(default=None, ge=0)


class SkillLoadIssue(BaseModel):
    path: str
    error: str


class SkillOutcomeStats(BaseModel):
    skill_name: str
    skill_version: int = Field(ge=1)
    observations: int = Field(ge=0)
    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    unevaluated_failures: int = Field(ge=0)
    post_acceptance_failures: int = Field(ge=0)
    posterior_success_rate: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    score_adjustment: float = Field(ge=-2, le=2)
    control_observations: int = Field(default=0, ge=0)
    control_accepted: int = Field(default=0, ge=0)
    control_rejected: int = Field(default=0, ge=0)
    control_unevaluated_failures: int = Field(default=0, ge=0)
    control_posterior_success_rate: float = Field(default=0.5, ge=0, le=1)
    estimated_lift: float = Field(default=0, ge=-1, le=1)
    ablation_confidence: float = Field(default=0, ge=0, le=1)
    ablation_score_adjustment: float = Field(default=0, ge=-1, le=1)


class SkillHealth(BaseModel):
    skill_name: str
    skill_version: int = Field(ge=1)
    status: SkillHealthStatus
    observations: int = Field(ge=0)
    posterior_success_rate: float = Field(ge=0, le=1)
    minimum_observations: int = Field(ge=1)
    quarantine_threshold: float = Field(ge=0, le=1)
    reason: str


class SkillMatch(BaseModel):
    skill: Skill
    path: str
    score: float = Field(ge=0)
    reasons: list[str]
    outcome_stats: SkillOutcomeStats | None = None
    health: SkillHealth | None = None
    quarantine_probe: bool = False


class SkillSearchResult(BaseModel):
    matches: list[SkillMatch]
    indexed_skills: int
    ignored_older_versions: int = 0
    invalid_files: list[SkillLoadIssue] = Field(default_factory=list)
    quarantined_skills: list[SkillHealth] = Field(default_factory=list)


class SkillAblation(BaseModel):
    skill_name: str
    skill_version: int = Field(ge=1)
    path: str
    original_rank: int = Field(ge=1)
    experiment_index: int = Field(ge=0)
    health: SkillHealth | None = None
    reason: str


class SkillRecommendationResult(BaseModel):
    diagnosis: FailureDiagnosis
    code_locations: list[CodeLocation] = Field(default_factory=list)
    skills: SkillSearchResult
    withheld_skills: list[SkillAblation] = Field(default_factory=list)


class PatchGeneratorConfig(BaseModel):
    type: Literal["command"] = "command"
    name: str = "command"
    argv: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    env: dict[str, str] = Field(default_factory=dict)
    max_patch_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)

    @field_validator("name")
    @classmethod
    def generator_name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("generator name must not be blank")
        return value.strip()

    @field_validator("argv")
    @classmethod
    def generator_arguments_must_not_be_blank(cls, value: list[str]) -> list[str]:
        if any(not argument for argument in value):
            raise ValueError("generator arguments must not be blank")
        return value


class OpenAIPatchGeneratorConfig(BaseModel):
    """Configuration for the built-in OpenAI Responses API patch provider."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["openai"] = "openai"
    name: str = "openai-responses"
    model: str = "gpt-5.6-sol"
    api_key_env: str = "OPENAI_API_KEY"
    base_url: str | None = None
    timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=20_000, ge=256, le=128_000)
    max_patch_bytes: int = Field(default=2 * 1024 * 1024, ge=1, le=2 * 1024 * 1024)
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    text_verbosity: Literal["low", "medium", "high"] = "low"
    store: bool = False
    allow_new_files: bool = False
    context_paths: list[str] = Field(default_factory=list, max_length=100)
    max_context_files: int = Field(default=24, ge=1, le=100)
    max_scan_files: int = Field(default=10_000, ge=100, le=100_000)
    max_discovered_paths: int = Field(default=2_000, ge=100, le=10_000)
    max_file_bytes: int = Field(default=64 * 1024, ge=1, le=1024 * 1024)
    max_context_bytes: int = Field(default=256 * 1024, ge=1024, le=4 * 1024 * 1024)
    max_input_bytes: int = Field(default=1024 * 1024, ge=4096, le=8 * 1024 * 1024)

    @field_validator("name", "model", "api_key_env")
    @classmethod
    def openai_strings_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("OpenAI generator values must not be blank")
        return value.strip()

    @field_validator("context_paths")
    @classmethod
    def context_paths_must_be_relative_files(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            path = Path(item)
            if (
                not item
                or path.is_absolute()
                or ".." in path.parts
                or (path.parts and ":" in path.parts[0])
            ):
                raise ValueError("OpenAI context paths must be safe relative paths")
            normalized.append(path.as_posix())
        return list(dict.fromkeys(normalized))


class AdaptivePatchGeneratorConfig(BaseModel):
    """Select one child generator from repository-scoped historical outcomes."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["adaptive"] = "adaptive"
    name: str = "adaptive"
    providers: list[dict[str, Any]] = Field(min_length=2, max_length=10)
    minimum_trials: int = Field(default=2, ge=1, le=20)
    exploration_weight: float = Field(default=0.35, ge=0, le=2)
    failover_phases: list[AutoFixPhase] = Field(
        default_factory=lambda: [AutoFixPhase.GENERATION, AutoFixPhase.DEDUPLICATION],
        max_length=3,
    )

    @field_validator("name")
    @classmethod
    def adaptive_name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Adaptive generator name must not be blank")
        return value.strip()

    @field_validator("failover_phases")
    @classmethod
    def failover_phases_must_be_safe(cls, value: list[AutoFixPhase]) -> list[AutoFixPhase]:
        allowed = {
            AutoFixPhase.GENERATION,
            AutoFixPhase.DEDUPLICATION,
            AutoFixPhase.VERIFICATION,
        }
        if any(item not in allowed for item in value):
            raise ValueError(
                "Adaptive failover phases may only include generation, deduplication, "
                "or verification"
            )
        return list(dict.fromkeys(value))


class ProviderOutcomeStats(BaseModel):
    provider: str
    failure_type: FailureType | None = None
    observations: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    rejected: int = Field(ge=0)
    failed: int = Field(ge=0)
    interrupted: int = Field(ge=0)
    posterior_success_rate: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    average_attempts: float = Field(ge=0)
    last_run_at: datetime | None = None


class ProviderSelectionCandidate(BaseModel):
    provider: str
    observations: int = Field(ge=0)
    posterior_success_rate: float = Field(ge=0, le=1)
    exploration_bonus: float = Field(ge=0)
    selection_score: float = Field(ge=0)
    under_sampled: bool = False


class ProviderFailoverEvent(BaseModel):
    trigger_attempt: int = Field(ge=1, le=10)
    trigger_phase: AutoFixPhase
    from_provider: str
    to_provider: str
    reason: str


class ProviderSelection(BaseModel):
    strategy: str = "bayesian_ucb"
    portfolio: str = "adaptive"
    minimum_trials: int = Field(default=1, ge=1)
    exploration_weight: float = Field(default=0, ge=0)
    failure_type: FailureType | None = None
    selected_provider: str
    initial_selected_provider: str | None = None
    exploration: bool
    reason: str
    candidates: list[ProviderSelectionCandidate]
    failovers: list[ProviderFailoverEvent] = Field(default_factory=list)


class AutoFixAttemptFeedback(BaseModel):
    attempt_number: int = Field(ge=1, le=10)
    phase: AutoFixPhase
    provider: str
    accepted: bool = False
    patch_sha256: str | None = None
    candidate_id: str | None = None
    status: CandidateStatus | None = None
    evaluation_summary: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    metrics_before: dict[str, float] = Field(default_factory=dict)
    metrics_after: dict[str, float] = Field(default_factory=dict)
    error_type: str | None = None
    error: str | None = None


class PatchGenerationContext(BaseModel):
    trace: AgentTrace
    diagnosis: FailureDiagnosis
    code_locations: list[CodeLocation] = Field(default_factory=list)
    skill_matches: list[SkillMatch] = Field(default_factory=list)
    verification_plan: PatchVerificationPlan | None = None
    protected_paths: list[str] = Field(default_factory=list)
    attempt_number: int = Field(default=1, ge=1, le=10)
    previous_attempts: list[AutoFixAttemptFeedback] = Field(default_factory=list)


class GeneratedPatch(BaseModel):
    provider: str
    patch: str
    patch_sha256: str
    duration_ms: float = Field(ge=0)
    stderr_tail: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AutoFixAttemptResult(BaseModel):
    attempt_number: int = Field(ge=1, le=10)
    generated_patch: GeneratedPatch | None = None
    evolution: EvolutionPipelineResult | None = None
    feedback: AutoFixAttemptFeedback


class AutoFixRunRecord(BaseModel):
    run_id: str
    repository_path: str
    trace_sha256: str = Field(min_length=64, max_length=64)
    trace: AgentTrace | None = None
    trace_persisted: bool = False
    generator_provider: str
    generator_selection: ProviderSelection | None = None
    failure_type: FailureType | None = None
    max_attempts: int = Field(ge=1, le=10)
    promote_requested: bool
    status: AutoFixRunStatus
    final_candidate_id: str | None = None
    error_type: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class AutoFixRunAttemptRecord(BaseModel):
    run_id: str
    attempt_number: int = Field(ge=1, le=10)
    feedback: AutoFixAttemptFeedback
    created_at: datetime


class AutoFixPipelineResult(BaseModel):
    run: AutoFixRunRecord
    recommendation: SkillRecommendationResult
    generated_patch: GeneratedPatch
    evolution: EvolutionPipelineResult
    attempts: list[AutoFixAttemptResult] = Field(default_factory=list)
