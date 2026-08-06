import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoharness.autofix import AutoFixPipeline
from autoharness.generation import (
    AdaptivePatchGenerator,
    CommandPatchGenerator,
    OpenAIResponsesPatchGenerator,
)
from autoharness.ledger import RepairLedger
from autoharness.models import (
    AdaptivePatchGeneratorConfig,
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixPhase,
    AutoFixRunStatus,
    BenchmarkCommand,
    CandidateStatus,
    EvaluationPolicy,
    EventKind,
    EventStatus,
    FailureType,
    MetricDirection,
    MetricRule,
    OpenAIPatchGeneratorConfig,
    PatchGeneratorConfig,
    PatchVerificationPlan,
    Skill,
    SkillHealthStatus,
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


def _record_skill_outcome(
    ledger: RepairLedger,
    repository: Path,
    skill_name: str,
    status: CandidateStatus,
    failure_type: FailureType = FailureType.REASONING,
) -> None:
    candidate = ledger.propose(
        title="Historical outcome",
        repository_path=repository,
        patch_sha256=skill_name[0] * 64,
        failure_type=failure_type,
        metadata={"retrieved_skills": [{"name": skill_name, "version": 1, "score": 10.0}]},
    )
    ledger.transition(candidate.candidate_id, status)


def _adaptive_repository(path: Path, mode: str) -> Path:
    repository = _repository(path)
    (repository / "generator.py").write_text(
        "import json, sys\n"
        f"mode = {mode!r}\n"
        "context = json.load(sys.stdin)\n"
        "attempt = context['attempt_number']\n"
        "previous = context['previous_attempts']\n"
        "assert attempt == len(previous) + 1\n"
        "if attempt == 1 and mode == 'generation_error':\n"
        "    print('provider unavailable', file=sys.stderr)\n"
        "    raise SystemExit(7)\n"
        "if attempt == 1 and mode == 'verification_error':\n"
        "    patch = '--- a/../secret\\n+++ b/../secret\\n@@ -1 +1 @@\\n-a\\n+b\\n'\n"
        "elif attempt == 1:\n"
        "    patch = '--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-1\\n+0\\n'\n"
        "else:\n"
        "    expected = {\n"
        "        'generation_error': 'generation',\n"
        "        'verification_error': 'verification',\n"
        "        'rejected': 'evaluation',\n"
        "    }[mode]\n"
        "    assert previous[-1]['phase'] == expected\n"
        "    if mode == 'rejected':\n"
        "        assert previous[-1]['metrics_before'] == {'score': 1.0}\n"
        "        assert previous[-1]['metrics_after'] == {'score': 0.0}\n"
        "        assert previous[-1]['rejection_reasons']\n"
        "    patch = '--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-1\\n+2\\n'\n"
        "sys.stdout.write(patch)\n",
        encoding="utf-8",
    )
    return repository


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
    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert result.run.final_candidate_id == result.evolution.candidate.candidate_id
    assert result.evolution.candidate.metadata["autofix_run_id"] == result.run.run_id
    assert result.run.trace is None
    assert len(ledger.autofix_attempts(result.run.run_id)) == 1
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"
    assert len(ledger.events(result.evolution.candidate.candidate_id)) == 4


def test_autofix_orders_skill_context_using_repository_outcomes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    skills = repository / ".autoharness" / "skills"
    for name in ("a_poor_repair", "z_reliable_repair"):
        SkillGenerator().save_versioned(
            Skill(
                name=name,
                description="Repair a low score",
                failure_type=FailureType.REASONING,
                triggers=["low score", "incorrect response"],
                context={"root_cause": "low value", "affected_components": ["value"]},
                workflow=["Increase value"],
                evaluation=["Run score benchmark"],
            ),
            skills,
        )
    ledger = RepairLedger(repository / ".autoharness" / "ledger.db")
    for _ in range(8):
        _record_skill_outcome(ledger, repository, "a_poor_repair", CandidateStatus.REJECTED)
        _record_skill_outcome(ledger, repository, "z_reliable_repair", CandidateStatus.VERIFIED)
    (repository / "generator.py").write_text(
        "import json, sys\n"
        "context = json.load(sys.stdin)\n"
        "assert context['skill_matches'][0]['skill']['name'] == 'z_reliable_repair'\n"
        "sys.stdout.write('--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-1\\n+2\\n')\n",
        encoding="utf-8",
    )

    result = AutoFixPipeline(_generator(), ledger, skills).run(
        _trace(), repository, _plan(), max_attempts=1
    )

    best = result.recommendation.skills.matches[0]
    assert best.skill.name == "z_reliable_repair"
    assert best.outcome_stats is not None
    assert best.outcome_stats.accepted == 8
    assert any("observed outcomes" in reason for reason in best.reasons)


def test_autofix_uses_failure_type_scoped_skill_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    skills = repository / ".autoharness" / "skills"
    _seed_skill(skills)
    ledger = RepairLedger(repository / ".autoharness" / "ledger.db")
    _record_skill_outcome(
        ledger,
        repository,
        "historical_score_repair",
        CandidateStatus.VERIFIED,
        FailureType.REASONING,
    )
    for _ in range(5):
        _record_skill_outcome(
            ledger,
            repository,
            "historical_score_repair",
            CandidateStatus.REJECTED,
            FailureType.RETRIEVAL,
        )

    result = AutoFixPipeline(_generator(), ledger, skills).run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_ablation_interval=0,
    )

    match = result.recommendation.skills.matches[0]
    assert match.outcome_stats is not None
    assert match.outcome_stats.failure_type == FailureType.REASONING
    assert match.outcome_stats.observations == 1
    assert match.outcome_stats.accepted == 1
    assert match.health.status == SkillHealthStatus.LEARNING


def test_autofix_records_periodic_skill_ablation_as_control_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    skills = repository / ".autoharness" / "skills"
    _seed_skill(skills)
    ledger = RepairLedger(repository / ".autoharness" / "ledger.db")
    pipeline = AutoFixPipeline(_generator(), ledger, skills)

    control = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_ablation_interval=1,
    )

    assert control.recommendation.skills.matches == []
    assert control.recommendation.withheld_skills[0].skill_name == "historical_score_repair"
    assert control.recommendation.withheld_skills[0].experiment_index == 0
    withheld = control.evolution.candidate.metadata["withheld_skills"][0]
    assert withheld["name"] == "historical_score_repair"
    assert withheld["original_rank"] == 1

    exposed = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_ablation_interval=0,
    )
    assert exposed.recommendation.skills.matches[0].skill.name == "historical_score_repair"
    stats = ledger.skill_outcomes(repository_path=repository)[("historical_score_repair", 1)]
    assert stats.observations == 1
    assert stats.accepted == 1
    assert stats.control_observations == 1
    assert stats.control_accepted == 1
    assert stats.estimated_lift == 0


def test_autofix_rotates_skill_ablation_and_preserves_quarantine_probes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    (repository / "generator.py").write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "sys.stdout.write('--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-1\\n+2\\n')\n",
        encoding="utf-8",
    )
    skills = tmp_path / "skills"
    for name in ("a_repair", "b_repair"):
        SkillGenerator().save_versioned(
            Skill(
                name=name,
                description="Repair low scores",
                failure_type=FailureType.REASONING,
                triggers=["low score", "incorrect response"],
                context={"affected_components": ["value"], "root_cause": "low value"},
                workflow=["Increase value"],
                evaluation=["Run score benchmark"],
            ),
            skills,
        )
    ledger = RepairLedger(tmp_path / "ledger.db")
    pipeline = AutoFixPipeline(_generator(), ledger, skills)

    first = pipeline.run(_trace(), repository, _plan(), max_attempts=1, skill_ablation_interval=1)
    second = pipeline.run(_trace(), repository, _plan(), max_attempts=1, skill_ablation_interval=1)

    assert first.recommendation.withheld_skills[0].skill_name == "a_repair"
    assert second.recommendation.withheld_skills[0].skill_name == "b_repair"
    assert first.recommendation.withheld_skills[0].experiment_index == 0
    assert second.recommendation.withheld_skills[0].experiment_index == 1


def test_autofix_safe_default_verifies_without_promoting(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")
    trace = _trace()

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "missing-skills").run(
        trace, repository, _plan(), promote=False, persist_trace=True
    )

    assert result.recommendation.skills.indexed_skills == 0
    assert result.evolution.candidate.status == CandidateStatus.VERIFIED
    assert result.evolution.skill is None
    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert result.run.trace == trace
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"


def test_autofix_optionally_recovers_stale_repository_runs_before_start(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")
    stale = ledger.start_autofix_run(
        repository_path=repository,
        trace=AgentTrace(task="Abandoned repair", events=[]),
        generator_provider="old-generator",
        max_attempts=1,
        promote_requested=False,
    )
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE autofix_runs SET updated_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", stale.run_id),
        )

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        recover_stale_after_seconds=60,
    )

    assert ledger.get_autofix_run(stale.run_id).status == AutoFixRunStatus.INTERRUPTED
    assert result.run.status == AutoFixRunStatus.SUCCEEDED


def test_autofix_records_rejected_generated_patch(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository", candidate=0)
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), promote=True
    )

    assert result.evolution.candidate.status == CandidateStatus.REJECTED
    assert [attempt.feedback.phase for attempt in result.attempts] == [
        AutoFixPhase.EVALUATION,
        AutoFixPhase.DEDUPLICATION,
        AutoFixPhase.DEDUPLICATION,
    ]
    assert len(ledger.list_candidates()) == 1
    assert result.run.status == AutoFixRunStatus.REJECTED
    assert len(ledger.autofix_attempts(result.run.run_id)) == 3
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"


def test_autofix_uses_rejection_feedback_to_repair_second_attempt(tmp_path: Path) -> None:
    repository = _adaptive_repository(tmp_path / "repository", "rejected")
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), promote=True
    )

    assert [attempt.feedback.phase for attempt in result.attempts] == [
        AutoFixPhase.EVALUATION,
        AutoFixPhase.COMPLETE,
    ]
    first = result.attempts[0].evolution
    second = result.attempts[1].evolution
    assert first is not None and first.candidate.status == CandidateStatus.REJECTED
    assert second is not None and second.candidate.status == CandidateStatus.LEARNED
    assert second.candidate.metadata["prior_candidate_ids"] == [first.candidate.candidate_id]
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"


def test_autofix_retries_generation_failure_with_structured_feedback(tmp_path: Path) -> None:
    repository = _adaptive_repository(tmp_path / "repository", "generation_error")
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), promote=True
    )

    assert result.attempts[0].generated_patch is None
    assert result.attempts[0].feedback.phase == AutoFixPhase.GENERATION
    assert result.attempts[0].feedback.error_type == "PatchGenerationError"
    assert result.attempts[1].feedback.phase == AutoFixPhase.COMPLETE
    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert [attempt.feedback.phase for attempt in ledger.autofix_attempts(result.run.run_id)] == [
        AutoFixPhase.GENERATION,
        AutoFixPhase.COMPLETE,
    ]
    assert result.evolution.candidate.metadata["autofix_attempt"] == 2
    assert result.evolution.candidate.metadata["previous_attempts"][0]["phase"] == (
        AutoFixPhase.GENERATION
    )
    assert len(ledger.list_candidates()) == 1


def test_autofix_retries_invalid_patch_and_audits_failed_candidate(tmp_path: Path) -> None:
    repository = _adaptive_repository(tmp_path / "repository", "verification_error")
    ledger = RepairLedger(tmp_path / "ledger.db")

    result = AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), promote=True
    )

    assert result.attempts[0].feedback.phase == AutoFixPhase.VERIFICATION
    assert result.attempts[0].feedback.status == CandidateStatus.FAILED
    assert result.attempts[1].feedback.status == CandidateStatus.LEARNED
    records = list(reversed(ledger.list_candidates()))
    assert [record.status for record in records] == [
        CandidateStatus.FAILED,
        CandidateStatus.LEARNED,
    ]
    assert records[0].metadata["failed_from_status"] == CandidateStatus.PROPOSED


def test_autofix_openai_provider_uses_evaluation_feedback_on_retry(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")

    class AdaptiveResponses:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> SimpleNamespace:
            self.calls.append(kwargs)
            replacement = "0" if len(self.calls) == 1 else "2"
            return SimpleNamespace(
                output_text=(f"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-1\n+{replacement}\n")
            )

    responses = AdaptiveResponses()
    client = SimpleNamespace(responses=responses)
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(
            name="model-test",
            model="gpt-test",
            context_paths=["value.txt"],
        ),
        client=client,
    )

    result = AutoFixPipeline(generator, ledger, tmp_path / "skills").run(
        _trace(), repository, _plan(), max_attempts=2
    )

    assert result.run.generator_provider == "model-test/gpt-test"
    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert [attempt.feedback.phase for attempt in result.attempts] == [
        AutoFixPhase.EVALUATION,
        AutoFixPhase.COMPLETE,
    ]
    second_context = json.loads(responses.calls[1]["input"])["autofix_context"]
    assert second_context["attempt_number"] == 2
    assert second_context["verification_plan"]["policy"]["rules"][0]["metric"] == "score"
    assert "benchmark.py" in second_context["protected_paths"]
    assert second_context["previous_attempts"][0]["phase"] == "evaluation"
    assert second_context["previous_attempts"][0]["metrics_after"] == {"score": 0.0}


def test_autofix_adaptive_portfolio_explores_unobserved_provider_on_next_run(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")
    first = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="first-provider",
            argv=[sys.executable, "generator.py"],
        )
    )
    second = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="second-provider",
            argv=[sys.executable, "generator.py"],
        )
    )
    adaptive = AdaptivePatchGenerator(
        AdaptivePatchGeneratorConfig(
            providers=[
                {
                    "name": "first-provider",
                    "argv": [sys.executable, "generator.py"],
                },
                {
                    "name": "second-provider",
                    "argv": [sys.executable, "generator.py"],
                },
            ],
            minimum_trials=1,
            exploration_weight=0,
        ),
        [first, second],
    )
    pipeline = AutoFixPipeline(adaptive, ledger, tmp_path / "skills")

    first_result = pipeline.run(_trace(), repository, _plan())
    second_result = pipeline.run(_trace(), repository, _plan())

    assert first_result.run.generator_provider == "first-provider"
    assert first_result.run.generator_selection is not None
    assert first_result.run.generator_selection.selected_provider == "first-provider"
    assert second_result.run.generator_provider == "second-provider"
    assert second_result.run.generator_selection is not None
    assert second_result.run.generator_selection.selected_provider == "second-provider"
    assert second_result.run.generator_selection.exploration
    outcomes = ledger.provider_outcomes(repository_path=repository)
    assert outcomes["first-provider"].succeeded == 1
    assert outcomes["second-provider"].succeeded == 1


def test_autofix_adaptive_portfolio_fails_over_and_attributes_each_provider(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    (repository / "unavailable.py").write_text(
        "import sys\nprint('temporary outage', file=sys.stderr)\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    ledger = RepairLedger(tmp_path / "ledger.db")
    unavailable = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="unavailable-provider",
            argv=[sys.executable, "unavailable.py"],
        )
    )
    working = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="working-provider",
            argv=[sys.executable, "generator.py"],
        )
    )
    adaptive = AdaptivePatchGenerator(
        AdaptivePatchGeneratorConfig(
            providers=[
                {
                    "name": "unavailable-provider",
                    "argv": [sys.executable, "unavailable.py"],
                },
                {
                    "name": "working-provider",
                    "argv": [sys.executable, "generator.py"],
                },
            ],
            minimum_trials=1,
        ),
        [unavailable, working],
    )

    result = AutoFixPipeline(adaptive, ledger, tmp_path / "skills").run(
        _trace(),
        repository,
        _plan(),
        max_attempts=2,
    )

    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert result.run.generator_provider == "working-provider"
    assert result.run.generator_selection is not None
    assert result.run.generator_selection.initial_selected_provider == "unavailable-provider"
    assert result.run.generator_selection.selected_provider == "working-provider"
    assert len(result.run.generator_selection.failovers) == 1
    assert result.attempts[0].feedback.phase == AutoFixPhase.GENERATION
    assert result.attempts[0].feedback.provider == "unavailable-provider"
    assert result.attempts[1].feedback.provider == "working-provider"
    outcomes = ledger.provider_outcomes(repository_path=repository)
    assert outcomes["unavailable-provider"].failed == 1
    assert outcomes["working-provider"].succeeded == 1


def test_autofix_adaptive_selection_uses_diagnosed_failure_type_context(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")

    def seed(provider: str, failure_type: FailureType, *, accepted: bool) -> None:
        run = ledger.start_autofix_run(
            repository_path=repository,
            trace=AgentTrace(task=f"Seed {provider} {failure_type.value}"),
            generator_provider=provider,
            max_attempts=1,
            promote_requested=False,
        )
        ledger.record_autofix_diagnosis(run.run_id, failure_type)
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

    seed("generalist", FailureType.REASONING, accepted=False)
    seed("reasoner", FailureType.REASONING, accepted=True)
    for _ in range(3):
        seed("generalist", FailureType.RETRIEVAL, accepted=True)
        seed("reasoner", FailureType.RETRIEVAL, accepted=False)

    first = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="generalist",
            argv=[sys.executable, "generator.py"],
        )
    )
    second = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="reasoner",
            argv=[sys.executable, "generator.py"],
        )
    )
    adaptive = AdaptivePatchGenerator(
        AdaptivePatchGeneratorConfig(
            providers=[
                {"name": "generalist", "argv": [sys.executable, "generator.py"]},
                {"name": "reasoner", "argv": [sys.executable, "generator.py"]},
            ],
            minimum_trials=1,
            exploration_weight=0,
        ),
        [first, second],
    )
    adaptive.configure_outcomes(ledger.provider_outcomes(repository_path=repository))
    assert adaptive.provider_name == "generalist"

    result = AutoFixPipeline(adaptive, ledger, tmp_path / "skills").run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
    )

    assert result.run.status == AutoFixRunStatus.SUCCEEDED
    assert result.run.failure_type == FailureType.REASONING
    assert result.run.generator_provider == "reasoner"
    assert result.run.generator_selection is not None
    assert result.run.generator_selection.failure_type == FailureType.REASONING
    assert result.run.generator_selection.initial_selected_provider == "reasoner"


def test_autofix_quarantines_harmful_skill_and_recovers_it_through_probes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "repository")
    skills = tmp_path / "skills"
    _seed_skill(skills)
    ledger = RepairLedger(tmp_path / "ledger.db")
    for _ in range(5):
        _record_skill_outcome(
            ledger,
            repository,
            "historical_score_repair",
            CandidateStatus.REJECTED,
        )
    generator = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="test-command-generator",
            argv=[sys.executable, "generator.py"],
        )
    )
    pipeline = AutoFixPipeline(generator, ledger, skills)

    quarantined = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_quarantine_probe_interval=0,
    )
    assert quarantined.recommendation.skills.matches == []
    assert quarantined.recommendation.skills.quarantined_skills[0].skill_name == (
        "historical_score_repair"
    )
    assert (
        quarantined.recommendation.skills.quarantined_skills[0].status
        == SkillHealthStatus.QUARANTINED
    )

    first_probe = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_quarantine_probe_interval=1,
        skill_ablation_interval=1,
    )
    first_match = first_probe.recommendation.skills.matches[0]
    assert first_match.quarantine_probe
    assert first_probe.recommendation.withheld_skills == []
    assert first_match.health.status == SkillHealthStatus.QUARANTINED
    retrieved = first_probe.evolution.candidate.metadata["retrieved_skills"][0]
    assert retrieved["health"] == SkillHealthStatus.QUARANTINED.value
    assert retrieved["quarantine_probe"] is True
    after_first_probe = ledger.skill_outcomes(repository_path=repository)[
        ("historical_score_repair", 1)
    ]
    assert after_first_probe.accepted == 1
    assert after_first_probe.posterior_success_rate == 0.3

    second_probe = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_quarantine_probe_interval=1,
    )
    assert second_probe.recommendation.skills.matches[0].quarantine_probe

    recovered = pipeline.run(
        _trace(),
        repository,
        _plan(),
        max_attempts=1,
        skill_quarantine_probe_interval=0,
    )
    recovered_match = recovered.recommendation.skills.matches[0]
    assert not recovered_match.quarantine_probe
    assert recovered_match.health.status == SkillHealthStatus.HEALTHY
    assert recovered_match.outcome_stats is not None
    assert recovered_match.outcome_stats.accepted == 2
    assert recovered_match.outcome_stats.posterior_success_rate > 0.3


def test_autofix_never_retries_after_source_was_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path / "repository")
    ledger = RepairLedger(tmp_path / "ledger.db")

    def fail_to_save_skill(*_args: object, **_kwargs: object) -> None:
        raise OSError("skill storage unavailable")

    monkeypatch.setattr(SkillGenerator, "save_versioned", fail_to_save_skill)

    with pytest.raises(OSError, match="skill storage unavailable"):
        AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
            _trace(), repository, _plan(), promote=True, max_attempts=3
        )

    records = ledger.list_candidates()
    assert len(records) == 1
    assert records[0].status == CandidateStatus.FAILED
    assert records[0].metadata["failed_from_status"] == CandidateStatus.PROMOTED
    run = ledger.list_autofix_runs()[0]
    assert run.status == AutoFixRunStatus.FAILED
    assert run.final_candidate_id == records[0].candidate_id
    assert run.error_type == "OSError"
    assert len(ledger.autofix_attempts(run.run_id)) == 1
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"


@pytest.mark.parametrize("max_attempts", [0, 11])
def test_autofix_rejects_invalid_attempt_limit(tmp_path: Path, max_attempts: int) -> None:
    repository = _repository(tmp_path / "repository")

    with pytest.raises(ValueError, match="between 1 and 10"):
        AutoFixPipeline(
            _generator(), RepairLedger(tmp_path / "ledger.db"), tmp_path / "skills"
        ).run(_trace(), repository, _plan(), max_attempts=max_attempts)


@pytest.mark.parametrize("probe_interval", [-1, 1001])
def test_autofix_rejects_invalid_skill_probe_interval(
    tmp_path: Path,
    probe_interval: int,
) -> None:
    repository = _repository(tmp_path / "repository")

    with pytest.raises(ValueError, match="between 0 and 1000"):
        AutoFixPipeline(
            _generator(), RepairLedger(tmp_path / "ledger.db"), tmp_path / "skills"
        ).run(
            _trace(),
            repository,
            _plan(),
            skill_quarantine_probe_interval=probe_interval,
        )


@pytest.mark.parametrize("ablation_interval", [-1, 1001])
def test_autofix_rejects_invalid_skill_ablation_interval(
    tmp_path: Path,
    ablation_interval: int,
) -> None:
    repository = _repository(tmp_path / "repository")

    with pytest.raises(ValueError, match="between 0 and 1000"):
        AutoFixPipeline(
            _generator(), RepairLedger(tmp_path / "ledger.db"), tmp_path / "skills"
        ).run(
            _trace(),
            repository,
            _plan(),
            skill_ablation_interval=ablation_interval,
        )


def test_autofix_protects_generator_program_from_its_output(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repository", modify_generator=True)
    ledger = RepairLedger(tmp_path / "ledger.db")

    with pytest.raises(VerificationError, match="protected path: generator.py"):
        AutoFixPipeline(_generator(), ledger, tmp_path / "skills").run(
            _trace(), repository, _plan(), promote=True
        )

    assert ledger.list_candidates()[0].status == CandidateStatus.FAILED
    run = ledger.list_autofix_runs()[0]
    assert run.status == AutoFixRunStatus.FAILED
    assert run.error_type == "VerificationError"
    assert run.trace is None
    assert len(ledger.autofix_attempts(run.run_id)) == 3
