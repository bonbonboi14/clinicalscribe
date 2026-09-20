from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ImmutableModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MutableReviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    RECORDING = "RECORDING"
    UPLOADING = "UPLOADING"
    PROCESSING = "PROCESSING"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssertionState(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NOT_MENTIONED = "NOT_MENTIONED"
    UNCERTAIN = "UNCERTAIN"


class ClaimState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNCERTAIN = "UNCERTAIN"


class ReviewStatus(StrEnum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Session(MutableReviewModel):
    id: UUID = Field(default_factory=uuid4)
    patient_id: UUID | None = None
    status: SessionStatus = SessionStatus.CREATED
    source_language: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class AudioChunk(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    sequence_number: int = Field(ge=0)
    storage_path: str
    checksum_sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)
    uploaded_at: datetime = Field(default_factory=utc_now)


class Job(MutableReviewModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    job_type: str
    status: JobStatus = JobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    lease_expires_at: datetime | None = None
    error: str | None = None


class TranscriptSegment(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    speaker_id: UUID | None = None
    sequence_number: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    source_language: str
    original_text: str
    normalized_text: str | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_timing(self) -> "TranscriptSegment":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class Speaker(MutableReviewModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    diarization_label: str
    display_name: str | None = None
    role: str | None = None
    manually_corrected: bool = False


class ClinicalFact(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    category: str
    name: str
    value: str | None = None
    assertion: AssertionState
    confidence: float = Field(ge=0, le=1)
    transcript_segment_ids: list[UUID] = Field(default_factory=list)
    original_text: str | None = None


class ExaminationFinding(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    original_phrase: str
    formal_term: str | None = None
    confidence: float = Field(ge=0, le=1)
    requires_confirmation: bool = True
    confirmed_by_clinician: bool = False
    transcript_segment_ids: list[UUID] = Field(default_factory=list)
    flagged_unmappable: bool = False


class ClerkingSheet(MutableReviewModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    version: int = Field(default=1, ge=1)
    structured_sections: dict[str, Any]
    fact_ids: list[UUID] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)


class ClinicalNote(MutableReviewModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    clerking_sheet_id: UUID
    version: int = Field(default=1, ge=1)
    language: str = "en"
    content: str
    status: ReviewStatus = ReviewStatus.DRAFT
    approved_by: str | None = None
    approved_at: datetime | None = None


class TreatmentPlan(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    note_id: UUID | None = None
    items: list[str] = Field(default_factory=list)
    transcript_segment_ids: list[UUID] = Field(default_factory=list)
    disclaimer: str = "Contains only treatment explicitly documented in the transcript."


class Differential(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    enabled_at_generation: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    fact_ids: list[UUID] = Field(default_factory=list)
    disclaimer: str


class ValidationFinding(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    artefact_type: str
    artefact_id: UUID
    claim_text: str
    state: ClaimState
    evidence_segment_ids: list[UUID] = Field(default_factory=list)
    explanation: str | None = None


class ExportRequest(ImmutableModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    note_id: UUID
    format: str
    destination: str | None = None
    requested_by: str
    requested_at: datetime = Field(default_factory=utc_now)
    clinician_approved: bool

    @model_validator(mode="after")
    def require_approval(self) -> "ExportRequest":
        if not self.clinician_approved:
            raise ValueError("Export requires clinician approval")
        return self
