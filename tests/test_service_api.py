from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from autoharness.api import app
from autoharness.ledger import RepairLedger
from autoharness.models import AgentTrace, CandidateStatus, FailureType, RepairExperience, Skill
from autoharness.service import AutoHarness
from autoharness.skills import SkillGenerator


def test_health_and_diagnose_api() -> None:
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"
    response = client.post(
        "/v1/diagnose",
        json={
            "task": "Call an order API",
            "events": [
                {
                    "kind": "tool_call",
                    "name": "order_api",
                    "status": "failure",
                    "error": "connection timeout",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["failure_type"] == "tool_failure"


def test_learn_persists_portable_yaml(tmp_path: Path) -> None:
    experience = RepairExperience(
        title="Order API timeout repair",
        failure_type=FailureType.TOOL,
        trigger_terms=["timeout", "order_api"],
        root_cause="Timeout was too short",
        repair_steps=["Increase the timeout", "Add bounded retries"],
        validation_steps=["Run timeout integration tests"],
        affected_components=["order_api"],
    )

    skill, path = AutoHarness().learn(experience, tmp_path)

    assert path.exists()
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert content["name"] == skill.name
    assert content["workflow"] == experience.repair_steps


def test_evaluation_api_rejects_threshold_violation() -> None:
    response = TestClient(app).post(
        "/v1/evaluate",
        json={
            "baseline": {"metrics": {"success_rate": 0.8}},
            "candidate": {"metrics": {"success_rate": 0.7}},
            "policy": {
                "rules": [
                    {
                        "metric": "success_rate",
                        "direction": "higher_is_better",
                        "threshold": 0.75,
                    }
                ]
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert "below minimum" in response.json()["rejection_reasons"][0]


def test_unknown_diagnosis_uses_global_skill_evidence(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    SkillGenerator().save(
        Skill(
            name="general_repair",
            description="General recovery procedure",
            failure_type=FailureType.REASONING,
            triggers=["unexpected"],
            context={"root_cause": "unexpected behavior"},
            workflow=["Inspect the behavior"],
            evaluation=["Run the benchmark"],
        ),
        skills,
    )
    ledger = RepairLedger(tmp_path / "ledger.db")
    candidate = ledger.propose(
        title="Historical global evidence",
        repository_path=tmp_path,
        patch_sha256="a" * 64,
        failure_type=FailureType.REASONING,
        metadata={"retrieved_skills": [{"name": "general_repair", "version": 1}]},
    )
    ledger.transition(candidate.candidate_id, CandidateStatus.VERIFIED)

    result = AutoHarness().recommend_skills(
        AgentTrace(task="Investigate unexpected behavior"),
        skills,
        repository_path=tmp_path,
        ledger_path=ledger.path,
    )

    assert result.diagnosis.failure_type == FailureType.UNKNOWN
    assert result.skills.matches[0].outcome_stats is not None
    assert result.skills.matches[0].outcome_stats.failure_type is None
    assert result.skills.matches[0].outcome_stats.accepted == 1
