from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from config.settings import AppConfig
from core.models import AssertionState, ClinicalFact, JobStatus, SessionStatus
from core.templates import NoteHallucinationFirewall, OllamaEngine, TemplateNoteEngine, TemplateRegistry
from models.clerking_sheet import ClerkingSheet
from storage.database import Database
from storage.jobs import (
    ClaimedJob, NoteGenerationStage, claim_note_generation_job, enqueue_treatment_plan_job,
)


logger = logging.getLogger(__name__)


class NoteGenerationWorker:
    def __init__(self, database: Database, settings: AppConfig, *, engine=None, worker_id: str | None = None) -> None:
        self.database = database
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.lease_seconds = int(settings.worker.get("lease_seconds", 300))
        self.firewall = NoteHallucinationFirewall()
        if engine is None:
            template_directory = Path(settings.note_generation.get("template_directory", "templates"))
            template = TemplateRegistry(template_directory).load(settings.note_generation.get("template", "primary_care"))
            engine_name = str(settings.note_generation.get("engine", "structured_template"))
            if engine_name == "ollama":
                engine = OllamaEngine(
                    template, model=str(settings.note_generation.get("ollama_model") or ""),
                    base_url=str(settings.note_generation.get("ollama_base_url", "http://127.0.0.1:11434")),
                )
            elif engine_name in {"local", "structured_template"}:
                engine = TemplateNoteEngine(template)
            else:
                raise ValueError(f"unsupported note generation engine: {engine_name}")
        self.engine = engine

    def process_once(self) -> bool:
        with self.database.transaction() as connection:
            job = claim_note_generation_job(connection, lease_owner=self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        try:
            self._process(job)
        except Exception as exc:
            logger.exception("note_generation_job_failed", extra={"job_id": str(job.id)})
            self._record_failure(job, exc)
        return True

    def _process(self, job: ClaimedJob) -> None:
        with self.database.connect() as connection:
            sheet_row = connection.execute(
                "SELECT * FROM clerking_sheets WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(job.session_id),)
            ).fetchone()
            fact_rows = connection.execute(
                "SELECT * FROM structured_facts WHERE session_id = ? ORDER BY created_at, id", (str(job.session_id),)
            ).fetchall()
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM notes WHERE session_id = ?", (str(job.session_id),)
            ).fetchone()[0]
        if sheet_row is None:
            raise ValueError("note generation requires a clerking sheet")
        sheet = ClerkingSheet.model_validate_json(sheet_row["structured_json"])
        facts = [ClinicalFact(
            id=UUID(row["id"]), session_id=UUID(row["session_id"]), category=row["category"], name=row["name"],
            value=row["value"], assertion=AssertionState(row["assertion"]), confidence=row["confidence"],
            transcript_segment_ids=[UUID(item) for item in json.loads(row["evidence_json"])], original_text=row["original_text"],
        ) for row in fact_rows]
        note = self.engine.generate(sheet).model_copy(update={"version": next_version})
        findings = self.firewall.validate(note, sheet, facts)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.transaction() as connection:
            self._require_lease(connection, job)
            connection.execute(
                "INSERT INTO notes(id, session_id, clerking_sheet_id, version, language, content, plain_text, "
                "template_name, engine, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(note.id), str(note.session_id), str(note.clerking_sheet_id), note.version, note.language,
                 note.content, note.plain_text, note.template_name, note.engine, note.status.value, now),
            )
            for finding in findings:
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
                (JobStatus.SUCCEEDED.value, NoteGenerationStage.GENERATED.value, now, str(job.id), job.lease_owner),
            )
            connection.execute(
                "UPDATE sessions SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                (SessionStatus.REVIEW.value, now, str(job.session_id)),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'clinical_note', ?, ?, ?, ?, ?)",
                (str(job.session_id), str(note.id), NoteGenerationStage.GENERATED.value, self.worker_id,
                 json.dumps({"version": note.version, "template": note.template_name, "engine": note.engine,
                             "claim_states": sorted({item.state.value for item in findings})}), now),
            )
            if enqueue_treatment_plan_job(
                connection, job.session_id,
                max_attempts=int(self.settings.worker.get("max_attempts", 3)), now=now,
            ):
                queued = connection.execute(
                    "SELECT id FROM jobs WHERE session_id = ? AND job_type = 'TREATMENT_PLAN'",
                    (str(job.session_id),),
                ).fetchone()
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                    "VALUES (?, 'job', ?, 'TREATMENT_PLAN_QUEUED', ?, ?, ?)",
                    (str(job.session_id), queued["id"], self.worker_id,
                     json.dumps({"note_id": str(note.id)}), now),
                )

    @staticmethod
    def _require_lease(connection, job: ClaimedJob) -> None:
        row = connection.execute("SELECT status, lease_owner FROM jobs WHERE id = ?", (str(job.id),)).fetchone()
        if row is None or row["lease_owner"] != job.lease_owner or row["status"] not in {"LEASED", "RUNNING"}:
            raise RuntimeError("note generation job lease was lost before persistence")

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
