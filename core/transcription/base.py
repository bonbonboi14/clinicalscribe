from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from core.models import TranscriptSegment


class TranscriptionProfile(StrEnum):
    FAST = "FAST"
    BALANCED = "BALANCED"
    ACCURATE = "ACCURATE"


class TranscriptionEngine(ABC):
    """Swappable transcription contract executed only by a worker process."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable engine identifier stored with the immutable transcript."""

    @property
    @abstractmethod
    def profile(self) -> TranscriptionProfile:
        """Runtime accuracy/performance profile."""

    @abstractmethod
    def transcribe(
        self, audio_path: Path, *, session_id: UUID | str
    ) -> list[TranscriptSegment]:
        """Return timestamped, source-language-preserving transcript segments."""
