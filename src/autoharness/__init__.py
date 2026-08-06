"""AutoHarness public package interface."""

from autoharness.generation import CommandPatchGenerator, OpenAIResponsesPatchGenerator
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
    OpenAIPatchGeneratorConfig,
    PatchGeneratorConfig,
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
    "CommandPatchGenerator",
    "EvaluationResult",
    "EvolutionPipelineResult",
    "FailureDiagnosis",
    "OpenAIPatchGeneratorConfig",
    "OpenAIResponsesPatchGenerator",
    "PatchGeneratorConfig",
    "PatchPromotionResult",
    "PatchVerificationResult",
    "RepairPipelineResult",
    "SkillRecommendationResult",
    "SkillOutcomeStats",
]
__version__ = "0.10.0"
