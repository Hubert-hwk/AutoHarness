"""End-to-end Diagnose -> Retrieve -> Generate -> Verify -> Learn orchestration."""

from __future__ import annotations

from pathlib import Path

from autoharness.evolution import EvolutionPipeline
from autoharness.generation import (
    PatchGenerationError,
    PatchGenerator,
    generator_provider_name,
)
from autoharness.ledger import RepairLedger
from autoharness.models import (
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixAttemptResult,
    AutoFixPhase,
    AutoFixPipelineResult,
    AutoFixRunRecord,
    AutoFixRunStatus,
    CandidateStatus,
    EvolutionPipelineResult,
    FailureType,
    GeneratedPatch,
    PatchGenerationContext,
    PatchVerificationPlan,
    ProviderSelection,
    RepairExperience,
    SkillAblation,
    SkillRecommendationResult,
)
from autoharness.registry import SkillRecommender
from autoharness.verification import PatchVerifier


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
        skill_quarantine_probe_interval: int = 10,
        skill_ablation_interval: int = 20,
        persist_trace: bool = False,
        recover_stale_after_seconds: float | None = None,
    ) -> AutoFixPipelineResult:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if not 0 <= skill_quarantine_probe_interval <= 1000:
            raise ValueError("skill_quarantine_probe_interval must be between 0 and 1000")
        if not 0 <= skill_ablation_interval <= 1000:
            raise ValueError("skill_ablation_interval must be between 0 and 1000")
        source = Path(repository).expanduser().resolve()
        if recover_stale_after_seconds is not None:
            self.ledger.recover_stale_autofix_runs(
                older_than_seconds=recover_stale_after_seconds,
                repository_path=source,
            )
        self._configure_generator(source)
        run = self.ledger.start_autofix_run(
            repository_path=source,
            trace=trace,
            generator_provider=self._provider_name(),
            generator_selection=self._generator_selection(),
            max_attempts=max_attempts,
            promote_requested=promote,
            persist_trace=persist_trace,
        )
        try:
            result = self._execute(
                run,
                trace,
                source,
                verification_plan,
                promote=promote,
                skill_limit=skill_limit,
                max_attempts=max_attempts,
                skill_quarantine_probe_interval=skill_quarantine_probe_interval,
                skill_ablation_interval=skill_ablation_interval,
            )
            status = (
                AutoFixRunStatus.REJECTED
                if result.evolution.candidate.status == CandidateStatus.REJECTED
                else AutoFixRunStatus.SUCCEEDED
            )
            completed = self.ledger.finish_autofix_run(
                run.run_id,
                status,
                final_candidate_id=result.evolution.candidate.candidate_id,
            )
            return result.model_copy(update={"run": completed})
        except Exception as exc:
            current = self.ledger.get_autofix_run(run.run_id)
            if current.status == AutoFixRunStatus.RUNNING:
                self.ledger.finish_autofix_run(
                    run.run_id,
                    AutoFixRunStatus.FAILED,
                    final_candidate_id=self._latest_candidate_id(run.run_id),
                    error=exc,
                )
            raise

    def _execute(
        self,
        run: AutoFixRunRecord,
        trace: AgentTrace,
        source: Path,
        verification_plan: PatchVerificationPlan,
        *,
        promote: bool,
        skill_limit: int,
        max_attempts: int,
        skill_quarantine_probe_interval: int,
        skill_ablation_interval: int,
    ) -> AutoFixPipelineResult:
        run_sequence = self.ledger.autofix_run_count(repository_path=source)
        quarantine_probe_index = None
        if (
            skill_quarantine_probe_interval > 0
            and run_sequence % skill_quarantine_probe_interval == 0
        ):
            quarantine_probe_index = run_sequence // skill_quarantine_probe_interval - 1
        diagnosis = self.recommender.diagnoser.diagnose(trace)
        evidence_failure_type = (
            None if diagnosis.failure_type == FailureType.UNKNOWN else diagnosis.failure_type
        )
        recommendation = self.recommender.recommend(
            trace,
            self.skill_directory,
            repository_path=source,
            limit=skill_limit,
            allow_missing_directory=True,
            diagnosis=diagnosis,
            outcome_stats=self.ledger.skill_outcomes(
                repository_path=source,
                failure_type=evidence_failure_type,
            ),
            quarantine_probe_index=quarantine_probe_index,
        )
        recommendation = self._apply_skill_ablation(
            recommendation,
            run_sequence=run_sequence,
            interval=skill_ablation_interval,
        )
        failure_type = recommendation.diagnosis.failure_type
        self.ledger.record_autofix_diagnosis(run.run_id, failure_type)
        self._configure_generator(source, failure_type=failure_type)
        self._sync_generator_selection(run.run_id)
        self.ledger.heartbeat_autofix_run(run.run_id)
        protected_paths = list(
            dict.fromkeys(
                [
                    *verification_plan.protected_paths,
                    *PatchVerifier.command_input_paths(source, verification_plan),
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
            self.ledger.heartbeat_autofix_run(run.run_id)
            context = PatchGenerationContext(
                trace=trace,
                diagnosis=recommendation.diagnosis,
                code_locations=recommendation.code_locations,
                skill_matches=recommendation.skills.matches,
                verification_plan=effective_plan,
                protected_paths=protected_paths,
                attempt_number=attempt_number,
                previous_attempts=feedback_history,
            )
            try:
                generated = self.generator.generate(source, context)
            except PatchGenerationError as exc:
                self._sync_generator_selection(run.run_id)
                feedback = AutoFixAttemptFeedback(
                    attempt_number=attempt_number,
                    phase=AutoFixPhase.GENERATION,
                    provider=self._provider_name(),
                    error_type=type(exc).__name__,
                    error=self._bounded_error(exc),
                )
                feedback_history.append(feedback)
                self.ledger.record_autofix_attempt(run.run_id, feedback)
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

            self._sync_generator_selection(run.run_id)
            self.ledger.heartbeat_autofix_run(run.run_id)

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
                self.ledger.record_autofix_attempt(run.run_id, feedback)
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
                        return self._result(run, recommendation, last_returnable, attempts)
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
                    run.run_id,
                    attempt_number,
                    max_attempts,
                    generated,
                    recommendation,
                    feedback_history,
                ),
            )
            feedback = self._feedback(attempt_number, generated, execution.result)
            feedback_history.append(feedback)
            self.ledger.record_autofix_attempt(run.run_id, feedback)
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
                return self._result(run, recommendation, last_returnable, attempts)
            if attempt_number == max_attempts:
                return self._result(run, recommendation, last_returnable, attempts)

        raise RuntimeError("AutoFix attempt loop ended without an outcome")

    @staticmethod
    def _result(
        run: AutoFixRunRecord,
        recommendation: SkillRecommendationResult,
        final: tuple[GeneratedPatch, EvolutionPipelineResult],
        attempts: list[AutoFixAttemptResult],
    ) -> AutoFixPipelineResult:
        generated, evolution = final
        return AutoFixPipelineResult(
            run=run,
            recommendation=recommendation,
            generated_patch=generated,
            evolution=evolution,
            attempts=attempts,
        )

    def _latest_candidate_id(self, run_id: str) -> str | None:
        for attempt in reversed(self.ledger.autofix_attempts(run_id)):
            if attempt.feedback.candidate_id is not None:
                return attempt.feedback.candidate_id
        return None

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
        return generator_provider_name(self.generator)

    def _configure_generator(
        self,
        repository: Path,
        *,
        failure_type: FailureType | None = None,
    ) -> None:
        configure = getattr(self.generator, "configure_outcomes", None)
        if callable(configure):
            configure(
                self.ledger.provider_outcomes(
                    repository_path=repository,
                    failure_type=failure_type,
                )
            )
        contextualize = getattr(self.generator, "configure_selection_context", None)
        if callable(contextualize):
            contextualize(failure_type)

    def _generator_selection(self) -> ProviderSelection | None:
        selection = getattr(self.generator, "selection_metadata", None)
        if selection is None or isinstance(selection, ProviderSelection):
            return selection
        return ProviderSelection.model_validate(selection)

    def _sync_generator_selection(self, run_id: str) -> None:
        selection = self._generator_selection()
        if selection is not None:
            self.ledger.update_autofix_generator_selection(
                run_id,
                generator_provider=self._provider_name(),
                generator_selection=selection,
            )

    @staticmethod
    def _apply_skill_ablation(
        recommendation: SkillRecommendationResult,
        *,
        run_sequence: int,
        interval: int,
    ) -> SkillRecommendationResult:
        if interval == 0 or run_sequence % interval != 0:
            return recommendation
        eligible = [
            (index, match)
            for index, match in enumerate(recommendation.skills.matches)
            if not match.quarantine_probe
        ]
        if not eligible:
            return recommendation
        experiment_index = run_sequence // interval - 1
        rotation_pool = sorted(
            eligible,
            key=lambda item: (item[1].skill.name, item[1].skill.version, item[1].path),
        )
        selected_index, selected = rotation_pool[experiment_index % len(rotation_pool)]
        ablation = SkillAblation(
            skill_name=selected.skill.name,
            skill_version=selected.skill.version,
            path=selected.path,
            original_rank=selected_index + 1,
            experiment_index=experiment_index,
            health=selected.health,
            reason=(
                f"Withheld from repository run {run_sequence} as controlled Skill ablation "
                f"experiment {experiment_index}"
            ),
        )
        remaining = [
            match
            for index, match in enumerate(recommendation.skills.matches)
            if index != selected_index
        ]
        return recommendation.model_copy(
            update={
                "skills": recommendation.skills.model_copy(update={"matches": remaining}),
                "withheld_skills": [*recommendation.withheld_skills, ablation],
            }
        )

    @staticmethod
    def _bounded_error(error: Exception) -> str:
        return str(error)[-4000:]

    @staticmethod
    def _attempt_metadata(
        run_id: str,
        attempt_number: int,
        max_attempts: int,
        generated: GeneratedPatch,
        recommendation: SkillRecommendationResult,
        previous_attempts: list[AutoFixAttemptFeedback],
    ) -> dict[str, object]:
        return {
            "autofix_run_id": run_id,
            "autofix_attempt": attempt_number,
            "autofix_max_attempts": max_attempts,
            "skill_context_fingerprint": recommendation.context_fingerprint,
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
                    "health": match.health.status.value if match.health is not None else None,
                    "health_basis": (
                        match.health.decision_basis.value if match.health is not None else None
                    ),
                    "estimated_lift": (
                        match.health.estimated_lift if match.health is not None else None
                    ),
                    "estimated_lift_interval": (
                        [
                            match.health.estimated_lift_lower_bound,
                            match.health.estimated_lift_upper_bound,
                        ]
                        if match.health is not None
                        else None
                    ),
                    "comparison_mode": (
                        match.health.comparison_mode.value if match.health is not None else None
                    ),
                    "matched_contexts": (
                        match.health.matched_contexts if match.health is not None else None
                    ),
                    "quarantine_probe": match.quarantine_probe,
                }
                for match in recommendation.skills.matches
            ],
            "withheld_skills": [
                {
                    "name": item.skill_name,
                    "version": item.skill_version,
                    "path": item.path,
                    "original_rank": item.original_rank,
                    "experiment_index": item.experiment_index,
                    "health": item.health.status.value if item.health is not None else None,
                    "health_basis": (
                        item.health.decision_basis.value if item.health is not None else None
                    ),
                    "estimated_lift": (
                        item.health.estimated_lift if item.health is not None else None
                    ),
                    "estimated_lift_interval": (
                        [
                            item.health.estimated_lift_lower_bound,
                            item.health.estimated_lift_upper_bound,
                        ]
                        if item.health is not None
                        else None
                    ),
                    "comparison_mode": (
                        item.health.comparison_mode.value if item.health is not None else None
                    ),
                    "matched_contexts": (
                        item.health.matched_contexts if item.health is not None else None
                    ),
                    "reason": item.reason,
                }
                for item in recommendation.withheld_skills
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
