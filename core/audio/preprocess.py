from __future__ import annotations

import math
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from storage.files import sha256_file


@dataclass(frozen=True)
class PreprocessedAudio:
    path: Path
    source_checksum_sha256: str
    output_checksum_sha256: str
    sample_rate_hz: int
    original_duration_ms: int
    processed_duration_ms: int
    gain_db: float
    silence_trimmed: bool


class AudioPreprocessor:
    """Decode to mono PCM, trim edge silence, and peak-normalise safely."""

    def __init__(
        self,
        *,
        sample_rate_hz: int = 16_000,
        silence_threshold_dbfs: float = -40.0,
        silence_padding_ms: int = 150,
        target_peak_dbfs: float = -1.0,
        decoder: Callable[..., Any] | None = None,
    ) -> None:
        self.sample_rate_hz = sample_rate_hz
        self.silence_threshold_dbfs = silence_threshold_dbfs
        self.silence_padding_ms = silence_padding_ms
        self.target_peak_dbfs = target_peak_dbfs
        self._decoder = decoder

    def _decode(self, path: Path) -> Any:
        decoder = self._decoder
        if decoder is None:
            try:
                from faster_whisper.audio import decode_audio
            except ImportError as exc:
                raise RuntimeError(
                    "Audio preprocessing requires the 'transcription' extra"
                ) from exc
            decoder = decode_audio
        return decoder(str(path), sampling_rate=self.sample_rate_hz)

    def preprocess(self, source: Path, output_directory: Path) -> PreprocessedAudio:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Audio preprocessing requires NumPy") from exc

        source = source.resolve(strict=True)
        source_checksum = sha256_file(source)
        output_directory.mkdir(parents=True, exist_ok=True)
        destination = output_directory / f"preprocessed-{source_checksum[:16]}.wav"

        audio = np.asarray(self._decode(source), dtype=np.float32).reshape(-1)
        original_samples = int(audio.size)
        if original_samples == 0:
            raise ValueError("decoded audio contains no samples")
        if not np.isfinite(audio).all():
            raise ValueError("decoded audio contains non-finite samples")

        threshold = 10.0 ** (self.silence_threshold_dbfs / 20.0)
        active = np.flatnonzero(np.abs(audio) >= threshold)
        silence_trimmed = False
        if active.size:
            padding = round(self.silence_padding_ms * self.sample_rate_hz / 1000)
            start = max(0, int(active[0]) - padding)
            end = min(original_samples, int(active[-1]) + padding + 1)
            silence_trimmed = start > 0 or end < original_samples
            audio = audio[start:end]

        peak = float(np.max(np.abs(audio)))
        target_peak = 10.0 ** (self.target_peak_dbfs / 20.0)
        gain = 1.0 if peak <= 0.0 else min(target_peak / peak, 32.0)
        gain_db = 0.0 if gain == 1.0 else 20.0 * math.log10(gain)
        audio = np.clip(audio * gain, -1.0, 1.0)

        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            try:
                pcm = (audio * 32767.0).astype("<i2").tobytes()
                with wave.open(str(temporary), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self.sample_rate_hz)
                    output.writeframes(pcm)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)

        return PreprocessedAudio(
            path=destination,
            source_checksum_sha256=source_checksum,
            output_checksum_sha256=sha256_file(destination),
            sample_rate_hz=self.sample_rate_hz,
            original_duration_ms=round(original_samples * 1000 / self.sample_rate_hz),
            processed_duration_ms=round(audio.size * 1000 / self.sample_rate_hz),
            gain_db=gain_db,
            silence_trimmed=silence_trimmed,
        )
