"""End-to-end Diagnose -> Retrieve -> Generate -> Verify -> Learn orchestration."""

from __future__ import annotations

from pathlib import Path

from autoharness.evolution import EvolutionPipeline
from autoharness.generation import PatchGenerationError, PatchGenerator
from autoharness.ledger import RepairLedger
from autoharness.models import (
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixAttemptResult,
    AutoFixPhase,
    AutoFixPipelineResult,
    CandidateStatus,
    EvolutionPipelineResult,
    GeneratedPatch,
    PatchGenerationContext,
    PatchVerificationPlan,
    RepairExperience,
    SkillRecommendationResult,
)
from autoharness.registry import SkillRecommender


class AutoFixPipeline:
    """Compose a patch provider with deterministic safety and evolution layers."""

    def __init__(
        self,
        generator: PatchGenerator,
        ledger: RepairLedger,
        skill_directory: str | Path,
    ) -> None:
        self.generator = generator
        self.ledger = ledger
        self.skill_directory = Path(skill_directory).expanduser().resolve()
        self.recommender = SkillRecommender()

    def run(
        self,
        trace: AgentTrace,
        repository: str | Path,
        verification_plan: PatchVerificationPlan,
        *,
        promote: bool = False,
        skill_limit: int = 5,
        max_attempts: int = 3,
    ) -> AutoFixPipelineResult:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        source = Path(repository).expanduser().resolve()
        recommendation = self.recommender.recommend(
            trace,
            self.skill_directory,
            repository_path=source,
            limit=skill_limit,
            allow_missing_directory=True,
        )
        protected_paths = list(
            dict.fromkeys(
                [
                    *verification_plan.protected_paths,
                    *self.generator.protected_paths(source),
                ]
            )
        )
        effective_plan = verification_plan.model_copy(update={"protected_paths": protected_paths})
        evolution_pipeline = EvolutionPipeline(self.ledger, self.skill_directory)
        feedback_history: list[AutoFixAttemptFeedback] = []
        attempts: list[AutoFixAttemptResult] = []
        seen_patches: dict[str, int] = {}
        last_returnable: tuple[GeneratedPatch, EvolutionPipelineResult] | None = None
        last_returnable_attempt = 0
        last_error: Exception | None = None
        last_error_attempt = 0

        for attempt_number in range(1, max_attempts + 1):
            context = PatchGenerationContext(
                trace=trace,
                diagnosis=recommendation.diagnosis,
                code_locations=recommendation.code_locations,
                skill_matches=recommendation.skills.matches,
                attempt_number=attempt_number,
                previous_attempts=feedback_history,
            )
            try:
                generated = self.generator.generate(source, context)
            except PatchGenerationError as exc:
                feedback = AutoFixAttemptFeedback(
                    attempt_number=attempt_number,
                    phase=AutoFixPhase.GENERATION,
                    provider=self._provider_name(),
                    error_type=type(exc).__name__,
                    error=self._bounded_error(exc),
                )
                feedback_history.append(feedback)
                attempts.append(
                    AutoFixAttemptResult(
                        attempt_number=attempt_number,
                        feedback=feedback,
                    )
                )
                last_error = exc
                last_error_attempt = attempt_number
                if attempt_number == max_attempts:
                    raise
                continue

            duplicate_of = seen_patches.get(generated.patch_sha256)
            if duplicate_of is not None:
                feedback = AutoFixAttemptFeedback(
                    attempt_number=attempt_number,
                    phase=AutoFixPhase.DEDUPLICATION,
                    provider=generated.provider,
                    patch_sha256=generated.patch_sha256,
                    error_type="DuplicatePatch",
                    error=f"Patch duplicates attempt {duplicate_of}",
                )
                feedback_history.append(feedback)
                attempts.append(
                    AutoFixAttemptResult(
                        attempt_number=attempt_number,
                        generated_patch=generated,
                        feedback=feedback,
                    )
                )
                if attempt_number == max_attempts:
                    if last_error is not None and last_error_attempt > last_returnable_attempt:
                        raise last_error
                    if last_returnable is not None:
                        return self._result(recommendation, last_returnable, attempts)
                    raise PatchGenerationError("Patch generator only returned duplicate patches")
                continue
            seen_patches[generated.patch_sha256] = attempt_number

            experience = self._experience(recommendation, generated.provider, effective_plan)
            execution = evolution_pipeline.run_attempt(
                source,
                generated.patch,
                effective_plan,
                experience,
                promote=promote,
                metadata=self._attempt_metadata(
                    attempt_number,
                    max_attempts,
                    generated,
                    recommendation,
                    feedback_history,
                ),
            )
            feedback = self._feedback(attempt_number, generated, execution.result)
            feedback_history.append(feedback)
            attempts.append(
                AutoFixAttemptResult(
                    attempt_number=attempt_number,
                    generated_patch=generated,
                    evolution=execution.result,
                    feedback=feedback,
                )
            )

            if execution.error is not None:
                last_error = execution.error
                last_error_attempt = attempt_number
                if (
                    execution.result.repair is not None
                    and execution.result.repair.promotion is not None
                ):
                    raise execution.error
                if attempt_number == max_attempts:
                    raise execution.error
                continue

            last_returnable = (generated, execution.result)
            last_returnable_attempt = attempt_number
            if execution.result.candidate.status != CandidateStatus.REJECTED:
                return self._result(recommendation, last_returnable, attempts)
            if attempt_number == max_attempts:
                return self._result(recommendation, last_returnable, attempts)

        raise RuntimeError("AutoFix attempt loop ended without an outcome")

    @staticmethod
    def _result(
        recommendation: SkillRecommendationResult,
        final: tuple[GeneratedPatch, EvolutionPipelineResult],
        attempts: list[AutoFixAttemptResult],
    ) -> AutoFixPipelineResult:
        generated, evolution = final
        return AutoFixPipelineResult(
            recommendation=recommendation,
            generated_patch=generated,
            evolution=evolution,
            attempts=attempts,
        )

    @staticmethod
    def _feedback(
        attempt_number: int,
        generated: GeneratedPatch,
        evolution: EvolutionPipelineResult,
    ) -> AutoFixAttemptFeedback:
        repair = evolution.repair
        verification = repair.verification if repair is not None else None
        status = evolution.candidate.status
        if evolution.error_type is not None:
            failed_from = evolution.candidate.metadata.get("failed_from_status", "proposed")
            phase = {
                "verified": AutoFixPhase.PROMOTION,
                "promoted": AutoFixPhase.LEARNING,
            }.get(str(failed_from), AutoFixPhase.VERIFICATION)
        elif status == CandidateStatus.REJECTED:
            phase = AutoFixPhase.EVALUATION
        else:
            phase = AutoFixPhase.COMPLETE
        return AutoFixAttemptFeedback(
            attempt_number=attempt_number,
            phase=phase,
            provider=generated.provider,
            accepted=bool(evolution.error_type is None and verification and verification.accepted),
            patch_sha256=generated.patch_sha256,
            candidate_id=evolution.candidate.candidate_id,
            status=status,
            evaluation_summary=(
                verification.evaluation.summary if verification is not None else None
            ),
            rejection_reasons=(
                verification.evaluation.rejection_reasons if verification is not None else []
            ),
            metrics_before=(
                verification.baseline.snapshot.metrics if verification is not None else {}
            ),
            metrics_after=(
                verification.candidate.snapshot.metrics if verification is not None else {}
            ),
            error_type=evolution.error_type,
            error=evolution.error,
        )

    def _provider_name(self) -> str:
        config = getattr(self.generator, "config", None)
        name = getattr(config, "name", None)
        return str(name or type(self.generator).__name__)

    @staticmethod
    def _bounded_error(error: Exception) -> str:
        return str(error)[-4000:]

    @staticmethod
    def _attempt_metadata(
        attempt_number: int,
        max_attempts: int,
        generated: GeneratedPatch,
        recommendation: SkillRecommendationResult,
        previous_attempts: list[AutoFixAttemptFeedback],
    ) -> dict[str, object]:
        return {
            "autofix_attempt": attempt_number,
            "autofix_max_attempts": max_attempts,
            "generator_provider": generated.provider,
            "generation_duration_ms": generated.duration_ms,
            "prior_candidate_ids": [
                item.candidate_id for item in previous_attempts if item.candidate_id is not None
            ],
            "previous_attempts": [
                item.model_dump(mode="json", exclude_none=True) for item in previous_attempts
            ],
            "retrieved_skills": [
                {
                    "name": match.skill.name,
                    "version": match.skill.version,
                    "path": match.path,
                    "score": match.score,
                }
                for match in recommendation.skills.matches
            ],
        }

    @staticmethod
    def _experience(
        recommendation: SkillRecommendationResult,
        provider: str,
        plan: PatchVerificationPlan,
    ) -> RepairExperience:
        diagnosis = recommendation.diagnosis
        components = list(
            dict.fromkeys(
                location.symbol
                for location in recommendation.code_locations
                if location.kind != "file"
            )
        )
        trigger_terms = diagnosis.search_terms or [diagnosis.failure_type.value]
        root_cause = diagnosis.likely_causes[0] if diagnosis.likely_causes else diagnosis.summary
        validation_steps = [
            *(f"Run benchmark command: {command.name}" for command in plan.commands),
            *(f"Enforce evaluation metric: {rule.metric}" for rule in plan.policy.rules),
        ]
        return RepairExperience(
            title=f"Autofix {diagnosis.failure_type.value.replace('_', ' ')}",
            failure_type=diagnosis.failure_type,
            trigger_terms=trigger_terms[:16],
            root_cause=root_cause,
            repair_steps=[f"Apply the unified diff generated by {provider}"],
            validation_steps=validation_steps,
            affected_components=components,
        )
