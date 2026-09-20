from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from core.models import TranscriptSegment
from core.transcription.base import TranscriptionEngine, TranscriptionProfile


@dataclass(frozen=True)
class FasterWhisperProfile:
    model_size: str
    beam_size: int
    best_of: int
    vad_filter: bool


PROFILES: dict[TranscriptionProfile, FasterWhisperProfile] = {
    TranscriptionProfile.FAST: FasterWhisperProfile("tiny", 1, 1, True),
    TranscriptionProfile.BALANCED: FasterWhisperProfile("small", 5, 5, True),
    TranscriptionProfile.ACCURATE: FasterWhisperProfile("medium", 5, 5, True),
}


class FasterWhisperEngine(TranscriptionEngine):
    """CPU-only Faster Whisper adapter with per-segment language detection."""

    def __init__(
        self,
        *,
        profile: TranscriptionProfile | str = TranscriptionProfile.BALANCED,
        model_size: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        model: Any | None = None,
    ) -> None:
        self._profile = TranscriptionProfile(str(profile).upper())
        selected = PROFILES[self._profile]
        self._settings = FasterWhisperProfile(
            model_size or selected.model_size,
            selected.beam_size,
            selected.best_of,
            selected.vad_filter,
        )
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._model = model

    @property
    def name(self) -> str:
        return f"faster-whisper:{self._settings.model_size}:{self._profile.value.lower()}"

    @property
    def profile(self) -> TranscriptionProfile:
        return self._profile

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Faster Whisper is not installed; install the 'transcription' extra"
                ) from exc
            kwargs: dict[str, Any] = {
                "device": self._device,
                "compute_type": self._compute_type,
            }
            if self._cpu_threads > 0:
                kwargs["cpu_threads"] = self._cpu_threads
            self._model = WhisperModel(self._settings.model_size, **kwargs)
        return self._model

    @staticmethod
    def _confidence(avg_logprob: float | None) -> float:
        if avg_logprob is None:
            return 0.0
        return max(0.0, min(1.0, math.exp(float(avg_logprob))))

    @staticmethod
    def _language_from_detection(result: Any, fallback: str) -> str:
        # Faster Whisper versions return either (language, probability, all_probs)
        # or a ranked list of (language, probability) pairs.
        if isinstance(result, tuple) and result and isinstance(result[0], str):
            return result[0]
        if isinstance(result, list) and result:
            first = result[0]
            if isinstance(first, (list, tuple)) and first and isinstance(first[0], str):
                return first[0]
        return fallback

    def _detect_segment_language(
        self, model: Any, audio: Any, start_seconds: float, end_seconds: float, fallback: str
    ) -> str:
        sample_rate = 16_000
        start = max(0, int(start_seconds * sample_rate))
        end = max(start + 1, int(end_seconds * sample_rate))
        clip = audio[start:end]
        if len(clip) == 0:
            return fallback
        try:
            return self._language_from_detection(model.detect_language(clip), fallback)
        except (AttributeError, RuntimeError, ValueError, TypeError):
            # Detection failure must not discard an otherwise valid transcript.
            return fallback

    def transcribe(
        self, audio_path: Path, *, session_id: UUID | str
    ) -> list[TranscriptSegment]:
        session_uuid = UUID(str(session_id))
        model = self._load_model()
        try:
            from faster_whisper.audio import decode_audio
        except ImportError as exc:
            raise RuntimeError("Faster Whisper audio decoder is unavailable") from exc

        audio = decode_audio(str(audio_path), sampling_rate=16_000)
        generated, info = model.transcribe(
            audio,
            beam_size=self._settings.beam_size,
            best_of=self._settings.best_of,
            vad_filter=self._settings.vad_filter,
            word_timestamps=False,
            condition_on_previous_text=True,
        )
        fallback_language = getattr(info, "language", None) or "und"
        output: list[TranscriptSegment] = []
        for sequence_number, segment in enumerate(generated):
            start_seconds = max(0.0, float(segment.start))
            end_seconds = max(start_seconds, float(segment.end))
            text = str(segment.text)
            language = self._detect_segment_language(
                model, audio, start_seconds, end_seconds, fallback_language
            )
            output.append(
                TranscriptSegment(
                    session_id=session_uuid,
                    sequence_number=sequence_number,
                    start_ms=round(start_seconds * 1000),
                    end_ms=round(end_seconds * 1000),
                    source_language=language,
                    original_text=text,
                    confidence=self._confidence(getattr(segment, "avg_logprob", None)),
                )
            )
        return output
