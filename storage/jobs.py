from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from core.models import JobStatus, SessionStatus


class TranscriptionStage(StrEnum):
    STARTED = "TRANSCRIPTION_STARTED"
    COMPLETE = "TRANSCRIPTION_COMPLETE"


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    session_id: UUID
    attempts: int
    max_attempts: int
    lease_owner: str


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_transcription_job(
    connection: sqlite3.Connection,
    session_id: UUID | str,
    *,
    max_attempts: int,
    now: str | None = None,
) -> bool:
    timestamp = now or utc_now_text()
    cursor = connection.execute(
        "INSERT OR IGNORE INTO jobs(id, session_id, job_type, status, payload_json, "
        "attempts, max_attempts, available_at, created_at, updated_at) "
        "VALUES (?, ?, 'TRANSCRIPTION', ?, '{}', 0, ?, ?, ?, ?)",
        (
            str(uuid4()),
            str(UUID(str(session_id))),
            JobStatus.PENDING.value,
            max_attempts,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return cursor.rowcount == 1


def claim_transcription_job(
    connection: sqlite3.Connection,
    *,
    lease_owner: str,
    lease_seconds: int,
) -> ClaimedJob | None:
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    row = connection.execute(
        "SELECT * FROM jobs WHERE job_type = 'TRANSCRIPTION' AND attempts < max_attempts "
        "AND ((status = ? AND available_at <= ?) OR "
        "(status IN (?, ?) AND lease_expires_at < ?)) "
        "ORDER BY available_at, created_at LIMIT 1",
        (
            JobStatus.PENDING.value,
            now_text,
            JobStatus.LEASED.value,
            JobStatus.RUNNING.value,
            now_text,
        ),
    ).fetchone()
    if row is None:
        return None
    lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    cursor = connection.execute(
        "UPDATE jobs SET status = ?, stage = ?, attempts = attempts + 1, "
        "lease_owner = ?, lease_expires_at = ?, error = NULL, updated_at = ? "
        "WHERE id = ? AND attempts = ?",
        (
            JobStatus.LEASED.value,
            TranscriptionStage.STARTED.value,
            lease_owner,
            lease_expires,
            now_text,
            row["id"],
            row["attempts"],
        ),
    )
    if cursor.rowcount != 1:
        return None
    connection.execute(
        "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
        (SessionStatus.PROCESSING.value, now_text, row["session_id"]),
    )
    connection.execute(
        "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, "
        "after_json, created_at) VALUES (?, 'job', ?, ?, ?, ?, ?)",
        (
            row["session_id"],
            row["id"],
            TranscriptionStage.STARTED.value,
            lease_owner,
            json.dumps({"attempt": row["attempts"] + 1}),
            now_text,
        ),
    )
    return ClaimedJob(
        id=UUID(row["id"]),
        session_id=UUID(row["session_id"]),
        attempts=row["attempts"] + 1,
        max_attempts=row["max_attempts"],
        lease_owner=lease_owner,
    )


def renew_lease(
    connection: sqlite3.Connection, job: ClaimedJob, *, lease_seconds: int
) -> bool:
    now = datetime.now(timezone.utc)
    cursor = connection.execute(
        "UPDATE jobs SET lease_expires_at = ?, updated_at = ? "
        "WHERE id = ? AND lease_owner = ? AND status IN (?, ?)",
        (
            (now + timedelta(seconds=lease_seconds)).isoformat(),
            now.isoformat(),
            str(job.id),
            job.lease_owner,
            JobStatus.LEASED.value,
            JobStatus.RUNNING.value,
        ),
    )
    return cursor.rowcount == 1
