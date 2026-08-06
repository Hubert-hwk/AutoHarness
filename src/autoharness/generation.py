"""Pluggable command and model-backed patch generation adapters."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI

from autoharness.models import (
    AdaptivePatchGeneratorConfig,
    FailureType,
    GeneratedPatch,
    OpenAIPatchGeneratorConfig,
    PatchGenerationContext,
    PatchGeneratorConfig,
    ProviderFailoverEvent,
    ProviderOutcomeStats,
    ProviderSelection,
    ProviderSelectionCandidate,
)
from autoharness.verification import (
    PatchValidator,
    SourceFingerprinter,
    VerificationError,
    isolated_repository_copy,
)


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


GeneratorConfig = PatchGeneratorConfig | OpenAIPatchGeneratorConfig | AdaptivePatchGeneratorConfig


def parse_patch_generator_config(value: object) -> GeneratorConfig:
    """Parse a tagged provider config while preserving legacy command JSON files."""
    if not isinstance(value, dict):
        raise ValueError("Patch generator configuration must be a JSON object")
    provider_type = value.get("type", "command")
    if provider_type == "command":
        return PatchGeneratorConfig.model_validate(value)
    if provider_type == "openai":
        return OpenAIPatchGeneratorConfig.model_validate(value)
    if provider_type == "adaptive":
        config = AdaptivePatchGeneratorConfig.model_validate(value)
        for child in config.providers:
            child_config = parse_patch_generator_config(child)
            if isinstance(child_config, AdaptivePatchGeneratorConfig):
                raise ValueError("Adaptive patch generator portfolios may not be nested")
        return config
    raise ValueError("Unknown patch generator type")


def create_patch_generator(config: GeneratorConfig) -> PatchGenerator:
    if isinstance(config, OpenAIPatchGeneratorConfig):
        return OpenAIResponsesPatchGenerator(config)
    if isinstance(config, AdaptivePatchGeneratorConfig):
        child_configs = [parse_patch_generator_config(item) for item in config.providers]
        if any(isinstance(item, AdaptivePatchGeneratorConfig) for item in child_configs):
            raise ValueError("Adaptive patch generator portfolios may not be nested")
        children = [create_patch_generator(item) for item in child_configs]
        return AdaptivePatchGenerator(config, children)
    return CommandPatchGenerator(config)


def requires_network_generation(config: GeneratorConfig) -> bool:
    if isinstance(config, OpenAIPatchGeneratorConfig):
        return True
    if isinstance(config, AdaptivePatchGeneratorConfig):
        return any(
            requires_network_generation(parse_patch_generator_config(item))
            for item in config.providers
        )
    return False


def generator_provider_name(generator: PatchGenerator) -> str:
    provider_name = getattr(generator, "provider_name", None)
    if provider_name:
        return str(provider_name)
    config = getattr(generator, "config", None)
    name = getattr(config, "name", None)
    return str(name or type(generator).__name__)


def _validated_patch(raw: str, *, max_patch_bytes: int) -> str:
    encoded = raw.encode("utf-8")
    if len(encoded) > max_patch_bytes:
        raise PatchGenerationError(f"Generated patch exceeds {max_patch_bytes} byte limit")
    patch = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not patch.strip():
        raise PatchGenerationError("Patch generator returned an empty patch")
    if "\x00" in patch:
        raise PatchGenerationError("Patch generator output contains NUL bytes")
    if not any(line.startswith(("--- ", "diff --git ")) for line in patch.splitlines()):
        raise PatchGenerationError("Patch generator output is not a unified diff")
    return patch


class CommandPatchGenerator:
    """Run a trusted argv provider in a disposable repository copy.

    The provider receives ``PatchGenerationContext`` JSON on stdin and must emit only a
    UTF-8 unified diff on stdout. Diagnostic output belongs on stderr.
    """

    stderr_limit = 4000

    def __init__(self, config: PatchGeneratorConfig) -> None:
        self.config = config

    @property
    def provider_name(self) -> str:
        return self.config.name

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
        try:
            decoded = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchGenerationError("Patch generator output is not valid UTF-8") from exc
        patch = _validated_patch(decoded, max_patch_bytes=self.config.max_patch_bytes)
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


class OpenAIResponsesPatchGenerator:
    """Generate one unified diff with the OpenAI Responses API.

    Repository contents are bounded and filtered before leaving the machine. The CLI adds a
    separate explicit network acknowledgement because traces and selected source are sent to the
    configured API endpoint.
    """

    _source_extensions = {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
    }
    _ignored_directories = {
        ".autoharness",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".uv-cache",
        ".uv-python",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "vendor",
    }
    _sensitive_names = {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
    _sensitive_suffixes = {".key", ".p12", ".pfx", ".pem"}
    _sensitive_name_markers = {"api_key", "credential", "private_key", "secret"}
    _secret_pattern = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
    _bearer_pattern = re.compile(r"(?i)(bearer\s+)[^\s,;}]+")
    _sensitive_key_pattern = re.compile(
        r"(^|_)(api_?key|authorization|cookie|credential|password|secret|token)($|_)",
        re.IGNORECASE,
    )
    _instructions = (
        "You repair a repository from a diagnosed failure. Return exactly one UTF-8 unified "
        "diff and no Markdown fences, prose, commands, or tool calls. Use repository-relative "
        "paths with a/ and b/ prefixes. Change only what is necessary. Do not modify existing "
        "files not included in the supplied repository context. Create new files only when "
        "patch_scope allows it. Never modify a protected_paths entry. Use the verification plan "
        "as the acceptance contract, incorporate previous attempt feedback, and ensure the patch "
        "can be applied to the exact supplied file contents."
    )

    def __init__(self, config: OpenAIPatchGeneratorConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    @property
    def provider_name(self) -> str:
        return f"{self.config.name}/{self.config.model}"

    def generate(
        self,
        repository: str | Path,
        context: PatchGenerationContext,
    ) -> GeneratedPatch:
        source = Path(repository).expanduser().resolve()
        if not source.is_dir():
            raise PatchGenerationError(f"Repository path is not a directory: {source}")
        started = time.monotonic()
        prompt, provided_paths = self._prompt(source, context)
        try:
            client = self._client or self._create_client()
            response = client.responses.create(
                model=self.config.model,
                instructions=self._instructions,
                input=prompt,
                max_output_tokens=self.config.max_output_tokens,
                reasoning={"effort": self.config.reasoning_effort},
                text={"verbosity": self.config.text_verbosity},
                store=self.config.store,
            )
        except PatchGenerationError:
            raise
        except Exception as exc:
            raise PatchGenerationError(
                f"OpenAI patch generation failed: {self._safe_error(exc)}"
            ) from exc
        duration_ms = (time.monotonic() - started) * 1000
        raw = getattr(response, "output_text", None)
        if not isinstance(raw, str):
            raise PatchGenerationError("OpenAI response did not contain text output")
        patch = _validated_patch(raw, max_patch_bytes=self.config.max_patch_bytes)
        self._enforce_patch_scope(source, patch, provided_paths, context)
        return GeneratedPatch(
            provider=self.provider_name,
            patch=patch,
            patch_sha256=SourceFingerprinter.patch_sha256(patch),
            duration_ms=duration_ms,
        )

    def protected_paths(self, repository: str | Path) -> list[str]:
        return []

    def _create_client(self) -> OpenAI:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise PatchGenerationError(
                f"OpenAI API key environment variable is not set: {self.config.api_key_env}"
            )
        options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": self.config.timeout_seconds,
            "max_retries": self.config.max_retries,
        }
        if self.config.base_url is not None:
            options["base_url"] = self.config.base_url
        return OpenAI(**options)

    def _prompt(self, source: Path, context: PatchGenerationContext) -> tuple[str, set[str]]:
        files, available_paths = self._repository_context(source, context)
        payload = {
            "autofix_context": self._sanitize(context.model_dump(mode="json")),
            "repository": {
                "name": source.name,
                "available_source_paths": available_paths,
                "files": files,
            },
            "patch_scope": {
                "modifiable_paths": [item["path"] for item in files],
                "allow_new_files": self.config.allow_new_files,
            },
            "required_output": "One directly applicable unified diff and nothing else.",
        }
        prompt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(prompt.encode("utf-8")) > self.config.max_input_bytes:
            raise PatchGenerationError(
                f"OpenAI generation input exceeds {self.config.max_input_bytes} byte limit"
            )
        return prompt, {item["path"] for item in files}

    def _enforce_patch_scope(
        self,
        source: Path,
        patch: str,
        provided_paths: set[str],
        context: PatchGenerationContext,
    ) -> None:
        metrics_file = (
            context.verification_plan.metrics_file
            if context.verification_plan is not None
            else ".autoharness-model-scope-sentinel"
        )
        try:
            changed_paths = PatchValidator().inspect(
                patch,
                protected_paths=context.protected_paths,
                metrics_file=metrics_file,
            )
        except VerificationError as exc:
            raise PatchGenerationError(f"OpenAI patch failed safety validation: {exc}") from exc
        outside_scope = [
            path
            for path in changed_paths
            if path not in provided_paths
            and not (self.config.allow_new_files and not (source / path).exists())
        ]
        if outside_scope:
            raise PatchGenerationError(
                "OpenAI patch modifies paths outside the supplied context: "
                + ", ".join(outside_scope)
            )

    def _repository_context(
        self,
        source: Path,
        context: PatchGenerationContext,
    ) -> tuple[list[dict[str, str]], list[str]]:
        discovered = self._discover_source_paths(source)
        requested = [*self.config.context_paths, *(item.path for item in context.code_locations)]
        ordered = list(dict.fromkeys([*requested, *discovered]))
        files: list[dict[str, str]] = []
        total_bytes = 0
        for relative_value in ordered:
            if len(files) >= self.config.max_context_files:
                break
            relative = Path(relative_value)
            unresolved = source / relative
            if unresolved.is_symlink():
                continue
            target = unresolved.resolve()
            try:
                target.relative_to(source)
            except ValueError:
                continue
            if self._is_sensitive(relative) or not target.is_file():
                continue
            try:
                raw = target.read_bytes()
            except OSError:
                continue
            if (
                len(raw) > self.config.max_file_bytes
                or total_bytes + len(raw) > self.config.max_context_bytes
            ):
                continue
            if b"\x00" in raw:
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
            files.append(
                {"path": relative.as_posix(), "content": self._redact_text(normalized_content)}
            )
            total_bytes += len(raw)
        if not files:
            raise PatchGenerationError("No safe source files are available for OpenAI generation")
        return files, discovered[:500]

    def _discover_source_paths(self, source: Path) -> list[str]:
        discovered: list[str] = []
        scanned_files = 0
        for root, directories, filenames in os.walk(source):
            directories[:] = sorted(
                item
                for item in directories
                if not item.startswith(".")
                and item.lower() not in self._ignored_directories
                and not (Path(root) / item).is_symlink()
            )
            root_path = Path(root)
            for filename in sorted(filenames):
                scanned_files += 1
                if scanned_files > self.config.max_scan_files:
                    return discovered
                target = root_path / filename
                relative = target.relative_to(source)
                if (
                    target.is_symlink()
                    or target.suffix.lower() not in self._source_extensions
                    or self._is_sensitive(relative)
                ):
                    continue
                discovered.append(relative.as_posix())
                if len(discovered) >= self.config.max_discovered_paths:
                    return discovered
        return discovered

    def _is_sensitive(self, relative: Path) -> bool:
        name = relative.name.lower()
        return (
            name in self._sensitive_names
            or name.startswith(".env.")
            or relative.suffix.lower() in self._sensitive_suffixes
            or any(marker in name for marker in self._sensitive_name_markers)
        )

    def _safe_error(self, error: Exception) -> str:
        detail = self._redact_text(str(error)[-4000:])
        api_key = os.environ.get(self.config.api_key_env)
        if api_key:
            detail = detail.replace(api_key, "[REDACTED]")
        return detail

    def _sanitize(self, value: Any, *, key: str | None = None) -> Any:
        if key is not None and self._sensitive_key_pattern.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): self._sanitize(item, key=str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def _redact_text(self, value: str) -> str:
        redacted = self._secret_pattern.sub("[REDACTED]", value)
        return self._bearer_pattern.sub(r"\1[REDACTED]", redacted)


class AdaptivePatchGenerator:
    """Choose one generator per AutoFix run using conservative historical evidence."""

    def __init__(
        self,
        config: AdaptivePatchGeneratorConfig,
        generators: list[PatchGenerator],
    ) -> None:
        if len(generators) != len(config.providers):
            raise ValueError("Adaptive generator count does not match provider configuration")
        names = [generator_provider_name(item) for item in generators]
        if len(names) != len(set(names)):
            raise ValueError("Adaptive generator provider names must be unique")
        self.config = config
        self.generators = generators
        self._selected_index = 0
        self._selection: ProviderSelection
        self.configure_outcomes({})

    @property
    def provider_name(self) -> str:
        return generator_provider_name(self.generators[self._selected_index])

    @property
    def selection_metadata(self) -> ProviderSelection:
        return self._selection

    def configure_outcomes(self, outcomes: dict[str, ProviderOutcomeStats]) -> None:
        evidence = [
            self._stats(generator_provider_name(item), outcomes) for item in self.generators
        ]
        under_sampled = [
            index
            for index, stats in enumerate(evidence)
            if stats.observations < self.config.minimum_trials
        ]
        if under_sampled:
            selected = min(under_sampled, key=lambda index: (evidence[index].observations, index))
            candidates = [
                ProviderSelectionCandidate(
                    provider=stats.provider,
                    observations=stats.observations,
                    posterior_success_rate=stats.posterior_success_rate,
                    exploration_bonus=0,
                    selection_score=stats.posterior_success_rate,
                    under_sampled=index in under_sampled,
                )
                for index, stats in enumerate(evidence)
            ]
            exploration = True
            reason = (
                f"Selected least-observed provider below minimum_trials="
                f"{self.config.minimum_trials}"
            )
        else:
            total = sum(item.observations for item in evidence)
            candidates = []
            for stats in evidence:
                bonus = self.config.exploration_weight * math.sqrt(
                    math.log(total + 1) / stats.observations
                )
                candidates.append(
                    ProviderSelectionCandidate(
                        provider=stats.provider,
                        observations=stats.observations,
                        posterior_success_rate=stats.posterior_success_rate,
                        exploration_bonus=round(bonus, 4),
                        selection_score=round(stats.posterior_success_rate + bonus, 4),
                    )
                )
            selected = max(
                range(len(candidates)),
                key=lambda index: (candidates[index].selection_score, -index),
            )
            posterior_best = max(
                range(len(candidates)),
                key=lambda index: (candidates[index].posterior_success_rate, -index),
            )
            exploration = selected != posterior_best
            reason = "Selected highest Bayesian posterior plus UCB exploration bonus"

        self._selected_index = selected
        self._selection = ProviderSelection(
            portfolio=self.config.name,
            minimum_trials=self.config.minimum_trials,
            exploration_weight=self.config.exploration_weight,
            selected_provider=candidates[selected].provider,
            initial_selected_provider=candidates[selected].provider,
            exploration=exploration,
            reason=reason,
            candidates=candidates,
        )

    def configure_selection_context(self, failure_type: FailureType | None) -> None:
        self._selection = self._selection.model_copy(update={"failure_type": failure_type})

    def generate(
        self,
        repository: str | Path,
        context: PatchGenerationContext,
    ) -> GeneratedPatch:
        self._apply_failover(context)
        return self.generators[self._selected_index].generate(repository, context)

    def protected_paths(self, repository: str | Path) -> list[str]:
        return list(
            dict.fromkeys(
                path
                for generator in self.generators
                for path in generator.protected_paths(repository)
            )
        )

    @staticmethod
    def _stats(
        provider: str,
        outcomes: dict[str, ProviderOutcomeStats],
    ) -> ProviderOutcomeStats:
        return outcomes.get(
            provider,
            ProviderOutcomeStats(
                provider=provider,
                observations=0,
                succeeded=0,
                rejected=0,
                failed=0,
                interrupted=0,
                posterior_success_rate=0.5,
                confidence=0,
                average_attempts=0,
            ),
        )

    def _apply_failover(self, context: PatchGenerationContext) -> None:
        if not context.previous_attempts or not self.config.failover_phases:
            return
        trigger = context.previous_attempts[-1]
        current_provider = self.provider_name
        if (
            trigger.phase not in self.config.failover_phases
            or trigger.provider != current_provider
            or any(
                item.trigger_attempt == trigger.attempt_number for item in self._selection.failovers
            )
        ):
            return

        attempted = {item.provider for item in context.previous_attempts}
        untried = [
            index
            for index, candidate in enumerate(self._selection.candidates)
            if candidate.provider not in attempted
        ]
        alternatives = untried or [
            index
            for index, candidate in enumerate(self._selection.candidates)
            if candidate.provider != current_provider
        ]
        if not alternatives:
            return
        selected = max(
            alternatives,
            key=lambda index: (self._selection.candidates[index].selection_score, -index),
        )
        target = self._selection.candidates[selected].provider
        reason = (
            f"Failed over after attempt {trigger.attempt_number} ended in "
            f"{trigger.phase.value}; selected "
            f"{'an untried provider' if untried else 'the best alternative provider'}"
        )
        event = ProviderFailoverEvent(
            trigger_attempt=trigger.attempt_number,
            trigger_phase=trigger.phase,
            from_provider=current_provider,
            to_provider=target,
            reason=reason,
        )
        self._selected_index = selected
        self._selection = self._selection.model_copy(
            update={
                "selected_provider": target,
                "exploration": True,
                "reason": reason,
                "failovers": [*self._selection.failovers, event],
            }
        )
