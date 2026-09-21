from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from core.export import ApprovalRequiredError, ExportService
from core.models import ClinicalNote, ReviewStatus
from core.templates import TemplateRegistry
from models.clerking_sheet import ClerkingSheet


router = APIRouter(prefix="/api/v1/sessions", tags=["clinical note review"])


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=200)


def _note(row) -> ClinicalNote:
    return ClinicalNote(
        id=UUID(row["id"]), session_id=UUID(row["session_id"]), clerking_sheet_id=UUID(row["clerking_sheet_id"]),
        version=row["version"], language=row["language"], content=row["content"], plain_text=row["plain_text"],
        template_name=row["template_name"], engine=row["engine"], status=ReviewStatus(row["status"]),
        approved_by=row["approved_by"], approved_at=row["approved_at"], created_at=row["created_at"],
    )


@router.get("/{session_id}/note-review")
def get_note_review(session_id: UUID, request: Request) -> dict:
    with request.app.state.database.connect() as connection:
        transcript = connection.execute(
            "SELECT id, version, source_language, original_text, segments_json, created_at FROM transcripts "
            "WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)
        ).fetchone()
        sheet_row = connection.execute(
            "SELECT structured_json FROM clerking_sheets WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)
        ).fetchone()
        note_row = connection.execute(
            "SELECT * FROM notes WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)
        ).fetchone()
        findings = connection.execute(
            "SELECT claim_text, state, evidence_json, explanation FROM validation_findings "
            "WHERE artefact_type = 'CLINICAL_NOTE' AND artefact_id = ? ORDER BY created_at, id",
            (note_row["id"] if note_row else "",),
        ).fetchall()
    if transcript is None or sheet_row is None or note_row is None:
        raise HTTPException(status_code=409, detail="transcript, clerking sheet, and clinical note are not all ready")
    return {
        "session_id": str(session_id),
        "transcript": {
            "id": transcript["id"], "version": transcript["version"], "source_language": transcript["source_language"],
            "original_text": transcript["original_text"], "segments": json.loads(transcript["segments_json"]),
            "created_at": transcript["created_at"],
        },
        "clerking_sheet": json.loads(sheet_row["structured_json"]),
        "clinical_note": _note(note_row).model_dump(mode="json"),
        "claim_validation": [
            {"claim_text": row["claim_text"], "state": row["state"], "evidence_segment_ids": json.loads(row["evidence_json"]),
             "explanation": row["explanation"]} for row in findings
        ],
        "github_push": {
            "enabled": request.app.state.settings.github.enabled,
            "available": (
                request.app.state.settings.github.enabled
                and note_row["status"] == ReviewStatus.APPROVED.value
            ),
        },
    }


@router.get("/{session_id}/clinical-note", response_model=ClinicalNote)
def get_clinical_note(session_id: UUID, request: Request) -> ClinicalNote:
    with request.app.state.database.connect() as connection:
        row = connection.execute("SELECT * FROM notes WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="clinical note has not been generated")
    return _note(row)


@router.post("/{session_id}/clinical-note/approval", response_model=ClinicalNote)
def approve_clinical_note(session_id: UUID, payload: ApprovalRequest, request: Request) -> ClinicalNote:
    now = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        source = connection.execute(
            "SELECT * FROM notes WHERE session_id = ? ORDER BY version DESC LIMIT 1", (str(session_id),)
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=409, detail="clinical note has not been generated")
        if source["status"] == ReviewStatus.APPROVED.value:
            return _note(source)
        validation = connection.execute(
            "SELECT state FROM validation_findings WHERE artefact_type = 'CLINICAL_NOTE' AND artefact_id = ?",
            (source["id"],),
        ).fetchall()
        if not validation or any(row["state"] in {"UNSUPPORTED", "CONTRADICTED"} for row in validation):
            raise HTTPException(status_code=409, detail="note cannot be approved without a passing hallucination-firewall result")
        note_id = str(uuid4())
        version = source["version"] + 1
        connection.execute(
            "INSERT INTO notes(id, session_id, clerking_sheet_id, version, language, content, plain_text, template_name, "
            "engine, status, approved_by, approved_at, created_at, supersedes_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, str(session_id), source["clerking_sheet_id"], version, source["language"], source["content"],
             source["plain_text"], source["template_name"], source["engine"], ReviewStatus.APPROVED.value,
             payload.actor, now, now, source["id"]),
        )
        old_findings = connection.execute(
            "SELECT * FROM validation_findings WHERE artefact_type = 'CLINICAL_NOTE' AND artefact_id = ?", (source["id"],)
        ).fetchall()
        for finding in old_findings:
            connection.execute(
                "INSERT INTO validation_findings(id, session_id, artefact_type, artefact_id, claim_text, state, evidence_json, explanation, created_at) "
                "VALUES (?, ?, 'CLINICAL_NOTE', ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), str(session_id), note_id, finding["claim_text"], finding["state"],
                 finding["evidence_json"], finding["explanation"], now),
            )
        connection.execute(
            "UPDATE sessions SET status = 'APPROVED', updated_at = ?, version = version + 1 WHERE id = ?", (now, str(session_id))
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, before_json, after_json, created_at) "
            "VALUES (?, 'clinical_note', ?, 'NOTE_APPROVED', ?, ?, ?, ?)",
            (str(session_id), note_id, payload.actor, json.dumps({"id": source["id"], "status": source["status"]}),
             json.dumps({"id": note_id, "status": "APPROVED", "version": version}), now),
        )
        approved = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return _note(approved)


@router.get("/{session_id}/clinical-note/export")
def export_clinical_note(
    session_id: UUID,
    request: Request,
    format: str = Query(pattern="^(txt|md|docx|pdf|print|json)$"),
) -> Response:
    service = ExportService(request.app.state.database)
    try:
        snapshot = service.load_approved_snapshot(session_id)
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rendered = service.render(snapshot, format)
    now = datetime.now(timezone.utc).isoformat()
    export_id = str(uuid4())
    checksum = hashlib.sha256(rendered.content).hexdigest()
    with request.app.state.database.transaction() as connection:
        connection.execute(
            "INSERT INTO exports(id, session_id, note_id, format, destination, status, requested_by, requested_at, completed_at, checksum_sha256) "
            "VALUES (?, ?, ?, ?, 'local', 'COMPLETED', 'local-clinician', ?, ?, ?)",
            (export_id, str(session_id), snapshot.note["id"], format.upper(), now, now, checksum),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
            "VALUES (?, 'export', ?, 'NOTE_EXPORTED', 'local-clinician', ?, ?)",
            (str(session_id), export_id, json.dumps({"note_id": snapshot.note["id"], "format": format, "checksum_sha256": checksum}), now),
        )
    disposition = "inline" if format == "print" else "attachment"
    return Response(
        rendered.content,
        media_type=rendered.media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{rendered.filename}"'},
    )


@router.get("/note-templates/available")
def available_templates(request: Request) -> dict[str, list[str]]:
    directory = request.app.state.settings.note_generation.get("template_directory", "templates")
    return {"templates": TemplateRegistry(directory).names()}
