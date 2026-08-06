import sys
from pathlib import Path

import pytest

from autoharness.diagnosis import FailureDiagnoser
from autoharness.generation import CommandPatchGenerator, PatchGenerationError
from autoharness.models import (
    AgentTrace,
    PatchGenerationContext,
    PatchGeneratorConfig,
)


def _context() -> PatchGenerationContext:
    trace = AgentTrace(task="Fix an incorrect low score", feedback="incorrect answer")
    return PatchGenerationContext(
        trace=trace,
        diagnosis=FailureDiagnoser().diagnose(trace),
    )


def test_command_generator_runs_in_copy_and_returns_unified_diff(tmp_path: Path) -> None:
    (tmp_path / "value.txt").write_bytes(b"1\n")
    (tmp_path / "generator.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "context = json.load(sys.stdin)\n"
        "assert context['trace']['task'].startswith('Fix')\n"
        "value = Path('value.txt').read_text().strip()\n"
        "sys.stdout.write(f'--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-{value}\\n+2\\n')\n",
        encoding="utf-8",
    )
    generator = CommandPatchGenerator(
        PatchGeneratorConfig(
            name="test-generator",
            argv=[sys.executable, "generator.py"],
        )
    )

    result = generator.generate(tmp_path, _context())

    assert result.provider == "test-generator"
    assert "+2\n" in result.patch
    assert "\r" not in result.patch
    assert len(result.patch_sha256) == 64
    assert generator.protected_paths(tmp_path) == ["generator.py"]
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "1\n"


@pytest.mark.parametrize(
    ("program", "message"),
    [
        (
            "import sys; print('provider error', file=sys.stderr); raise SystemExit(7)",
            "provider error",
        ),
        ("print('not a patch')", "not a unified diff"),
        ("print('')", "empty patch"),
    ],
)
def test_command_generator_reports_invalid_provider_output(
    tmp_path: Path, program: str, message: str
) -> None:
    (tmp_path / "generator.py").write_text(program, encoding="utf-8")
    generator = CommandPatchGenerator(PatchGeneratorConfig(argv=[sys.executable, "generator.py"]))

    with pytest.raises(PatchGenerationError, match=message):
        generator.generate(tmp_path, _context())


def test_command_generator_enforces_timeout(tmp_path: Path) -> None:
    (tmp_path / "generator.py").write_text(
        "import time\ntime.sleep(2)\n",
        encoding="utf-8",
    )
    generator = CommandPatchGenerator(
        PatchGeneratorConfig(
            argv=[sys.executable, "generator.py"],
            timeout_seconds=0.05,
        )
    )

    with pytest.raises(PatchGenerationError, match="timed out"):
        generator.generate(tmp_path, _context())
