"""Isolated benchmark execution and non-destructive patch verification."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from autoharness.evaluation import EvaluationGate
from autoharness.models import (
    BenchmarkCommand,
    BenchmarkRun,
    CommandExecution,
    EvaluationSnapshot,
    PatchPromotionResult,
    PatchVerificationPlan,
    PatchVerificationResult,
    RepairPipelineResult,
    SourceFileFingerprint,
    VerificationAttestation,
)


class VerificationError(RuntimeError):
    """Raised when a patch or benchmark cannot be verified safely."""


class PatchValidator:
    """Validate patch structure and paths before delegating to ``git apply``."""

    max_patch_bytes = 2 * 1024 * 1024

    def inspect(
        self,
        patch: str,
        protected_paths: list[str],
        metrics_file: str,
    ) -> list[str]:
        if not patch.strip():
            raise VerificationError("Patch is empty")
        if len(patch.encode("utf-8")) > self.max_patch_bytes:
            raise VerificationError("Patch exceeds the 2 MiB safety limit")
        if "GIT binary patch" in patch:
            raise VerificationError("Binary patches are not supported")
        if "120000" in patch and ("new file mode" in patch or "new mode" in patch):
            raise VerificationError("Patches may not create or modify symbolic links")

        paths: set[str] = set()
        for line in patch.splitlines():
            if line.startswith(("--- ", "+++ ")):
                parsed = self._parse_header_path(line[4:])
                if parsed is not None:
                    paths.add(parsed)
        if not paths:
            raise VerificationError("Patch does not contain unified diff file headers")

        protected = [*protected_paths, metrics_file]
        for path in paths:
            if any(path == item or path.startswith(f"{item}/") for item in protected):
                raise VerificationError(f"Patch modifies protected path: {path}")
        return sorted(paths)

    def apply(self, workspace: Path, patch: str) -> None:
        git = shutil.which("git")
        if git is None:
            raise VerificationError("git is required to apply patch candidates")
        check = self._git_apply(git, workspace, patch, check=True)
        if check.returncode != 0:
            detail = self._tail(check.stderr or check.stdout)
            raise VerificationError(f"Patch check failed: {detail}")
        applied = self._git_apply(git, workspace, patch, check=False)
        if applied.returncode != 0:
            detail = self._tail(applied.stderr or applied.stdout)
            raise VerificationError(f"Patch apply failed: {detail}")

    def _parse_header_path(self, raw: str) -> str | None:
        value = raw.split("\t", 1)[0].strip()
        if value == "/dev/null":
            return None
        if value.startswith('"'):
            try:
                value = str(json.loads(value))
            except json.JSONDecodeError as exc:
                raise VerificationError("Patch contains an invalid quoted path") from exc
        if value.startswith(("a/", "b/")):
            value = value[2:]
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise VerificationError(f"Patch contains an unsafe path: {value}")
        if {".autoharness", ".git"}.intersection(path.parts) or ":" in path.parts[0]:
            raise VerificationError(f"Patch contains a protected path: {value}")
        return path.as_posix()

    @staticmethod
    def _git_apply(
        git: str,
        workspace: Path,
        patch: str,
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        command = [git, "apply", "--whitespace=error"]
        if check:
            command.append("--check")
        command.append("-")
        try:
            return subprocess.run(
                command,
                cwd=workspace,
                input=patch.encode("utf-8"),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VerificationError("Patch application timed out") from exc

    @staticmethod
    def _tail(value: str | bytes, limit: int = 2000) -> str:
        decoded = value.decode(errors="replace") if isinstance(value, bytes) else value
        return decoded[-limit:].strip()


class BenchmarkRunner:
    """Execute trusted argv commands without a shell and collect JSON metrics."""

    output_limit = 4000

    def run(self, workspace: Path, plan: PatchVerificationPlan, run_id: str) -> BenchmarkRun:
        metrics_path = workspace / Path(plan.metrics_file)
        self._remove_stale_metrics(metrics_path)
        executions: list[CommandExecution] = []

        for command in plan.commands:
            execution = self._execute(workspace, command)
            executions.append(execution)
            if plan.stop_on_failure and execution.exit_code != 0:
                break

        failed = [execution.name for execution in executions if execution.exit_code != 0]
        metrics = self._read_metrics(metrics_path)
        snapshot = EvaluationSnapshot(
            run_id=run_id,
            metrics=metrics,
            tests_passed=not failed and len(executions) == len(plan.commands),
            failed_tests=failed,
            metadata={"commands_completed": len(executions)},
        )
        return BenchmarkRun(snapshot=snapshot, commands=executions)

    def _execute(self, workspace: Path, command: BenchmarkCommand) -> CommandExecution:
        environment = os.environ.copy()
        environment.update(command.env)
        environment["AUTOHARNESS_WORKSPACE"] = str(workspace)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command.argv,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                shell=False,
                check=False,
            )
            return CommandExecution(
                name=command.name,
                argv=command.argv,
                exit_code=completed.returncode,
                duration_ms=(time.monotonic() - started) * 1000,
                stdout_tail=self._tail(completed.stdout),
                stderr_tail=self._tail(completed.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            return CommandExecution(
                name=command.name,
                argv=command.argv,
                exit_code=-1,
                duration_ms=(time.monotonic() - started) * 1000,
                stdout_tail=self._tail(exc.stdout),
                stderr_tail=self._tail(exc.stderr),
                timed_out=True,
            )
        except OSError as exc:
            return CommandExecution(
                name=command.name,
                argv=command.argv,
                exit_code=-1,
                duration_ms=(time.monotonic() - started) * 1000,
                stderr_tail=str(exc),
            )

    @staticmethod
    def _remove_stale_metrics(path: Path) -> None:
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.exists():
            raise VerificationError(f"Metrics path is not a file: {path.name}")

    @staticmethod
    def _read_metrics(path: Path) -> dict[str, float]:
        if not path.is_file():
            return {}
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationError(f"Cannot read benchmark metrics: {exc}") from exc
        if not isinstance(raw, dict):
            raise VerificationError("Benchmark metrics must be a JSON object")
        metrics: dict[str, float] = {}
        for key, value in raw.items():
            if (
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int | float)
            ):
                raise VerificationError("Benchmark metrics must map strings to finite numbers")
            number = float(value)
            if not math.isfinite(number):
                raise VerificationError("Benchmark metrics must map strings to finite numbers")
            metrics[key] = number
        return metrics

    def _tail(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        decoded = value.decode(errors="replace") if isinstance(value, bytes) else value
        return decoded[-self.output_limit :]


class SourceFingerprinter:
    """Capture stable hashes for the exact source files touched by a patch."""

    @staticmethod
    def patch_sha256(patch: str) -> str:
        return hashlib.sha256(patch.encode("utf-8")).hexdigest()

    def capture(self, source: Path, paths: list[str]) -> list[SourceFileFingerprint]:
        fingerprints: list[SourceFileFingerprint] = []
        for relative in sorted(paths):
            target = source / Path(relative)
            if target.is_symlink():
                raise VerificationError(f"Patch target may not be a symbolic link: {relative}")
            if not target.exists():
                fingerprints.append(SourceFileFingerprint(path=relative, existed=False))
                continue
            if not target.is_file():
                raise VerificationError(f"Patch target is not a regular file: {relative}")
            fingerprints.append(
                SourceFileFingerprint(
                    path=relative,
                    existed=True,
                    sha256=self._file_sha256(target),
                )
            )
        return fingerprints

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class PatchVerifier:
    """Benchmark a source baseline and patched candidate in separate disposable copies."""

    def __init__(self) -> None:
        self.patch_validator = PatchValidator()
        self.benchmark_runner = BenchmarkRunner()
        self.evaluation_gate = EvaluationGate()
        self.fingerprinter = SourceFingerprinter()

    def verify(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
    ) -> PatchVerificationResult:
        source = Path(repository).expanduser().resolve()
        if not source.is_dir():
            raise VerificationError(f"Repository path is not a directory: {source}")
        protected_paths = [*plan.protected_paths, *self._command_input_paths(source, plan)]
        changed_paths = self.patch_validator.inspect(
            patch,
            protected_paths,
            plan.metrics_file,
        )
        attestation = VerificationAttestation(
            patch_sha256=self.fingerprinter.patch_sha256(patch),
            source_files=self.fingerprinter.capture(source, changed_paths),
        )

        with self._isolated_copy(source) as baseline_workspace:
            baseline = self.benchmark_runner.run(baseline_workspace, plan, "baseline")
        with self._isolated_copy(source) as candidate_workspace:
            self.patch_validator.apply(candidate_workspace, patch)
            candidate = self.benchmark_runner.run(candidate_workspace, plan, "candidate")

        evaluation = self.evaluation_gate.evaluate(
            baseline.snapshot,
            candidate.snapshot,
            plan.policy,
        )
        return PatchVerificationResult(
            accepted=evaluation.accepted,
            changed_paths=changed_paths,
            baseline=baseline,
            candidate=candidate,
            evaluation=evaluation,
            attestation=attestation,
        )

    @staticmethod
    def _command_input_paths(source: Path, plan: PatchVerificationPlan) -> list[str]:
        """Protect relative files named by benchmark commands from candidate edits."""
        protected: list[str] = []
        for command in plan.commands:
            for argument in command.argv:
                path = Path(argument)
                if path.is_absolute() or ".." in path.parts:
                    continue
                if (source / path).is_file():
                    protected.append(path.as_posix())
        return protected

    @contextmanager
    def _isolated_copy(self, source: Path) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="autoharness-") as temporary:
            workspace = Path(temporary) / "workspace"
            shutil.copytree(source, workspace, ignore=self._copy_ignore, symlinks=True)
            yield workspace

    @staticmethod
    def _copy_ignore(directory: str, names: list[str]) -> set[str]:
        excluded_names = {
            ".git",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".uv-cache",
            ".uv-python",
            ".venv",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
        }
        ignored = {name for name in names if name in excluded_names}
        base = Path(directory)
        ignored.update(name for name in names if (base / name).is_symlink())
        return ignored


class PatchPromoter:
    """Apply an accepted patch only when its verified source state is still current."""

    def __init__(self) -> None:
        self.patch_validator = PatchValidator()
        self.fingerprinter = SourceFingerprinter()

    def promote(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        verification: PatchVerificationResult,
    ) -> PatchPromotionResult:
        if not verification.accepted:
            raise VerificationError("Rejected patches cannot be promoted")
        source = Path(repository).expanduser().resolve()
        if not source.is_dir():
            raise VerificationError(f"Repository path is not a directory: {source}")

        protected_paths = [*plan.protected_paths, *PatchVerifier._command_input_paths(source, plan)]
        changed_paths = self.patch_validator.inspect(
            patch,
            protected_paths,
            plan.metrics_file,
        )
        if changed_paths != verification.changed_paths:
            raise VerificationError("Patch paths differ from the verified candidate")

        patch_sha256 = self.fingerprinter.patch_sha256(patch)
        if patch_sha256 != verification.attestation.patch_sha256:
            raise VerificationError("Patch content differs from the verified candidate")

        current = self.fingerprinter.capture(source, changed_paths)
        if current != verification.attestation.source_files:
            raise VerificationError("Source files changed after verification; re-run verification")

        backup = self._create_backup(source, current, patch_sha256)
        try:
            self.patch_validator.apply(source, patch)
        except VerificationError:
            self._restore_backup(source, backup, current)
            raise
        return PatchPromotionResult(
            applied=True,
            changed_paths=changed_paths,
            patch_sha256=patch_sha256,
            backup_path=str(backup),
        )

    @staticmethod
    def _create_backup(
        source: Path,
        fingerprints: list[SourceFileFingerprint],
        patch_sha256: str,
    ) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = source / ".autoharness" / "backups" / f"{timestamp}-{patch_sha256[:8]}"
        files = backup / "files"
        files.mkdir(parents=True)
        for fingerprint in fingerprints:
            if fingerprint.existed:
                destination = files / Path(fingerprint.path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / Path(fingerprint.path), destination)
        manifest = {
            "patch_sha256": patch_sha256,
            "source_files": [item.model_dump(mode="json") for item in fingerprints],
        }
        (backup / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
        return backup

    @staticmethod
    def _restore_backup(
        source: Path,
        backup: Path,
        fingerprints: list[SourceFileFingerprint],
    ) -> None:
        for fingerprint in fingerprints:
            target = source / Path(fingerprint.path)
            if fingerprint.existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / "files" / Path(fingerprint.path), target)
            elif target.is_file() or target.is_symlink():
                target.unlink()


class RepairPipeline:
    """Verify a repair candidate and optionally promote it to the source checkout."""

    def __init__(self) -> None:
        self.verifier = PatchVerifier()
        self.promoter = PatchPromoter()

    def run(
        self,
        repository: str | Path,
        patch: str,
        plan: PatchVerificationPlan,
        *,
        promote: bool = False,
    ) -> RepairPipelineResult:
        verification = self.verifier.verify(repository, patch, plan)
        promotion = None
        if promote and verification.accepted:
            promotion = self.promoter.promote(repository, patch, plan, verification)
        return RepairPipelineResult(verification=verification, promotion=promotion)
