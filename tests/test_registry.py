from pathlib import Path

import pytest
import yaml

from autoharness.models import (
    FailureType,
    Skill,
    SkillHealthStatus,
    SkillOutcomeStats,
    SkillQuery,
)
from autoharness.registry import SkillRegistry, SkillRegistryError
from autoharness.skills import SkillGenerator


def _skill(name: str, version: int = 1, failure_type: FailureType = FailureType.REASONING) -> Skill:
    return Skill(
        name=name,
        description="Improve a low benchmark score",
        failure_type=failure_type,
        triggers=["low score", "incorrect answer"],
        context={
            "root_cause": "The configured value was too low",
            "affected_components": ["value"],
        },
        workflow=["Increase the value"],
        evaluation=["Run the score benchmark"],
        version=version,
    )


def test_registry_keeps_latest_version_and_explains_ranking(tmp_path: Path) -> None:
    generator = SkillGenerator()
    generator.save_versioned(_skill("improve_score"), tmp_path)
    generator.save_versioned(_skill("improve_score"), tmp_path)
    generator.save_versioned(
        _skill("repair_tool", failure_type=FailureType.TOOL),
        tmp_path,
    )

    result = SkillRegistry(tmp_path).search(
        SkillQuery(
            failure_type=FailureType.REASONING,
            text="The answer is incorrect and has a low score",
            components=["value"],
        )
    )

    assert result.indexed_skills == 2
    assert result.ignored_older_versions == 1
    assert len(result.matches) == 1
    assert result.matches[0].skill.name == "improve_score"
    assert result.matches[0].skill.version == 2
    assert result.matches[0].score > 10
    assert any("failure type" in reason for reason in result.matches[0].reasons)
    assert any("component" in reason for reason in result.matches[0].reasons)


def test_registry_reports_invalid_files_without_losing_valid_skills(tmp_path: Path) -> None:
    SkillGenerator().save(_skill("valid"), tmp_path)
    (tmp_path / "invalid.yaml").write_text("- not\n- an\n- object\n", encoding="utf-8")

    result = SkillRegistry(tmp_path).search(
        SkillQuery(failure_type=FailureType.REASONING, text="low score")
    )

    assert result.indexed_skills == 1
    assert len(result.invalid_files) == 1
    assert "must contain an object" in result.invalid_files[0].error
    assert result.matches[0].skill.name == "valid"


def test_registry_supports_cross_failure_search_when_requested(tmp_path: Path) -> None:
    SkillGenerator().save(
        _skill("tool_timeout", failure_type=FailureType.TOOL),
        tmp_path,
    )

    result = SkillRegistry(tmp_path).search(
        SkillQuery(
            failure_type=FailureType.REASONING,
            text="incorrect answer with low score",
            same_failure_only=False,
        )
    )

    assert result.matches[0].skill.name == "tool_timeout"


def test_registry_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(SkillRegistryError, match="not a directory"):
        SkillRegistry(tmp_path / "missing").search(SkillQuery(text="anything"))


def test_registry_rejects_oversized_skill_as_invalid(tmp_path: Path) -> None:
    path = tmp_path / "large.yaml"
    path.write_text(
        yaml.safe_dump(_skill("large").model_dump(mode="json")) + ("#" * 100),
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)
    registry.max_skill_bytes = 10

    result = registry.search(SkillQuery(text="low score"))

    assert result.indexed_skills == 0
    assert "1 MiB safety limit" in result.invalid_files[0].error


def test_registry_reranks_with_explainable_outcome_evidence(tmp_path: Path) -> None:
    generator = SkillGenerator()
    generator.save(_skill("a_poor"), tmp_path)
    generator.save(_skill("z_good"), tmp_path)
    outcomes = {
        ("a_poor", 1): SkillOutcomeStats(
            skill_name="a_poor",
            skill_version=1,
            observations=10,
            accepted=0,
            rejected=10,
            unevaluated_failures=0,
            post_acceptance_failures=0,
            posterior_success_rate=0.1429,
            confidence=0.6667,
            score_adjustment=-0.952,
        ),
        ("z_good", 1): SkillOutcomeStats(
            skill_name="z_good",
            skill_version=1,
            observations=10,
            accepted=10,
            rejected=0,
            unevaluated_failures=0,
            post_acceptance_failures=0,
            posterior_success_rate=0.8571,
            confidence=0.6667,
            score_adjustment=0.952,
        ),
    }

    result = SkillRegistry(tmp_path, outcomes).search(
        SkillQuery(
            failure_type=FailureType.REASONING,
            text="incorrect answer with a low score",
            components=["value"],
        )
    )

    assert [match.skill.name for match in result.matches] == ["z_good"]
    assert result.matches[0].outcome_stats == outcomes[("z_good", 1)]
    assert any("observed outcomes" in reason for reason in result.matches[0].reasons)
    assert any("score +0.952" in reason for reason in result.matches[0].reasons)
    assert result.quarantined_skills[0].skill_name == "a_poor"
    assert result.quarantined_skills[0].status == SkillHealthStatus.QUARANTINED

    included = SkillRegistry(tmp_path, outcomes).search(
        SkillQuery(
            failure_type=FailureType.REASONING,
            text="incorrect answer with a low score",
            components=["value"],
            include_quarantined=True,
        )
    )
    assert [match.skill.name for match in included.matches] == ["z_good", "a_poor"]
    assert any("explicit override" in reason for reason in included.matches[1].reasons)

    probe = SkillRegistry(tmp_path, outcomes).search(
        SkillQuery(
            failure_type=FailureType.REASONING,
            text="incorrect answer with a low score",
            components=["value"],
            quarantine_probe_index=0,
        )
    )
    assert [match.skill.name for match in probe.matches] == ["z_good", "a_poor"]
    assert probe.matches[1].quarantine_probe
    assert any("controlled quarantine" in reason for reason in probe.matches[1].reasons)


def test_new_skill_version_resets_quarantine_history(tmp_path: Path) -> None:
    generator = SkillGenerator()
    generator.save_versioned(_skill("repair"), tmp_path)
    generator.save_versioned(_skill("repair"), tmp_path)
    old_outcome = SkillOutcomeStats(
        skill_name="repair",
        skill_version=1,
        observations=10,
        accepted=0,
        rejected=10,
        unevaluated_failures=0,
        post_acceptance_failures=0,
        posterior_success_rate=0.1429,
        confidence=0.6667,
        score_adjustment=-0.952,
    )

    result = SkillRegistry(tmp_path, {("repair", 1): old_outcome}).search(
        SkillQuery(failure_type=FailureType.REASONING, text="incorrect low score")
    )

    assert result.matches[0].skill.version == 2
    assert result.matches[0].health.status == SkillHealthStatus.UNOBSERVED
    assert result.quarantined_skills == []


def test_registry_explains_and_applies_controlled_ablation_score(tmp_path: Path) -> None:
    for name in ("a_harmful", "z_beneficial"):
        SkillGenerator().save(_skill(name), tmp_path)
    outcomes = {
        (name, 1): SkillOutcomeStats(
            skill_name=name,
            skill_version=1,
            observations=4,
            accepted=2,
            rejected=2,
            unevaluated_failures=0,
            post_acceptance_failures=0,
            posterior_success_rate=0.5,
            confidence=4 / 9,
            score_adjustment=adjustment,
            control_observations=4,
            control_accepted=control_accepted,
            control_rejected=4 - control_accepted,
            control_posterior_success_rate=control_rate,
            estimated_lift=0.5 - control_rate,
            ablation_confidence=4 / 9,
            ablation_score_adjustment=adjustment,
        )
        for name, adjustment, control_accepted, control_rate in (
            ("a_harmful", -0.3, 3, 5 / 8),
            ("z_beneficial", 0.3, 1, 3 / 8),
        )
    }

    result = SkillRegistry(tmp_path, outcomes).search(
        SkillQuery(text="incorrect answer with low score", limit=2)
    )

    assert [match.skill.name for match in result.matches] == ["z_beneficial", "a_harmful"]
    assert any("controlled ablation" in reason for reason in result.matches[0].reasons)
    assert any("estimated lift +0.125" in reason for reason in result.matches[0].reasons)


def test_registry_rotates_controlled_probes_across_quarantined_skills(tmp_path: Path) -> None:
    for name in ("a_harmful", "b_harmful"):
        SkillGenerator().save(_skill(name), tmp_path)
    outcomes = {
        (name, 1): SkillOutcomeStats(
            skill_name=name,
            skill_version=1,
            observations=5,
            accepted=0,
            rejected=5,
            unevaluated_failures=0,
            post_acceptance_failures=0,
            posterior_success_rate=0.2222,
            confidence=0.5,
            score_adjustment=-0.5556,
        )
        for name in ("a_harmful", "b_harmful")
    }
    registry = SkillRegistry(tmp_path, outcomes)

    first = registry.search(
        SkillQuery(text="incorrect low score", limit=1, quarantine_probe_index=0)
    )
    second = registry.search(
        SkillQuery(text="incorrect low score", limit=1, quarantine_probe_index=1)
    )

    assert first.matches[0].skill.name == "a_harmful"
    assert second.matches[0].skill.name == "b_harmful"
    assert first.matches[0].quarantine_probe
    assert second.matches[0].quarantine_probe
