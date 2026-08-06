# AutoHarness

**A Self-Evolving AutoFix Agent for AI Systems**

AutoHarness is an open-source reliability layer for AI agents. It turns runtime traces,
logs, and user feedback into explainable failure diagnoses, relevant code locations, and
reusable repair knowledge.

This repository contains the Phase 1 MVP: an **Agent Debug Copilot** that works locally
and does not require an LLM or external service.

## What works today

- Typed trace ingestion for plans, tool calls, retrieval, memory, responses, and feedback.
- Explainable failure classification across planning, retrieval, tool, memory, reasoning,
  validation, and unknown failures.
- Python repository indexing with AST-derived files, classes, functions, imports, and calls.
- Issue-to-code localization using diagnosis signals, identifiers, paths, docstrings, and
  source text.
- Repair recommendations and YAML skill generation from validated repair experiences.
- CLI and FastAPI interfaces backed by the same application service.

```text
Trace / logs / feedback
          |
          v
  Failure diagnosis -----> repair checks
          |
          v
  Python code graph -----> candidate files and symbols
          |
          v
  validated repair ------> reusable skill
```

## Quick start

Python 3.11+ is required. Keep all dependencies in this repository by creating `.venv`
at the project root:

```bash
uv sync --extra dev
uv run autoharness analyze examples/retrieval_failure.json --repo .
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
- `skills.py` converts validated repairs into portable YAML skills.
- `service.py` orchestrates the Observe → Diagnose → Localize → Learn workflow.
- `api.py` and `cli.py` are transport adapters.

The deterministic core is intentional: it provides a measurable baseline before adding an
LLM diagnosis provider. Future phases will add patch generation in an isolated workspace,
before/after evaluation gates, persistent trace storage, and feedback-driven harness
optimization.

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

- **Phase 1 — Agent Debug Copilot:** trace ingestion, diagnosis, and code localization.
- **Phase 2 — AutoFix Agent:** sandboxed patch generation, tests, and evaluation gates.
- **Phase 3 — Self-Evolving Harness:** harness optimization, experience memory, and skill
  selection/evolution.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).

