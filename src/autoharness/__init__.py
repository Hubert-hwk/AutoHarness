"""AutoHarness public package interface."""

from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    EvaluationResult,
    EvolutionPipelineResult,
    FailureDiagnosis,
    PatchPromotionResult,
    PatchVerificationResult,
    RepairPipelineResult,
)
from autoharness.service import AutoHarness

__all__ = [
    "AgentTrace",
    "AnalysisResult",
    "AutoHarness",
    "EvaluationResult",
    "EvolutionPipelineResult",
    "FailureDiagnosis",
    "PatchPromotionResult",
    "PatchVerificationResult",
    "RepairPipelineResult",
]
__version__ = "0.3.0"
