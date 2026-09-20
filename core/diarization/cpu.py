from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path
from typing import Sequence
from uuid import UUID

from core.diarization.base import DiarizationEngine, SpeakerTurn
from core.models import TranscriptSegment


class CpuAcousticDiarizationEngine(DiarizationEngine):
    """Small deterministic CPU baseline using per-segment acoustic features.

    It intentionally does not infer Doctor/Patient roles. Those are clinician review
    decisions. Deployments may replace this engine with pyannote without changing the
    worker or persistence contracts.
    """

    def __init__(self, *, min_speakers: int = 1, max_speakers: int = 2) -> None:
        if min_speakers < 1 or max_speakers < min_speakers:
            raise ValueError("invalid speaker bounds")
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    @property
    def name(self) -> str:
        return "cpu-acoustic-v1"

    def diarize(
        self,
        audio_path: Path,
        segments: Sequence[TranscriptSegment],
        *,
        session_id: UUID | str,
    ) -> list[SpeakerTurn]:
        session_uuid = UUID(str(session_id))
        if any(segment.session_id != session_uuid for segment in segments):
            raise ValueError("segments belong to a different session")
        if not segments:
            return []
        samples, sample_rate = self._read_pcm16_mono(audio_path)
        features = [self._features(samples, sample_rate, item.start_ms, item.end_ms) for item in segments]
        labels, confidence = self._cluster(features)
        return [
            SpeakerTurn(
                segment_id=segment.id,
                diarization_label=f"SPEAKER_{label:02d}",
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                confidence=confidence[index],
            )
            for index, (segment, label) in enumerate(zip(segments, labels, strict=True))
        ]

    @staticmethod
    def _read_pcm16_mono(path: Path) -> tuple[array, int]:
        with wave.open(str(path), "rb") as source:
            if source.getsampwidth() != 2:
                raise ValueError("CPU diarization requires 16-bit PCM WAV")
            channels = source.getnchannels()
            rate = source.getframerate()
            raw = array("h", source.readframes(source.getnframes()))
        if channels > 1:
            raw = array("h", (sum(raw[i : i + channels]) // channels for i in range(0, len(raw), channels)))
        return raw, rate

    @staticmethod
    def _features(samples: array, rate: int, start_ms: int, end_ms: int) -> tuple[float, float, float]:
        start = min(len(samples), max(0, start_ms * rate // 1000))
        end = min(len(samples), max(start + 1, end_ms * rate // 1000))
        clip = samples[start:end]
        if not clip:
            return (0.0, 0.0, 0.0)
        scale = 32768.0
        rms = math.sqrt(sum((value / scale) ** 2 for value in clip) / len(clip))
        crossings = sum(1 for left, right in zip(clip, clip[1:]) if (left < 0) != (right < 0))
        zcr = crossings / max(1, len(clip) - 1)
        window = max(1, rate // 50)
        energies = [sum(abs(value) for value in clip[i : i + window]) / (scale * len(clip[i : i + window])) for i in range(0, len(clip), window)]
        variance = sum((value - (sum(energies) / len(energies))) ** 2 for value in energies) / len(energies)
        return (rms, zcr, math.sqrt(variance))

    def _cluster(self, features: list[tuple[float, float, float]]) -> tuple[list[int], list[float]]:
        if self.max_speakers == 1 or len(features) < 2:
            return [0] * len(features), [0.5] * len(features)
        normalized = self._normalize(features)
        first = min(normalized)
        second = max(normalized, key=lambda value: self._distance(value, first))
        if self._distance(first, second) < 0.75 and self.min_speakers == 1:
            return [0] * len(features), [0.5] * len(features)
        centers = [first, second]
        labels = [0] * len(normalized)
        for _ in range(12):
            new_labels = [min(range(2), key=lambda idx: self._distance(item, centers[idx])) for item in normalized]
            if new_labels == labels and _ > 0:
                break
            labels = new_labels
            for cluster in range(2):
                members = [item for item, label in zip(normalized, labels) if label == cluster]
                if members:
                    centers[cluster] = tuple(sum(item[i] for item in members) / len(members) for i in range(3))
        # Stabilize labels by first appearance, not arbitrary centroid order.
        order: dict[int, int] = {}
        stable = [order.setdefault(label, len(order)) for label in labels]
        confidence = []
        for item, label in zip(normalized, labels):
            own = self._distance(item, centers[label])
            other = self._distance(item, centers[1 - label])
            confidence.append(max(0.5, min(0.99, other / max(1e-9, own + other))))
        return stable, confidence

    @staticmethod
    def _normalize(features: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
        result: list[tuple[float, float, float]] = []
        means = [sum(item[i] for item in features) / len(features) for i in range(3)]
        scales = [math.sqrt(sum((item[i] - means[i]) ** 2 for item in features) / len(features)) or 1.0 for i in range(3)]
        for item in features:
            result.append(tuple((item[i] - means[i]) / scales[i] for i in range(3)))
        return result

    @staticmethod
    def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))
