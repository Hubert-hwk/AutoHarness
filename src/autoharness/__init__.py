"""AutoHarness public package interface."""

from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    AutoFixAttemptFeedback,
    AutoFixAttemptResult,
    AutoFixPhase,
    AutoFixPipelineResult,
    EvaluationResult,
    EvolutionPipelineResult,
    FailureDiagnosis,
    PatchPromotionResult,
    PatchVerificationResult,
    RepairPipelineResult,
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
    "EvaluationResult",
    "EvolutionPipelineResult",
    "FailureDiagnosis",
    "PatchPromotionResult",
    "PatchVerificationResult",
    "RepairPipelineResult",
    "SkillRecommendationResult",
]
__version__ = "0.6.0"
