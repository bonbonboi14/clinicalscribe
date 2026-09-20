from __future__ import annotations

from pathlib import Path
from typing import Protocol

from core.models import TranscriptSegment


class TranscriptionEngine(Protocol):
    """Swappable transcription contract; implementations execute in the worker."""

    @property
    def name(self) -> str: ...

    def transcribe(self, audio_path: Path, *, session_id: str) -> list[TranscriptSegment]: ...

