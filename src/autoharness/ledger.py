"""Persistent repair candidate ledger and auditable lifecycle transitions."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoharness.models import (
    AgentTrace,
    AutoFixAttemptFeedback,
    AutoFixPhase,
    AutoFixRunAttemptRecord,
    AutoFixRunRecord,
    AutoFixRunStatus,
    CandidateStatus,
    FailureType,
    ProviderOutcomeStats,
    ProviderSelection,
    RepairCandidateEvent,
    RepairCandidateRecord,
    SkillComparisonMode,
    SkillContextBalance,
    SkillOutcomeStats,
)


class LedgerError(RuntimeError):
    """Raised when repair history is missing, invalid, or cannot transition."""


ALLOWED_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.PROPOSED: frozenset(
        {CandidateStatus.VERIFIED, CandidateStatus.REJECTED, CandidateStatus.FAILED}
    ),
    CandidateStatus.VERIFIED: frozenset({CandidateStatus.PROMOTED, CandidateStatus.FAILED}),
    CandidateStatus.PROMOTED: frozenset({CandidateStatus.LEARNED, CandidateStatus.FAILED}),
    CandidateStatus.REJECTED: frozenset(),
    CandidateStatus.LEARNED: frozenset(),
    CandidateStatus.FAILED: frozenset(),
}


class RepairLedger:
    """SQLite-backed source of truth for repair candidates and lifecycle events."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def propose(
        self,
        *,
        title: str,
        repository_path: str | Path,
        patch_sha256: str,
        failure_type: FailureType,
        metadata: dict[str, Any] | None = None,
    ) -> RepairCandidateRecord:
        candidate_id = uuid4().hex
        now = self._now()
        payload = metadata or {}
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO repair_candidates (
                    candidate_id, title, repository_path, patch_sha256, failure_type,
                    status, changed_paths, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    title,
                    str(Path(repository_path).expanduser().resolve()),
                    patch_sha256,
                    failure_type.value,
                    CandidateStatus.PROPOSED.value,
                    "[]",
                    self._json(payload),
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                candidate_id,
                CandidateStatus.PROPOSED,
                payload,
                now,
            )
        return self.get(candidate_id)

    def transition(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        details: dict[str, Any] | None = None,
        changed_paths: list[str] | None = None,
    ) -> RepairCandidateRecord:
        event_details = details or {}
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repair_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(f"Unknown repair candidate: {candidate_id}")
            current = CandidateStatus(row["status"])
            if status not in ALLOWED_TRANSITIONS[current]:
                raise LedgerError(f"Invalid repair transition: {current.value} -> {status.value}")

            metadata = self._load_json(row["metadata"])
            metadata.update(event_details)
            paths = (
                changed_paths
                if changed_paths is not None
                else self._load_json(row["changed_paths"])
            )
            now = self._now()
            connection.execute(
                """
                UPDATE repair_candidates
                SET status = ?, changed_paths = ?, metadata = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    status.value,
                    self._json(paths),
                    self._json(metadata),
                    now,
                    candidate_id,
                ),
            )
            self._insert_event(connection, candidate_id, status, event_details, now)
        return self.get(candidate_id)

    def get(self, candidate_id: str) -> RepairCandidateRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repair_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise LedgerError(f"Unknown repair candidate: {candidate_id}")
        return self._row_to_record(row)

    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        limit: int = 50,
    ) -> list[RepairCandidateRecord]:
        bounded_limit = max(1, min(limit, 500))
        with self._connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM repair_candidates ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM repair_candidates
                    WHERE status = ? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status.value, bounded_limit),
                ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def events(self, candidate_id: str) -> list[RepairCandidateEvent]:
        self.get(candidate_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT sequence, candidate_id, status, details, created_at
                FROM repair_events WHERE candidate_id = ? ORDER BY sequence
                """,
                (candidate_id,),
            ).fetchall()
        return [
            RepairCandidateEvent(
                sequence=row["sequence"],
                candidate_id=row["candidate_id"],
                status=CandidateStatus(row["status"]),
                details=self._load_json(row["details"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def start_autofix_run(
        self,
        *,
        repository_path: str | Path,
        trace: AgentTrace,
        generator_provider: str,
        generator_selection: ProviderSelection | None = None,
        max_attempts: int,
        promote_requested: bool,
        persist_trace: bool = False,
    ) -> AutoFixRunRecord:
        if not 1 <= max_attempts <= 10:
            raise LedgerError("AutoFix max_attempts must be between 1 and 10")
        provider = generator_provider.strip()
        if not provider:
            raise LedgerError("AutoFix generator provider must not be blank")
        trace_json = self._json(trace.model_dump(mode="json"))
        trace_sha256 = hashlib.sha256(trace_json.encode("utf-8")).hexdigest()
        run_id = uuid4().hex
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO autofix_runs (
                    run_id, repository_path, trace_sha256, trace_json,
                    generator_provider, generator_selection, max_attempts,
                    promote_requested, status,
                    final_candidate_id, error_type, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    run_id,
                    str(Path(repository_path).expanduser().resolve()),
                    trace_sha256,
                    trace_json if persist_trace else None,
                    provider,
                    (
                        self._json(generator_selection.model_dump(mode="json"))
                        if generator_selection is not None
                        else None
                    ),
                    max_attempts,
                    int(promote_requested),
                    AutoFixRunStatus.RUNNING.value,
                    now,
                    now,
                ),
            )
        return self.get_autofix_run(run_id)

    def record_autofix_diagnosis(
        self,
        run_id: str,
        failure_type: FailureType,
    ) -> AutoFixRunRecord:
        """Attach the diagnosed failure context before the first provider attempt."""
        now = self._now()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE autofix_runs
                SET failure_type = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (failure_type.value, now, run_id, AutoFixRunStatus.RUNNING.value),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM autofix_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"Unknown AutoFix run: {run_id}")
                raise LedgerError(f"AutoFix run is already terminal: {run_id}")
        return self.get_autofix_run(run_id)

    def update_autofix_generator_selection(
        self,
        run_id: str,
        *,
        generator_provider: str,
        generator_selection: ProviderSelection,
    ) -> AutoFixRunRecord:
        """Persist an adaptive in-run provider change while the run is active."""
        provider = generator_provider.strip()
        if not provider:
            raise LedgerError("AutoFix generator provider must not be blank")
        now = self._now()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE autofix_runs
                SET generator_provider = ?, generator_selection = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    provider,
                    self._json(generator_selection.model_dump(mode="json")),
                    now,
                    run_id,
                    AutoFixRunStatus.RUNNING.value,
                ),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM autofix_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"Unknown AutoFix run: {run_id}")
                raise LedgerError(f"AutoFix run is already terminal: {run_id}")
        return self.get_autofix_run(run_id)

    def heartbeat_autofix_run(self, run_id: str) -> AutoFixRunRecord:
        """Refresh a running lease so explicit stale-run recovery will not claim it."""
        now = self._now()
        with self._connection() as connection:
            refreshed = connection.execute(
                "UPDATE autofix_runs SET updated_at = ? WHERE run_id = ? AND status = ?",
                (now, run_id, AutoFixRunStatus.RUNNING.value),
            )
            if refreshed.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM autofix_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"Unknown AutoFix run: {run_id}")
                raise LedgerError(f"AutoFix run is already terminal: {run_id}")
        return self.get_autofix_run(run_id)

    def recover_stale_autofix_runs(
        self,
        *,
        older_than_seconds: float,
        repository_path: str | Path | None = None,
    ) -> list[AutoFixRunRecord]:
        """Atomically interrupt stale runs and fail their non-terminal candidates."""
        if older_than_seconds < 0:
            raise LedgerError("older_than_seconds must be non-negative")
        now_value = datetime.now(UTC)
        now = now_value.isoformat()
        cutoff = (now_value - timedelta(seconds=older_than_seconds)).isoformat()
        message = f"AutoFix run heartbeat exceeded {older_than_seconds:g} seconds"
        parameters: tuple[Any, ...]
        query = (
            "SELECT run_id, repository_path FROM autofix_runs "
            "WHERE status = ? AND updated_at <= ? ORDER BY updated_at"
        )
        parameters = (AutoFixRunStatus.RUNNING.value, cutoff)
        if repository_path is not None:
            query = (
                "SELECT run_id, repository_path FROM autofix_runs "
                "WHERE status = ? AND updated_at <= ? AND repository_path = ? "
                "ORDER BY updated_at"
            )
            parameters = (
                AutoFixRunStatus.RUNNING.value,
                cutoff,
                str(Path(repository_path).expanduser().resolve()),
            )

        recovered_ids: list[str] = []
        with self._connection() as connection:
            stale_rows = connection.execute(query, parameters).fetchall()
            for stale in stale_rows:
                run_id = str(stale["run_id"])
                claimed = connection.execute(
                    """
                    UPDATE autofix_runs
                    SET status = ?, error_type = ?, error = ?, updated_at = ?
                    WHERE run_id = ? AND status = ? AND updated_at <= ?
                    """,
                    (
                        AutoFixRunStatus.INTERRUPTED.value,
                        "AutoFixInterrupted",
                        message,
                        now,
                        run_id,
                        AutoFixRunStatus.RUNNING.value,
                        cutoff,
                    ),
                )
                if claimed.rowcount != 1:
                    continue
                active = [
                    row
                    for row in connection.execute(
                        """
                        SELECT * FROM repair_candidates
                        WHERE repository_path = ? AND status IN (?, ?, ?)
                        ORDER BY updated_at DESC
                        """,
                        (
                            str(stale["repository_path"]),
                            CandidateStatus.PROPOSED.value,
                            CandidateStatus.VERIFIED.value,
                            CandidateStatus.PROMOTED.value,
                        ),
                    ).fetchall()
                    if self._candidate_run_id(row) == run_id
                ]
                latest_attempt = connection.execute(
                    """
                    SELECT candidate_id FROM autofix_attempts
                    WHERE run_id = ? AND candidate_id IS NOT NULL
                    ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                final_candidate_id = (
                    str(active[0]["candidate_id"])
                    if active
                    else (
                        str(latest_attempt["candidate_id"]) if latest_attempt is not None else None
                    )
                )
                connection.execute(
                    "UPDATE autofix_runs SET final_candidate_id = ? WHERE run_id = ?",
                    (final_candidate_id, run_id),
                )
                for candidate_row in active:
                    current = CandidateStatus(candidate_row["status"])
                    details = {
                        "error": message,
                        "error_type": "AutoFixInterrupted",
                        "failed_from_status": current.value,
                    }
                    metadata = self._load_json(candidate_row["metadata"])
                    metadata.update(details)
                    candidate_id = str(candidate_row["candidate_id"])
                    failed = connection.execute(
                        """
                        UPDATE repair_candidates
                        SET status = ?, metadata = ?, updated_at = ?
                        WHERE candidate_id = ? AND status = ?
                        """,
                        (
                            CandidateStatus.FAILED.value,
                            self._json(metadata),
                            now,
                            candidate_id,
                            current.value,
                        ),
                    )
                    if failed.rowcount == 1:
                        self._insert_event(
                            connection,
                            candidate_id,
                            CandidateStatus.FAILED,
                            details,
                            now,
                        )
                recovered_ids.append(run_id)
        return [
            self.get_autofix_run(run_id).model_copy(update={"trace": None})
            for run_id in recovered_ids
        ]

    def record_autofix_attempt(
        self,
        run_id: str,
        feedback: AutoFixAttemptFeedback,
    ) -> AutoFixRunAttemptRecord:
        now = self._now()
        try:
            with self._connection() as connection:
                refreshed = connection.execute(
                    """
                    UPDATE autofix_runs SET updated_at = ?
                    WHERE run_id = ? AND status = ?
                    """,
                    (now, run_id, AutoFixRunStatus.RUNNING.value),
                )
                if refreshed.rowcount != 1:
                    row = connection.execute(
                        "SELECT status FROM autofix_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise LedgerError(f"Unknown AutoFix run: {run_id}")
                    raise LedgerError(f"AutoFix run is already terminal: {run_id}")
                row = connection.execute(
                    "SELECT max_attempts FROM autofix_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                assert row is not None
                if feedback.attempt_number > row["max_attempts"]:
                    raise LedgerError(
                        f"AutoFix attempt {feedback.attempt_number} exceeds run budget "
                        f"{row['max_attempts']}"
                    )
                exists = connection.execute(
                    """
                    SELECT 1 FROM autofix_attempts
                    WHERE run_id = ? AND attempt_number = ?
                    """,
                    (run_id, feedback.attempt_number),
                ).fetchone()
                if exists is not None:
                    raise LedgerError(
                        f"AutoFix attempt {feedback.attempt_number} already exists for run {run_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO autofix_attempts (
                        run_id, attempt_number, phase, candidate_id,
                        patch_sha256, feedback, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        feedback.attempt_number,
                        feedback.phase.value,
                        feedback.candidate_id,
                        feedback.patch_sha256,
                        self._json(feedback.model_dump(mode="json")),
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(
                f"Cannot record AutoFix attempt {feedback.attempt_number} for run {run_id}: {exc}"
            ) from exc
        return next(
            item
            for item in self.autofix_attempts(run_id)
            if item.attempt_number == feedback.attempt_number
        )

    def finish_autofix_run(
        self,
        run_id: str,
        status: AutoFixRunStatus,
        *,
        final_candidate_id: str | None = None,
        error: Exception | None = None,
    ) -> AutoFixRunRecord:
        if status == AutoFixRunStatus.RUNNING:
            raise LedgerError("Cannot finish an AutoFix run with running status")
        now = self._now()
        with self._connection() as connection:
            finished = connection.execute(
                """
                UPDATE autofix_runs
                SET status = ?, final_candidate_id = ?, error_type = ?, error = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (
                    status.value,
                    final_candidate_id,
                    type(error).__name__ if error is not None else None,
                    self._error_text(error) if error is not None else None,
                    now,
                    run_id,
                    AutoFixRunStatus.RUNNING.value,
                ),
            )
            if finished.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM autofix_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"Unknown AutoFix run: {run_id}")
                raise LedgerError(f"AutoFix run is already terminal: {run_id}")
        return self.get_autofix_run(run_id)

    def get_autofix_run(self, run_id: str) -> AutoFixRunRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM autofix_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise LedgerError(f"Unknown AutoFix run: {run_id}")
        return self._row_to_autofix_run(row)

    def list_autofix_runs(
        self,
        *,
        status: AutoFixRunStatus | None = None,
        limit: int = 50,
    ) -> list[AutoFixRunRecord]:
        bounded_limit = max(1, min(limit, 500))
        with self._connection() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM autofix_runs ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM autofix_runs
                    WHERE status = ? ORDER BY created_at DESC LIMIT ?
                    """,
                    (status.value, bounded_limit),
                ).fetchall()
        return [self._row_to_autofix_run(row).model_copy(update={"trace": None}) for row in rows]

    def autofix_run_count(self, *, repository_path: str | Path | None = None) -> int:
        """Count durable runs for deterministic repository-scoped maintenance cadence."""
        with self._connection() as connection:
            if repository_path is None:
                row = connection.execute("SELECT COUNT(*) AS count FROM autofix_runs").fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM autofix_runs WHERE repository_path = ?",
                    (str(Path(repository_path).expanduser().resolve()),),
                ).fetchone()
        return int(row["count"])

    def autofix_attempts(self, run_id: str) -> list[AutoFixRunAttemptRecord]:
        self.get_autofix_run(run_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, attempt_number, feedback, created_at
                FROM autofix_attempts WHERE run_id = ? ORDER BY attempt_number
                """,
                (run_id,),
            ).fetchall()
        return [
            AutoFixRunAttemptRecord(
                run_id=row["run_id"],
                attempt_number=row["attempt_number"],
                feedback=AutoFixAttemptFeedback.model_validate(self._load_json(row["feedback"])),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def provider_outcomes(
        self,
        *,
        repository_path: str | Path | None = None,
        failure_type: FailureType | None = None,
        limit: int = 5000,
    ) -> dict[str, ProviderOutcomeStats]:
        """Aggregate attempted provider outcomes in an optional diagnosis context."""
        bounded_limit = max(1, min(limit, 50_000))
        conditions = ["status != ?"]
        parameter_values: list[Any] = [AutoFixRunStatus.RUNNING.value]
        if repository_path is not None:
            conditions.append("repository_path = ?")
            parameter_values.append(str(Path(repository_path).expanduser().resolve()))
        if failure_type is not None:
            conditions.append("failure_type = ?")
            parameter_values.append(failure_type.value)
        parameter_values.append(bounded_limit)
        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT recent.run_id, recent.generator_provider, recent.status, recent.updated_at,
                   a.attempt_number, a.feedback
            FROM (
                SELECT run_id, generator_provider, status, updated_at
                FROM autofix_runs
                WHERE {where_clause}
                ORDER BY updated_at DESC
                LIMIT ?
            ) AS recent
            LEFT JOIN autofix_attempts AS a ON a.run_id = recent.run_id
            ORDER BY recent.updated_at DESC, a.attempt_number
        """
        with self._connection() as connection:
            rows = connection.execute(query, tuple(parameter_values)).fetchall()

        runs: dict[str, dict[str, Any]] = {}
        for row in rows:
            run = runs.setdefault(
                str(row["run_id"]),
                {
                    "generator_provider": str(row["generator_provider"]),
                    "status": AutoFixRunStatus(row["status"]),
                    "updated_at": row["updated_at"],
                    "providers": {},
                },
            )
            if row["feedback"] is not None:
                feedback = AutoFixAttemptFeedback.model_validate(
                    self._load_json(str(row["feedback"]))
                )
                run["providers"].setdefault(feedback.provider, []).append(feedback)

        aggregates: dict[str, dict[str, Any]] = {}
        for run in runs.values():
            status = run["status"]
            providers: dict[str, list[AutoFixAttemptFeedback]] = run["providers"]
            if status == AutoFixRunStatus.INTERRUPTED and not providers:
                providers = {run["generator_provider"]: []}
            for provider, feedback_items in providers.items():
                counts = aggregates.setdefault(
                    provider,
                    {
                        "succeeded": 0,
                        "rejected": 0,
                        "failed": 0,
                        "interrupted": 0,
                        "attempts": 0,
                        "last_run_at": run["updated_at"],
                    },
                )
                if status == AutoFixRunStatus.INTERRUPTED:
                    counts["interrupted"] += 1
                    continue
                if any(item.accepted for item in feedback_items):
                    counts["succeeded"] += 1
                elif any(
                    item.phase == AutoFixPhase.EVALUATION or item.status == CandidateStatus.REJECTED
                    for item in feedback_items
                ):
                    counts["rejected"] += 1
                else:
                    counts["failed"] += 1
                counts["attempts"] += len(feedback_items)

        results: dict[str, ProviderOutcomeStats] = {}
        for provider, counts in aggregates.items():
            observations = counts["succeeded"] + counts["rejected"] + counts["failed"]
            posterior = (counts["succeeded"] + 2) / (observations + 4)
            confidence = observations / (observations + 5)
            results[provider] = ProviderOutcomeStats(
                provider=provider,
                failure_type=failure_type,
                observations=observations,
                succeeded=counts["succeeded"],
                rejected=counts["rejected"],
                failed=counts["failed"],
                interrupted=counts["interrupted"],
                posterior_success_rate=round(posterior, 4),
                confidence=round(confidence, 4),
                average_attempts=(
                    round(counts["attempts"] / observations, 3) if observations else 0
                ),
                last_run_at=counts["last_run_at"],
            )
        return results

    def skill_outcomes(
        self,
        *,
        repository_path: str | Path | None = None,
        failure_type: FailureType | None = None,
        limit: int = 5000,
    ) -> dict[tuple[str, int], SkillOutcomeStats]:
        """Aggregate run-deduplicated exposed and controlled Skill outcomes.

        Retrieved Skills provide associative evidence. Periodic holdouts provide a controlled
        estimate only across diagnostic contexts that contain both arms. Legacy records without
        context fingerprints retain their historical unstratified behavior. Bayesian shrinkage
        and bounded adjustments keep both signals from overpowering semantic relevance.
        """
        bounded_limit = max(1, min(limit, 50_000))
        conditions: list[str] = []
        parameters: list[object] = []
        if repository_path is not None:
            conditions.append("repository_path = ?")
            parameters.append(str(Path(repository_path).expanduser().resolve()))
        if failure_type is not None:
            conditions.append("failure_type = ?")
            parameters.append(failure_type.value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            "SELECT candidate_id, status, metadata FROM repair_candidates"
            f"{where} ORDER BY updated_at DESC LIMIT ?"
        )
        parameters.append(bounded_limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        precedence = {"unevaluated_failures": 0, "rejected": 1, "accepted": 2}
        units: dict[tuple[str, tuple[str, int], str], dict[str, object]] = {}
        for row in rows:
            status = CandidateStatus(row["status"])
            if status == CandidateStatus.PROPOSED:
                continue
            metadata = self._load_json(row["metadata"])
            outcome = self._outcome_category(status, metadata)
            run_id = metadata.get("autofix_run_id")
            unit_id = (
                run_id.strip()
                if isinstance(run_id, str) and run_id.strip()
                else row["candidate_id"]
            )
            arms = (
                ("exposed", metadata.get("retrieved_skills", [])),
                ("control", metadata.get("withheld_skills", [])),
            )
            for arm, evidence in arms:
                if not isinstance(evidence, list):
                    continue
                seen: set[tuple[str, int]] = set()
                for item in evidence:
                    key = self._skill_key(item)
                    if key is None or key in seen:
                        continue
                    seen.add(key)
                    unit_key = (unit_id, key, arm)
                    existing = units.get(unit_key)
                    post_acceptance_failure = (
                        status == CandidateStatus.FAILED and outcome == "accepted"
                    )
                    if existing is None:
                        units[unit_key] = {
                            "outcome": outcome,
                            "post_acceptance_failure": post_acceptance_failure,
                            "context": self._skill_context_fingerprint(metadata, unit_id),
                        }
                    else:
                        existing_outcome = str(existing["outcome"])
                        if precedence[outcome] > precedence[existing_outcome]:
                            existing["outcome"] = outcome
                        existing["post_acceptance_failure"] = (
                            bool(existing["post_acceptance_failure"]) or post_acceptance_failure
                        )
                        context = self._skill_context_fingerprint(metadata, unit_id)
                        if existing["context"] != context:
                            existing["context"] = None

        aggregates: dict[tuple[str, int], dict[str, int]] = {}
        context_arms: dict[tuple[tuple[str, int], str], set[str]] = {}
        contextual_skills: set[tuple[str, int]] = set()
        for (_, key, arm), evidence in units.items():
            stored_context = evidence["context"]
            if isinstance(stored_context, str):
                contextual_skills.add(key)
                context_arms.setdefault((key, stored_context), set()).add(arm)
        matched_contexts = {
            (key, context)
            for (key, context), arms in context_arms.items()
            if arms == {"exposed", "control"}
        }
        matched_contexts_by_skill: dict[tuple[str, int], set[str]] = {}
        for key, context in matched_contexts:
            matched_contexts_by_skill.setdefault(key, set()).add(context)
        for (_, key, arm), evidence in units.items():
            counts = aggregates.setdefault(
                key,
                {
                    "accepted": 0,
                    "rejected": 0,
                    "unevaluated_failures": 0,
                    "post_acceptance_failures": 0,
                    "control_accepted": 0,
                    "control_rejected": 0,
                    "control_unevaluated_failures": 0,
                    "comparison_exposed_accepted": 0,
                    "comparison_exposed_rejected": 0,
                    "comparison_exposed_unevaluated_failures": 0,
                    "comparison_control_accepted": 0,
                    "comparison_control_rejected": 0,
                    "comparison_control_unevaluated_failures": 0,
                },
            )
            outcome = str(evidence["outcome"])
            prefix = "" if arm == "exposed" else "control_"
            counts[f"{prefix}{outcome}"] += 1
            if arm == "exposed" and bool(evidence["post_acceptance_failure"]):
                counts["post_acceptance_failures"] += 1
            stored_context = evidence["context"]
            include_comparison = key not in contextual_skills or (
                isinstance(stored_context, str) and (key, stored_context) in matched_contexts
            )
            if include_comparison:
                counts[f"comparison_{arm}_{outcome}"] += 1

        results: dict[tuple[str, int], SkillOutcomeStats] = {}
        for (name, version), counts in aggregates.items():
            observations = counts["accepted"] + counts["rejected"] + counts["unevaluated_failures"]
            control_observations = (
                counts["control_accepted"]
                + counts["control_rejected"]
                + counts["control_unevaluated_failures"]
            )
            posterior = (counts["accepted"] + 2) / (observations + 4)
            confidence = observations / (observations + 5)
            control_posterior = (counts["control_accepted"] + 2) / (control_observations + 4)
            comparison_exposed_observations = (
                counts["comparison_exposed_accepted"]
                + counts["comparison_exposed_rejected"]
                + counts["comparison_exposed_unevaluated_failures"]
            )
            comparison_control_observations = (
                counts["comparison_control_accepted"]
                + counts["comparison_control_rejected"]
                + counts["comparison_control_unevaluated_failures"]
            )
            comparison_exposed_posterior = (counts["comparison_exposed_accepted"] + 2) / (
                comparison_exposed_observations + 4
            )
            comparison_control_posterior = (counts["comparison_control_accepted"] + 2) / (
                comparison_control_observations + 4
            )
            estimated_lift = comparison_exposed_posterior - comparison_control_posterior
            exposed_variance = self._beta_posterior_variance(
                counts["comparison_exposed_accepted"], comparison_exposed_observations
            )
            control_variance = self._beta_posterior_variance(
                counts["comparison_control_accepted"], comparison_control_observations
            )
            lift_standard_error = math.sqrt(exposed_variance + control_variance)
            lift_margin = 1.96 * lift_standard_error
            lift_lower_bound = max(-1.0, estimated_lift - lift_margin)
            lift_upper_bound = min(1.0, estimated_lift + lift_margin)
            comparison_exposed_confidence = comparison_exposed_observations / (
                comparison_exposed_observations + 5
            )
            comparison_control_confidence = comparison_control_observations / (
                comparison_control_observations + 5
            )
            ablation_confidence = min(comparison_exposed_confidence, comparison_control_confidence)
            ablation_adjustment = max(-1.0, min(1.0, estimated_lift * 2 * ablation_confidence))
            associative_adjustment = (posterior - 0.5) * 4 * confidence
            adjustment = max(-2.0, min(2.0, associative_adjustment + ablation_adjustment))
            matched_for_skill = matched_contexts_by_skill.get((name, version), set())
            if (name, version) not in contextual_skills:
                comparison_mode = SkillComparisonMode.LEGACY_UNSTRATIFIED
            elif matched_for_skill:
                comparison_mode = SkillComparisonMode.MATCHED_CONTEXT
            else:
                comparison_mode = SkillComparisonMode.NO_CONTEXT_OVERLAP
            results[(name, version)] = SkillOutcomeStats(
                skill_name=name,
                skill_version=version,
                failure_type=failure_type,
                observations=observations,
                accepted=counts["accepted"],
                rejected=counts["rejected"],
                unevaluated_failures=counts["unevaluated_failures"],
                post_acceptance_failures=counts["post_acceptance_failures"],
                posterior_success_rate=round(posterior, 4),
                confidence=round(confidence, 4),
                score_adjustment=round(adjustment, 3),
                control_observations=control_observations,
                control_accepted=counts["control_accepted"],
                control_rejected=counts["control_rejected"],
                control_unevaluated_failures=counts["control_unevaluated_failures"],
                control_posterior_success_rate=round(control_posterior, 4),
                estimated_lift=round(estimated_lift, 4),
                estimated_lift_standard_error=round(lift_standard_error, 4),
                estimated_lift_lower_bound=round(lift_lower_bound, 4),
                estimated_lift_upper_bound=round(lift_upper_bound, 4),
                ablation_confidence=round(ablation_confidence, 4),
                ablation_score_adjustment=round(ablation_adjustment, 3),
                comparison_mode=comparison_mode,
                matched_contexts=len(matched_for_skill),
                comparison_exposed_observations=comparison_exposed_observations,
                comparison_exposed_accepted=counts["comparison_exposed_accepted"],
                comparison_exposed_rejected=counts["comparison_exposed_rejected"],
                comparison_exposed_unevaluated_failures=counts[
                    "comparison_exposed_unevaluated_failures"
                ],
                comparison_control_observations=comparison_control_observations,
                comparison_control_accepted=counts["comparison_control_accepted"],
                comparison_control_rejected=counts["comparison_control_rejected"],
                comparison_control_unevaluated_failures=counts[
                    "comparison_control_unevaluated_failures"
                ],
                comparison_exposed_posterior_success_rate=round(comparison_exposed_posterior, 4),
                comparison_control_posterior_success_rate=round(comparison_control_posterior, 4),
            )
        return results

    def skill_context_balances(
        self,
        context_fingerprint: str,
        *,
        repository_path: str | Path | None = None,
        failure_type: FailureType | None = None,
        limit: int = 5000,
    ) -> dict[tuple[str, int], SkillContextBalance]:
        """Count run-deduplicated exposed/control arms for one valid repair context."""
        normalized_context = context_fingerprint.casefold()
        if len(normalized_context) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_context
        ):
            raise ValueError("context_fingerprint must be a 64-character lowercase hex digest")
        bounded_limit = max(1, min(limit, 50_000))
        conditions: list[str] = []
        parameters: list[object] = []
        if repository_path is not None:
            conditions.append("repository_path = ?")
            parameters.append(str(Path(repository_path).expanduser().resolve()))
        if failure_type is not None:
            conditions.append("failure_type = ?")
            parameters.append(failure_type.value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            "SELECT candidate_id, status, metadata FROM repair_candidates"
            f"{where} ORDER BY updated_at DESC LIMIT ?"
        )
        parameters.append(bounded_limit)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()

        units: set[tuple[str, tuple[str, int], str]] = set()
        for row in rows:
            if CandidateStatus(row["status"]) == CandidateStatus.PROPOSED:
                continue
            metadata = self._load_json(row["metadata"])
            run_id = metadata.get("autofix_run_id")
            unit_id = (
                run_id.strip()
                if isinstance(run_id, str) and run_id.strip()
                else row["candidate_id"]
            )
            if self._skill_context_fingerprint(metadata, unit_id) != normalized_context:
                continue
            for arm, evidence in (
                ("exposed", metadata.get("retrieved_skills", [])),
                ("control", metadata.get("withheld_skills", [])),
            ):
                if not isinstance(evidence, list):
                    continue
                for item in evidence:
                    key = self._skill_key(item)
                    if key is not None:
                        units.add((unit_id, key, arm))

        counts: dict[tuple[str, int], dict[str, int]] = {}
        for _, key, arm in units:
            counts.setdefault(key, {"exposed": 0, "control": 0})[arm] += 1
        return {
            key: SkillContextBalance(
                skill_name=key[0],
                skill_version=key[1],
                context_fingerprint=normalized_context,
                exposed_runs=arms["exposed"],
                control_runs=arms["control"],
                paired_runs=min(arms["exposed"], arms["control"]),
                control_deficit=max(0, arms["exposed"] - arms["control"]),
                control_surplus=max(0, arms["control"] - arms["exposed"]),
            )
            for key, arms in counts.items()
        }

    @staticmethod
    def _beta_posterior_variance(accepted: int, observations: int) -> float:
        alpha = accepted + 2
        beta = observations - accepted + 2
        total = alpha + beta
        return (alpha * beta) / (total * total * (total + 1))

    @staticmethod
    def _outcome_category(status: CandidateStatus, metadata: dict[str, Any]) -> str:
        failed_from = metadata.get("failed_from_status")
        accepted = status in {
            CandidateStatus.VERIFIED,
            CandidateStatus.PROMOTED,
            CandidateStatus.LEARNED,
        } or (
            status == CandidateStatus.FAILED
            and (
                bool(metadata.get("evaluation_accepted"))
                or failed_from
                in {
                    CandidateStatus.VERIFIED.value,
                    CandidateStatus.PROMOTED.value,
                }
            )
        )
        if accepted:
            return "accepted"
        if status == CandidateStatus.REJECTED:
            return "rejected"
        return "unevaluated_failures"

    @staticmethod
    def _skill_key(value: Any) -> tuple[str, int] | None:
        if not isinstance(value, dict):
            return None
        name = value.get("name", value.get("skill_name"))
        version = value.get("version", value.get("skill_version"))
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None
        return name.strip(), version

    @staticmethod
    def _skill_context_fingerprint(metadata: dict[str, Any], unit_id: str) -> str | None:
        if "skill_context_fingerprint" not in metadata:
            return None
        value = metadata.get("skill_context_fingerprint")
        if not isinstance(value, str) or len(value) != 64:
            return f"invalid:{unit_id}"
        normalized = value.casefold()
        if any(character not in "0123456789abcdef" for character in normalized):
            return f"invalid:{unit_id}"
        return normalized

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS repair_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    repository_path TEXT NOT NULL,
                    patch_sha256 TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    changed_paths TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (candidate_id) REFERENCES repair_candidates(candidate_id)
                );
                CREATE TABLE IF NOT EXISTS autofix_runs (
                    run_id TEXT PRIMARY KEY,
                    repository_path TEXT NOT NULL,
                    trace_sha256 TEXT NOT NULL,
                    trace_json TEXT,
                    generator_provider TEXT NOT NULL,
                    generator_selection TEXT,
                    failure_type TEXT,
                    max_attempts INTEGER NOT NULL,
                    promote_requested INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    final_candidate_id TEXT,
                    error_type TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (final_candidate_id) REFERENCES repair_candidates(candidate_id)
                );
                CREATE TABLE IF NOT EXISTS autofix_attempts (
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    candidate_id TEXT,
                    patch_sha256 TEXT,
                    feedback TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, attempt_number),
                    FOREIGN KEY (run_id) REFERENCES autofix_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (candidate_id) REFERENCES repair_candidates(candidate_id)
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_status
                    ON repair_candidates(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_patch
                    ON repair_candidates(patch_sha256);
                CREATE INDEX IF NOT EXISTS idx_candidates_repository
                    ON repair_candidates(repository_path, updated_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_repository_failure
                    ON repair_candidates(repository_path, failure_type, updated_at);
                CREATE INDEX IF NOT EXISTS idx_events_candidate
                    ON repair_events(candidate_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_autofix_runs_status
                    ON autofix_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_autofix_runs_repository
                    ON autofix_runs(repository_path, created_at);
                CREATE INDEX IF NOT EXISTS idx_autofix_attempts_candidate
                    ON autofix_attempts(candidate_id);
                """
            )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(autofix_runs)").fetchall()
            }
            for column, declaration in (
                ("generator_selection", "generator_selection TEXT"),
                ("failure_type", "failure_type TEXT"),
            ):
                if column in run_columns:
                    continue
                try:
                    connection.execute(f"ALTER TABLE autofix_runs ADD COLUMN {declaration}")
                except sqlite3.OperationalError:
                    refreshed = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(autofix_runs)").fetchall()
                    }
                    if column not in refreshed:
                        raise
            connection.execute(
                """
                UPDATE autofix_runs
                SET failure_type = (
                    SELECT candidate.failure_type
                    FROM autofix_attempts AS attempt
                    JOIN repair_candidates AS candidate
                      ON candidate.candidate_id = attempt.candidate_id
                    WHERE attempt.run_id = autofix_runs.run_id
                    ORDER BY attempt.attempt_number DESC
                    LIMIT 1
                )
                WHERE failure_type IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM autofix_attempts AS attempt
                    JOIN repair_candidates AS candidate
                      ON candidate.candidate_id = attempt.candidate_id
                    WHERE attempt.run_id = autofix_runs.run_id
                  )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_autofix_runs_provider_context
                ON autofix_runs(repository_path, failure_type, updated_at)
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        candidate_id: str,
        status: CandidateStatus,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO repair_events (candidate_id, status, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (candidate_id, status.value, RepairLedger._json(details), created_at),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RepairCandidateRecord:
        return RepairCandidateRecord(
            candidate_id=row["candidate_id"],
            title=row["title"],
            repository_path=row["repository_path"],
            patch_sha256=row["patch_sha256"],
            failure_type=FailureType(row["failure_type"]),
            status=CandidateStatus(row["status"]),
            changed_paths=RepairLedger._load_json(row["changed_paths"]),
            metadata=RepairLedger._load_json(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_autofix_run(row: sqlite3.Row) -> AutoFixRunRecord:
        trace_json = row["trace_json"]
        selection_json = row["generator_selection"]
        return AutoFixRunRecord(
            run_id=row["run_id"],
            repository_path=row["repository_path"],
            trace_sha256=row["trace_sha256"],
            trace=(
                AgentTrace.model_validate(RepairLedger._load_json(trace_json))
                if trace_json is not None
                else None
            ),
            trace_persisted=trace_json is not None,
            generator_provider=row["generator_provider"],
            generator_selection=(
                ProviderSelection.model_validate(RepairLedger._load_json(selection_json))
                if selection_json is not None
                else None
            ),
            failure_type=(FailureType(row["failure_type"]) if row["failure_type"] else None),
            max_attempts=row["max_attempts"],
            promote_requested=bool(row["promote_requested"]),
            status=AutoFixRunStatus(row["status"]),
            final_candidate_id=row["final_candidate_id"],
            error_type=row["error_type"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _candidate_run_id(row: sqlite3.Row) -> str | None:
        metadata = RepairLedger._load_json(row["metadata"])
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("autofix_run_id")
        return value if isinstance(value, str) else None

    @staticmethod
    def _error_text(error: Exception) -> str:
        return str(error)[-4000:]

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load_json(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
