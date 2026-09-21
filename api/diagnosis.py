from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from models.differential import (
    DIFFERENTIAL_DISABLED_MESSAGE,
    DIFFERENTIAL_DISCLAIMER,
    DifferentialResult,
)
from storage.jobs import enqueue_differential_job


router = APIRouter(prefix="/api/v1/sessions", tags=["differential diagnosis"])


class DiagnosisToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _toggle_state(request: Request, session_row) -> dict:
    system_enabled = request.app.state.settings.diagnosis.enabled
    stored = session_row["diagnosis_enabled"]
    session_enabled = None if stored is None else bool(stored)
    return {
        "system_enabled": system_enabled,
        "session_enabled": session_enabled,
        "effective_enabled": system_enabled and session_enabled is not False,
        "disabled_message": DIFFERENTIAL_DISABLED_MESSAGE,
    }


@router.get("/{session_id}/diagnosis-settings")
def get_diagnosis_settings(session_id: UUID, request: Request) -> dict:
    with request.app.state.database.connect() as connection:
        row = connection.execute(
            "SELECT diagnosis_enabled FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _toggle_state(request, row)


@router.put("/{session_id}/diagnosis-settings")
def set_diagnosis_settings(session_id: UUID, payload: DiagnosisToggleRequest, request: Request) -> dict:
    if payload.enabled and not request.app.state.settings.diagnosis.enabled:
        raise HTTPException(status_code=409, detail="differential diagnosis is disabled by system configuration")
    now = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        row = connection.execute(
            "SELECT diagnosis_enabled FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        connection.execute(
            "UPDATE sessions SET diagnosis_enabled = ?, updated_at = ?, version = version + 1 WHERE id = ?",
            (int(payload.enabled), now, str(session_id)),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, action, actor, before_json, after_json, created_at) "
            "VALUES (?, 'diagnosis_settings', 'DIFFERENTIAL_TOGGLE_CHANGED', 'local-clinician', ?, ?, ?)",
            (str(session_id), json.dumps({"enabled": row["diagnosis_enabled"]}),
             json.dumps({"enabled": payload.enabled}), now),
        )
        if payload.enabled:
            ready = connection.execute(
                "SELECT 1 FROM treatment_plans WHERE session_id = ? LIMIT 1", (str(session_id),)
            ).fetchone()
            if ready:
                enqueue_differential_job(
                    connection, session_id,
                    max_attempts=int(request.app.state.settings.worker.get("max_attempts", 3)), now=now,
                )
        else:
            connection.execute(
                "UPDATE jobs SET status = 'CANCELLED', updated_at = ? "
                "WHERE session_id = ? AND job_type = 'DIFFERENTIAL' AND status = 'PENDING'",
                (now, str(session_id)),
            )
        updated = connection.execute(
            "SELECT diagnosis_enabled FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
    return _toggle_state(request, updated)


@router.get("/{session_id}/diagnosis-review")
def get_diagnosis_review(session_id: UUID, request: Request) -> dict:
    with request.app.state.database.connect() as connection:
        session = connection.execute(
            "SELECT diagnosis_enabled FROM sessions WHERE id = ?", (str(session_id),)
        ).fetchone()
        row = connection.execute(
            "SELECT * FROM differentials WHERE session_id = ? ORDER BY version DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    toggle = _toggle_state(request, session)
    if not toggle["effective_enabled"]:
        return {
            "session_id": str(session_id), **toggle, "disclaimer": None,
            "differential": None, "rendered": DIFFERENTIAL_DISABLED_MESSAGE,
        }
    if row is None:
        return {
            "session_id": str(session_id), **toggle, "disclaimer": DIFFERENTIAL_DISCLAIMER,
            "differential": None, "rendered": "Differential diagnosis: [Pending generation]",
        }
    result = DifferentialResult.model_validate_json(row["result_json"])
    return {
        "session_id": str(session_id), **toggle, "disclaimer": result.disclaimer,
        "differential": result.model_dump(mode="json"), "rendered": result.render_text(),
    }

