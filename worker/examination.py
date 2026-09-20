from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.examination import ExaminationInterpreter
from core.models import JobStatus, SessionStatus
from storage.database import Database
from storage.jobs import (
    ClaimedJob, ExaminationInterpretationStage, claim_examination_interpretation_job,
    enqueue_structuring_job,
)


logger = logging.getLogger(__name__)


class ExaminationInterpretationWorker:
    def __init__(
        self, database: Database, settings: AppConfig, *,
        interpreter: ExaminationInterpreter | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        examination = settings.examination
        self.interpreter = interpreter or ExaminationInterpreter(
            confidence_threshold=float(examination.get("confidence_threshold", 0.8)),
            pass_through_unmappable=bool(examination.get("pass_through_unmappable", True)),
        )
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_examination_interpretation_job(
                connection, lease_owner=self.worker_id, lease_seconds=self.lease_seconds
            )
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("examination_interpretation_job_failed", extra={"job_id": str(job.id)})
            self._record_failure(job, exc)
        return True

    def _process(self, job: ClaimedJob) -> None:
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT COUNT(*) FROM examination_findings WHERE session_id = ?", (str(job.session_id),)
            ).fetchone()[0]
        findings = [] if existing else self.interpreter.interpret(self._load_segments(job.session_id))
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            self._require_lease(connection, job)
            for finding in findings:
                connection.execute(
                    "INSERT INTO examination_findings(id, session_id, raw_text, interpreted_text, confidence, "
                    "status, evidence_json, mapping_key, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (str(finding.id), str(finding.session_id), finding.raw_text, finding.interpreted_text,
                     finding.confidence, finding.status.value,
                     json.dumps([str(item) for item in finding.transcript_segment_ids]), finding.mapping_key, now),
                )
            total = existing + len(findings)
            connection.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (JobStatus.SUCCEEDED.value, ExaminationInterpretationStage.COMPLETE.value,
                 now, str(job.id), job.lease_owner),
            )
            enqueue_structuring_job(
                connection, job.session_id,
                max_attempts=int(self.settings.worker.get("max_attempts", 3)), now=now,
            )
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (SessionStatus.PROCESSING.value, now, str(job.session_id)),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'examination_findings', ?, ?, ?, ?, ?)",
                (str(job.session_id), str(job.id), ExaminationInterpretationStage.COMPLETE.value,
                 self.worker_id, json.dumps({"finding_count": total}), now),
            )

    def _load_segments(self, session_id: UUID) -> list[dict]:
        with self.database.connect() as connection:
            transcript = connection.execute(
                "SELECT segments_json FROM transcripts WHERE session_id = ? ORDER BY version DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
            translated = connection.execute(
                "SELECT storage_path FROM transcript_artifacts WHERE session_id = ? "
                "AND kind = 'TRANSLATED_TRANSCRIPT' ORDER BY version DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        if transcript is None:
            raise ValueError("session has no transcript")
        language = {}
        if translated:
            payload = json.loads(Path(translated["storage_path"]).read_text(encoding="utf-8"))
            language = {str(item["segment_id"]): item for item in payload["segments"]}
        segments = []
        for item in json.loads(transcript["segments_json"]):
            metadata = language.get(str(item["id"]), {})
            segments.append({**item, "segment_id": item["id"], "session_id": str(session_id), **metadata})
        return segments

    @staticmethod
    def _require_lease(connection, job: ClaimedJob) -> None:
        owned = connection.execute(
            "SELECT status, lease_owner FROM jobs WHERE id = ?", (str(job.id),)
        ).fetchone()
        if owned is None or owned["lease_owner"] != job.lease_owner or owned["status"] not in (
            JobStatus.LEASED.value, JobStatus.RUNNING.value,
        ):
            raise RuntimeError("examination interpretation job lease was lost before persistence")

    def _record_failure(self, job: ClaimedJob, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        terminal = job.attempts >= job.max_attempts
        status = JobStatus.FAILED if terminal else JobStatus.PENDING
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "error = ?, updated_at = ? WHERE id = ? AND lease_owner = ?",
                (status.value, (now + timedelta(seconds=min(60, 2**job.attempts))).isoformat(),
                 error, now.isoformat(), str(job.id), job.lease_owner),
            )
            if cursor.rowcount and terminal:
                connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (SessionStatus.FAILED.value, now.isoformat(), str(job.session_id)),
                )
