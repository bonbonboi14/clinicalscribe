from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExaminationFindingStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING_REVIEW = "PENDING_REVIEW"
    UNRECOGNISED = "UNRECOGNISED"


class ExaminationFinding(BaseModel):
    """A traceable interpretation of one spoken examination phrase.

    ``raw_text`` is immutable source evidence. ``interpreted_text`` is either a
    configured clinical term or the unchanged source phrase for pass-through.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    raw_text: str = Field(min_length=1)
    interpreted_text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: ExaminationFindingStatus
    transcript_segment_ids: list[UUID] = Field(default_factory=list)
    mapping_key: str | None = None

    @model_validator(mode="after")
    def enforce_pass_through(self) -> "ExaminationFinding":
        if self.status == ExaminationFindingStatus.UNRECOGNISED:
            if self.interpreted_text != self.raw_text:
                raise ValueError("unrecognised findings must pass raw_text through unchanged")
            if self.mapping_key is not None:
                raise ValueError("unrecognised findings cannot identify a mapping")
        return self
