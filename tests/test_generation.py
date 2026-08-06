import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import OpenAI as SDKOpenAI

from autoharness.diagnosis import FailureDiagnoser
from autoharness.generation import (
    AdaptivePatchGenerator,
    CommandPatchGenerator,
    OpenAIResponsesPatchGenerator,
    PatchGenerationError,
    create_patch_generator,
    parse_patch_generator_config,
    requires_network_generation,
)
from autoharness.models import (
    AdaptivePatchGeneratorConfig,
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixPhase,
    FailureType,
    OpenAIPatchGeneratorConfig,
    PatchGenerationContext,
    PatchGeneratorConfig,
    ProviderOutcomeStats,
)


class _FakeResponses:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return SimpleNamespace(output_text=output)


class _FakeOpenAIClient:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.responses = _FakeResponses(outputs)


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


def test_openai_generator_sends_bounded_redacted_context_and_returns_patch(
    tmp_path: Path,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n# sk-testsecret123456\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=never-send\n", encoding="utf-8")
    (tmp_path / ".uv-cache").mkdir()
    (tmp_path / ".uv-cache" / "cached.py").write_text("NEVER = 'send'\n", encoding="utf-8")
    (tmp_path / "secret_config.py").write_text("PASSWORD = 'never-send'\n", encoding="utf-8")
    client = _FakeOpenAIClient(
        ["--- a/app.py\r\n+++ b/app.py\r\n@@ -1 +1 @@\r\n-VALUE = 1\r\n+VALUE = 2\r\n"]
    )
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(context_paths=["app.py", ".env"]),
        client=client,
    )
    context = _context().model_copy(
        update={
            "trace": _context().trace.model_copy(
                update={"metadata": {"api_key": "never-send", "owner": "test"}}
            )
        }
    )

    result = generator.generate(tmp_path, context)

    assert result.provider == "openai-responses/gpt-5.6-sol"
    assert "\r" not in result.patch
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-sol"
    assert call["reasoning"] == {"effort": "medium"}
    assert call["text"] == {"verbosity": "low"}
    assert call["store"] is False
    payload = json.loads(call["input"])
    assert payload["autofix_context"]["trace"]["metadata"]["api_key"] == "[REDACTED]"
    assert payload["repository"]["files"] == [
        {"path": "app.py", "content": "VALUE = 1\n# [REDACTED]\n"}
    ]
    assert payload["repository"]["available_source_paths"] == ["app.py"]
    assert "never-send" not in call["input"]
    assert generator.protected_paths(tmp_path) == []


def test_openai_generator_requires_key_before_creating_default_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.delenv("AUTOHARNESS_TEST_OPENAI_KEY", raising=False)
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(api_key_env="AUTOHARNESS_TEST_OPENAI_KEY")
    )

    with pytest.raises(PatchGenerationError, match="environment variable is not set"):
        generator.generate(tmp_path, _context())


def test_openai_generator_redacts_provider_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    secret = "sk-providersecret123456"
    monkeypatch.setenv("AUTOHARNESS_TEST_OPENAI_KEY", secret)
    client = _FakeOpenAIClient(
        [RuntimeError(f"authorization failed for {secret}; Bearer opaque-token")]
    )
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(api_key_env="AUTOHARNESS_TEST_OPENAI_KEY"),
        client=client,
    )

    with pytest.raises(PatchGenerationError, match=r"\[REDACTED\]") as error:
        generator.generate(tmp_path, _context())
    assert secret not in str(error.value)
    assert "opaque-token" not in str(error.value)


def test_openai_generator_rejects_missing_safe_context_and_oversized_input(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(context_paths=[".env"]),
        client=_FakeOpenAIClient(["unused"]),
    )
    with pytest.raises(PatchGenerationError, match="No safe source files"):
        generator.generate(tmp_path, _context())

    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    oversized = _context().model_copy(
        update={"trace": _context().trace.model_copy(update={"logs": ["x" * 5000]})}
    )
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(max_input_bytes=4096),
        client=_FakeOpenAIClient(["unused"]),
    )
    with pytest.raises(PatchGenerationError, match="input exceeds"):
        generator.generate(tmp_path, oversized)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (SimpleNamespace(), "did not contain text output"),
        (SimpleNamespace(output_text="explanation only"), "not a unified diff"),
    ],
)
def test_openai_generator_rejects_invalid_responses(
    tmp_path: Path, output: SimpleNamespace, message: str
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    class StaticResponses:
        def create(self, **_kwargs: Any) -> SimpleNamespace:
            return output

    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(),
        client=SimpleNamespace(responses=StaticResponses()),
    )
    with pytest.raises(PatchGenerationError, match=message):
        generator.generate(tmp_path, _context())


def test_openai_generator_enforces_supplied_file_scope(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    patch = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-OTHER = 1\n+OTHER = 2\n"
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(context_paths=["app.py"], max_context_files=1),
        client=_FakeOpenAIClient([patch]),
    )

    with pytest.raises(PatchGenerationError, match="outside the supplied context: other.py"):
        generator.generate(tmp_path, _context())


def test_openai_generator_rejects_protected_paths_before_verification(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(),
        client=_FakeOpenAIClient([patch]),
    )
    context = _context().model_copy(update={"protected_paths": ["app.py"]})

    with pytest.raises(PatchGenerationError, match="modifies protected path: app.py"):
        generator.generate(tmp_path, context)


def test_openai_generator_allows_new_files_only_when_configured(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    patch = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+VALUE = 2\n"
    denied = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(),
        client=_FakeOpenAIClient([patch]),
    )
    with pytest.raises(PatchGenerationError, match="outside the supplied context: new.py"):
        denied.generate(tmp_path, _context())

    allowed = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(allow_new_files=True),
        client=_FakeOpenAIClient([patch]),
    )
    assert "+++ b/new.py" in allowed.generate(tmp_path, _context()).patch


def test_openai_generator_builds_official_client_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    secret = "sk-clientsecret123456"
    monkeypatch.setenv("AUTOHARNESS_TEST_OPENAI_KEY", secret)
    captured: dict[str, Any] = {}

    class FakeSDKClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.responses = _FakeResponses(
                ["--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"]
            )

    monkeypatch.setattr("autoharness.generation.OpenAI", FakeSDKClient)
    generator = OpenAIResponsesPatchGenerator(
        OpenAIPatchGeneratorConfig(
            api_key_env="AUTOHARNESS_TEST_OPENAI_KEY",
            base_url="https://api.example.test/v1",
            timeout_seconds=42,
            max_retries=1,
        )
    )

    generator.generate(tmp_path, _context())

    assert captured == {
        "api_key": secret,
        "base_url": "https://api.example.test/v1",
        "timeout": 42,
        "max_retries": 1,
    }


def test_openai_generator_round_trips_through_official_sdk_without_network(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    captured: dict[str, Any] = {}
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"

    def respond(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": patch,
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as http_client:
        client = SDKOpenAI(
            api_key="sdk-test-key",
            base_url="https://api.example.test/v1",
            http_client=http_client,
            max_retries=0,
        )
        generator = OpenAIResponsesPatchGenerator(
            OpenAIPatchGeneratorConfig(model="gpt-test"),
            client=client,
        )
        result = generator.generate(tmp_path, _context())

    assert result.patch == patch
    assert captured["path"] == "/v1/responses"
    assert captured["authorization"] == "Bearer sdk-test-key"
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["store"] is False
    assert isinstance(captured["body"]["input"], str)


def test_generator_config_factory_preserves_command_compatibility() -> None:
    legacy = parse_patch_generator_config(
        {"argv": [sys.executable, "generator.py"], "legacy_extra": True}
    )
    openai_config = parse_patch_generator_config({"type": "openai", "model": "gpt-test"})

    assert isinstance(legacy, PatchGeneratorConfig)
    assert isinstance(create_patch_generator(legacy), CommandPatchGenerator)
    assert isinstance(openai_config, OpenAIPatchGeneratorConfig)
    assert isinstance(create_patch_generator(openai_config), OpenAIResponsesPatchGenerator)
    with pytest.raises(ValueError, match="Unknown patch generator type"):
        parse_patch_generator_config({"type": "unknown"})
    with pytest.raises(ValueError, match="safe relative paths"):
        OpenAIPatchGeneratorConfig(context_paths=["../secret.py"])


def _provider_stats(
    provider: str,
    *,
    observations: int,
    succeeded: int,
    posterior: float,
) -> ProviderOutcomeStats:
    return ProviderOutcomeStats(
        provider=provider,
        observations=observations,
        succeeded=succeeded,
        rejected=observations - succeeded,
        failed=0,
        interrupted=0,
        posterior_success_rate=posterior,
        confidence=observations / (observations + 5),
        average_attempts=1,
    )


def test_adaptive_generator_explores_under_sampled_then_uses_bayesian_ucb() -> None:
    first = CommandPatchGenerator(PatchGeneratorConfig(name="first", argv=["first-provider"]))
    second = CommandPatchGenerator(PatchGeneratorConfig(name="second", argv=["second-provider"]))
    config = AdaptivePatchGeneratorConfig(
        providers=[
            {"name": "first", "argv": ["first-provider"]},
            {"name": "second", "argv": ["second-provider"]},
        ],
        minimum_trials=2,
        exploration_weight=0.5,
    )
    adaptive = AdaptivePatchGenerator(config, [first, second])

    assert adaptive.provider_name == "first"
    adaptive.configure_outcomes(
        {"first": _provider_stats("first", observations=1, succeeded=1, posterior=0.6)}
    )
    adaptive.configure_selection_context(FailureType.REASONING)
    assert adaptive.provider_name == "second"
    assert adaptive.selection_metadata.failure_type == FailureType.REASONING
    assert adaptive.selection_metadata.exploration
    assert "least-observed" in adaptive.selection_metadata.reason
    assert adaptive.selection_metadata.minimum_trials == 2
    assert adaptive.selection_metadata.exploration_weight == 0.5

    adaptive.configure_outcomes(
        {
            "first": _provider_stats("first", observations=10, succeeded=8, posterior=0.75),
            "second": _provider_stats("second", observations=2, succeeded=1, posterior=0.7),
        }
    )
    assert adaptive.provider_name == "second"
    assert adaptive.selection_metadata.exploration
    assert adaptive.selection_metadata.candidates[1].exploration_bonus > (
        adaptive.selection_metadata.candidates[0].exploration_bonus
    )

    exploit = AdaptivePatchGenerator(
        config.model_copy(update={"exploration_weight": 0}),
        [first, second],
    )
    exploit.configure_outcomes(
        {
            "first": _provider_stats("first", observations=10, succeeded=8, posterior=0.75),
            "second": _provider_stats("second", observations=2, succeeded=1, posterior=0.7),
        }
    )
    assert exploit.provider_name == "first"
    assert not exploit.selection_metadata.exploration


def test_adaptive_generator_protects_every_child_program(tmp_path: Path) -> None:
    (tmp_path / "first.py").write_text("print('first')\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("print('second')\n", encoding="utf-8")
    first = CommandPatchGenerator(
        PatchGeneratorConfig(name="first", argv=[sys.executable, "first.py"])
    )
    second = CommandPatchGenerator(
        PatchGeneratorConfig(name="second", argv=[sys.executable, "second.py"])
    )
    adaptive = AdaptivePatchGenerator(
        AdaptivePatchGeneratorConfig(
            providers=[
                {"name": "first", "argv": [sys.executable, "first.py"]},
                {"name": "second", "argv": [sys.executable, "second.py"]},
            ]
        ),
        [first, second],
    )

    assert adaptive.protected_paths(tmp_path) == ["first.py", "second.py"]


def test_adaptive_config_factory_detects_network_and_rejects_invalid_portfolios() -> None:
    payload = {
        "type": "adaptive",
        "providers": [
            {"type": "command", "name": "local", "argv": ["generator"]},
            {"type": "openai", "name": "model", "model": "gpt-test"},
        ],
    }
    config = parse_patch_generator_config(payload)

    assert isinstance(config, AdaptivePatchGeneratorConfig)
    assert requires_network_generation(config)
    assert isinstance(create_patch_generator(config), AdaptivePatchGenerator)

    nested = {
        "type": "adaptive",
        "providers": [payload, {"name": "fallback", "argv": ["fallback"]}],
    }
    with pytest.raises(ValueError, match="may not be nested"):
        parse_patch_generator_config(nested)

    duplicate = AdaptivePatchGeneratorConfig(
        providers=[
            {"name": "same", "argv": ["one"]},
            {"name": "same", "argv": ["two"]},
        ]
    )
    with pytest.raises(ValueError, match="provider names must be unique"):
        create_patch_generator(duplicate)

    with pytest.raises(ValueError, match="failover phases"):
        AdaptivePatchGeneratorConfig(
            providers=payload["providers"],
            failover_phases=[AutoFixPhase.EVALUATION],
        )


def test_adaptive_generator_fails_over_to_untried_provider_after_generation_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "value.txt").write_text("1\n", encoding="utf-8")
    (tmp_path / "failing.py").write_text(
        "import sys\nprint('provider unavailable', file=sys.stderr)\nraise SystemExit(2)\n",
        encoding="utf-8",
    )
    (tmp_path / "working.py").write_text(
        "from pathlib import Path\n"
        "current = Path('value.txt').read_text().strip()\n"
        "print(f'--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-{current}\\n+2')\n",
        encoding="utf-8",
    )
    first = CommandPatchGenerator(
        PatchGeneratorConfig(name="failing", argv=[sys.executable, "failing.py"])
    )
    second = CommandPatchGenerator(
        PatchGeneratorConfig(name="working", argv=[sys.executable, "working.py"])
    )
    adaptive = AdaptivePatchGenerator(
        AdaptivePatchGeneratorConfig(
            providers=[
                {"name": "failing", "argv": [sys.executable, "failing.py"]},
                {"name": "working", "argv": [sys.executable, "working.py"]},
            ],
            minimum_trials=1,
        ),
        [first, second],
    )

    with pytest.raises(PatchGenerationError, match="provider unavailable"):
        adaptive.generate(tmp_path, _context())

    evaluation_context = _context().model_copy(
        update={
            "attempt_number": 2,
            "previous_attempts": [
                AutoFixAttemptFeedback(
                    attempt_number=1,
                    phase=AutoFixPhase.EVALUATION,
                    provider="failing",
                    accepted=False,
                    rejection_reasons=["score below threshold"],
                )
            ],
        }
    )
    with pytest.raises(PatchGenerationError, match="provider unavailable"):
        adaptive.generate(tmp_path, evaluation_context)
    assert adaptive.provider_name == "failing"
    assert adaptive.selection_metadata.failovers == []

    retry_context = _context().model_copy(
        update={
            "attempt_number": 2,
            "previous_attempts": [
                AutoFixAttemptFeedback(
                    attempt_number=1,
                    phase=AutoFixPhase.GENERATION,
                    provider="failing",
                    error_type="PatchGenerationError",
                    error="provider unavailable",
                )
            ],
        }
    )
    generated = adaptive.generate(tmp_path, retry_context)

    assert generated.provider == "working"
    assert adaptive.selection_metadata.initial_selected_provider == "failing"
    assert adaptive.selection_metadata.selected_provider == "working"
    assert adaptive.selection_metadata.failovers[0].trigger_attempt == 1
    assert adaptive.selection_metadata.failovers[0].trigger_phase == AutoFixPhase.GENERATION
    assert adaptive.selection_metadata.failovers[0].from_provider == "failing"
    assert adaptive.selection_metadata.failovers[0].to_provider == "working"

    exhausted_context = _context().model_copy(
        update={
            "attempt_number": 3,
            "previous_attempts": [
                *retry_context.previous_attempts,
                AutoFixAttemptFeedback(
                    attempt_number=2,
                    phase=AutoFixPhase.GENERATION,
                    provider="working",
                    error_type="PatchGenerationError",
                    error="temporary failure",
                ),
            ],
        }
    )
    with pytest.raises(PatchGenerationError, match="provider unavailable"):
        adaptive.generate(tmp_path, exhausted_context)
    assert adaptive.provider_name == "failing"
    assert adaptive.selection_metadata.failovers[1].from_provider == "working"
    assert adaptive.selection_metadata.failovers[1].to_provider == "failing"
    assert "best alternative" in adaptive.selection_metadata.failovers[1].reason
