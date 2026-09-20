from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from models.clerking_sheet import ClerkingSheet


router = APIRouter(prefix="/api/v1/sessions", tags=["clerking review"])


@router.get("/{session_id}/clerking-sheet", response_model=ClerkingSheet)
def get_clerking_sheet(session_id: UUID, request: Request) -> ClerkingSheet:
    with request.app.state.database.connect() as connection:
        row = connection.execute(
            "SELECT structured_json FROM clerking_sheets WHERE session_id = ? ORDER BY version DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail="clerking sheet has not been generated")
    return ClerkingSheet.model_validate(json.loads(row["structured_json"]))
