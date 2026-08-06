# Changelog

All notable changes to AutoHarness are documented here.

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
