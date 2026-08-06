import sys
from pathlib import Path

import pytest
import yaml

from autoharness.evolution import EvolutionPipeline
from autoharness.ledger import RepairLedger
from autoharness.models import (
    AgentTrace,
    BenchmarkCommand,
    CandidateStatus,
    EvaluationPolicy,
    EventKind,
    EventStatus,
    FailureType,
    MetricDirection,
    MetricRule,
    PatchVerificationPlan,
    RepairExperience,
    TraceEvent,
)
from autoharness.service import AutoHarness
from autoharness.verification import VerificationError


def _repository(path: Path) -> Path:
    path.mkdir()
    (path / "value.txt").write_bytes(b"1\n")
    (path / "benchmark.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "value = float(Path('value.txt').read_text().strip())\n"
        "Path('.autoharness-metrics.json').write_text(json.dumps({'score': value}))\n",
        encoding="utf-8",
    )
    return path


def _patch(candidate: int) -> str:
    return f"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-1\n+{candidate}\n"


def _plan() -> PatchVerificationPlan:
    return PatchVerificationPlan(
        commands=[BenchmarkCommand(name="benchmark", argv=[sys.executable, "benchmark.py"])],
        policy=EvaluationPolicy(
            rules=[
                MetricRule(
                    metric="score",
                    direction=MetricDirection.HIGHER_IS_BETTER,
                    threshold=1,
                )
            ]
        ),
    )


def _experience() -> RepairExperience:
    return RepairExperience(
        title="Improve score",
        failure_type=FailureType.REASONING,
        trigger_terms=["low score"],
        root_cause="The configured value was too low",
        repair_steps=["Increase value"],
        validation_steps=["Run score benchmark"],
        affected_components=["value"],
    )


def test_evolution_pipeline_records_promotes_and_learns(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")
    skills = tmp_path / "skills"

    result = EvolutionPipeline(ledger, skills).run(
        repository,
        _patch(2),
        _plan(),
        _experience(),
        promote=True,
    )

    assert result.candidate.status == CandidateStatus.LEARNED
    assert result.skill is not None
    assert result.skill.version == 1
    assert result.skill_path is not None
    skill_data = yaml.safe_load(Path(result.skill_path).read_text(encoding="utf-8"))
    assert skill_data["context"]["success_metrics"] == {
        "score_after": 2.0,
        "score_before": 1.0,
        "score_delta": 1.0,
    }
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"
    assert [event.status for event in ledger.events(result.candidate.candidate_id)] == [
        CandidateStatus.PROPOSED,
        CandidateStatus.VERIFIED,
        CandidateStatus.PROMOTED,
        CandidateStatus.LEARNED,
    ]
    recommendation = AutoHarness().recommend_skills(
        AgentTrace(
            task="Fix a low score and incorrect answer",
            events=[
                TraceEvent(
                    kind=EventKind.RESPONSE,
                    status=EventStatus.FAILURE,
                    error="incorrect answer",
                )
            ],
            feedback="The benchmark has a low score",
        ),
        skills,
    )
    assert recommendation.skills.matches[0].skill.name == "improve_score"
    assert recommendation.skills.matches[0].skill.version == 1


def test_evolution_pipeline_records_rejected_candidate(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = EvolutionPipeline(ledger, tmp_path / "skills").run(
        repository,
        _patch(0),
        _plan(),
        _experience(),
        promote=True,
    )

    assert result.candidate.status == CandidateStatus.REJECTED
    assert result.skill is None
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"
    assert ledger.list_candidates()[0].metadata["rejection_reasons"]


def test_evolution_pipeline_records_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")
    unsafe_patch = "--- a/../secret\n+++ b/../secret\n@@ -1 +1 @@\n-a\n+b\n"

    with pytest.raises(VerificationError, match="unsafe path"):
        EvolutionPipeline(ledger, tmp_path / "skills").run(
            repository,
            unsafe_patch,
            _plan(),
            _experience(),
            promote=True,
        )

    record = ledger.list_candidates()[0]
    assert record.status == CandidateStatus.FAILED
    assert record.metadata["error_type"] == "VerificationError"


def test_versioned_skill_history_is_preserved(tmp_path: Path) -> None:
    ledger = RepairLedger(tmp_path / "ledger.db")
    pipeline = EvolutionPipeline(ledger, tmp_path / "skills")

    first = pipeline.run(
        _repository(tmp_path / "first"), _patch(2), _plan(), _experience(), promote=True
    )
    second = pipeline.run(
        _repository(tmp_path / "second"), _patch(2), _plan(), _experience(), promote=True
    )

    assert first.skill is not None and first.skill.version == 1
    assert second.skill is not None and second.skill.version == 2
    assert len(list((tmp_path / "skills").glob("improve_score.v*.yaml"))) == 2
