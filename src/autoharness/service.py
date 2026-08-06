"""Application service orchestrating AutoHarness capabilities."""

from __future__ import annotations

from pathlib import Path

from autoharness.code_graph import PythonCodeGraph
from autoharness.diagnosis import FailureDiagnoser
from autoharness.evaluation import EvaluationGate
from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSnapshot,
    PatchVerificationPlan,
    PatchVerificationResult,
    RepairExperience,
    Skill,
)
from autoharness.skills import SkillGenerator
from autoharness.verification import PatchVerifier


class AutoHarness:
    def __init__(self) -> None:
        self.diagnoser = FailureDiagnoser()
        self.evaluation_gate = EvaluationGate()
        self.skill_generator = SkillGenerator()
        self.patch_verifier = PatchVerifier()

    def analyze(
        self,
        trace: AgentTrace,
        repository_path: str | Path | None = None,
        candidate_limit: int = 8,
    ) -> AnalysisResult:
        diagnosis = self.diagnoser.diagnose(trace)
        if repository_path is None:
            return AnalysisResult(diagnosis=diagnosis)

        graph = PythonCodeGraph(repository_path)
        stats = graph.build()
        locations = graph.locate(diagnosis, limit=candidate_limit)
        return AnalysisResult(
            diagnosis=diagnosis,
            code_locations=locations,
            indexed_files=stats.files,
            indexed_symbols=stats.symbols,
        )

    def learn(self, experience: RepairExperience, directory: str | Path) -> tuple[Skill, Path]:
        skill = self.skill_generator.from_experience(experience)
        return skill, self.skill_generator.save(skill, directory)

    def evaluate(
        self,
        baseline: EvaluationSnapshot,
        candidate: EvaluationSnapshot,
        policy: EvaluationPolicy,
    ) -> EvaluationResult:
        return self.evaluation_gate.evaluate(baseline, candidate, policy)

    def verify_patch(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
    ) -> PatchVerificationResult:
        return self.patch_verifier.verify(repository, patch, plan)
