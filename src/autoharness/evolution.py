"""Auditable repair-to-skill evolution pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoharness.ledger import ALLOWED_TRANSITIONS, LedgerError, RepairLedger
from autoharness.models import (
    CandidateStatus,
    EvolutionPipelineResult,
    PatchVerificationPlan,
    RepairExperience,
    RepairPipelineResult,
)
from autoharness.skills import SkillGenerator
from autoharness.verification import RepairPipeline, SourceFingerprinter


class EvolutionPipeline:
    """Verify, promote, record, and learn from one repair candidate."""

    def __init__(self, ledger: RepairLedger, skill_directory: str | Path) -> None:
        self.ledger = ledger
        self.skill_directory = Path(skill_directory).expanduser().resolve()
        self.repair_pipeline = RepairPipeline()
        self.skill_generator = SkillGenerator()

    def run(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        experience: RepairExperience,
        *,
        promote: bool = True,
    ) -> EvolutionPipelineResult:
        source = Path(repository).expanduser().resolve()
        candidate = self.ledger.propose(
            title=experience.title,
            repository_path=source,
            patch_sha256=SourceFingerprinter.patch_sha256(patch),
            failure_type=experience.failure_type,
            metadata={"trigger_terms": experience.trigger_terms},
        )
        try:
            repair = self.repair_pipeline.run(source, patch, plan, promote=promote)
            verification = repair.verification
            verification_details: dict[str, Any] = {
                "evaluation_summary": verification.evaluation.summary,
                "metrics_before": verification.baseline.snapshot.metrics,
                "metrics_after": verification.candidate.snapshot.metrics,
            }
            if not verification.accepted:
                verification_details["rejection_reasons"] = (
                    verification.evaluation.rejection_reasons
                )
                candidate = self.ledger.transition(
                    candidate.candidate_id,
                    CandidateStatus.REJECTED,
                    details=verification_details,
                    changed_paths=verification.changed_paths,
                )
                return EvolutionPipelineResult(candidate=candidate, repair=repair)

            candidate = self.ledger.transition(
                candidate.candidate_id,
                CandidateStatus.VERIFIED,
                details=verification_details,
                changed_paths=verification.changed_paths,
            )
            if repair.promotion is None:
                return EvolutionPipelineResult(candidate=candidate, repair=repair)

            candidate = self.ledger.transition(
                candidate.candidate_id,
                CandidateStatus.PROMOTED,
                details={
                    "backup_path": repair.promotion.backup_path,
                    "applied_at": repair.promotion.applied_at.isoformat(),
                },
            )
            enriched = self._enrich_experience(experience, repair)
            skill = self.skill_generator.from_experience(enriched)
            skill, skill_path = self.skill_generator.save_versioned(
                skill,
                self.skill_directory,
            )
            candidate = self.ledger.transition(
                candidate.candidate_id,
                CandidateStatus.LEARNED,
                details={"skill_path": str(skill_path), "skill_version": skill.version},
            )
            return EvolutionPipelineResult(
                candidate=candidate,
                repair=repair,
                skill=skill,
                skill_path=str(skill_path),
            )
        except Exception as exc:
            self._record_failure(candidate.candidate_id, exc)
            raise

    def _record_failure(self, candidate_id: str, error: Exception) -> None:
        try:
            current = self.ledger.get(candidate_id)
            if CandidateStatus.FAILED in ALLOWED_TRANSITIONS[current.status]:
                self.ledger.transition(
                    candidate_id,
                    CandidateStatus.FAILED,
                    details={"error": str(error), "error_type": type(error).__name__},
                )
        except LedgerError:
            return

    @staticmethod
    def _enrich_experience(
        experience: RepairExperience,
        repair: RepairPipelineResult,
    ) -> RepairExperience:
        before = repair.verification.baseline.snapshot.metrics
        after = repair.verification.candidate.snapshot.metrics
        metrics = dict(experience.success_metrics)
        for name in sorted(before.keys() | after.keys()):
            if name in before:
                metrics[f"{name}_before"] = before[name]
            if name in after:
                metrics[f"{name}_after"] = after[name]
            if name in before and name in after:
                metrics[f"{name}_delta"] = after[name] - before[name]
        return experience.model_copy(update={"success_metrics": metrics})
