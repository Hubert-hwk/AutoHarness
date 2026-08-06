import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from autoharness.cli import app


def _verification_files(root: Path) -> tuple[Path, Path]:
    (root / "value.txt").write_bytes(b"1\n")
    (root / "benchmark.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "value = float(Path('value.txt').read_text().strip())\n"
        "Path('.autoharness-metrics.json').write_text(json.dumps({'score': value}))\n",
        encoding="utf-8",
    )
    patch = root / "candidate.patch"
    patch.write_bytes(b"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-1\n+2\n")
    plan = root / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "commands": [{"name": "benchmark", "argv": [sys.executable, "benchmark.py"]}],
                "policy": {"rules": [{"metric": "score", "direction": "higher_is_better"}]},
            }
        ),
        encoding="utf-8",
    )
    return patch, plan


def test_verify_patch_cli_requires_execution_acknowledgement(tmp_path: Path) -> None:
    patch, plan = _verification_files(tmp_path)

    result = CliRunner().invoke(
        app,
        ["verify-patch", str(patch), str(plan), "--repo", str(tmp_path)],
    )

    assert result.exit_code == 4
    assert "--allow-command-execution" in result.output


def test_verify_patch_cli_outputs_accepted_result(tmp_path: Path) -> None:
    patch, plan = _verification_files(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "verify-patch",
            str(patch),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
        ],
    )

    assert result.exit_code == 0
    assert '"accepted": true' in result.output


def test_repair_patch_cli_requires_source_acknowledgement(tmp_path: Path) -> None:
    patch, plan = _verification_files(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "repair-patch",
            str(patch),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
        ],
    )

    assert result.exit_code == 5
    assert "--apply-to-source" in result.output


def test_repair_patch_cli_promotes_candidate(tmp_path: Path) -> None:
    patch, plan = _verification_files(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "repair-patch",
            str(patch),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
            "--apply-to-source",
        ],
    )

    assert result.exit_code == 0
    assert '"applied": true' in result.output
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "2\n"
