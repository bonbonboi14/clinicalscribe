from core.transcription.base import TranscriptionEngine

__all__ = ["TranscriptionEngine"]
"""Swappable local transcription engines."""

from core.transcription.base import TranscriptionEngine, TranscriptionProfile
from core.transcription.faster_whisper import FasterWhisperEngine

__all__ = ["FasterWhisperEngine", "TranscriptionEngine", "TranscriptionProfile"]
