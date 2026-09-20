from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from core.examination.mapper import PhraseToClinicalTermMapper
from models.examination_finding import ExaminationFinding, ExaminationFindingStatus


class ExaminationInterpreter:
    """Maps spoken findings while retaining verbatim source and provenance."""

    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n;]+")
    _EXAMINATION_CUE = re.compile(
        r"\b(on (?:physical )?exam(?:ination)?|exam(?:ination)? (?:shows?|reveals?)|"
        r"nose|nostril|throat|tonsil|ear|eardrum|neck gland|voice|pulse|heart|chest|"
        r"abdomen|skin|pupil|leg|oedema|edema|temperature|blood pressure)\b",
        re.IGNORECASE,
    )
    _PREFIX = re.compile(
        r"^(?:and\s+)?(?:on (?:physical )?exam(?:ination)?|exam(?:ination)? (?:shows?|reveals?))\s*[:,\-]?\s*",
        re.IGNORECASE,
    )

    def __init__(
        self,
        mapper: PhraseToClinicalTermMapper | None = None,
        *,
        confidence_threshold: float = 0.8,
        pass_through_unmappable: bool = True,
    ) -> None:
        if not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between 0 and 1")
        self.mapper = mapper or PhraseToClinicalTermMapper.from_directory()
        self.confidence_threshold = confidence_threshold
        self.pass_through_unmappable = pass_through_unmappable

    def interpret_phrase(
        self,
        raw_text: str,
        *,
        session_id: UUID | str,
        transcript_segment_ids: Iterable[UUID | str] = (),
    ) -> ExaminationFinding:
        source = raw_text.strip()
        if not source:
            raise ValueError("raw examination text cannot be empty")
        mapped = self.mapper.map_phrase(source)
        evidence = [UUID(str(item)) for item in transcript_segment_ids]
        if mapped is None:
            if not self.pass_through_unmappable:
                raise ValueError(f"unrecognised examination phrase: {source}")
            return ExaminationFinding(
                session_id=UUID(str(session_id)), raw_text=source, interpreted_text=source,
                confidence=0.0, status=ExaminationFindingStatus.UNRECOGNISED,
                transcript_segment_ids=evidence,
            )
        status = (
            ExaminationFindingStatus.CONFIRMED
            if mapped.confidence >= self.confidence_threshold
            else ExaminationFindingStatus.PENDING_REVIEW
        )
        return ExaminationFinding(
            session_id=UUID(str(session_id)), raw_text=source,
            interpreted_text=mapped.interpreted_text, confidence=mapped.confidence,
            status=status, transcript_segment_ids=evidence, mapping_key=mapped.mapping_key,
        )

    def interpret(self, segments: Iterable[dict[str, Any] | Any]) -> list[ExaminationFinding]:
        findings: list[ExaminationFinding] = []
        for segment in segments:
            getter = segment.get if isinstance(segment, dict) else lambda key, default=None: getattr(segment, key, default)
            source = str(
                getter("english_text") or getter("translated_text") or
                getter("clean_text") or getter("original_text") or ""
            )
            session_id = getter("session_id")
            segment_id = getter("segment_id") or getter("id")
            if not session_id or not segment_id:
                raise ValueError("each examination segment requires session_id and id")
            examination_context = False
            for sentence in self._sentences(source):
                examination_context = examination_context or bool(self._PREFIX.match(sentence))
                phrase = self._PREFIX.sub("", sentence).strip(" .,:-")
                if not phrase:
                    continue
                mapped = self.mapper.map_phrase(phrase)
                if mapped is None and not examination_context and not self._EXAMINATION_CUE.search(sentence):
                    continue
                findings.append(self.interpret_phrase(
                    phrase, session_id=session_id, transcript_segment_ids=[segment_id]
                ))
        return findings

    @classmethod
    def _sentences(cls, text: str) -> list[str]:
        return [part.strip() for part in cls._SENTENCE_SPLIT.split(text.strip()) if part.strip()]
