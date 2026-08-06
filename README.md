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
- Outcome-aware Skill ranking that conservatively incorporates accepted, rejected, and failed
  repair history from the current repository.
- Recoverable Skill health gating that quarantines repeatedly harmful experience and performs
  controlled, rotating re-evaluation probes.
- Pluggable patch generation through an isolated command protocol or the built-in OpenAI
  Responses API provider.
- Explainable adaptive generator portfolios that learn from repository-scoped run outcomes,
  sample every provider, and retain controlled exploration.
- Failure-type-aware provider learning that selects specialists independently for planning,
  retrieval, tool, memory, reasoning, validation, and unknown failures.
- In-run provider failover that preserves retry feedback while routing generation outages and
  duplicate patches to another portfolio member.
- End-to-end AutoFix orchestration from diagnosis and Skill retrieval through verification,
  optional promotion, and learning.
- Feedback-driven AutoFix retries with structured failures, evaluation metrics, rejection
  reasons, and duplicate-patch suppression.
- Persistent AutoFix run and attempt records covering generation errors through final outcomes,
  with privacy-safe Trace hashing by default.
- Heartbeat-backed recovery for interrupted AutoFix runs with atomic unfinished-candidate cleanup.
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
  --repo PATH \
  --ledger PATH/.autoharness/ledger.db
```

Recommendations are ranked using failure type, trigger phrases and tokens, affected code
components, Skill content, version, and optional observed outcomes. Outcome evidence is scoped
to `--repo` when supplied. A Beta(2,2) prior plus sample-confidence shrinkage limits the
adjustment to about +/-2 points, so a few correlated observations cannot overpower semantic
relevance. Every match reports accepted, rejected, and failed counts, posterior rate, score
adjustment, and textual reasons. This is associative evidence, not a causal claim that a
retrieved Skill alone produced the result.

Every relevant Skill also receives an explainable health state. New Skills are `unobserved`; Skills
with fewer than five repository-scoped observations remain `learning`. At five or more observations,
a Beta posterior success rate at or below 0.30 moves the Skill to `quarantined`; otherwise it is
`healthy`. Quarantined Skills are removed from generator context but remain in
`quarantined_skills` audit output with counts, thresholds, and reasons. This gate is intentionally
conservative and continues to use associative evidence rather than claiming causal attribution.

Inspect quarantined matches manually when needed:

```bash
autoharness recommend-skills TRACE.json \
  --skills PATH/.autoharness/skills \
  --repo PATH \
  --ledger PATH/.autoharness/ledger.db \
  --include-quarantined
```

AutoFix probes one semantically relevant quarantined Skill every ten repository runs by default.
Probes rotate when several Skills are quarantined, occupy at most one retrieval slot, and carry a
`quarantine_probe` flag into candidate metadata. Their accepted, rejected, or failed outcomes feed
the same Bayesian health calculation, allowing automatic recovery. Set
`--skill-probe-interval N` to change the cadence or `0` to disable it. Publishing a newer immutable
Skill version also starts a fresh health record while preserving the old version and its evidence.

Invalid YAML is reported without hiding valid Skills, and older versions remain on disk for
auditability. Use `--cross-failure` to explicitly allow experience from other failure
categories; unknown diagnoses automatically fall back to cross-category retrieval.

Inspect the outcome evidence independently:

```bash
autoharness skill-outcomes \
  --ledger PATH/.autoharness/ledger.db \
  --repo PATH
```

Generate and verify a repair directly from a failing trace:

```bash
autoharness autofix TRACE.json GENERATOR.json PLAN.json \
  --repo PATH \
  --allow-command-execution
```

AutoFix diagnoses the trace, retrieves relevant learned Skills, builds a typed generation
context, runs the configured generator in a disposable repository copy, and verifies its
unified diff against the baseline. The safe default does not modify the source checkout.
It makes up to three attempts by default. Every later attempt receives the prior generation,
verification, or evaluation outcomes through `previous_attempts`; use `--max-attempts 1..10`
to control the retry budget. Identical patches are not benchmarked twice.
Each invocation creates a durable `run_id` before diagnosis or generation begins, then attaches the
diagnosed failure type before the first provider attempt. Successful, rejected, and failed runs
remain queryable even when no valid patch was produced.
Add `--apply-to-source` to promote an accepted patch and learn a versioned Skill:

```bash
autoharness autofix TRACE.json GENERATOR.json PLAN.json \
  --repo PATH \
  --allow-command-execution \
  --apply-to-source
```

The generator configuration is JSON. Command configs remain the default and do not require a
`type` field. `argv` is executed directly without a shell, receives the generation context as
JSON on standard input, writes one UTF-8 unified diff to standard output, and may write
diagnostics to standard error:

```json
{
  "type": "command",
  "name": "local-generator",
  "argv": ["python", "generator.py"],
  "timeout_seconds": 120,
  "max_patch_bytes": 1048576
}
```

Use the built-in OpenAI Responses provider without writing a wrapper program:

```json
{
  "type": "openai",
  "name": "openai-responses",
  "model": "gpt-5.6-sol",
  "api_key_env": "OPENAI_API_KEY",
  "reasoning_effort": "medium",
  "store": false,
  "context_paths": ["src/agent.py", "tests/test_agent.py"]
}
```

Set the named environment variable outside the JSON file, then explicitly acknowledge both
trusted local benchmark execution and network generation:

```bash
autoharness autofix TRACE.json examples/verification_target/openai_generator.json PLAN.json \
  --repo PATH \
  --allow-command-execution \
  --allow-network-generation
```

AutoHarness uses the official OpenAI Python SDK and the
[Responses API](https://developers.openai.com/api/docs/guides/latest-model). The configured model
receives the Trace, diagnosis, learned Skill matches, effective verification plan, protected paths,
selected repository source, and structured prior-attempt feedback. `context_paths` are prioritized;
remaining source files are discovered within configurable file, per-file, aggregate, and request
byte limits. Virtual environments, VCS data, build output, symlinks, common credential files, common
secret-bearing fields, and OpenAI-style key tokens are excluded or redacted. This is defense in
depth, not a substitute for reviewing what a repository and Trace contain before allowing network
generation.

Response storage is requested off by default (`store: false`). See OpenAI's
[API data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint) for
the platform policy that applies to an account. The API key is read only from the configured
environment variable and is never placed in the model prompt, generator JSON, candidate metadata,
or ledger. Provider errors and configuration validation are emitted without input values and with
key-like tokens redacted.

Model output is still untrusted. AutoHarness accepts only one plain unified diff, applies the same
path and binary checks used by deterministic verification, rejects modifications to protected or
unprovided files, and permits new files only with `allow_new_files: true`. The normal disposable
baseline/candidate benchmarks remain the authority on whether a patch is accepted.

Combine two or more providers in an adaptive portfolio:

```json
{
  "type": "adaptive",
  "name": "repair-portfolio",
  "minimum_trials": 2,
  "exploration_weight": 0.35,
  "failover_phases": ["generation", "deduplication"],
  "providers": [
    {
      "type": "command",
      "name": "local-generator",
      "argv": ["python", "generator.py"]
    },
    {
      "type": "openai",
      "name": "openai-responses",
      "model": "gpt-5.6-sol"
    }
  ]
}
```

Provider names must be unique and portfolios cannot be nested. AutoHarness first gives every
provider `minimum_trials` terminal observations, using configuration order as the deterministic
tie-breaker. It then ranks providers with a Beta(2,2) posterior success rate plus a bounded UCB
exploration bonus. Setting `exploration_weight` to zero keeps minimum sampling but makes later
selection pure exploitation. The evidence is scoped to the current repository and is associative:
a successful run does not prove that its generator alone caused the result. Failed infrastructure
and generation attempts count as failures only for providers that were actually invoked;
interrupted runs are reported but excluded from quality observations because no terminal judgment
was reached.

Selection is contextual. AutoHarness creates the durable run record first, diagnoses the Trace,
stores the resulting `failure_type`, and then re-selects from outcomes matching both the repository
and diagnosis. A provider's retrieval history therefore cannot make it the preferred reasoning
provider. Each failure category receives its own minimum-sampling and UCB exploration cycle;
missing contextual evidence starts from the neutral Beta(2,2) prior instead of silently borrowing
another category. Runs that fail before diagnosis remain auditable with a null failure type and do
not enter contextual provider evidence.

Within one AutoFix run, the default `failover_phases` routes generation errors and duplicate
patches to the highest-scored provider not yet tried in that run. After every provider has been
tried, it reuses the best alternative rather than immediately repeating the latest failure. Add
`"verification"` to fail over after unsafe or invalid patch verification, or set the list to `[]`
to disable in-run failover. Evaluation rejection is intentionally not a valid failover phase: the
same provider receives the benchmark metrics and rejection reasons on its next attempt so it can
correct the patch. Promotion and learning failures can occur after source mutation or acceptance
and are never routed to another generator.

Each AutoFix run stores the initial and current provider, every candidate's posterior, exploration
bonus and score, and every failover trigger, route, and human-readable reason. Inspect the
aggregates independently:

```bash
autoharness provider-outcomes \
  --ledger PATH/.autoharness/ledger.db \
  --repo PATH \
  --failure-type reasoning_failure
```

Aggregation reads the newest 5,000 terminal runs by default; use `--limit` to choose a different
bounded history window. Omit `--failure-type` for the repository-wide aggregate. Filtered output
labels every row with its failure type. Evidence is classified once per run/provider pair from
immutable attempt records: any accepted attempt is a success, an evaluated rejection without later
success is a rejection, and other terminal attempted work is a failure. Runs that fail before
invoking a generator are excluded instead of blaming the configured provider. Existing ledgers
and v0.11 selection JSON remain readable; historical run contexts are backfilled when a linked
candidate provides reliable evidence and otherwise remain null. If any portfolio child is a
network provider, `--allow-network-generation` is required even when the provider selected for a
particular run is local. Every command program in the portfolio is protected from generated
patches, not only the currently selected program. See
[`adaptive_portfolio.json`](examples/verification_target/adaptive_portfolio.json) for cross-run
exploration and [`failover_portfolio.json`](examples/verification_target/failover_portfolio.json)
for deterministic in-run recovery.

The generation context includes `attempt_number`, the effective verification contract, and
structured `previous_attempts` entries with phase, candidate status, patch digest, before/after
metrics, rejection reasons, and bounded errors. AutoHarness stores this provenance with the next
candidate in the Repair Ledger. Once source promotion succeeds it will never retry, even if later
Skill persistence fails, preventing a second repair against already-mutated source.

By default the run record stores only a canonical SHA-256 of the input Trace. Use
`--persist-trace` only when full Trace retention is appropriate for the repository's privacy
policy. Run listings never expand stored Trace content; a single-run lookup does:

```bash
autoharness autofix-runs --ledger PATH/.autoharness/ledger.db
autoharness autofix-run RUN_ID --ledger PATH/.autoharness/ledger.db
```

Attempt rows are immutable and include their phase, provider, patch digest, candidate link,
metrics, rejection reasons, and bounded error. Candidate metadata also carries its `run_id`,
providing navigation in both directions.

Runs refresh a heartbeat between diagnosis, generation, and attempt-recording phases. Recover
abandoned `running` records explicitly, or opt into repository-scoped recovery before a new run:

```bash
autoharness autofix-recover \
  --ledger PATH/.autoharness/ledger.db \
  --repo PATH \
  --older-than-seconds 3600

autoharness autofix TRACE.json GENERATOR.json PLAN.json \
  --repo PATH \
  --allow-command-execution \
  --recover-stale-after 3600
```

Recovery atomically marks claimed runs `interrupted` and moves their unfinished candidates to
`failed`, retaining the prior candidate status in metadata. It is disabled by default. Heartbeats
cannot advance while a generator or benchmark subprocess is blocking, so choose a threshold
longer than the worst-case generator timeout plus benchmark duration.

Generator commands are trusted local processes, not an operating-system sandbox. AutoHarness
protects the generator program from modifying itself, rejects malformed, binary, oversized,
or unsafe patches, and never executes a generated shell command. See
[`examples/verification_target/generator.json`](examples/verification_target/generator.json)
for a minimal provider. The
[`adaptive_generator.json`](examples/verification_target/adaptive_generator.json) example
deliberately fails its first evaluation, consumes the feedback, and repairs the second attempt.

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
- `generation.py` defines the provider protocol, isolated command generator, bounded repository
  context builder, OpenAI Responses adapter, and explainable adaptive portfolio selector with
  deterministic in-run failover.
- `autofix.py` orchestrates feedback-driven Diagnose -> Retrieve -> Generate -> Verify ->
  Promote -> Learn attempts.
- `RepairPipeline` promotes accepted candidates with stale-source detection and backups.
- `ledger.py` persists heartbeat-backed AutoFix runs, immutable attempts, atomic interruption
  recovery, diagnosed failure context, provider selection and failover evidence, attempt-aware
  repository-and-failure-scoped outcomes, the repair state machine, append-only lifecycle events,
  and Skill outcome associations in SQLite.
- `evolution.py` orchestrates verification, promotion, history, and versioned Skill learning.
- `skills.py` converts validated repairs into portable YAML skills.
- `registry.py` safely indexes and ranks the latest learned Skill versions with conservative,
  explainable outcome adjustments, recoverable quarantine gates, and rotating probes.
- `service.py` orchestrates the Observe -> Diagnose -> Repair -> Evaluate -> Learn workflow.
- `api.py` and `cli.py` are transport adapters.

The deterministic verification core is intentional: it provides a measurable authority while
keeping generation providers replaceable. Future phases will add richer benchmark integrations,
causal credit assignment, and feedback-driven harness optimization.

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
  history plus pluggable isolated patch generation, feedback-driven retries, and persistent run
  auditing with interrupted-run recovery are available. A built-in OpenAI Responses provider now
  consumes verification and retry feedback; richer benchmark adapters are next.
- **Phase 3 - Self-Evolving Harness:** versioned repair Skills and explainable retrieval are
  available with outcome-aware selection. Adaptive provider portfolios now perform controlled,
  auditable exploration, attempt-aware failover attribution, and failure-type-specialized
  selection. Skill health now closes the outcome-to-retrieval feedback loop with quarantine and
  recovery probes; finer causal credit assignment and harness optimization are next.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
