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
from autoharness.verification import (
    PatchPromoter,
    PatchValidator,
    PatchVerifier,
    RepairPipeline,
    VerificationError,
)


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
    assert len(result.attestation.patch_sha256) == 64
    assert result.attestation.source_files[0].path == "value.txt"
    assert result.attestation.source_files[0].existed


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
        (
            "--- a/.autoharness/state.json\n+++ b/.autoharness/state.json\n@@ -1 +1 @@\n-a\n+b\n",
            "protected path",
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


def test_repair_pipeline_promotes_accepted_patch_with_backup(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = RepairPipeline().run(repository, _patch(2), _plan(), promote=True)

    assert result.verification.accepted
    assert result.promotion is not None
    assert result.promotion.applied
    assert (repository / "value.txt").read_text(encoding="utf-8") == "2\n"
    backup = Path(result.promotion.backup_path)
    assert (backup / "files" / "value.txt").read_text(encoding="utf-8") == "1\n"
    assert (backup / "manifest.json").is_file()


def test_rejected_patch_is_never_promoted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    result = RepairPipeline().run(repository, _patch(0), _plan(), promote=True)

    assert not result.verification.accepted
    assert result.promotion is None
    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"
    assert not (repository / ".autoharness").exists()


def test_promoter_rejects_source_change_after_verification(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    patch = _patch(2)
    plan = _plan()
    verification = PatchVerifier().verify(repository, patch, plan)
    (repository / "value.txt").write_bytes(b"3\n")

    with pytest.raises(VerificationError, match="Source files changed"):
        PatchPromoter().promote(repository, patch, plan, verification)


def test_promoter_rejects_patch_replacement(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan()
    verification = PatchVerifier().verify(repository, _patch(2), plan)

    with pytest.raises(VerificationError, match="Patch content differs"):
        PatchPromoter().promote(repository, _patch(3), plan, verification)


def test_promoter_restores_backup_when_apply_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    patch = _patch(2)
    plan = _plan()
    verification = PatchVerifier().verify(repository, patch, plan)
    promoter = PatchPromoter()

    def fail_after_write(workspace: Path, _patch_text: str) -> None:
        (workspace / "value.txt").write_bytes(b"corrupt\n")
        raise VerificationError("simulated apply failure")

    monkeypatch.setattr(promoter.patch_validator, "apply", fail_after_write)

    with pytest.raises(VerificationError, match="simulated apply failure"):
        promoter.promote(repository, patch, plan, verification)

    assert (repository / "value.txt").read_text(encoding="utf-8") == "1\n"
