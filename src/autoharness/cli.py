"""Command-line interface for AutoHarness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from autoharness.code_graph import PythonCodeGraph
from autoharness.generation import (
    GeneratorConfig,
    PatchGenerationError,
    create_patch_generator,
    parse_patch_generator_config,
    requires_network_generation,
)
from autoharness.ledger import LedgerError, RepairLedger
from autoharness.models import (
    AgentTrace,
    AutoFixRunStatus,
    CandidateStatus,
    EvaluationRequest,
    PatchVerificationPlan,
    RepairExperience,
)
from autoharness.registry import SkillRegistryError
from autoharness.service import AutoHarness
from autoharness.verification import VerificationError

app = typer.Typer(no_args_is_help=True, help="Diagnose and improve AI agent systems.")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Cannot read JSON from {path}: {exc}") from exc


def _load_generator_config(path: Path) -> GeneratorConfig:
    try:
        return parse_patch_generator_config(_load_json(path))
    except ValidationError as exc:
        safe_errors = exc.errors(include_url=False, include_input=False)
        raise typer.BadParameter(
            f"Invalid patch generator configuration: {json.dumps(safe_errors)}"
        ) from exc
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid patch generator configuration: {exc}") from exc


@app.command()
def analyze(
    trace_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    repo: Annotated[Path | None, typer.Option("--repo", help="Python repository to index")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 8,
) -> None:
    """Diagnose a trace and optionally localize the issue in code."""
    trace = AgentTrace.model_validate(_load_json(trace_file))
    result = AutoHarness().analyze(trace, repo, limit)
    typer.echo(result.model_dump_json(indent=2))


@app.command("index")
def index_repository(
    repository: Annotated[Path, typer.Argument(exists=True, file_okay=False, readable=True)],
) -> None:
    """Build a lightweight Python code graph and print its statistics."""
    stats = PythonCodeGraph(repository).build()
    typer.echo(json.dumps(vars(stats), indent=2))


@app.command()
def learn(
    repair_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    output: Annotated[Path, typer.Option("--output", help="Skill output directory")] = Path(
        "skills"
    ),
) -> None:
    """Convert a validated repair experience into a reusable YAML skill."""
    experience = RepairExperience.model_validate(_load_json(repair_file))
    skill, path = AutoHarness().learn(experience, output)
    typer.echo(f"Created {skill.name} at {path}")


@app.command()
def evaluate(
    evaluation_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Apply tests, thresholds, and regression gates to a repair candidate."""
    request = EvaluationRequest.model_validate(_load_json(evaluation_file))
    result = AutoHarness().evaluate(request.baseline, request.candidate, request.policy)
    typer.echo(result.model_dump_json(indent=2))
    if not result.accepted:
        raise typer.Exit(code=2)


@app.command("verify-patch")
def verify_patch(
    patch_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    repo: Annotated[
        Path, typer.Option("--repo", exists=True, file_okay=False, readable=True)
    ] = Path("."),
    allow_command_execution: Annotated[
        bool,
        typer.Option(
            "--allow-command-execution",
            help="Required acknowledgement that benchmark commands are trusted.",
        ),
    ] = False,
) -> None:
    """Verify a unified diff in disposable baseline and candidate workspaces."""
    if not allow_command_execution:
        typer.echo(
            "Refusing to execute benchmark commands without --allow-command-execution",
            err=True,
        )
        raise typer.Exit(code=4)
    try:
        patch = patch_file.read_text(encoding="utf-8")
        plan = PatchVerificationPlan.model_validate(_load_json(plan_file))
        result = AutoHarness().verify_patch(repo, patch, plan)
    except (OSError, VerificationError) as exc:
        typer.echo(f"Verification failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(result.model_dump_json(indent=2))
    if not result.accepted:
        raise typer.Exit(code=2)


@app.command("repair-patch")
def repair_patch(
    patch_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    repo: Annotated[
        Path, typer.Option("--repo", exists=True, file_okay=False, readable=True)
    ] = Path("."),
    allow_command_execution: Annotated[
        bool,
        typer.Option(
            "--allow-command-execution",
            help="Required acknowledgement that benchmark commands are trusted.",
        ),
    ] = False,
    apply_to_source: Annotated[
        bool,
        typer.Option(
            "--apply-to-source",
            help="Required acknowledgement that an accepted patch may modify the source.",
        ),
    ] = False,
) -> None:
    """Verify and promote an accepted patch to the source with a recoverable backup."""
    if not allow_command_execution:
        typer.echo(
            "Refusing to execute benchmark commands without --allow-command-execution",
            err=True,
        )
        raise typer.Exit(code=4)
    if not apply_to_source:
        typer.echo("Refusing to modify the source without --apply-to-source", err=True)
        raise typer.Exit(code=5)
    try:
        patch = patch_file.read_text(encoding="utf-8")
        plan = PatchVerificationPlan.model_validate(_load_json(plan_file))
        result = AutoHarness().repair_patch(repo, patch, plan, promote=True)
    except (OSError, VerificationError) as exc:
        typer.echo(f"Repair failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(result.model_dump_json(indent=2))
    if not result.verification.accepted:
        raise typer.Exit(code=2)


@app.command("evolve-patch")
def evolve_patch(
    patch_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    experience_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    repo: Annotated[
        Path, typer.Option("--repo", exists=True, file_okay=False, readable=True)
    ] = Path("."),
    ledger: Annotated[Path | None, typer.Option("--ledger")] = None,
    skills: Annotated[Path | None, typer.Option("--skills")] = None,
    allow_command_execution: Annotated[bool, typer.Option("--allow-command-execution")] = False,
    apply_to_source: Annotated[bool, typer.Option("--apply-to-source")] = False,
) -> None:
    """Verify, promote, record, and learn a reusable skill from one repair."""
    if not allow_command_execution:
        typer.echo(
            "Refusing to execute benchmark commands without --allow-command-execution",
            err=True,
        )
        raise typer.Exit(code=4)
    if not apply_to_source:
        typer.echo("Refusing to modify the source without --apply-to-source", err=True)
        raise typer.Exit(code=5)
    source = repo.expanduser().resolve()
    ledger_path = ledger or source / ".autoharness" / "ledger.db"
    skill_directory = skills or source / ".autoharness" / "skills"
    try:
        patch = patch_file.read_text(encoding="utf-8")
        plan = PatchVerificationPlan.model_validate(_load_json(plan_file))
        experience = RepairExperience.model_validate(_load_json(experience_file))
        result = AutoHarness().evolve_patch(
            source,
            patch,
            plan,
            experience,
            ledger_path=ledger_path,
            skill_directory=skill_directory,
            promote=True,
        )
    except (LedgerError, OSError, VerificationError) as exc:
        typer.echo(f"Evolution failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(result.model_dump_json(indent=2))
    if result.candidate.status == CandidateStatus.REJECTED:
        raise typer.Exit(code=2)


@app.command("repair-history")
def repair_history(
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
    status: Annotated[CandidateStatus | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
) -> None:
    """List persisted repair candidates, newest first."""
    records = RepairLedger(ledger).list_candidates(status=status, limit=limit)
    typer.echo(json.dumps([item.model_dump(mode="json") for item in records], indent=2))


@app.command("repair-events")
def repair_events(
    candidate_id: Annotated[str, typer.Argument()],
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
) -> None:
    """Show the append-only lifecycle events for one repair candidate."""
    try:
        events = RepairLedger(ledger).events(candidate_id)
    except LedgerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(json.dumps([item.model_dump(mode="json") for item in events], indent=2))


@app.command("recommend-skills")
def recommend_skills(
    trace_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    skills: Annotated[Path, typer.Option("--skills", exists=True, file_okay=False)],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", exists=True, file_okay=False, readable=True),
    ] = None,
    ledger: Annotated[
        Path | None,
        typer.Option("--ledger", exists=True, dir_okay=False, readable=True),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 5,
    cross_failure: Annotated[
        bool,
        typer.Option("--cross-failure", help="Allow matches from other failure types."),
    ] = False,
) -> None:
    """Diagnose a trace and retrieve relevant learned repair Skills."""
    try:
        trace = AgentTrace.model_validate(_load_json(trace_file))
        result = AutoHarness().recommend_skills(
            trace,
            skills,
            repository_path=repo,
            ledger_path=ledger,
            limit=limit,
            same_failure_only=not cross_failure,
        )
    except (OSError, SkillRegistryError, ValueError) as exc:
        typer.echo(f"Skill recommendation failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(result.model_dump_json(indent=2))


@app.command("skill-outcomes")
def skill_outcomes(
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", exists=True, file_okay=False, readable=True),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50_000)] = 5000,
) -> None:
    """Show outcome evidence associated with retrieved Skill versions."""
    outcomes = RepairLedger(ledger).skill_outcomes(repository_path=repo, limit=limit)
    ordered = sorted(
        outcomes.values(),
        key=lambda item: (-item.observations, item.skill_name, -item.skill_version),
    )
    typer.echo(json.dumps([item.model_dump(mode="json") for item in ordered], indent=2))


@app.command("provider-outcomes")
def provider_outcomes(
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", exists=True, file_okay=False, readable=True),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50_000)] = 5000,
) -> None:
    """Show repository-scoped generator outcome evidence used by adaptive portfolios."""
    outcomes = RepairLedger(ledger).provider_outcomes(repository_path=repo, limit=limit)
    ordered = sorted(outcomes.values(), key=lambda item: (-item.observations, item.provider))
    typer.echo(json.dumps([item.model_dump(mode="json") for item in ordered], indent=2))


@app.command("autofix-runs")
def autofix_runs(
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
    status: Annotated[AutoFixRunStatus | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
) -> None:
    """List persisted AutoFix runs, newest first."""
    runs = RepairLedger(ledger).list_autofix_runs(status=status, limit=limit)
    typer.echo(json.dumps([item.model_dump(mode="json") for item in runs], indent=2))


@app.command("autofix-run")
def autofix_run(
    run_id: Annotated[str, typer.Argument()],
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
) -> None:
    """Show one AutoFix run and its immutable attempt records."""
    history = RepairLedger(ledger)
    try:
        run = history.get_autofix_run(run_id)
        attempts = history.autofix_attempts(run_id)
    except LedgerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(
        json.dumps(
            {
                "run": run.model_dump(mode="json"),
                "attempts": [item.model_dump(mode="json") for item in attempts],
            },
            indent=2,
        )
    )


@app.command("autofix-recover")
def autofix_recover(
    ledger: Annotated[Path, typer.Option("--ledger", exists=True, dir_okay=False)],
    older_than_seconds: Annotated[
        float,
        typer.Option("--older-than-seconds", min=0),
    ] = 3600,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", exists=True, file_okay=False, readable=True),
    ] = None,
) -> None:
    """Interrupt stale AutoFix runs and fail their unfinished candidates atomically."""
    recovered = RepairLedger(ledger).recover_stale_autofix_runs(
        older_than_seconds=older_than_seconds,
        repository_path=repo,
    )
    typer.echo(json.dumps([item.model_dump(mode="json") for item in recovered], indent=2))


@app.command("autofix")
def autofix(
    trace_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    generator_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    plan_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    repo: Annotated[
        Path, typer.Option("--repo", exists=True, file_okay=False, readable=True)
    ] = Path("."),
    ledger: Annotated[Path | None, typer.Option("--ledger")] = None,
    skills: Annotated[Path | None, typer.Option("--skills")] = None,
    skill_limit: Annotated[int, typer.Option("--skill-limit", min=1, max=50)] = 5,
    max_attempts: Annotated[int, typer.Option("--max-attempts", min=1, max=10)] = 3,
    allow_command_execution: Annotated[bool, typer.Option("--allow-command-execution")] = False,
    allow_network_generation: Annotated[
        bool,
        typer.Option(
            "--allow-network-generation",
            help="Allow a network provider to send the Trace and selected source context.",
        ),
    ] = False,
    apply_to_source: Annotated[bool, typer.Option("--apply-to-source")] = False,
    persist_trace: Annotated[
        bool,
        typer.Option(
            "--persist-trace",
            help="Store the full trace in the ledger; the default stores only its SHA-256.",
        ),
    ] = False,
    recover_stale_after_seconds: Annotated[
        float | None,
        typer.Option(
            "--recover-stale-after",
            min=0,
            help="Interrupt older running records for this repository before starting.",
        ),
    ] = None,
) -> None:
    """Diagnose, retrieve experience, generate, verify, and optionally learn a repair."""
    if not allow_command_execution:
        typer.echo(
            "Refusing to execute generator and benchmark commands without "
            "--allow-command-execution",
            err=True,
        )
        raise typer.Exit(code=4)
    source = repo.expanduser().resolve()
    ledger_path = ledger or source / ".autoharness" / "ledger.db"
    skill_directory = skills or source / ".autoharness" / "skills"
    try:
        trace = AgentTrace.model_validate(_load_json(trace_file))
        generator_config = _load_generator_config(generator_file)
        if requires_network_generation(generator_config) and not allow_network_generation:
            typer.echo(
                "Refusing to send Trace and source context to a network provider without "
                "--allow-network-generation",
                err=True,
            )
            raise typer.Exit(code=6)
        verification_plan = PatchVerificationPlan.model_validate(_load_json(plan_file))
        result = AutoHarness().autofix(
            trace,
            source,
            create_patch_generator(generator_config),
            verification_plan,
            ledger_path=ledger_path,
            skill_directory=skill_directory,
            promote=apply_to_source,
            skill_limit=skill_limit,
            max_attempts=max_attempts,
            persist_trace=persist_trace,
            recover_stale_after_seconds=recover_stale_after_seconds,
        )
    except (LedgerError, OSError, PatchGenerationError, VerificationError) as exc:
        typer.echo(f"Autofix failed: {exc}", err=True)
        raise typer.Exit(code=3) from exc
    typer.echo(result.model_dump_json(indent=2))
    if result.evolution.candidate.status == CandidateStatus.REJECTED:
        raise typer.Exit(code=2)


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    reload: Annotated[bool, typer.Option()] = False,
) -> None:
    """Run the AutoHarness API server."""
    import uvicorn

    uvicorn.run("autoharness.api:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
