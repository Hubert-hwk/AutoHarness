from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from autoharness.api import app
from autoharness.models import FailureType, RepairExperience
from autoharness.service import AutoHarness


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
