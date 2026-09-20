from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Sequence

from core.languages.translation import TranslationEngine
from core.models import SegmentLanguageMetadata, TranscriptSegment, TranslationStatus


class LanguageProcessor:
    def __init__(self, translator: TranslationEngine | None = None) -> None:
        self.translator = translator

    @staticmethod
    def clean(text: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()

    def process(self, segments: Sequence[TranscriptSegment]) -> list[SegmentLanguageMetadata]:
        grouped: dict[str, list[str]] = defaultdict(list)
        positions: dict[str, list[int]] = defaultdict(list)
        clean = [self.clean(segment.original_text) for segment in segments]
        if self.translator is not None:
            for index, segment in enumerate(segments):
                if not self._is_english(segment.source_language):
                    grouped[segment.source_language].append(clean[index])
                    positions[segment.source_language].append(index)
        output: list[str | None] = [None] * len(segments)
        if self.translator is not None:
            for language, texts in grouped.items():
                values = self.translator.translate(texts, source_language=language, target_language="en")
                for index, value in zip(positions[language], values, strict=True):
                    output[index] = self.clean(value)
        metadata: list[SegmentLanguageMetadata] = []
        for index, segment in enumerate(segments):
            english = self._is_english(segment.source_language)
            status = (
                TranslationStatus.SOURCE_ENGLISH
                if english
                else TranslationStatus.TRANSLATED
                if output[index] is not None
                else TranslationStatus.NOT_REQUESTED
            )
            metadata.append(
                SegmentLanguageMetadata(
                    segment_id=segment.id,
                    source_language=segment.source_language,
                    original_text=segment.original_text,
                    clean_text=clean[index],
                    translated_text=clean[index] if english else output[index],
                    translation_status=status,
                    confidence=segment.confidence,
                )
            )
        return metadata

    @staticmethod
    def _is_english(language: str) -> bool:
        return language.lower().split("-")[0] == "en"
