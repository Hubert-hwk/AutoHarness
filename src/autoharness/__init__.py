"""AutoHarness public package interface."""

from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    EvaluationResult,
    FailureDiagnosis,
    PatchVerificationResult,
)
from autoharness.service import AutoHarness

__all__ = [
    "AgentTrace",
    "AnalysisResult",
    "AutoHarness",
    "EvaluationResult",
    "FailureDiagnosis",
    "PatchVerificationResult",
]
__version__ = "0.2.0"
