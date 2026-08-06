"""Command-line interface for AutoHarness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from autoharness.code_graph import PythonCodeGraph
from autoharness.models import (
    AgentTrace,
    EvaluationRequest,
    PatchVerificationPlan,
    RepairExperience,
)
from autoharness.service import AutoHarness
from autoharness.verification import VerificationError

app = typer.Typer(no_args_is_help=True, help="Diagnose and improve AI agent systems.")


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Cannot read JSON from {path}: {exc}") from exc


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
