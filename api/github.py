from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from core.export import ApprovalRequiredError, ExportService
from core.github import GitHubPushError, GitHubPusher, GitHubTokenMissingError


router = APIRouter(prefix="/api/v1/sessions", tags=["GitHub export"])


class GitHubPushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=200)


def _record_blocked(request: Request, session_id: UUID, actor: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        if connection.execute("SELECT 1 FROM sessions WHERE id = ?", (str(session_id),)).fetchone():
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, action, actor, after_json, created_at) "
                "VALUES (?, 'github_push', 'GITHUB_PUSH_BLOCKED', ?, ?, ?)",
                (str(session_id), actor, json.dumps({"reason": reason}), now),
            )


@router.post("/{session_id}/github/push")
def push_session_to_github(
    session_id: UUID, payload: GitHubPushRequest, request: Request
) -> dict:
    settings = request.app.state.settings.github
    if not settings.enabled:
        reason = "GitHub push is disabled in configuration"
        _record_blocked(request, session_id, payload.actor, reason)
        raise HTTPException(status_code=409, detail=reason)
    service = ExportService(request.app.state.database)
    try:
        snapshot = service.load_approved_snapshot(session_id)
    except ApprovalRequiredError as exc:
        _record_blocked(request, session_id, payload.actor, str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    export_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        connection.execute(
            "INSERT INTO exports(id, session_id, note_id, format, destination, status, requested_by, requested_at) "
            "VALUES (?, ?, ?, 'GITHUB', ?, 'PENDING', ?, ?)",
            (export_id, str(session_id), snapshot.note["id"], settings.repo_url, payload.actor, now),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
            "VALUES (?, 'github_push', ?, 'GITHUB_PUSH_REQUESTED', ?, ?, ?)",
            (
                str(session_id), export_id, payload.actor,
                json.dumps({"note_id": snapshot.note["id"], "branch": settings.branch}), now,
            ),
        )

    # Reload so audit_log.json includes the manual push request itself.
    snapshot = service.load_approved_snapshot(session_id)
    files = service.github_files(snapshot, push_transcripts=settings.push_transcripts)
    manifest_checksum = hashlib.sha256(
        b"".join(path.encode("utf-8") + b"\0" + files[path] for path in sorted(files))
    ).hexdigest()
    pusher = GitHubPusher(
        settings.repo_url,
        branch=settings.branch,
        remote_name=settings.remote_name,
        token_env=settings.token_env,
    )
    try:
        result = pusher.push_session(
            session_id,
            specialty=snapshot.specialty,
            session_date=snapshot.session_date,
            files=files,
        )
    except GitHubPushError as exc:
        failed_at = datetime.now(timezone.utc).isoformat()
        with request.app.state.database.transaction() as connection:
            connection.execute(
                "UPDATE exports SET status = 'FAILED', completed_at = ? WHERE id = ?",
                (failed_at, export_id),
            )
            connection.execute(
                "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
                "VALUES (?, 'github_push', ?, 'GITHUB_PUSH_FAILED', ?, ?, ?)",
                (
                    str(session_id), export_id, payload.actor,
                    json.dumps({"error": str(exc), "branch": settings.branch}), failed_at,
                ),
            )
        status_code = 409 if isinstance(exc, GitHubTokenMissingError) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    completed_at = datetime.now(timezone.utc).isoformat()
    with request.app.state.database.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status = 'COMPLETED', completed_at = ?, checksum_sha256 = ? WHERE id = ?",
            (completed_at, manifest_checksum, export_id),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
            "VALUES (?, 'github_push', ?, 'GITHUB_PUSH_SUCCEEDED', ?, ?, ?)",
            (
                str(session_id), export_id, payload.actor,
                json.dumps({
                    "commit": result.commit_hexsha,
                    "branch": result.branch,
                    "session_path": result.session_path,
                    "files": list(result.files),
                    "manifest_checksum_sha256": manifest_checksum,
                }), completed_at,
            ),
        )
    return {
        "status": "COMPLETED",
        "commit": result.commit_hexsha,
        "branch": result.branch,
        "session_path": result.session_path,
        "files": list(result.files),
    }
