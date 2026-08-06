"""Pluggable patch generation interfaces and isolated command adapter."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Protocol

from autoharness.models import (
    GeneratedPatch,
    PatchGenerationContext,
    PatchGeneratorConfig,
)
from autoharness.verification import SourceFingerprinter, isolated_repository_copy


class PatchGenerationError(RuntimeError):
    """Raised when a patch provider fails or returns an invalid payload."""


class PatchGenerator(Protocol):
    """Provider-neutral interface used by the AutoFix orchestrator."""

    def generate(
        self,
        repository: str | Path,
        context: PatchGenerationContext,
    ) -> GeneratedPatch: ...

    def protected_paths(self, repository: str | Path) -> list[str]: ...


class CommandPatchGenerator:
    """Run a trusted argv provider in a disposable repository copy.

    The provider receives ``PatchGenerationContext`` JSON on stdin and must emit only a
    UTF-8 unified diff on stdout. Diagnostic output belongs on stderr.
    """

    stderr_limit = 4000

    def __init__(self, config: PatchGeneratorConfig) -> None:
        self.config = config

    def generate(
        self,
        repository: str | Path,
        context: PatchGenerationContext,
    ) -> GeneratedPatch:
        source = Path(repository).expanduser().resolve()
        if not source.is_dir():
            raise PatchGenerationError(f"Repository path is not a directory: {source}")
        environment = os.environ.copy()
        environment.update(self.config.env)
        environment["AUTOHARNESS_GENERATION"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        payload = context.model_dump_json().encode("utf-8")
        started = time.monotonic()
        try:
            with isolated_repository_copy(source) as workspace:
                environment["AUTOHARNESS_WORKSPACE"] = str(workspace)
                completed = subprocess.run(
                    self.config.argv,
                    cwd=workspace,
                    env=environment,
                    input=payload,
                    capture_output=True,
                    timeout=self.config.timeout_seconds,
                    shell=False,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise PatchGenerationError(
                f"Patch generator timed out after {self.config.timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise PatchGenerationError(f"Cannot execute patch generator: {exc}") from exc

        duration_ms = (time.monotonic() - started) * 1000
        stderr = completed.stderr.decode("utf-8", errors="replace")[-self.stderr_limit :]
        if completed.returncode != 0:
            detail = stderr.strip() or f"exit code {completed.returncode}"
            raise PatchGenerationError(f"Patch generator failed: {detail}")
        if len(completed.stdout) > self.config.max_patch_bytes:
            raise PatchGenerationError(
                f"Generated patch exceeds {self.config.max_patch_bytes} byte limit"
            )
        try:
            decoded = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchGenerationError("Patch generator output is not valid UTF-8") from exc
        patch = decoded.replace("\r\n", "\n").replace("\r", "\n")
        if not patch.strip():
            raise PatchGenerationError("Patch generator returned an empty patch")
        if "\x00" in patch:
            raise PatchGenerationError("Patch generator output contains NUL bytes")
        if not any(line.startswith(("--- ", "diff --git ")) for line in patch.splitlines()):
            raise PatchGenerationError("Patch generator output is not a unified diff")
        return GeneratedPatch(
            provider=self.config.name,
            patch=patch,
            patch_sha256=SourceFingerprinter.patch_sha256(patch),
            duration_ms=duration_ms,
            stderr_tail=stderr,
        )

    def protected_paths(self, repository: str | Path) -> list[str]:
        source = Path(repository).expanduser().resolve()
        protected: list[str] = []
        for argument in self.config.argv:
            path = Path(argument)
            if path.is_absolute() or ".." in path.parts:
                continue
            if (source / path).is_file():
                protected.append(path.as_posix())
        return protected
