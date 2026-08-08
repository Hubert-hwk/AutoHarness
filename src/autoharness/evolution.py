"""Auditable repair-to-skill evolution pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoharness.ledger import ALLOWED_TRANSITIONS, LedgerError, RepairLedger
from autoharness.models import (
    CandidateStatus,
    EvolutionPipelineResult,
    PatchVerificationPlan,
    RepairCandidateRecord,
    RepairExperience,
    RepairPipelineResult,
)
from autoharness.skills import SkillGenerator
from autoharness.verification import RepairPipeline, SourceFingerprinter


@dataclass(frozen=True)
class EvolutionAttempt:
    """Captured evolution outcome plus the original exception, when one occurred."""

    result: EvolutionPipelineResult
    error: Exception | None = None


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
        metadata: dict[str, Any] | None = None,
    ) -> EvolutionPipelineResult:
        attempt = self.run_attempt(
            repository,
            patch,
            plan,
            experience,
            promote=promote,
            metadata=metadata,
        )
        if attempt.error is not None:
            raise attempt.error
        return attempt.result

    def run_attempt(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        experience: RepairExperience,
        *,
        promote: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> EvolutionAttempt:
        """Run one candidate while returning failures as auditable data for retry loops."""
        source = Path(repository).expanduser().resolve()
        proposal_metadata = dict(metadata or {})
        proposal_metadata["trigger_terms"] = experience.trigger_terms
        candidate = self.ledger.propose(
            title=experience.title,
            repository_path=source,
            patch_sha256=SourceFingerprinter.patch_sha256(patch),
            failure_type=experience.failure_type,
            metadata=proposal_metadata,
        )
        repair: RepairPipelineResult | None = None
        try:
            repair = self.repair_pipeline.run(source, patch, plan, promote=promote)
            verification = repair.verification
            verification_details: dict[str, Any] = {
                "evaluation_accepted": verification.accepted,
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
                return EvolutionAttempt(EvolutionPipelineResult(candidate=candidate, repair=repair))

            candidate = self.ledger.transition(
                candidate.candidate_id,
                CandidateStatus.VERIFIED,
                details=verification_details,
                changed_paths=verification.changed_paths,
            )
            if repair.promotion is None:
                return EvolutionAttempt(EvolutionPipelineResult(candidate=candidate, repair=repair))

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
            return EvolutionAttempt(
                EvolutionPipelineResult(
                    candidate=candidate,
                    repair=repair,
                    skill=skill,
                    skill_path=str(skill_path),
                )
            )
        except Exception as exc:
            candidate = self._record_failure(candidate.candidate_id, exc)
            error_text = self._error_text(exc)
            return EvolutionAttempt(
                EvolutionPipelineResult(
                    candidate=candidate,
                    repair=repair,
                    error_type=type(exc).__name__,
                    error=error_text,
                ),
                exc,
            )

    def _record_failure(self, candidate_id: str, error: Exception) -> RepairCandidateRecord:
        try:
            current = self.ledger.get(candidate_id)
            if CandidateStatus.FAILED in ALLOWED_TRANSITIONS[current.status]:
                return self.ledger.transition(
                    candidate_id,
                    CandidateStatus.FAILED,
                    details={
                        "error": self._error_text(error),
                        "error_type": type(error).__name__,
                        "failed_from_status": current.status.value,
                    },
                )
        except LedgerError:
            pass
        return self.ledger.get(candidate_id)

    @staticmethod
    def _error_text(error: Exception) -> str:
        return str(error)[-4000:]

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
