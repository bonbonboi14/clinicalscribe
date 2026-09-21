from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.clerking import ClerkingSheetGenerator
from core.models import AssertionState, ClinicalFact, JobStatus, SessionStatus
from models.examination_finding import ExaminationFindingStatus
from core.structuring import ClinicalFactExtractor
from storage.database import Database
from storage.jobs import ClaimedJob, StructuringStage, claim_structuring_job, enqueue_note_generation_job


logger = logging.getLogger(__name__)


class StructuringWorker:
    def __init__(
        self, database: Database, settings: AppConfig, *,
        extractor: ClinicalFactExtractor | None = None,
        generator: ClerkingSheetGenerator | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.extractor = extractor or ClinicalFactExtractor()
        self.generator = generator or ClerkingSheetGenerator()
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_structuring_job(connection, lease_owner=self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("structuring_job_failed", extra={"job_id": str(job.id)})
            self._record_failure(job, exc)
        return True

    def _process(self, job: ClaimedJob) -> None:
        facts = self._load_facts(job.session_id)
        if not facts:
            segments = self._load_segments(job.session_id)
            facts = self.extractor.extract(segments)
            examination_facts = self._load_examination_facts(job.session_id)
            if examination_facts:
                # The interpreter owns PE wording. This prevents the general
                # extractor from bypassing review with raw examination prose.
                facts = [fact for fact in facts if fact.category != "PE"] + examination_facts
            now = datetime.now(timezone.utc).isoformat()
            with self.database.transaction() as connection:
                self._require_lease(connection, job)
                for fact in facts:
                    connection.execute(
                        "INSERT INTO structured_facts(id, session_id, category, name, value, assertion, confidence, evidence_json, original_text, version, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (str(fact.id), str(fact.session_id), fact.category, fact.name, fact.value, fact.assertion.value,
                         fact.confidence, json.dumps([str(item) for item in fact.transcript_segment_ids]), fact.original_text, now),
                    )
                connection.execute(
                    "UPDATE jobs SET stage = ?, updated_at = ? WHERE id = ? AND lease_owner = ?",
                    (StructuringStage.COMPLETE.value, now, str(job.id), job.lease_owner),
                )
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                    "VALUES (?, 'structured_facts', ?, ?, ?, ?, ?)",
                    (str(job.session_id), str(job.id), StructuringStage.COMPLETE.value, self.worker_id, json.dumps({"fact_count": len(facts)}), now),
                )

        with self.database.connect() as connection:
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM clerking_sheets WHERE session_id = ?", (str(job.session_id),)
            ).fetchone()[0]
        sheet = self.generator.generate(job.session_id, facts, version=next_version)
        validation_findings = self.generator.safety_firewall.validate_clerking_sheet(sheet, facts).findings
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            self._require_lease(connection, job)
            existing = connection.execute(
                "SELECT id FROM clerking_sheets WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(job.session_id),)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO clerking_sheets(id, session_id, version, structured_json, fact_ids_json, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (str(sheet.id), str(sheet.session_id), sheet.version, sheet.model_dump_json(),
                    json.dumps([str(item) for item in sheet.fact_ids]), sheet.status, now),
                )
                for finding in validation_findings:
                    connection.execute(
                        "INSERT INTO validation_findings(id, session_id, artefact_type, artefact_id, claim_text, state, evidence_json, explanation, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(finding.id), str(finding.session_id), finding.artefact_type, str(finding.artefact_id),
                         finding.claim_text, finding.state.value,
                         json.dumps([str(item) for item in finding.evidence_segment_ids]), finding.explanation, now),
                    )
            else:
                sheet_id = existing["id"]
            sheet_id = str(sheet.id) if existing is None else sheet_id
            connection.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (JobStatus.SUCCEEDED.value, StructuringStage.CLERKING_SHEET_GENERATED.value, now, str(job.id), job.lease_owner),
            )
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (SessionStatus.REVIEW.value, now, str(job.session_id)),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'clerking_sheet', ?, ?, ?, ?, ?)",
                (str(job.session_id), sheet_id, StructuringStage.CLERKING_SHEET_GENERATED.value, self.worker_id,
                 json.dumps({"version": sheet.version, "fact_count": len(sheet.fact_ids)}), now),
            )
            if enqueue_note_generation_job(
                connection, job.session_id,
                max_attempts=int(self.settings.worker.get("max_attempts", 3)), now=now,
            ):
                queued_job = connection.execute(
                    "SELECT id FROM jobs WHERE session_id = ? AND job_type = 'NOTE_GENERATION'",
                    (str(job.session_id),),
                ).fetchone()
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                    "VALUES (?, 'job', ?, 'NOTE_GENERATION_QUEUED', ?, ?, ?)",
                    (str(job.session_id), queued_job["id"], self.worker_id,
                     json.dumps({"clerking_sheet_id": sheet_id}), now),
                )

    def _load_segments(self, session_id: UUID) -> list[dict]:
        with self.database.connect() as connection:
            transcript = connection.execute(
                "SELECT segments_json FROM transcripts WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)
            ).fetchone()
            translated = connection.execute(
                "SELECT storage_path FROM transcript_artifacts WHERE session_id = ? AND kind = 'TRANSLATED_TRANSCRIPT' ORDER BY version DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
            role_rows = connection.execute(
                "SELECT a.segment_id, r.role FROM segment_speaker_assignments a "
                "JOIN speakers s ON s.id = a.speaker_id "
                "JOIN speaker_revisions r ON r.speaker_id = s.id "
                "WHERE a.session_id = ? "
                "AND a.version = (SELECT MAX(a2.version) FROM segment_speaker_assignments a2 WHERE a2.segment_id = a.segment_id) "
                "AND r.version = (SELECT MAX(r2.version) FROM speaker_revisions r2 WHERE r2.speaker_id = s.id)",
                (str(session_id),),
            ).fetchall()
        if transcript is None:
            raise ValueError("session has no transcript")
        language = {}
        speaker_roles = {row["segment_id"]: row["role"] for row in role_rows}
        if translated:
            payload = json.loads(Path(translated["storage_path"]).read_text(encoding="utf-8"))
            language = {str(item["segment_id"]): item for item in payload["segments"]}
        result = []
        for item in json.loads(transcript["segments_json"]):
            metadata = language.get(str(item["id"]), {})
            result.append({
                **item, "segment_id": item["id"], "session_id": str(session_id),
                "speaker_role": speaker_roles.get(str(item["id"])), **metadata,
            })
        return result

    def _load_facts(self, session_id: UUID) -> list[ClinicalFact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM structured_facts WHERE session_id = ? ORDER BY created_at, id", (str(session_id),)
            ).fetchall()
        return [ClinicalFact(
            id=UUID(row["id"]), session_id=UUID(row["session_id"]), category=row["category"], name=row["name"],
            value=row["value"], assertion=AssertionState(row["assertion"]), confidence=row["confidence"],
            transcript_segment_ids=[UUID(item) for item in json.loads(row["evidence_json"])], original_text=row["original_text"],
        ) for row in rows]

    def _load_examination_facts(self, session_id: UUID) -> list[ClinicalFact]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT f.*, r.interpreted_text AS revised_text, r.status AS revised_status "
                "FROM examination_findings f LEFT JOIN examination_finding_revisions r ON r.id = ("
                "SELECT r2.id FROM examination_finding_revisions r2 WHERE r2.finding_id = f.id "
                "ORDER BY r2.version DESC LIMIT 1) WHERE f.session_id = ? ORDER BY f.created_at, f.id",
                (str(session_id),),
            ).fetchall()
        facts = []
        for row in rows:
            status = ExaminationFindingStatus(row["revised_status"] or row["status"])
            confirmed = status == ExaminationFindingStatus.CONFIRMED
            text = (row["revised_text"] or row["interpreted_text"]) if confirmed else row["raw_text"]
            facts.append(ClinicalFact(
                session_id=session_id, category="PE", name=text, value=text,
                assertion=AssertionState.POSITIVE if confirmed else AssertionState.UNCERTAIN,
                confidence=row["confidence"],
                transcript_segment_ids=[UUID(item) for item in json.loads(row["evidence_json"])],
                original_text=row["raw_text"],
            ))
        return facts

    @staticmethod
    def _require_lease(connection, job: ClaimedJob) -> None:
        owned = connection.execute("SELECT status, lease_owner FROM jobs WHERE id = ?", (str(job.id),)).fetchone()
        if owned is None or owned["lease_owner"] != job.lease_owner or owned["status"] not in (JobStatus.LEASED.value, JobStatus.RUNNING.value):
            raise RuntimeError("structuring job lease was lost before persistence")

    def _record_failure(self, job: ClaimedJob, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        terminal = job.attempts >= job.max_attempts
        status = JobStatus.FAILED if terminal else JobStatus.PENDING
        error = f"{type(exc).__name__}: {exc}"[:2000]
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, lease_expires_at = NULL, error = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (status.value, (now + timedelta(seconds=min(60, 2**job.attempts))).isoformat(), error, now.isoformat(), str(job.id), job.lease_owner),
            )
            if cursor.rowcount and terminal:
                connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (SessionStatus.FAILED.value, now.isoformat(), str(job.session_id)),
                )
