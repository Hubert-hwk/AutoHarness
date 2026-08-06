"""AutoHarness public package interface."""

from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    AutoFixAttemptFeedback,
    AutoFixAttemptResult,
    AutoFixPhase,
    AutoFixPipelineResult,
    AutoFixRunAttemptRecord,
    AutoFixRunRecord,
    AutoFixRunStatus,
    EvaluationResult,
    EvolutionPipelineResult,
    FailureDiagnosis,
    PatchPromotionResult,
    PatchVerificationResult,
    RepairPipelineResult,
    SkillOutcomeStats,
    SkillRecommendationResult,
)
from autoharness.service import AutoHarness

__all__ = [
    "AgentTrace",
    "AnalysisResult",
    "AutoHarness",
    "AutoFixAttemptFeedback",
    "AutoFixAttemptResult",
    "AutoFixPhase",
    "AutoFixPipelineResult",
    "AutoFixRunAttemptRecord",
    "AutoFixRunRecord",
    "AutoFixRunStatus",
    "EvaluationResult",
    "EvolutionPipelineResult",
    "FailureDiagnosis",
    "PatchPromotionResult",
    "PatchVerificationResult",
    "RepairPipelineResult",
    "SkillRecommendationResult",
    "SkillOutcomeStats",
]
__version__ = "0.8.0"
