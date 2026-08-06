# Changelog

All notable changes to AutoHarness are documented here.

## 0.15.0 - 2026-08-07

- Periodically withhold one relevant non-probe Skill from AutoFix generation context on a
  configurable, deterministic repository-run cadence.
- Persist withheld Skill identity, original rank, health, and experiment index for audit.
- Aggregate exposed and control outcomes once per AutoFix run so retries do not inflate evidence.
- Compute separate Beta(2,2) posteriors, controlled lift, confidence, and a bounded score adjustment.
- Explain controlled evidence in Skill ranking while retaining conservative associative scoring.
- Keep quarantine recovery probes eligible for generation and outside the ablation pool.
- Add lifecycle, rotation, retry-deduplication, ranking, validation, and CLI coverage.

## 0.14.0 - 2026-08-07

- Add explainable unobserved, learning, healthy, and quarantined states for retrieved Skills.
- Quarantine a Skill after at least five observations when its Beta posterior remains at or below 0.30.
- Keep quarantined Skills out of generation context while listing their health evidence for audit.
- Probe one relevant quarantined Skill on a configurable repository-run cadence and rotate probes.
- Feed successful probe outcomes back into health computation so recovered Skills re-enter normally.
- Allow explicit CLI inspection overrides and let a new Skill version begin with clean health evidence.
- Persist health and probe provenance with repair candidates and add full quarantine/recovery tests.

## 0.13.0 - 2026-08-07

- Persist the diagnosed failure type on every AutoFix run before its first provider attempt.
- Re-select adaptive providers from repository-and-failure-type outcome evidence after diagnosis.
- Keep run creation ahead of diagnosis so early pipeline failures remain durably auditable.
- Isolate planning, retrieval, tool, memory, reasoning, validation, and unknown provider histories.
- Add optional failure-type filtering to `provider-outcomes` and label its returned evidence.
- Migrate existing ledgers in place and backfill context from linked historical repair candidates.
- Add context isolation, global-versus-specialist selection, CLI, migration, and lifecycle tests.

## 0.12.0 - 2026-08-07

- Fail over within an AutoFix run when an adaptive provider cannot generate progress.
- Try the highest-scored untried provider before reusing the best available alternative.
- Keep evaluation rejection with the current provider so structured feedback can repair its next attempt.
- Persist initial and current providers plus every failover trigger, route, and reason atomically.
- Attribute one outcome per run/provider pair from actual immutable attempt evidence.
- Exclude pre-generation failures from provider quality evidence and keep interrupted work non-judgmental.
- Add deterministic failover examples and unit, ledger, CLI, and end-to-end integration coverage.

## 0.11.0 - 2026-08-07

- Add adaptive portfolios over command and OpenAI patch generators.
- Aggregate repository-scoped provider outcomes with a conservative Beta(2,2) prior.
- Select under-sampled providers first, then balance posterior success and bounded UCB exploration.
- Persist every selection decision, candidate score, and exploration reason with its AutoFix run.
- Migrate existing SQLite ledgers in place without losing earlier run or attempt history.
- Protect every command program in a portfolio and require network consent when any child may use it.
- Add `provider-outcomes` plus deterministic selection, migration, CLI, and end-to-end tests.

## 0.10.0 - 2026-08-07

- Add a built-in OpenAI Responses API patch generator using the official Python SDK.
- Select command or OpenAI providers from backward-compatible tagged JSON configuration.
- Send bounded source context, the effective verification contract, and retry feedback to models.
- Redact common secret-bearing Trace fields, source tokens, SDK errors, and validation inputs.
- Require a separate CLI acknowledgement before Trace or source context may leave the machine.
- Enforce model patches against supplied files and protected paths before benchmark execution.
- Keep response storage disabled by default and make new-file generation explicitly opt-in.
- Add mocked SDK, AutoFix retry, CLI privacy, scope, and configuration compatibility tests.

## 0.9.0 - 2026-08-07

- Add heartbeat leases for active AutoFix runs and refresh them between pipeline phases.
- Atomically recover stale running records as interrupted without claiming recently refreshed work.
- Fail unfinished candidates in the same SQLite transaction while preserving their prior status.
- Scope automatic recovery to the current repository and keep it explicitly opt-in.
- Add `autofix-recover` for manual recovery and `autofix --recover-stale-after` for startup recovery.
- Cover repository isolation, candidate cleanup, Trace redaction, and end-to-end startup recovery.

## 0.8.0 - 2026-08-07

- Add persistent AutoFix run records with running, succeeded, rejected, and failed states.
- Persist immutable attempt records for generation errors, duplicate patches, verification,
  evaluation, promotion, and learning outcomes.
- Link runs, attempts, and repair candidates in both result models and SQLite metadata.
- Store only a canonical Trace SHA-256 by default, with explicit opt-in full Trace persistence.
- Redact full Trace content from run listings while retaining it in explicit single-run queries.
- Add `autofix-runs` and `autofix-run` audit commands plus privacy and lifecycle tests.

## 0.7.0 - 2026-08-07

- Aggregate repository-scoped outcomes for every retrieved Skill version from the Repair Ledger.
- Distinguish accepted repairs, rejections, pre-evaluation failures, and post-acceptance failures.
- Apply bounded Bayesian outcome adjustments without overpowering diagnosis and content relevance.
- Include outcome statistics and score adjustments in every explainable Skill match.
- Feed outcome-aware ordering into AutoFix automatically and expose `--ledger` retrieval support.
- Add the `skill-outcomes` audit CLI plus ranking, aggregation, repository-scope, and CLI tests.

## 0.6.0 - 2026-08-07

- Add feedback-driven AutoFix retries with a configurable one-to-ten attempt budget.
- Send prior generation failures, verification errors, metrics, and rejection reasons to the
  next patch-generation attempt.
- Capture failed evolution attempts as typed results while preserving original exception behavior.
- Skip repeated verification for duplicate patches and retain attempt provenance in the ledger.
- Stop retrying after successful source promotion to avoid operating on already-mutated source.
- Add an adaptive two-attempt generator example and end-to-end retry tests.

## 0.5.0 - 2026-08-07

- Add a typed patch-generator protocol with JSON context and unified-diff output.
- Run command generators in disposable repository copies with time, size, and output guards.
- Add the end-to-end AutoFix loop: diagnose, retrieve, generate, verify, promote, and learn.
- Protect generator programs from generated patches and preserve verify-only behavior by default.
- Add the `autofix` CLI workflow, example generator, and Windows newline compatibility.

## 0.4.0 - 2026-08-07

- Add a safe YAML Skill Registry with invalid-file diagnostics and resource limits.
- Deduplicate learned experience to the latest immutable version of each Skill.
- Rank Skills using failure type, triggers, content, affected components, and version.
- Add diagnosis-driven `recommend-skills` with explainable matches and cross-failure fallback.

## 0.3.0 - 2026-08-07

- Add a SQLite Repair Ledger with an enforced candidate state machine and event history.
- Add the end-to-end Evolution Pipeline: propose, verify/reject, promote, and learn.
- Enrich learned Skills with baseline, candidate, and delta benchmark metrics.
- Preserve immutable Skill versions instead of overwriting earlier repair knowledge.
- Add `evolve-patch`, `repair-history`, and `repair-events` CLI workflows.

## 0.2.0 - 2026-08-07

- Add before/after evaluation snapshots and configurable metric policies.
- Enforce candidate test results, metric thresholds, and relative regression budgets.
- Expose evaluation through the Python service, FastAPI, and CLI.
- Add a CI-friendly non-zero exit code when the CLI rejects a candidate.
- Add disposable baseline/candidate workspaces and non-destructive unified-diff verification.
- Protect benchmark inputs, tests, metrics, and repository metadata from candidate changes.
- Add trusted-command acknowledgement and machine-readable patch verification results.
- Add verified-candidate promotion with patch digests and touched-file fingerprints.
- Detect source changes between verification and promotion and create recoverable backups.

## 0.1.0 - 2026-08-07

- Add typed agent trace ingestion and explainable failure diagnosis.
- Add Python AST code indexing and issue localization.
- Add repair-experience-to-skill generation.
- Add FastAPI and CLI interfaces.
