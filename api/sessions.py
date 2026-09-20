from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from api.schemas import (
    ChunkUploadResponse,
    SessionCreate,
    SessionResponse,
    UploadStatusResponse,
)
from core.models import SessionStatus
from storage.database import Database
from storage.files import AudioFileStore, sha256_bytes


router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_assembly_locks: dict[str, threading.Lock] = {}
_assembly_locks_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database(request: Request) -> Database:
    return request.app.state.database


def _file_store(request: Request) -> AudioFileStore:
    return request.app.state.audio_file_store


def _assembly_lock(session_id: UUID) -> threading.Lock:
    with _assembly_locks_guard:
        return _assembly_locks.setdefault(str(session_id), threading.Lock())


def _session_or_404(connection: sqlite3.Connection, session_id: UUID) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM sessions WHERE id = ?", (str(session_id),)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def _upload_status(database: Database, session_id: UUID) -> UploadStatusResponse:
    with database.connect() as connection:
        session = _session_or_404(connection, session_id)
        received = [
            row["sequence_number"]
            for row in connection.execute(
                "SELECT sequence_number FROM audio_chunks "
                "WHERE session_id = ? ORDER BY sequence_number",
                (str(session_id),),
            )
        ]
    final_sequence = session["final_sequence_number"]
    received_set = set(received)
    missing = (
        [number for number in range(final_sequence + 1) if number not in received_set]
        if final_sequence is not None
        else []
    )
    return UploadStatusResponse(
        session_id=session_id,
        status=SessionStatus(session["status"]),
        received_chunks=received,
        final_sequence_number=final_sequence,
        missing_chunks=missing,
        assembled=session["assembled_audio_path"] is not None,
        assembled_audio_path=session["assembled_audio_path"],
        assembled_checksum_sha256=session["assembled_checksum_sha256"],
        assembled_size_bytes=session["assembled_size_bytes"],
    )


def _try_assemble(request: Request, session_id: UUID) -> None:
    database = _database(request)
    with _assembly_lock(session_id):
        with database.connect() as connection:
            session = _session_or_404(connection, session_id)
            if session["assembled_audio_path"] is not None:
                return
            final_sequence = session["final_sequence_number"]
            if final_sequence is None:
                return
            chunks = connection.execute(
                "SELECT sequence_number, storage_path FROM audio_chunks "
                "WHERE session_id = ? AND sequence_number <= ? ORDER BY sequence_number",
                (str(session_id), final_sequence),
            ).fetchall()
        if [row["sequence_number"] for row in chunks] != list(range(final_sequence + 1)):
            return

        assembled_path, checksum, size = _file_store(request).assemble(
            session_id,
            [row["storage_path"] for row in chunks],
            filename=session["original_filename"],
            content_type=session["audio_content_type"],
        )
        now = _now()
        with database.transaction() as connection:
            current = _session_or_404(connection, session_id)
            if current["assembled_audio_path"] is None:
                connection.execute(
                    "UPDATE sessions SET assembled_audio_path = ?, "
                    "assembled_checksum_sha256 = ?, assembled_size_bytes = ?, "
                    "assembled_at = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (str(assembled_path), checksum, size, now, now, str(session_id)),
                )
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, "
                    "actor, after_json, created_at) VALUES (?, 'audio_recording', ?, "
                    "'ASSEMBLED', 'system', ?, ?)",
                    (
                        str(session_id),
                        str(session_id),
                        json.dumps({"checksum_sha256": checksum, "size_bytes": size}),
                        now,
                    ),
                )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    request: Request,
    payload: SessionCreate = Body(default_factory=SessionCreate),
) -> SessionResponse:
    database = _database(request)
    session_id = uuid4()
    patient_id = uuid4() if payload.patient is not None else None
    now = _now()
    with database.transaction() as connection:
        if payload.patient is not None:
            patient_data = payload.patient.model_dump(mode="json", exclude_none=True)
            external_id = patient_data.pop("external_id", None)
            connection.execute(
                "INSERT INTO patients(id, external_id, demographics_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(patient_id), external_id, json.dumps(patient_data), now, now),
            )
        connection.execute(
            "INSERT INTO sessions(id, patient_id, status, source_language, created_at, updated_at, "
            "original_filename, audio_content_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(session_id),
                str(patient_id) if patient_id else None,
                SessionStatus.CREATED.value,
                payload.source_language,
                now,
                now,
                payload.original_filename,
                payload.audio_content_type,
            ),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, "
            "after_json, created_at) VALUES (?, 'session', ?, 'CREATED', 'client', ?, ?)",
            (str(session_id), str(session_id), payload.model_dump_json(), now),
        )
    return SessionResponse(
        id=session_id,
        patient_id=patient_id,
        status=SessionStatus.CREATED,
        source_language=payload.source_language,
        created_at=datetime.fromisoformat(now),
        updated_at=datetime.fromisoformat(now),
    )


@router.get("/{session_id}/upload", response_model=UploadStatusResponse)
def get_upload_status(session_id: UUID, request: Request) -> UploadStatusResponse:
    return _upload_status(_database(request), session_id)


@router.post("/{session_id}/chunks", response_model=ChunkUploadResponse)
async def upload_chunk(
    session_id: UUID,
    request: Request,
    sequence_number: Annotated[int, Query(ge=0)],
    x_chunk_sha256: Annotated[str, Header(alias="X-Chunk-SHA256")],
    is_final: Annotated[bool, Query()] = False,
    x_file_name: Annotated[str | None, Header(alias="X-File-Name")] = None,
) -> ChunkUploadResponse:
    if not _SHA256_PATTERN.fullmatch(x_chunk_sha256):
        raise HTTPException(status_code=400, detail="X-Chunk-SHA256 must be 64 hexadecimal characters")
    checksum = x_chunk_sha256.lower()
    max_size = int(request.app.state.settings.upload.get("chunk_size_bytes", 5 * 1024 * 1024))
    blocks: list[bytes] = []
    received_size = 0
    async for block in request.stream():
        received_size += len(block)
        if received_size > max_size:
            raise HTTPException(status_code=413, detail=f"chunk exceeds configured {max_size}-byte limit")
        blocks.append(block)
    data = b"".join(blocks)
    if sha256_bytes(data) != checksum:
        raise HTTPException(status_code=422, detail="chunk checksum does not match X-Chunk-SHA256")

    database = _database(request)
    already_present = False
    with database.connect() as connection:
        session = _session_or_404(connection, session_id)
        final_sequence = session["final_sequence_number"]
        if final_sequence is not None and sequence_number > final_sequence:
            raise HTTPException(status_code=409, detail="chunk sequence is beyond the final chunk")
        if is_final and final_sequence is not None and final_sequence != sequence_number:
            raise HTTPException(status_code=409, detail="a different final chunk is already registered")
        existing = connection.execute(
            "SELECT * FROM audio_chunks WHERE session_id = ? AND sequence_number = ?",
            (str(session_id), sequence_number),
        ).fetchone()
    if existing is not None:
        if existing["checksum_sha256"] != checksum or existing["size_bytes"] != len(data):
            raise HTTPException(status_code=409, detail="chunk sequence already has different immutable audio")
        already_present = True
    else:
        try:
            storage_path = _file_store(request).store_chunk(
                session_id, sequence_number, data, checksum
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        now = _now()
        try:
            with database.transaction() as connection:
                _session_or_404(connection, session_id)
                connection.execute(
                    "INSERT INTO audio_chunks(id, session_id, sequence_number, storage_path, "
                    "checksum_sha256, size_bytes, uploaded_at, is_final) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()), str(session_id), sequence_number, str(storage_path),
                        checksum, len(data), now, int(is_final),
                    ),
                )
                connection.execute(
                    "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, "
                    "after_json, created_at) VALUES (?, 'audio_chunk', NULL, 'UPLOADED', "
                    "'client', ?, ?)",
                    (
                        str(session_id),
                        json.dumps({"sequence_number": sequence_number, "checksum_sha256": checksum}),
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            with database.connect() as connection:
                raced = connection.execute(
                    "SELECT checksum_sha256, size_bytes FROM audio_chunks "
                    "WHERE session_id = ? AND sequence_number = ?",
                    (str(session_id), sequence_number),
                ).fetchone()
            if raced is None or raced["checksum_sha256"] != checksum or raced["size_bytes"] != len(data):
                raise HTTPException(status_code=409, detail="concurrent chunk upload conflict")
            already_present = True

    now = _now()
    with database.transaction() as connection:
        current = _session_or_404(connection, session_id)
        final_sequence = current["final_sequence_number"]
        if is_final and final_sequence is not None and final_sequence != sequence_number:
            raise HTTPException(status_code=409, detail="a different final chunk is already registered")
        connection.execute(
            "UPDATE sessions SET status = ?, final_sequence_number = COALESCE(final_sequence_number, ?), "
            "original_filename = COALESCE(original_filename, ?), "
            "audio_content_type = COALESCE(audio_content_type, ?), updated_at = ?, version = version + 1 "
            "WHERE id = ?",
            (
                SessionStatus.UPLOADING.value,
                sequence_number if is_final else None,
                x_file_name,
                request.headers.get("content-type"),
                now,
                str(session_id),
            ),
        )

    await run_in_threadpool(_try_assemble, request, session_id)
    upload_status = _upload_status(database, session_id)
    return ChunkUploadResponse(
        **upload_status.model_dump(),
        sequence_number=sequence_number,
        checksum_sha256=checksum,
        size_bytes=len(data),
        already_present=already_present,
    )
