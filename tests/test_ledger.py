import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from autoharness.ledger import LedgerError, RepairLedger
from autoharness.models import (
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixPhase,
    AutoFixRunStatus,
    CandidateStatus,
    FailureType,
    ProviderSelection,
    ProviderSelectionCandidate,
)


def _candidate(ledger: RepairLedger, repository: Path):
    return ledger.propose(
        title="Repair retrieval",
        repository_path=repository,
        patch_sha256="a" * 64,
        failure_type=FailureType.RETRIEVAL,
        metadata={"source": "test"},
    )


def test_ledger_persists_candidate_and_append_only_events(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    candidate = _candidate(ledger, tmp_path)
    candidate = ledger.transition(
        candidate.candidate_id,
        CandidateStatus.VERIFIED,
        details={"score": 0.9},
        changed_paths=["retriever.py"],
    )
    candidate = ledger.transition(
        candidate.candidate_id,
        CandidateStatus.PROMOTED,
        details={"backup_path": "backup"},
    )

    restored = RepairLedger(database).get(candidate.candidate_id)
    events = RepairLedger(database).events(candidate.candidate_id)

    assert restored.status == CandidateStatus.PROMOTED
    assert restored.changed_paths == ["retriever.py"]
    assert restored.metadata["score"] == 0.9
    assert [event.status for event in events] == [
        CandidateStatus.PROPOSED,
        CandidateStatus.VERIFIED,
        CandidateStatus.PROMOTED,
    ]


def test_ledger_rejects_invalid_transition(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    candidate = _candidate(ledger, tmp_path)

    with pytest.raises(LedgerError, match="proposed -> learned"):
        ledger.transition(candidate.candidate_id, CandidateStatus.LEARNED)


def test_ledger_filters_and_bounds_history(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    rejected = _candidate(ledger, tmp_path)
    ledger.transition(rejected.candidate_id, CandidateStatus.REJECTED)
    _candidate(ledger, tmp_path)

    records = ledger.list_candidates(status=CandidateStatus.REJECTED, limit=1000)

    assert len(records) == 1
    assert records[0].candidate_id == rejected.candidate_id


def test_ledger_reports_unknown_candidate(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")

    with pytest.raises(LedgerError, match="Unknown repair candidate"):
        ledger.events("missing")


def _skill_candidate(
    ledger: RepairLedger,
    repository: Path,
    skill_name: str,
    *,
    version: int = 1,
):
    return ledger.propose(
        title="Outcome candidate",
        repository_path=repository,
        patch_sha256=(skill_name[0] * 64),
        failure_type=FailureType.REASONING,
        metadata={
            "retrieved_skills": [
                {"name": skill_name, "version": version, "score": 10.0},
                {"name": skill_name, "version": version, "score": 10.0},
            ]
        },
    )


def test_ledger_aggregates_repository_scoped_skill_outcomes(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    repository = tmp_path / "repository"
    other_repository = tmp_path / "other"

    accepted = _skill_candidate(ledger, repository, "score_repair")
    ledger.transition(accepted.candidate_id, CandidateStatus.VERIFIED)
    rejected = _skill_candidate(ledger, repository, "score_repair")
    ledger.transition(rejected.candidate_id, CandidateStatus.REJECTED)
    failed = _skill_candidate(ledger, repository, "score_repair")
    ledger.transition(failed.candidate_id, CandidateStatus.FAILED)
    post_acceptance = _skill_candidate(ledger, repository, "score_repair")
    ledger.transition(post_acceptance.candidate_id, CandidateStatus.VERIFIED)
    ledger.transition(
        post_acceptance.candidate_id,
        CandidateStatus.FAILED,
        details={"failed_from_status": CandidateStatus.VERIFIED.value},
    )
    outside = _skill_candidate(ledger, other_repository, "score_repair")
    ledger.transition(outside.candidate_id, CandidateStatus.VERIFIED)

    scoped = ledger.skill_outcomes(repository_path=repository)
    global_stats = ledger.skill_outcomes()

    stats = scoped[("score_repair", 1)]
    assert stats.observations == 4
    assert stats.accepted == 2
    assert stats.rejected == 1
    assert stats.unevaluated_failures == 1
    assert stats.post_acceptance_failures == 1
    assert stats.posterior_success_rate == 0.5
    assert stats.score_adjustment == 0
    assert global_stats[("score_repair", 1)].accepted == 3


def test_ledger_shrinks_skill_outcome_scores_toward_neutral(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    for _ in range(10):
        good = _skill_candidate(ledger, tmp_path, "good")
        ledger.transition(good.candidate_id, CandidateStatus.VERIFIED)
        poor = _skill_candidate(ledger, tmp_path, "poor")
        ledger.transition(poor.candidate_id, CandidateStatus.REJECTED)

    outcomes = ledger.skill_outcomes(repository_path=tmp_path)

    assert outcomes[("good", 1)].posterior_success_rate == pytest.approx(0.8571)
    assert outcomes[("poor", 1)].posterior_success_rate == pytest.approx(0.1429)
    assert outcomes[("good", 1)].score_adjustment == pytest.approx(0.952)
    assert outcomes[("poor", 1)].score_adjustment == pytest.approx(-0.952)


def test_ledger_persists_autofix_run_without_trace_by_default(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    trace = AgentTrace(task="Sensitive task", events=[], feedback="private feedback")
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=trace,
        generator_provider="test-generator",
        max_attempts=3,
        promote_requested=False,
    )
    candidate = _candidate(ledger, tmp_path)
    candidate = ledger.transition(candidate.candidate_id, CandidateStatus.VERIFIED)
    feedback = AutoFixAttemptFeedback(
        attempt_number=1,
        phase=AutoFixPhase.COMPLETE,
        provider="test-generator",
        accepted=True,
        patch_sha256=candidate.patch_sha256,
        candidate_id=candidate.candidate_id,
        status=candidate.status,
    )

    attempt = ledger.record_autofix_attempt(run.run_id, feedback)
    completed = ledger.finish_autofix_run(
        run.run_id,
        AutoFixRunStatus.SUCCEEDED,
        final_candidate_id=candidate.candidate_id,
    )
    restored = RepairLedger(database).get_autofix_run(run.run_id)

    assert run.trace is None
    assert not run.trace_persisted
    assert len(run.trace_sha256) == 64
    assert attempt.feedback == feedback
    assert completed.status == AutoFixRunStatus.SUCCEEDED
    assert restored.final_candidate_id == candidate.candidate_id
    assert restored.trace is None
    assert RepairLedger(database).autofix_attempts(run.run_id)[0].feedback == feedback
    with sqlite3.connect(database) as connection:
        stored_trace = connection.execute(
            "SELECT trace_json FROM autofix_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
    assert stored_trace is None


def test_ledger_optionally_persists_trace_and_failed_run(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    trace = AgentTrace(task="Persist this trace", events=[])
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=trace,
        generator_provider="test-generator",
        max_attempts=1,
        promote_requested=True,
        persist_trace=True,
    )
    feedback = AutoFixAttemptFeedback(
        attempt_number=1,
        phase=AutoFixPhase.GENERATION,
        provider="test-generator",
        error_type="RuntimeError",
        error="generation failed",
    )
    ledger.record_autofix_attempt(run.run_id, feedback)

    completed = ledger.finish_autofix_run(
        run.run_id,
        AutoFixRunStatus.FAILED,
        error=RuntimeError("x" * 5000),
    )

    assert completed.trace == trace
    assert completed.trace_persisted
    assert completed.error_type == "RuntimeError"
    assert completed.error is not None and len(completed.error) == 4000
    listed = ledger.list_autofix_runs(status=AutoFixRunStatus.FAILED)
    assert listed[0].run_id == completed.run_id
    assert listed[0].trace is None
    assert listed[0].trace_persisted


def test_ledger_enforces_immutable_autofix_attempts_and_terminal_runs(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Test invariants", events=[]),
        generator_provider="test-generator",
        max_attempts=1,
        promote_requested=False,
    )
    feedback = AutoFixAttemptFeedback(
        attempt_number=1,
        phase=AutoFixPhase.GENERATION,
        provider="test-generator",
    )
    ledger.record_autofix_attempt(run.run_id, feedback)

    with pytest.raises(LedgerError, match="already exists"):
        ledger.record_autofix_attempt(run.run_id, feedback)

    with pytest.raises(LedgerError, match="exceeds run budget 1"):
        ledger.record_autofix_attempt(
            run.run_id,
            feedback.model_copy(update={"attempt_number": 2}),
        )

    ledger.finish_autofix_run(run.run_id, AutoFixRunStatus.FAILED)
    with pytest.raises(LedgerError, match="already terminal"):
        ledger.record_autofix_attempt(
            run.run_id,
            feedback.model_copy(update={"attempt_number": 2}),
        )
    with pytest.raises(LedgerError, match="already terminal"):
        ledger.finish_autofix_run(run.run_id, AutoFixRunStatus.FAILED)
    with pytest.raises(LedgerError, match="already terminal"):
        ledger.heartbeat_autofix_run(run.run_id)
    with pytest.raises(LedgerError, match="non-negative"):
        ledger.recover_stale_autofix_runs(older_than_seconds=-1)


def test_ledger_atomically_recovers_stale_run_and_active_candidate(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    repository = tmp_path / "repository"
    run = ledger.start_autofix_run(
        repository_path=repository,
        trace=AgentTrace(task="Interrupted repair", events=[]),
        generator_provider="test-generator",
        max_attempts=3,
        promote_requested=True,
        persist_trace=True,
    )
    candidate = ledger.propose(
        title="Interrupted candidate",
        repository_path=repository,
        patch_sha256="f" * 64,
        failure_type=FailureType.REASONING,
        metadata={"autofix_run_id": run.run_id, "autofix_attempt": 1},
    )
    candidate = ledger.transition(candidate.candidate_id, CandidateStatus.VERIFIED)
    ledger.record_autofix_attempt(
        run.run_id,
        AutoFixAttemptFeedback(
            attempt_number=1,
            phase=AutoFixPhase.COMPLETE,
            provider="test-generator",
            accepted=True,
            candidate_id=candidate.candidate_id,
            patch_sha256=candidate.patch_sha256,
            status=candidate.status,
        ),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE autofix_runs SET updated_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )

    recovered = ledger.recover_stale_autofix_runs(
        older_than_seconds=60,
        repository_path=repository,
    )

    assert len(recovered) == 1
    assert recovered[0].status == AutoFixRunStatus.INTERRUPTED
    assert recovered[0].final_candidate_id == candidate.candidate_id
    assert recovered[0].error_type == "AutoFixInterrupted"
    assert recovered[0].trace is None
    assert recovered[0].trace_persisted
    failed = ledger.get(candidate.candidate_id)
    assert failed.status == CandidateStatus.FAILED
    assert failed.metadata["failed_from_status"] == CandidateStatus.VERIFIED
    assert ledger.events(candidate.candidate_id)[-1].status == CandidateStatus.FAILED


def test_ledger_heartbeat_and_repository_scope_prevent_stale_recovery(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    protected_repository = tmp_path / "protected"
    other_repository = tmp_path / "other"
    protected = ledger.start_autofix_run(
        repository_path=protected_repository,
        trace=AgentTrace(task="Still active", events=[]),
        generator_provider="test-generator",
        max_attempts=1,
        promote_requested=False,
    )
    other = ledger.start_autofix_run(
        repository_path=other_repository,
        trace=AgentTrace(task="Different repository", events=[]),
        generator_provider="test-generator",
        max_attempts=1,
        promote_requested=False,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE autofix_runs SET updated_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )

    heartbeat = ledger.heartbeat_autofix_run(protected.run_id)
    recovered = ledger.recover_stale_autofix_runs(
        older_than_seconds=60,
        repository_path=protected_repository,
    )

    assert heartbeat.updated_at > protected.updated_at
    assert recovered == []
    assert ledger.get_autofix_run(protected.run_id).status == AutoFixRunStatus.RUNNING
    assert ledger.get_autofix_run(other.run_id).status == AutoFixRunStatus.RUNNING


def test_ledger_recovery_and_completion_have_exactly_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Race recovery against completion"),
        generator_provider="test-generator",
        max_attempts=1,
        promote_requested=False,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE autofix_runs SET updated_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )

    def finish() -> AutoFixRunStatus | str:
        try:
            return ledger.finish_autofix_run(run.run_id, AutoFixRunStatus.SUCCEEDED).status
        except LedgerError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovery_future = executor.submit(
            ledger.recover_stale_autofix_runs,
            older_than_seconds=0,
        )
        finish_future = executor.submit(finish)
        recovered = recovery_future.result()
        finished = finish_future.result()

    terminal = ledger.get_autofix_run(run.run_id).status
    if terminal == AutoFixRunStatus.INTERRUPTED:
        assert [item.run_id for item in recovered] == [run.run_id]
        assert finished == "lost"
    else:
        assert terminal == AutoFixRunStatus.SUCCEEDED
        assert recovered == []
        assert finished == AutoFixRunStatus.SUCCEEDED


def test_ledger_persists_generator_selection_and_aggregates_scoped_outcomes(
    tmp_path: Path,
) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    repository = tmp_path / "repository"
    other_repository = tmp_path / "other"
    selection = ProviderSelection(
        selected_provider="fast",
        exploration=False,
        reason="Highest score",
        candidates=[
            ProviderSelectionCandidate(
                provider="fast",
                observations=2,
                posterior_success_rate=0.5,
                exploration_bonus=0,
                selection_score=0.5,
            )
        ],
    )

    def record(
        provider: str,
        status: AutoFixRunStatus,
        repo: Path,
        attempts: int,
        *,
        selected: ProviderSelection | None = None,
    ) -> None:
        run = ledger.start_autofix_run(
            repository_path=repo,
            trace=AgentTrace(task=f"Run {provider} {status}"),
            generator_provider=provider,
            generator_selection=selected,
            max_attempts=max(1, attempts),
            promote_requested=False,
        )
        for attempt in range(1, attempts + 1):
            ledger.record_autofix_attempt(
                run.run_id,
                AutoFixAttemptFeedback(
                    attempt_number=attempt,
                    phase=(
                        AutoFixPhase.EVALUATION
                        if status == AutoFixRunStatus.REJECTED
                        else AutoFixPhase.COMPLETE
                    ),
                    provider=provider,
                    accepted=status == AutoFixRunStatus.SUCCEEDED,
                    status=(
                        CandidateStatus.REJECTED if status == AutoFixRunStatus.REJECTED else None
                    ),
                ),
            )
        ledger.finish_autofix_run(run.run_id, status)

    record("fast", AutoFixRunStatus.SUCCEEDED, repository, 2, selected=selection)
    record("fast", AutoFixRunStatus.REJECTED, repository, 1)
    record("slow", AutoFixRunStatus.FAILED, repository, 1)
    record("slow", AutoFixRunStatus.INTERRUPTED, repository, 0)
    record("fast", AutoFixRunStatus.SUCCEEDED, other_repository, 1)

    outcomes = ledger.provider_outcomes(repository_path=repository)

    assert outcomes["fast"].observations == 2
    assert outcomes["fast"].succeeded == 1
    assert outcomes["fast"].rejected == 1
    assert outcomes["fast"].posterior_success_rate == 0.5
    assert outcomes["fast"].average_attempts == 1.5
    assert outcomes["slow"].observations == 1
    assert outcomes["slow"].failed == 1
    assert outcomes["slow"].interrupted == 1
    assert outcomes["slow"].posterior_success_rate == 0.4
    assert outcomes["slow"].average_attempts == 1
    persisted = next(
        run for run in ledger.list_autofix_runs() if run.generator_selection is not None
    )
    assert persisted.generator_selection == selection


def test_provider_outcomes_attribute_failover_to_each_attempted_provider(
    tmp_path: Path,
) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    selection = ProviderSelection(
        selected_provider="unavailable",
        initial_selected_provider="unavailable",
        exploration=False,
        reason="Initial choice",
        candidates=[
            ProviderSelectionCandidate(
                provider=provider,
                observations=0,
                posterior_success_rate=0.5,
                exploration_bonus=0,
                selection_score=0.5,
            )
            for provider in ("unavailable", "working")
        ],
    )
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Fail over"),
        generator_provider="unavailable",
        generator_selection=selection,
        max_attempts=2,
        promote_requested=False,
    )
    ledger.record_autofix_attempt(
        run.run_id,
        AutoFixAttemptFeedback(
            attempt_number=1,
            phase=AutoFixPhase.GENERATION,
            provider="unavailable",
            error_type="PatchGenerationError",
            error="offline",
        ),
    )
    updated_selection = selection.model_copy(
        update={"selected_provider": "working", "reason": "Failover"}
    )
    updated = ledger.update_autofix_generator_selection(
        run.run_id,
        generator_provider="working",
        generator_selection=updated_selection,
    )
    ledger.record_autofix_attempt(
        run.run_id,
        AutoFixAttemptFeedback(
            attempt_number=2,
            phase=AutoFixPhase.COMPLETE,
            provider="working",
            accepted=True,
        ),
    )
    ledger.finish_autofix_run(run.run_id, AutoFixRunStatus.SUCCEEDED)
    with pytest.raises(LedgerError, match="already terminal"):
        ledger.update_autofix_generator_selection(
            run.run_id,
            generator_provider="unavailable",
            generator_selection=selection,
        )
    unattributed = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Diagnosis failed before generation"),
        generator_provider="not-invoked",
        max_attempts=1,
        promote_requested=False,
    )
    ledger.finish_autofix_run(unattributed.run_id, AutoFixRunStatus.FAILED)

    outcomes = ledger.provider_outcomes(repository_path=tmp_path)

    assert updated.generator_provider == "working"
    assert updated.generator_selection == updated_selection
    assert outcomes["unavailable"].failed == 1
    assert outcomes["unavailable"].average_attempts == 1
    assert outcomes["working"].succeeded == 1
    assert outcomes["working"].average_attempts == 1
    assert "not-invoked" not in outcomes


def test_provider_outcomes_are_isolated_by_diagnosed_failure_type(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")

    def record(
        provider: str,
        failure_type: FailureType,
        *,
        accepted: bool,
    ) -> str:
        run = ledger.start_autofix_run(
            repository_path=tmp_path,
            trace=AgentTrace(task=f"{failure_type.value} with {provider}"),
            generator_provider=provider,
            max_attempts=1,
            promote_requested=False,
        )
        diagnosed = ledger.record_autofix_diagnosis(run.run_id, failure_type)
        assert diagnosed.failure_type == failure_type
        ledger.record_autofix_attempt(
            run.run_id,
            AutoFixAttemptFeedback(
                attempt_number=1,
                phase=AutoFixPhase.COMPLETE if accepted else AutoFixPhase.GENERATION,
                provider=provider,
                accepted=accepted,
            ),
        )
        ledger.finish_autofix_run(
            run.run_id,
            AutoFixRunStatus.SUCCEEDED if accepted else AutoFixRunStatus.FAILED,
        )
        return run.run_id

    reasoning_run = record("reasoner", FailureType.REASONING, accepted=True)
    record("reasoner", FailureType.RETRIEVAL, accepted=False)
    record("retriever", FailureType.REASONING, accepted=False)
    record("retriever", FailureType.RETRIEVAL, accepted=True)

    reasoning = ledger.provider_outcomes(
        repository_path=tmp_path,
        failure_type=FailureType.REASONING,
    )
    retrieval = ledger.provider_outcomes(
        repository_path=tmp_path,
        failure_type=FailureType.RETRIEVAL,
    )
    global_outcomes = ledger.provider_outcomes(repository_path=tmp_path)

    assert reasoning["reasoner"].succeeded == 1
    assert reasoning["reasoner"].failure_type == FailureType.REASONING
    assert reasoning["retriever"].failed == 1
    assert retrieval["retriever"].succeeded == 1
    assert retrieval["reasoner"].failed == 1
    assert global_outcomes["reasoner"].observations == 2
    assert global_outcomes["reasoner"].failure_type is None
    with pytest.raises(LedgerError, match="already terminal"):
        ledger.record_autofix_diagnosis(reasoning_run, FailureType.TOOL)


def test_ledger_backfills_failure_type_from_historical_candidate_attempt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.db"
    ledger = RepairLedger(database)
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Historical contextual repair"),
        generator_provider="historical",
        max_attempts=1,
        promote_requested=False,
    )
    candidate = ledger.propose(
        title="Historical repair",
        repository_path=tmp_path,
        patch_sha256="f" * 64,
        failure_type=FailureType.MEMORY,
        metadata={"autofix_run_id": run.run_id},
    )
    ledger.record_autofix_attempt(
        run.run_id,
        AutoFixAttemptFeedback(
            attempt_number=1,
            phase=AutoFixPhase.COMPLETE,
            provider="historical",
            accepted=True,
            candidate_id=candidate.candidate_id,
        ),
    )
    ledger.finish_autofix_run(run.run_id, AutoFixRunStatus.SUCCEEDED)
    assert ledger.get_autofix_run(run.run_id).failure_type is None

    reopened = RepairLedger(database)

    assert reopened.get_autofix_run(run.run_id).failure_type == FailureType.MEMORY


def test_ledger_migrates_pre_selection_autofix_schema(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE autofix_runs (
                run_id TEXT PRIMARY KEY,
                repository_path TEXT NOT NULL,
                trace_sha256 TEXT NOT NULL,
                trace_json TEXT,
                generator_provider TEXT NOT NULL,
                max_attempts INTEGER NOT NULL,
                promote_requested INTEGER NOT NULL,
                status TEXT NOT NULL,
                final_candidate_id TEXT,
                error_type TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    ledger = RepairLedger(database)
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Legacy schema migration"),
        generator_provider="legacy",
        max_attempts=1,
        promote_requested=False,
    )

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(autofix_runs)")}
    assert "generator_selection" in columns
    assert "failure_type" in columns
    assert run.generator_selection is None
    assert run.failure_type is None
