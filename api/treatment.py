from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from models.treatment_plan import TreatmentPlan


router = APIRouter(prefix="/api/v1/sessions", tags=["treatment plan review"])


@router.get("/{session_id}/treatment-review")
def get_treatment_review(session_id: UUID, request: Request) -> dict:
    with request.app.state.database.connect() as connection:
        transcript = connection.execute(
            "SELECT id, version, source_language, original_text, segments_json, created_at FROM transcripts "
            "WHERE session_id = ? ORDER BY version DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
        plan_row = connection.execute(
            "SELECT * FROM treatment_plans WHERE session_id = ? ORDER BY version DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
        findings = connection.execute(
            "SELECT claim_text, state, evidence_json, explanation FROM validation_findings "
            "WHERE artefact_type = 'TREATMENT_PLAN' AND artefact_id = ? ORDER BY created_at, id",
            (plan_row["id"] if plan_row else "",),
        ).fetchall()
    if transcript is None or plan_row is None:
        raise HTTPException(status_code=409, detail="transcript and treatment plan are not both ready")
    plan = TreatmentPlan.model_validate_json(plan_row["structured_json"])
    return {
        "session_id": str(session_id),
        "review_layout": ["TRANSCRIPT", "TREATMENT PLAN"],
        "transcript": {
            "id": transcript["id"], "version": transcript["version"],
            "source_language": transcript["source_language"], "original_text": transcript["original_text"],
            "segments": json.loads(transcript["segments_json"]), "created_at": transcript["created_at"],
        },
        "treatment_plan": plan.model_dump(mode="json"),
        "rendered_treatment_plan": plan.render_text(),
        "claim_validation": [
            {"claim_text": row["claim_text"], "state": row["state"],
             "evidence_segment_ids": json.loads(row["evidence_json"]), "explanation": row["explanation"]}
            for row in findings
        ],
    }

