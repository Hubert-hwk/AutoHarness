import json
import sqlite3
import sys
from pathlib import Path

from typer.testing import CliRunner

from autoharness.cli import app
from autoharness.ledger import RepairLedger
from autoharness.models import AgentTrace, CandidateStatus, FailureType, Skill
from autoharness.skills import SkillGenerator


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


def test_evolve_patch_cli_records_and_learns(tmp_path: Path) -> None:
    patch, plan = _verification_files(tmp_path)
    experience = tmp_path / "experience.json"
    experience.write_text(
        json.dumps(
            {
                "title": "Improve score",
                "failure_type": "reasoning_failure",
                "trigger_terms": ["low score"],
                "root_cause": "Value was too low",
                "repair_steps": ["Increase value"],
                "validation_steps": ["Run benchmark"],
            }
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "state" / "ledger.db"
    skills = tmp_path / "state" / "skills"

    result = CliRunner().invoke(
        app,
        [
            "evolve-patch",
            str(patch),
            str(plan),
            str(experience),
            "--repo",
            str(tmp_path),
            "--ledger",
            str(ledger),
            "--skills",
            str(skills),
            "--allow-command-execution",
            "--apply-to-source",
        ],
    )

    assert result.exit_code == 0
    assert '"status": "learned"' in result.output
    history = CliRunner().invoke(app, ["repair-history", "--ledger", str(ledger)])
    assert history.exit_code == 0
    assert '"status": "learned"' in history.output
    trace = tmp_path / "trace.json"
    trace.write_text(
        json.dumps(
            {
                "task": "Fix an incorrect answer with a low score",
                "events": [
                    {
                        "kind": "response",
                        "status": "failure",
                        "error": "incorrect answer",
                    }
                ],
                "feedback": "low score",
            }
        ),
        encoding="utf-8",
    )
    recommendation = CliRunner().invoke(
        app,
        ["recommend-skills", str(trace), "--skills", str(skills)],
    )
    assert recommendation.exit_code == 0
    assert '"name": "improve_score"' in recommendation.output


def test_autofix_cli_runs_full_pipeline(tmp_path: Path) -> None:
    _, plan = _verification_files(tmp_path)
    (tmp_path / "generator.py").write_text(
        "import json, sys\n"
        "context = json.load(sys.stdin)\n"
        "candidate = 0 if not context['previous_attempts'] else 2\n"
        "if context['previous_attempts']:\n"
        "    assert context['previous_attempts'][-1]['phase'] == 'evaluation'\n"
        "sys.stdout.write(\n"
        "    f'--- a/value.txt\\n+++ b/value.txt\\n@@ -1 +1 @@\\n-1\\n+{candidate}\\n'\n"
        ")\n",
        encoding="utf-8",
    )
    generator = tmp_path / "generator.json"
    generator.write_text(
        json.dumps(
            {
                "name": "cli-generator",
                "argv": [sys.executable, "generator.py"],
            }
        ),
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text(
        json.dumps(
            {
                "task": "Fix an incorrect low score",
                "events": [
                    {
                        "kind": "response",
                        "status": "failure",
                        "error": "incorrect answer",
                    }
                ],
                "feedback": "low score",
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "autofix",
            str(trace),
            str(generator),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
            "--apply-to-source",
        ],
    )

    assert result.exit_code == 0
    assert '"provider": "cli-generator"' in result.output
    assert '"status": "learned"' in result.output
    payload = json.loads(result.output)
    assert payload["run"]["status"] == "succeeded"
    assert payload["run"]["trace"] is None
    assert len(payload["attempts"]) == 2
    assert payload["attempts"][0]["feedback"]["phase"] == "evaluation"
    assert payload["attempts"][1]["feedback"]["phase"] == "complete"
    assert (tmp_path / "value.txt").read_text(encoding="utf-8") == "2\n"
    ledger = tmp_path / ".autoharness" / "ledger.db"
    runs = CliRunner().invoke(app, ["autofix-runs", "--ledger", str(ledger)])
    detail = CliRunner().invoke(
        app,
        ["autofix-run", payload["run"]["run_id"], "--ledger", str(ledger)],
    )
    assert runs.exit_code == 0
    assert json.loads(runs.output)[0]["status"] == "succeeded"
    assert detail.exit_code == 0
    assert len(json.loads(detail.output)["attempts"]) == 2
    missing = CliRunner().invoke(
        app,
        ["autofix-run", "missing", "--ledger", str(ledger)],
    )
    assert missing.exit_code == 3
    assert "Unknown AutoFix run" in missing.output


def test_autofix_openai_cli_requires_network_acknowledgement(tmp_path: Path) -> None:
    _, plan = _verification_files(tmp_path)
    generator = tmp_path / "openai-generator.json"
    generator.write_text(
        json.dumps(
            {
                "type": "openai",
                "model": "gpt-test",
                "context_paths": ["value.txt"],
            }
        ),
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"task": "Fix the low value"}), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "autofix",
            str(trace),
            str(generator),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
        ],
    )

    assert result.exit_code == 6
    assert "--allow-network-generation" in result.output
    assert not (tmp_path / ".autoharness" / "ledger.db").exists()


def test_autofix_generator_config_errors_do_not_echo_secret_values(tmp_path: Path) -> None:
    _, plan = _verification_files(tmp_path)
    secret = "sk-misplacedsecret123456"
    generator = tmp_path / "invalid-generator.json"
    generator.write_text(
        json.dumps({"type": "openai", "api_key": secret}),
        encoding="utf-8",
    )
    trace = tmp_path / "trace.json"
    trace.write_text(json.dumps({"task": "Fix the low value"}), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "autofix",
            str(trace),
            str(generator),
            str(plan),
            "--repo",
            str(tmp_path),
            "--allow-command-execution",
        ],
    )

    assert result.exit_code == 2
    assert "Invalid patch generator configuration" in result.output
    assert "extra_forbidden" in result.output
    assert secret not in result.output


def test_autofix_recover_cli_interrupts_stale_run(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.db"
    ledger = RepairLedger(ledger_path)
    run = ledger.start_autofix_run(
        repository_path=tmp_path,
        trace=AgentTrace(task="Recover an abandoned run"),
        generator_provider="test-generator",
        max_attempts=2,
        promote_requested=False,
        persist_trace=True,
    )
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            "UPDATE autofix_runs SET updated_at = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", run.run_id),
        )

    result = CliRunner().invoke(
        app,
        [
            "autofix-recover",
            "--ledger",
            str(ledger_path),
            "--older-than-seconds",
            "60",
            "--repo",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["run_id"] == run.run_id
    assert payload[0]["status"] == "interrupted"
    assert payload[0]["error_type"] == "AutoFixInterrupted"
    assert payload[0]["trace"] is None


def test_skill_outcomes_cli_and_outcome_aware_recommendation(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    SkillGenerator().save(
        Skill(
            name="reliable_score_repair",
            description="Repair low scores",
            failure_type=FailureType.REASONING,
            triggers=["low score", "incorrect answer"],
            context={"root_cause": "low value"},
            workflow=["Increase value"],
            evaluation=["Run score benchmark"],
        ),
        skills,
    )
    ledger = RepairLedger(tmp_path / "ledger.db")
    candidate = ledger.propose(
        title="Historical repair",
        repository_path=tmp_path,
        patch_sha256="a" * 64,
        failure_type=FailureType.REASONING,
        metadata={
            "retrieved_skills": [{"name": "reliable_score_repair", "version": 1, "score": 9.0}]
        },
    )
    ledger.transition(candidate.candidate_id, CandidateStatus.VERIFIED)
    trace = tmp_path / "outcome-trace.json"
    trace.write_text(
        json.dumps(
            {
                "task": "Fix an incorrect answer with a low score",
                "events": [
                    {
                        "kind": "response",
                        "status": "failure",
                        "error": "incorrect answer",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    outcomes = CliRunner().invoke(
        app,
        ["skill-outcomes", "--ledger", str(ledger.path), "--repo", str(tmp_path)],
    )
    recommendation = CliRunner().invoke(
        app,
        [
            "recommend-skills",
            str(trace),
            "--skills",
            str(skills),
            "--repo",
            str(tmp_path),
            "--ledger",
            str(ledger.path),
        ],
    )

    assert outcomes.exit_code == 0
    assert json.loads(outcomes.output)[0]["accepted"] == 1
    assert recommendation.exit_code == 0
    match = json.loads(recommendation.output)["skills"]["matches"][0]
    assert match["outcome_stats"]["accepted"] == 1
    assert any("observed outcomes" in reason for reason in match["reasons"])
