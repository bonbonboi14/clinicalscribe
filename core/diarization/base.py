from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.models import TranscriptSegment


class SpeakerTurn(BaseModel):
    """Automatic diarization output. Labels are machine identities, not clinical roles."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    segment_id: UUID
    diarization_label: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_timing(self) -> "SpeakerTurn":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class DiarizationEngine(ABC):
    """Worker-only, swappable speaker diarization boundary."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def diarize(
        self,
        audio_path: Path,
        segments: Sequence[TranscriptSegment],
        *,
        session_id: UUID | str,
    ) -> list[SpeakerTurn]:
        raise NotImplementedError
