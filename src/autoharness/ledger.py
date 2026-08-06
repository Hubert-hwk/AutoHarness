"""Persistent repair candidate ledger and auditable lifecycle transitions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from autoharness.models import (
    CandidateStatus,
    FailureType,
    RepairCandidateEvent,
    RepairCandidateRecord,
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
                CREATE INDEX IF NOT EXISTS idx_candidates_status
                    ON repair_candidates(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_candidates_patch
                    ON repair_candidates(patch_sha256);
                CREATE INDEX IF NOT EXISTS idx_events_candidate
                    ON repair_events(candidate_id, sequence);
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
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load_json(value: str) -> Any:
        return json.loads(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
