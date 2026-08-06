import sys
from pathlib import Path

import pytest

from autoharness.autofix import AutoFixPipeline
from autoharness.generation import CommandPatchGenerator
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
    PatchGeneratorConfig,
    PatchVerificationPlan,
    Skill,
    TraceEvent,
)
from autoharness.skills import SkillGenerator
from autoharness.verification import VerificationError


def _repository(path: Path, candidate: int = 2, *, modify_generator: bool = False) -> Path:
    path.mkdir()
    (path / "value.txt").write_bytes(b"1\n")
    (path / "benchmark.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "value = float(Path('value.txt').read_text().strip())\n"
        "Path('.autoharness-metrics.json').write_text(json.dumps({'score': value}))\n",
        encoding="utf-8",
    )
    target = "generator.py" if modify_generator else "value.txt"
    original = "import json, sys" if modify_generator else "1"
    replacement = "import json, sys # changed" if modify_generator else str(candidate)
    patch = f"--- a/{target}\n+++ b/{target}\n@@ -1 +1 @@\n-{original}\n+{replacement}\n"
    (path / "generator.py").write_text(
        "import json, sys\n"
        "context = json.load(sys.stdin)\n"
        "matches = context['skill_matches']\n"
        "assert not matches or matches[0]['skill']['name'] == 'historical_score_repair'\n"
        f"sys.stdout.write({patch!r})\n",
        encoding="utf-8",
    )
    return path


def _trace() -> AgentTrace:
    return AgentTrace(
        task="Fix an incorrect response with a low score",
        events=[
            TraceEvent(
                kind=EventKind.RESPONSE,
                status=EventStatus.FAILURE,
                error="incorrect answer",
            )
        ],
        feedback="The benchmark has a low score",
    )


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


def _generator() -> CommandPatchGenerator:
    return CommandPatchGenerator(
        PatchGeneratorConfig(
            name="test-command-generator",
            argv=[sys.executable, "generator.py"],
        )
    )


def _seed_skill(directory: Path) -> None:
    SkillGenerator().save_versioned(
        Skill(
            name="historical_score_repair",
            description="Repair low scores",
            failure_type=FailureType.REASONING,
            triggers=["low score", "incorrect response"],
            context={"affected_components": ["value"], "root_cause": "low value"},
            workflow=["Increase value"],
            evaluation=["Run score benchmark"],
        ),
        directory,
    )


def test_autofix_retrieves_generates_promotes_and_learns(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    skills = repository / ".autoharness" / "skills"
    _seed_skill(skills)
    ledger = RepairLedger(repository / ".autoharness" / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, skills).run(
        _trace(), repository, _plan(), promote=True
    )

    assert result.recommendation.skills.matches[0].skill.name == "historical_score_repair"
    assert result.generated_patch.provider == "test-command-generator"
    assert result.evolution.candidate.status == CandidateStatus.LEARNED
    assert result.evolution.skill is not None
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"
    assert len(ledger.events(result.evolution.candidate.candidate_id)) == 4


def test_autofix_safe_default_verifies_without_promoting(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "missing-skills").run(
        _trace(), repository, _plan(), promote=False
    )

    assert result.recommendation.skills.indexed_skills == 0
    assert result.evolution.candidate.status == CandidateStatus.VERIFIED
    assert result.evolution.skill is None
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"


def test_autofix_records_rejected_generated_patch(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository", candidate=0)
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), promote=True
    )

    assert result.evolution.candidate.status == CandidateStatus.REJECTED
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"


def test_autofix_protects_generator_program_from_its_output(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository", modify_generator=True)
    ledger = RepairLedger(tmp_path / "ledger.db")

    with pytest.raises(VerificationError, match="protected path: generator.py"):
        AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
            _trace(), repository, _plan(), promote=True
        )

    assert ledger.list_candidates()[0].status == CandidateStatus.FAILED
