"""AutoHarness public package interface."""

from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    EvaluationResult,
    FailureDiagnosis,
)
from autoharness.service import AutoHarness

__all__ = [
    "AgentTrace",
    "AnalysisResult",
    "AutoHarness",
    "EvaluationResult",
    "FailureDiagnosis",
]
__version__ = "0.2.0"
