"""Application service orchestrating AutoHarness capabilities."""

from __future__ import annotations

from pathlib import Path

from autoharness.autofix import AutoFixPipeline
from autoharness.code_graph import PythonCodeGraph
from autoharness.diagnosis import FailureDiagnoser
from autoharness.evaluation import EvaluationGate
from autoharness.evolution import EvolutionPipeline
from autoharness.generation import PatchGenerator
from autoharness.ledger import RepairLedger
from autoharness.models import (
    AgentTrace,
    AnalysisResult,
    AutoFixPipelineResult,
    EvaluationPolicy,
    EvaluationResult,
    EvaluationSnapshot,
    EvolutionPipelineResult,
    FailureType,
    PatchVerificationPlan,
    PatchVerificationResult,
    RepairExperience,
    RepairPipelineResult,
    Skill,
    SkillRecommendationResult,
)
from autoharness.registry import SkillRecommender
from autoharness.skills import SkillGenerator
from autoharness.verification import PatchVerifier, RepairPipeline


class AutoHarness:
    def __init__(self) -> None:
        self.diagnoser = FailureDiagnoser()
        self.evaluation_gate = EvaluationGate()
        self.skill_generator = SkillGenerator()
        self.patch_verifier = PatchVerifier()
        self.repair_pipeline = RepairPipeline()
        self.skill_recommender = SkillRecommender()

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

    def repair_patch(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        *,
        promote: bool = False,
    ) -> RepairPipelineResult:
        return self.repair_pipeline.run(repository, patch, plan, promote=promote)

    def evolve_patch(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        experience: RepairExperience,
        *,
        ledger_path: str | Path,
        skill_directory: str | Path,
        promote: bool = True,
    ) -> EvolutionPipelineResult:
        pipeline = EvolutionPipeline(RepairLedger(ledger_path), skill_directory)
        return pipeline.run(repository, patch, plan, experience, promote=promote)

    def recommend_skills(
        self,
        trace: AgentTrace,
        skill_directory: str | Path,
        *,
        repository_path: str | Path | None = None,
        ledger_path: str | Path | None = None,
        limit: int = 5,
        same_failure_only: bool = True,
        include_quarantined: bool = False,
    ) -> SkillRecommendationResult:
        diagnosis = self.diagnoser.diagnose(trace)
        evidence_failure_type = (
            None if diagnosis.failure_type == FailureType.UNKNOWN else diagnosis.failure_type
        )
        outcomes = (
            RepairLedger(ledger_path).skill_outcomes(
                repository_path=repository_path,
                failure_type=evidence_failure_type,
            )
            if ledger_path is not None
            else None
        )
        return self.skill_recommender.recommend(
            trace,
            skill_directory,
            repository_path=repository_path,
            limit=limit,
            same_failure_only=same_failure_only,
            diagnosis=diagnosis,
            outcome_stats=outcomes,
            include_quarantined=include_quarantined,
        )

    def autofix(
        self,
        trace: AgentTrace,
        repository: str | Path,
        generator: PatchGenerator,
        verification_plan: PatchVerificationPlan,
        *,
        ledger_path: str | Path,
        skill_directory: str | Path,
        promote: bool = False,
        skill_limit: int = 5,
        max_attempts: int = 3,
        skill_quarantine_probe_interval: int = 10,
        skill_ablation_interval: int = 20,
        persist_trace: bool = False,
        recover_stale_after_seconds: float | None = None,
    ) -> AutoFixPipelineResult:
        pipeline = AutoFixPipeline(
            generator,
            RepairLedger(ledger_path),
            skill_directory,
        )
        return pipeline.run(
            trace,
            repository,
            verification_plan,
            promote=promote,
            skill_limit=skill_limit,
            max_attempts=max_attempts,
            skill_quarantine_probe_interval=skill_quarantine_probe_interval,
            skill_ablation_interval=skill_ablation_interval,
            persist_trace=persist_trace,
            recover_stale_after_seconds=recover_stale_after_seconds,
        )
