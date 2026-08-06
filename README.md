# AutoHarness

**A Self-Evolving AutoFix Agent for AI Systems**

AutoHarness is an open-source reliability layer for AI agents. It turns runtime traces,
logs, and user feedback into explainable failure diagnoses, relevant code locations, and
reusable repair knowledge.

The current release contains a deterministic Agent Debug Copilot and an auditable repair
pipeline that work locally without requiring an LLM or external service.

## What works today

- Typed trace ingestion for plans, tool calls, retrieval, memory, responses, and feedback.
- Explainable failure classification across planning, retrieval, tool, memory, reasoning,
  validation, and unknown failures.
- Python repository indexing with AST-derived files, classes, functions, imports, and calls.
- Issue-to-code localization using diagnosis signals, identifiers, paths, docstrings, and
  source text.
- Repair recommendations and YAML skill generation from validated repair experiences.
- Before/after evaluation gates with test requirements, hard thresholds, and per-metric
  regression budgets.
- Non-destructive patch verification in separate disposable baseline and candidate copies.
- Guarded source promotion, persistent repair history, and versioned skill evolution.
- Explainable retrieval over the latest learned Skill versions for future failures.
- Pluggable patch generation through an isolated, JSON-in/unified-diff-out command protocol.
- End-to-end AutoFix orchestration from diagnosis and Skill retrieval through verification,
  optional promotion, and learning.
- CLI and FastAPI interfaces backed by the same application service.

```text
Trace / logs / feedback
          |
          v
  Failure diagnosis -----> Skill retrieval
          |
          v
  Python code graph -----> patch generator
          |
          v
  evaluation gates ------> guarded promotion
          |                        |
          v                        v
  repair ledger <--------- versioned skill
```

## Quick start

Python 3.11+ is required. Keep all dependencies in this repository by creating `.venv`
at the project root:

```bash
uv sync --extra dev
uv run autoharness analyze examples/retrieval_failure.json --repo .
uv run autoharness evaluate examples/evaluation_gate.json
uv run autoharness serve --reload
```

If `uv` is unavailable:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"  # Windows
```

The API will be available at `http://127.0.0.1:8000`; interactive documentation is at
`/docs`.

## CLI

Analyze a trace and optionally localize the issue in a Python repository:

```bash
autoharness analyze TRACE.json --repo PATH --limit 8
```

Index a repository:

```bash
autoharness index PATH
```

Create a reusable skill from a successful repair:

```bash
autoharness learn repair.json --output skills/
```

Evaluate a repair candidate against its baseline. The command exits with code `2` when a
gate rejects the candidate, so it can be used directly in CI:

```bash
autoharness evaluate examples/evaluation_gate.json
```

Each metric declares whether higher or lower is better, an optional hard threshold, and
the maximum relative regression it may tolerate. Candidate test failures are rejected by
default.

Verify a unified diff without changing the source repository:

```bash
autoharness verify-patch \
  examples/verification_target/improve.patch \
  examples/verification_target/plan.json \
  --repo examples/verification_target \
  --allow-command-execution
```

The verifier rejects unsafe paths, binary and symlink patches, protected-file changes, and
changes to benchmark programs named in command arguments. It copies the repository twice,
runs the baseline, applies the patch only to the candidate copy, runs the candidate, then
passes both snapshots to the evaluation gate. The original repository is never modified.

Benchmark commands are executed directly without a shell, but they are still processes on
the local machine. Only use trusted commands and pass `--allow-command-execution` explicitly;
the disposable workspace is not an operating-system security sandbox.

Promote an accepted candidate to the source checkout:

```bash
autoharness repair-patch PATCH PLAN \
  --repo PATH \
  --allow-command-execution \
  --apply-to-source
```

`repair-patch` performs verification and promotion in one process. It records the patch
SHA-256 and the original hash/existence of every touched file, then checks them again before
applying the patch. Rejected candidates, replaced patch content, or source changes after
verification are never promoted. Original files are copied to
`PATH/.autoharness/backups/` before mutation so an applied repair remains recoverable.

Run the full self-evolution loop:

```bash
autoharness evolve-patch PATCH PLAN EXPERIENCE \
  --repo PATH \
  --allow-command-execution \
  --apply-to-source
```

The evolution pipeline records every candidate and state transition in
`PATH/.autoharness/ledger.db`. A successful repair moves through
`proposed -> verified -> promoted -> learned`; rejected and failed attempts are retained for
analysis. The generated versioned Skill includes baseline, candidate, and delta metrics.

Inspect the history or the event stream for one candidate:

```bash
autoharness repair-history --ledger PATH/.autoharness/ledger.db
autoharness repair-events CANDIDATE_ID --ledger PATH/.autoharness/ledger.db
```

Retrieve relevant repair experience for a new trace:

```bash
autoharness recommend-skills TRACE.json \
  --skills PATH/.autoharness/skills \
  --repo PATH
```

Recommendations are ranked using failure type, trigger phrases and tokens, affected code
components, Skill content, and version. Every match includes its score and reasons. Invalid
YAML is reported without hiding valid Skills, and older versions are ignored while remaining
on disk for auditability. Use `--cross-failure` to explicitly allow experience from other
failure categories; unknown diagnoses automatically fall back to cross-category retrieval.

Generate and verify a repair directly from a failing trace:

```bash
autoharness autofix TRACE.json GENERATOR.json PLAN.json \
  --repo PATH \
  --allow-command-execution
```

AutoFix diagnoses the trace, retrieves relevant learned Skills, builds a typed generation
context, runs the configured generator in a disposable repository copy, and verifies its
unified diff against the baseline. The safe default does not modify the source checkout.
Add `--apply-to-source` to promote an accepted patch and learn a versioned Skill:

```bash
autoharness autofix TRACE.json GENERATOR.json PLAN.json \
  --repo PATH \
  --allow-command-execution \
  --apply-to-source
```

The generator configuration is JSON. `argv` is executed directly without a shell, receives
the generation context as JSON on standard input, writes one UTF-8 unified diff to standard
output, and may write diagnostics to standard error:

```json
{
  "name": "local-generator",
  "argv": ["python", "generator.py"],
  "timeout_seconds": 120,
  "max_patch_bytes": 1048576
}
```

Generator commands are trusted local processes, not an operating-system sandbox. AutoHarness
protects the generator program from modifying itself, rejects malformed, binary, oversized,
or unsafe patches, and never executes a generated shell command. See
[`examples/verification_target/generator.json`](examples/verification_target/generator.json)
for a minimal provider.

See [`examples/repair_experience.json`](examples/repair_experience.json) for the expected
repair format.

## API example

```bash
curl -X POST http://127.0.0.1:8000/v1/analyze \
  -H "content-type: application/json" \
  -d @examples/retrieval_failure.json
```

To include code localization, wrap the trace in an analysis request:

```json
{
  "trace": { "task": "answer a policy question", "events": [] },
  "repository_path": "/absolute/path/to/python/repository",
  "candidate_limit": 8
}
```

## Architecture

- `models.py` defines the stable domain and API contracts.
- `diagnosis.py` contains the deterministic, inspectable failure taxonomy engine.
- `code_graph.py` builds and queries a lightweight Python code graph.
- `evaluation.py` enforces tests, metric thresholds, and regression budgets.
- `verification.py` validates patches and runs isolated before/after benchmarks.
- `generation.py` defines the provider protocol and isolated command generator.
- `autofix.py` orchestrates Diagnose -> Retrieve -> Generate -> Verify -> Promote -> Learn.
- `RepairPipeline` promotes accepted candidates with stale-source detection and backups.
- `ledger.py` persists the repair state machine and append-only lifecycle events in SQLite.
- `evolution.py` orchestrates verification, promotion, history, and versioned Skill learning.
- `skills.py` converts validated repairs into portable YAML skills.
- `registry.py` safely indexes and ranks the latest learned Skill versions.
- `service.py` orchestrates the Observe -> Diagnose -> Repair -> Evaluate -> Learn workflow.
- `api.py` and `cli.py` are transport adapters.

The deterministic core is intentional: it provides a measurable baseline while keeping
generation providers replaceable. Future phases will add model-backed provider adapters,
persistent trace storage, and feedback-driven harness optimization.

## Development

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

## Inspiration

- [Better Harness](https://github.com/QoderAI/better-harness): feedback-driven harness
  optimization loops.
- [Harness Handbook](https://github.com/Ruhan-Wang/Harness_Handbook): harness organization
  and engineering context.
- [code-review-graph](https://github.com/tirth8205/code-review-graph): graph-based code
  understanding.
- [cangjie-skill](https://github.com/kangarooking/cangjie-skill): reusable experience as
  skills.

## Roadmap

- **Phase 1 - Agent Debug Copilot:** trace ingestion, diagnosis, and code localization.
- **Phase 2 - AutoFix Agent:** evaluation, verification, guarded promotion, and repair
  history plus pluggable isolated patch generation are available; model-backed providers and
  richer benchmark adapters are next.
- **Phase 3 - Self-Evolving Harness:** versioned repair Skills and explainable retrieval are
  available; harness optimization and outcome-aware experience selection are next.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
