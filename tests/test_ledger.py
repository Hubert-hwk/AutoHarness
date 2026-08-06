from pathlib import Path

import pytest

from autoharness.ledger import LedgerError, RepairLedger
from autoharness.models import CandidateStatus, FailureType


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
