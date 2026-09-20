from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request

from api.schemas import (
    SpeakerConversationReview,
    SpeakerCorrectionsRequest,
    SpeakerReview,
    TranscriptArtefactResponse,
    TranscriptSegmentReview,
)
from core.models import SpeakerRole, TranscriptSegment, TranslationStatus
from storage.artefacts import TranscriptArtefactStore
from storage.database import Database


router = APIRouter(prefix="/api/v1/sessions", tags=["speaker review"])


def _latest_context(connection: sqlite3.Connection, session_id: UUID) -> tuple[sqlite3.Row, sqlite3.Row]:
    transcript = connection.execute(
        "SELECT * FROM transcripts WHERE session_id = ? ORDER BY version DESC LIMIT 1",
        (str(session_id),),
    ).fetchone()
    if transcript is None:
        raise HTTPException(status_code=404, detail="transcript not found")
    run = connection.execute(
        "SELECT * FROM diarization_runs WHERE session_id = ? AND transcript_id = ? ORDER BY created_at DESC LIMIT 1",
        (str(session_id), transcript["id"]),
    ).fetchone()
    if run is None:
        raise HTTPException(status_code=409, detail="diarization is not complete")
    return transcript, run


def _latest_speakers(connection: sqlite3.Connection, session_id: UUID) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT s.id, s.diarization_label, r.display_name, r.role, r.is_manual, r.version "
        "FROM speakers s JOIN speaker_revisions r ON r.speaker_id = s.id "
        "WHERE s.session_id = ? AND r.version = (SELECT MAX(r2.version) FROM speaker_revisions r2 WHERE r2.speaker_id = s.id) "
        "ORDER BY s.diarization_label",
        (str(session_id),),
    ).fetchall()


def _latest_assignments(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT a.* FROM segment_speaker_assignments a WHERE a.diarization_run_id = ? "
        "AND a.version = (SELECT MAX(a2.version) FROM segment_speaker_assignments a2 WHERE a2.segment_id = a.segment_id) "
        "ORDER BY a.created_at, a.segment_id",
        (run_id,),
    ).fetchall()


def _language_metadata(connection: sqlite3.Connection, session_id: UUID) -> dict[str, dict]:
    row = connection.execute(
        "SELECT storage_path FROM transcript_artifacts WHERE session_id = ? AND kind = 'TRANSLATED_TRANSCRIPT' "
        "ORDER BY version DESC LIMIT 1",
        (str(session_id),),
    ).fetchone()
    if row is None:
        return {}
    payload = json.loads(Path(row["storage_path"]).read_text(encoding="utf-8"))
    return {str(item["segment_id"]): item for item in payload["segments"]}


def _review_from_connection(
    connection: sqlite3.Connection, session_id: UUID
) -> SpeakerConversationReview:
    transcript, run = _latest_context(connection, session_id)
    speaker_rows = _latest_speakers(connection, session_id)
    assignment_rows = _latest_assignments(connection, run["id"])
    metadata = _language_metadata(connection, session_id)
    artefact_rows = connection.execute(
        "SELECT kind, version, language, checksum_sha256 FROM transcript_artifacts WHERE session_id = ? ORDER BY kind, version",
        (str(session_id),),
    ).fetchall()
    speakers = {
        row["id"]: SpeakerReview(
            id=UUID(row["id"]), diarization_label=row["diarization_label"], display_name=row["display_name"],
            role=SpeakerRole(row["role"]), manually_corrected=bool(row["is_manual"]), revision=row["version"],
        )
        for row in speaker_rows
    }
    assignments = {row["segment_id"]: row for row in assignment_rows}
    segments = []
    for segment_data in json.loads(transcript["segments_json"]):
        segment = TranscriptSegment.model_validate(segment_data)
        assignment = assignments.get(str(segment.id))
        if assignment is None:
            raise HTTPException(status_code=500, detail="diarization assignment is incomplete")
        speaker = speakers[assignment["speaker_id"]]
        item = metadata.get(str(segment.id), {})
        segments.append(
            TranscriptSegmentReview(
                segment_id=segment.id, sequence_number=segment.sequence_number,
                start_ms=segment.start_ms, end_ms=segment.end_ms, source_language=segment.source_language,
                original_text=segment.original_text, clean_text=item.get("clean_text", segment.original_text.strip()),
                english_text=item.get("translated_text"),
                translation_status=TranslationStatus(item.get("translation_status", "NOT_REQUESTED")),
                speaker_id=speaker.id, diarization_label=speaker.diarization_label,
                speaker_display_name=speaker.display_name, speaker_role=speaker.role,
                diarization_confidence=assignment["confidence"], assignment_revision=assignment["version"],
            )
        )
    return SpeakerConversationReview(
        session_id=session_id, transcript_id=UUID(transcript["id"]), diarization_run_id=UUID(run["id"]),
        diarization_engine=run["engine"], speakers=list(speakers.values()), segments=segments,
        artefacts=[TranscriptArtefactResponse(**dict(row)) for row in artefact_rows],
    )


def _review(database: Database, session_id: UUID) -> SpeakerConversationReview:
    with database.connect() as connection:
        return _review_from_connection(connection, session_id)


@router.get("/{session_id}/speaker-review", response_model=SpeakerConversationReview)
def get_speaker_review(session_id: UUID, request: Request) -> SpeakerConversationReview:
    return _review(request.app.state.database, session_id)


@router.post("/{session_id}/speaker-corrections", response_model=SpeakerConversationReview)
def apply_speaker_corrections(
    session_id: UUID, payload: SpeakerCorrectionsRequest, request: Request
) -> SpeakerConversationReview:
    database: Database = request.app.state.database
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        transcript, run = _latest_context(connection, session_id)
        valid_speakers = {row["id"] for row in _latest_speakers(connection, session_id)}
        assignments = {row["segment_id"]: row for row in _latest_assignments(connection, run["id"])}
        requested: dict[str, str] = {}
        for correction in payload.segment_assignments:
            if str(correction.segment_id) not in assignments:
                raise HTTPException(status_code=422, detail="segment does not belong to this diarization run")
            if str(correction.speaker_id) not in valid_speakers:
                raise HTTPException(status_code=422, detail="speaker does not belong to this session")
            requested[str(correction.segment_id)] = str(correction.speaker_id)
        for correction in payload.speakers:
            speaker_id = str(correction.speaker_id)
            if speaker_id not in valid_speakers:
                raise HTTPException(status_code=422, detail="speaker does not belong to this session")
            if correction.merge_into_speaker_id is not None:
                target = str(correction.merge_into_speaker_id)
                if target not in valid_speakers or target == speaker_id:
                    raise HTTPException(status_code=422, detail="invalid merge target")
                for segment_id, assignment in assignments.items():
                    if assignment["speaker_id"] == speaker_id:
                        requested[segment_id] = target
            latest = connection.execute(
                "SELECT * FROM speaker_revisions WHERE speaker_id = ? ORDER BY version DESC LIMIT 1",
                (speaker_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO speaker_revisions(id, speaker_id, session_id, version, display_name, role, is_manual, actor, created_at, supersedes_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (str(uuid4()), speaker_id, str(session_id), latest["version"] + 1, correction.display_name,
                 correction.role.value, payload.actor, now, latest["id"]),
            )
        for segment_id, speaker_id in requested.items():
            previous = assignments[segment_id]
            if previous["speaker_id"] == speaker_id:
                continue
            connection.execute(
                "INSERT INTO segment_speaker_assignments(id, diarization_run_id, session_id, segment_id, speaker_id, version, confidence, is_manual, actor, created_at, supersedes_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (str(uuid4()), run["id"], str(session_id), segment_id, speaker_id, previous["version"] + 1,
                 previous["confidence"], payload.actor, now, previous["id"]),
            )
        review = _review_from_connection(connection, session_id)
        version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM transcript_artifacts WHERE session_id = ? AND kind = 'SPEAKER_LABELLED_TRANSCRIPT'",
            (str(session_id),),
        ).fetchone()[0]
        store: TranscriptArtefactStore = request.app.state.transcript_artefact_store
        stored = store.store(
            session_id,
            "speaker_labelled_transcript",
            version,
            {
                "session_id": str(session_id), "transcript_id": str(review.transcript_id),
                "diarization_run_id": str(review.diarization_run_id), "kind": "SPEAKER_LABELLED_TRANSCRIPT",
                "segments": [item.model_dump(mode="json") for item in review.segments],
            },
        )
        connection.execute(
            "INSERT INTO transcript_artifacts(id, session_id, transcript_id, diarization_run_id, kind, version, storage_path, checksum_sha256, created_at) "
            "VALUES (?, ?, ?, ?, 'SPEAKER_LABELLED_TRANSCRIPT', ?, ?, ?, ?)",
            (str(uuid4()), str(session_id), str(review.transcript_id), str(review.diarization_run_id), version,
             str(stored.path), stored.checksum_sha256, now),
        )
        connection.execute(
            "INSERT INTO audit_events(session_id, artefact_type, artefact_id, action, actor, after_json, created_at) "
            "VALUES (?, 'speaker_review', ?, 'SPEAKER_CORRECTIONS_APPLIED', ?, ?, ?)",
            (str(session_id), run["id"], payload.actor, payload.model_dump_json(), now),
        )
    return _review(database, session_id)
