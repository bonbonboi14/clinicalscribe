from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from models.examination_finding import ExaminationFinding, ExaminationFindingStatus


router = APIRouter(prefix="/api/v1/sessions", tags=["examination review"])


class ExaminationConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpreted_text: str = Field(min_length=1)
    actor: str = Field(default="local-clinician", min_length=1)


def _effective_findings(connection, session_id: UUID) -> list[ExaminationFinding]:
    rows = connection.execute(
        "SELECT f.*, r.interpreted_text AS revised_text, r.status AS revised_status "
        "FROM examination_findings f LEFT JOIN examination_finding_revisions r ON r.id = ("
        "SELECT r2.id FROM examination_finding_revisions r2 WHERE r2.finding_id = f.id "
        "ORDER BY r2.version DESC LIMIT 1) WHERE f.session_id = ? ORDER BY f.created_at, f.id",
        (str(session_id),),
    ).fetchall()
    return [ExaminationFinding(
        id=UUID(row["id"]), session_id=UUID(row["session_id"]), raw_text=row["raw_text"],
        interpreted_text=row["revised_text"] or row["interpreted_text"], confidence=row["confidence"],
        status=ExaminationFindingStatus(row["revised_status"] or row["status"]),
        transcript_segment_ids=[UUID(item) for item in json.loads(row["evidence_json"])],
        mapping_key=row["mapping_key"],
    ) for row in rows]


@router.get("/{session_id}/examination-findings", response_model=list[ExaminationFinding])
def get_examination_findings(session_id: UUID, request: Request) -> list[ExaminationFinding]:
    with request.app.state.database.connect() as connection:
        job = connection.execute(
            "SELECT status FROM jobs WHERE session_id = ? AND job_type = 'EXAMINATION_INTERPRETATION'",
            (str(session_id),),
        ).fetchone()
        findings = _effective_findings(connection, session_id)
    if job is None:
        raise HTTPException(status_code=409, detail="examination interpretation has not been queued")
    if job["status"] != "SUCCEEDED":
        raise HTTPException(status_code=409, detail="examination interpretation is not complete")
    return findings


@router.post(
    "/{session_id}/examination-findings/{finding_id}/confirmation",
    response_model=list[ExaminationFinding],
)
def confirm_examination_finding(
    session_id: UUID, finding_id: UUID, confirmation: ExaminationConfirmation, request: Request,
) -> list[ExaminationFinding]:
    interpreted_text = " ".join(confirmation.interpreted_text.strip().split())
    if not interpreted_text:
        raise HTTPException(status_code=422, detail="interpreted_text cannot be blank")
    now = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        finding = connection.execute(
            "SELECT * FROM examination_findings WHERE id = ? AND session_id = ?",
            (str(finding_id), str(session_id)),
        ).fetchone()
        if finding is None:
            raise HTTPException(status_code=404, detail="examination finding not found")
        previous = connection.execute(
            "SELECT * FROM examination_finding_revisions WHERE finding_id = ? ORDER BY version DESC LIMIT 1",
            (str(finding_id),),
        ).fetchone()
        version = (previous["version"] if previous else 0) + 1
        revision_id = str(uuid4())
        connection.execute(
            "INSERT INTO examination_finding_revisions(id, finding_id, session_id, version, interpreted_text, "
            "status, actor, created_at, supersedes_id) VALUES (?, ?, ?, ?, ?, 'CONFIRMED', ?, ?, ?)",
            (revision_id, str(finding_id), str(session_id), version, interpreted_text,
             confirmation.actor, now, previous["id"] if previous else None),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, before_json, "
            "after_json, created_at) VALUES (?, 'examination_finding_revision', ?, "
            "'EXAMINATION_FINDING_CONFIRMED', ?, ?, ?, ?)",
            (str(session_id), revision_id, confirmation.actor,
             json.dumps({"interpreted_text": previous["interpreted_text"] if previous else finding["interpreted_text"],
                         "status": previous["status"] if previous else finding["status"]}),
             json.dumps({"interpreted_text": interpreted_text, "status": "CONFIRMED", "version": version}), now),
        )
        return _effective_findings(connection, session_id)
