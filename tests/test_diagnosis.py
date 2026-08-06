from autoharness.diagnosis import FailureDiagnoser
from autoharness.models import AgentTrace, EventKind, EventStatus, FailureType, TraceEvent


def test_retrieval_failure_has_evidence_and_checks() -> None:
    trace = AgentTrace(
        task="Answer from the policy corpus",
        events=[
            TraceEvent(
                kind=EventKind.RETRIEVAL,
                name="retriever",
                status=EventStatus.FAILURE,
                error="recall below threshold",
                output={"documents": []},
            )
        ],
        feedback="The answer used the wrong document.",
    )

    diagnosis = FailureDiagnoser().diagnose(trace)

    assert diagnosis.failure_type == FailureType.RETRIEVAL
    assert diagnosis.confidence > 0.5
    assert diagnosis.evidence
    assert "retriev" in diagnosis.search_terms
    assert diagnosis.recommended_checks


def test_unknown_failure_for_sparse_trace() -> None:
    diagnosis = FailureDiagnoser().diagnose(AgentTrace(task="Do something", events=[]))

    assert diagnosis.failure_type == FailureType.UNKNOWN
    assert diagnosis.confidence == 0.2
