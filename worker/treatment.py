from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.models import AssertionState, ClinicalFact, JobStatus, SessionStatus
from core.treatment import TreatmentPlanGenerator
from core.validation import HallucinationFirewall, NoteValidator
from storage.database import Database
from storage.jobs import ClaimedJob, TreatmentPlanStage, claim_treatment_plan_job


logger = logging.getLogger(__name__)


class TreatmentPlanWorker:
    def __init__(
        self,
        database: Database,
        settings: AppConfig,
        *,
        generator: TreatmentPlanGenerator | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.generator = generator or TreatmentPlanGenerator()
        self.validator = NoteValidator()
        self.firewall = HallucinationFirewall(self.validator)
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_treatment_plan_job(
                connection, lease_owner=self.worker_id, lease_seconds=self.lease_seconds
            )
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("treatment_plan_job_failed", extra={"job_id": str(job.id)})
            self._record_failure(job, exc)
        return True

    def _process(self, job: ClaimedJob) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM structured_facts WHERE session_id = ? ORDER BY created_at, id",
                (str(job.session_id),),
            ).fetchall()
            note = connection.execute(
                "SELECT id FROM notes WHERE session_id = ? ORDER BY version DESC LIMIT 1",
                (str(job.session_id),),
            ).fetchone()
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM treatment_plans WHERE session_id = ?",
                (str(job.session_id),),
            ).fetchone()[0]
        facts = [ClinicalFact(
            id=UUID(row["id"]), session_id=UUID(row["session_id"]), category=row["category"],
            name=row["name"], value=row["value"], assertion=AssertionState(row["assertion"]),
            confidence=row["confidence"],
            transcript_segment_ids=[UUID(item) for item in json.loads(row["evidence_json"])],
            original_text=row["original_text"],
        ) for row in rows]
        plan = self.generator.generate(
            job.session_id,
            facts,
            note_id=UUID(note["id"]) if note else None,
            version=next_version,
        )
        validation = self.firewall.require_safe(self.validator.validate_treatment_plan(plan, facts))
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            self._require_lease(connection, job)
            connection.execute(
                "INSERT INTO treatment_plans(id, session_id, note_id, version, items_json, evidence_json, "
                "structured_json, fact_ids_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(plan.id), str(plan.session_id), str(plan.note_id) if plan.note_id else None, plan.version,
                 "[]", json.dumps([str(item) for item in plan.transcript_segment_ids]),
                 plan.model_dump_json(), json.dumps([str(item) for item in plan.fact_ids]), plan.status, now),
            )
            for finding in validation.findings:
                connection.execute(
                    "INSERT INTO validation_findings(id, session_id, artefact_type, artefact_id, claim_text, state, "
                    "evidence_json, explanation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(finding.id), str(finding.session_id), finding.artefact_type, str(finding.artefact_id),
                     finding.claim_text, finding.state.value,
                     json.dumps([str(item) for item in finding.evidence_segment_ids]), finding.explanation, now),
                )
            connection.execute(
                "UPDATE jobs SET status = ?, stage = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND lease_owner = ?",
                (JobStatus.SUCCEEDED.value, TreatmentPlanStage.GENERATED.value, now, str(job.id), job.lease_owner),
            )
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (SessionStatus.REVIEW.value, now, str(job.session_id)),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'treatment_plan', ?, ?, ?, ?, ?)",
                (str(job.session_id), str(plan.id), TreatmentPlanStage.GENERATED.value, self.worker_id,
                 json.dumps({"version": plan.version, "fact_count": len(plan.fact_ids),
                             "claim_states": sorted({item.state.value for item in validation.findings})}), now),
            )

    @staticmethod
    def _require_lease(connection, job: ClaimedJob) -> None:
        row = connection.execute("SELECT status, lease_owner FROM jobs WHERE id = ?", (str(job.id),)).fetchone()
        if row is None or row["lease_owner"] != job.lease_owner or row["status"] not in {"LEASED", "RUNNING"}:
            raise RuntimeError("treatment plan job lease was lost before persistence")

    def _record_failure(self, job: ClaimedJob, exc: Exception) -> None:
        now = datetime.now(timezone.utc)
        terminal = job.attempts >= job.max_attempts
        status = JobStatus.FAILED if terminal else JobStatus.PENDING
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, available_at = ?, lease_owner = NULL, lease_expires_at = NULL, error = ?, "
                "updated_at = ? WHERE id = ? AND lease_owner = ?",
                (status.value, (now + timedelta(seconds=min(60, 2**job.attempts))).isoformat(),
                 f"{type(exc).__name__}: {exc}"[:2000], now.isoformat(), str(job.id), job.lease_owner),
            )
            if cursor.rowcount and terminal:
                connection.execute(
                    "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (SessionStatus.FAILED.value, now.isoformat(), str(job.session_id)),
                )
