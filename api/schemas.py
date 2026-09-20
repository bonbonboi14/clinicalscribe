from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.models import SessionStatus, SpeakerRole, TranslationStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatientMetadata(ApiModel):
    external_id: str | None = None
    full_name: str | None = None
    date_of_birth: date | None = None
    sex: str | None = None
    gender: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    identifiers: dict[str, str] | None = None
    additional: dict[str, Any] | None = None


class SessionCreate(ApiModel):
    patient: PatientMetadata | None = None
    source_language: str | None = None
    original_filename: str | None = None
    audio_content_type: str | None = None


class SessionResponse(ApiModel):
    id: UUID
    patient_id: UUID | None
    status: SessionStatus
    source_language: str | None
    created_at: datetime
    updated_at: datetime


class UploadStatusResponse(ApiModel):
    session_id: UUID
    status: SessionStatus
    received_chunks: list[int]
    final_sequence_number: int | None
    missing_chunks: list[int]
    assembled: bool
    assembled_audio_path: str | None
    assembled_checksum_sha256: str | None
    assembled_size_bytes: int | None


class ChunkUploadResponse(UploadStatusResponse):
    sequence_number: int
    checksum_sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int
    already_present: bool


class SpeakerReview(ApiModel):
    id: UUID
    diarization_label: str
    display_name: str | None
    role: SpeakerRole
    manually_corrected: bool
    revision: int


class TranscriptSegmentReview(ApiModel):
    segment_id: UUID
    sequence_number: int
    start_ms: int
    end_ms: int
    source_language: str
    original_text: str
    clean_text: str
    english_text: str | None
    translation_status: TranslationStatus
    speaker_id: UUID
    diarization_label: str
    speaker_display_name: str | None
    speaker_role: SpeakerRole
    diarization_confidence: float
    assignment_revision: int


class TranscriptArtefactResponse(ApiModel):
    kind: str
    version: int
    language: str | None
    checksum_sha256: str


class SpeakerConversationReview(ApiModel):
    session_id: UUID
    transcript_id: UUID
    diarization_run_id: UUID
    diarization_engine: str
    speakers: list[SpeakerReview]
    segments: list[TranscriptSegmentReview]
    artefacts: list[TranscriptArtefactResponse]


class SpeakerCorrection(ApiModel):
    speaker_id: UUID
    display_name: str | None = None
    role: SpeakerRole
    merge_into_speaker_id: UUID | None = None


class SegmentSpeakerCorrection(ApiModel):
    segment_id: UUID
    speaker_id: UUID


class SpeakerCorrectionsRequest(ApiModel):
    actor: str = Field(min_length=1, max_length=200)
    speakers: list[SpeakerCorrection] = Field(default_factory=list)
    segment_assignments: list[SegmentSpeakerCorrection] = Field(default_factory=list)
