import json
import sys
from pathlib import Path

import pytest

from autoharness.models import (
    BenchmarkCommand,
    EvaluationPolicy,
    MetricDirection,
    MetricRule,
    PatchVerificationPlan,
)
from autoharness.verification import PatchValidator, PatchVerifier, VerificationError


def _repository(path: Path) -> Path:
    # Keep LF bytes even on Windows; patch input must not be newline-translated.
    (path / "value.txt").write_bytes(b"1\n")
    (path / "benchmark.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "score = float(Path('value.txt').read_text().strip())\n"
        "Path('.autoharness-metrics.json').write_text(\n"
        "    json.dumps({'score': score}), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    return path


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


def _patch(candidate: int) -> str:
    return f"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-1\n+{candidate}\n"


def test_patch_verifier_accepts_improvement_without_mutating_source(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = PatchVerifier().verify(repository, _patch(2), _plan())

    assert result.accepted
    assert result.changed_paths == ["value.txt"]
    assert result.baseline.snapshot.metrics == {"score": 1.0}
    assert result.candidate.snapshot.metrics == {"score": 2.0}
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"
    assert not (repository / ".autoharness-metrics.json").exists()


def test_patch_verifier_rejects_regression(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = PatchVerifier().verify(repository, _patch(0), _plan())

    assert not result.accepted
    assert any("regressed" in reason for reason in result.evaluation.rejection_reasons)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            "--- a/../secret.txt\n+++ b/../secret.txt\n@@ -1 +1 @@\n-a\n+b\n",
            "unsafe path",
        ),
        (
            "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-a\n+b\n",
            "protected path",
        ),
        (
            "--- a/image.bin\n+++ b/image.bin\nGIT binary patch\n",
            "Binary patches",
        ),
    ],
)
def test_patch_validator_rejects_unsafe_changes(patch: str, message: str) -> None:
    with pytest.raises(VerificationError, match=message):
        PatchValidator().inspect(patch, ["tests"], ".autoharness-metrics.json")


def test_verifier_protects_benchmark_program_from_patch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    patch = (
        "--- a/benchmark.py\n"
        "+++ b/benchmark.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-import json\n"
        "+import json # modified\n"
        " from pathlib import Path\n"
    )

    with pytest.raises(VerificationError, match="protected path: benchmark.py"):
        PatchVerifier().verify(repository, patch, _plan())


def test_invalid_metrics_are_reported(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "benchmark.py").write_text(
        "from pathlib import Path\n"
        "Path('.autoharness-metrics.json').write_text('[]', encoding='utf-8')\n",
        encoding="utf-8",
    )

    with pytest.raises(VerificationError, match="JSON object"):
        PatchVerifier().verify(repository, _patch(2), _plan())


def test_plan_json_round_trip(tmp_path: Path) -> None:
    plan = _plan()
    output = tmp_path / "plan.json"
    output.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    restored = PatchVerificationPlan.model_validate(json.loads(output.read_text(encoding="utf-8")))

    assert restored == plan
