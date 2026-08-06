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
