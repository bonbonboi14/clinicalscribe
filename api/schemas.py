from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.models import SessionStatus


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
