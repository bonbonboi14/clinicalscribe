from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from core.models import AssertionState, ClinicalFact, TranscriptSegment


@dataclass(frozen=True)
class _Utterance:
    segment_id: UUID
    text: str


class ClinicalFactExtractor:
    """Conservative, local rule extractor that emits only text-grounded facts.

    The extractor intentionally prefers omission/UNCERTAIN over completing a
    clinical concept from medical knowledge. Generated facts always retain the
    source utterance and segment identifier.
    """

    _NEGATION = re.compile(r"\b(no|not|never|den(?:y|ies|ied)|without)\b", re.I)
    _UNCERTAIN = re.compile(r"\b(maybe|perhaps|possibly|unsure|uncertain|might|could be)\b", re.I)
    _PMH = re.compile(
        r"\b(diabetes(?: mellitus)?|hypertension|high blood pressure|asthma|copd|"
        r"heart disease|stroke|cancer|epilepsy|kidney disease|liver disease)\b",
        re.I,
    )
    _KNOWN_DRUG = re.compile(
        r"\b(paracetamol|acetaminophen|ibuprofen|aspirin|metformin|amlodipine|"
        r"losartan|atorvastatin|omeprazole|insulin|warfarin|amoxicillin)\b",
        re.I,
    )
    _SYMPTOM = re.compile(
        r"\b(chest pain|abdominal pain|headache|fever|cough|shortness of breath|"
        r"breathlessness|vomiting|nausea|diarrh(?:ea|oea)|dizziness|palpitations|"
        r"weakness|rash|pain)\b",
        re.I,
    )

    def extract(
        self,
        segments: Iterable[TranscriptSegment | dict[str, Any]] | str,
        *,
        session_id: UUID | str | None = None,
        segment_id: UUID | str | None = None,
    ) -> list[ClinicalFact]:
        if isinstance(segments, str):
            if session_id is None:
                raise ValueError("session_id is required when extracting a transcript string")
            segments = [{
                "id": str(segment_id or uuid4()), "session_id": str(session_id),
                "original_text": segments,
            }]
        facts: list[ClinicalFact] = []
        seen: set[tuple[str, str, str, str]] = set()
        for raw in segments:
            segment_id = UUID(str(raw.id if isinstance(raw, TranscriptSegment) else raw["segment_id"] if "segment_id" in raw else raw["id"]))
            source = raw.original_text if isinstance(raw, TranscriptSegment) else str(
                raw.get("english_text") or raw.get("translated_text") or raw.get("clean_text") or raw.get("original_text") or ""
            )
            for sentence in self._sentences(source):
                utterance = _Utterance(segment_id=segment_id, text=sentence)
                for fact in self._extract_utterance(utterance, self._session_id(raw)):
                    key = (fact.category, fact.name.casefold(), (fact.value or "").casefold(), fact.assertion.value)
                    if key not in seen:
                        seen.add(key)
                        facts.append(fact)
        return facts

    @staticmethod
    def _session_id(raw: TranscriptSegment | dict[str, Any]) -> UUID:
        return UUID(str(raw.session_id if isinstance(raw, TranscriptSegment) else raw["session_id"]))

    @staticmethod
    def _sentences(text: str) -> list[str]:
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", text.strip()) if part.strip()]

    def _assertion(self, text: str) -> AssertionState:
        if self._NEGATION.search(text):
            return AssertionState.NEGATIVE
        if self._UNCERTAIN.search(text):
            return AssertionState.UNCERTAIN
        return AssertionState.POSITIVE

    def _fact(
        self, utterance: _Utterance, session_id: UUID, category: str, name: str,
        *, value: str | None = None, assertion: AssertionState | None = None, confidence: float = 0.9,
    ) -> ClinicalFact:
        return ClinicalFact(
            session_id=session_id, category=category, name=name.strip(), value=value,
            assertion=assertion or self._assertion(utterance.text), confidence=confidence,
            transcript_segment_ids=[utterance.segment_id], original_text=utterance.text,
        )

    def _extract_utterance(self, utterance: _Utterance, session_id: UUID) -> list[ClinicalFact]:
        text = utterance.text
        lower = text.casefold()
        facts: list[ClinicalFact] = []

        for condition in self._PMH.findall(text):
            facts.append(self._fact(utterance, session_id, "PMH", condition, assertion=self._assertion(text)))

        if re.search(r"\b(tablet|pill|medication|medicine|drug|taking|take|on)\b", lower):
            drug = self._KNOWN_DRUG.search(text)
            unnamed = re.search(r"\b(?:an?\s+)?(?:unnamed\s+)?(?:tablet|pill|medication|medicine)\b", text, re.I)
            if drug or unnamed:
                drug_name = drug.group(1) if drug else "Unnamed tablet"
                dose = self._match(text, r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml)\b")
                frequency = self._match(text, r"\b(?:once|twice|three times|four times)\s+(?:a|per)\s+day\b|\b(?:daily|nightly|weekly|bd|tds|qds|od)\b")
                duration = self._match(text, r"\b(?:for|since)\s+(?:about\s+)?\d+\s+(?:day|week|month|year)s?\b")
                value = json.dumps({"drug": drug_name, "dose": dose, "frequency": frequency, "duration": duration})
                facts.append(self._fact(utterance, session_id, "DH", drug_name, value=value))

        if "allerg" in lower:
            reaction = self._match(text, r"\b(?:causes?|caused|reaction(?: is| was)?|with)\s+([^.,;]+)")
            subject = self._match(text, r"allerg(?:ic|y)\s+(?:to\s+)?([^.,;]+)") or "Allergy discussed"
            value = json.dumps({"drug": subject, "food": None, "environmental": None, "reaction": reaction})
            facts.append(self._fact(utterance, session_id, "ALLERGY", subject, value=value))

        labelled_hpc = {
            "HPC_ONSET": r"\bonset(?: was| is)?\s*[:\-]?\s*([^.,;]+)|\b(?:started|began)\s+([^.,;]+)",
            "HPC_DURATION": r"\bduration(?: was| is)?\s*[:\-]?\s*([^.,;]+)|\b(for\s+(?:about\s+)?\d+\s+(?:hour|day|week|month|year)s?)\b",
            "HPC_CHARACTER": r"\bcharacter(?: was| is)?\s*[:\-]?\s*([^.,;]+)|\b(sharp|dull|burning|cramping|stabbing|throbbing)\b",
            "HPC_RADIATION": r"\bradiat(?:es|ing|ion)(?: to| was| is)?\s*[:\-]?\s*([^.,;]+)",
            "HPC_SEVERITY": r"\bseverity(?: was| is)?\s*[:\-]?\s*([^.,;]+)|\b(\d{1,2}\s*/\s*10)\b",
            "HPC_ALLEVIATING": r"\b(?:relieved|alleviated|better)\s+(?:by|with)\s+([^.,;]+)",
            "HPC_AGGRAVATING": r"\b(?:worse|aggravated)\s+(?:by|with|on)\s+([^.,;]+)",
            "HPC_ASSOCIATED": r"\bassociated with\s+([^.,;]+)",
        }
        for category, pattern in labelled_hpc.items():
            match = re.search(pattern, text, re.I)
            if match:
                value = next((group for group in match.groups() if group), match.group(0)).strip()
                facts.append(self._fact(utterance, session_id, category, value, value=value))

        symptom = self._SYMPTOM.search(text)
        if symptom:
            category = "SYSTEMIC_REVIEW" if self._assertion(text) == AssertionState.NEGATIVE else "PC"
            facts.append(self._fact(utterance, session_id, category, symptom.group(1), assertion=self._assertion(text)))

        for category, pattern in (
            ("FH", r"\b(?:family history|mother|father|sister|brother)\b"),
            ("SH", r"\b(?:smok(?:e|es|ing)|alcohol|occupation|lives? with|social history)\b"),
            ("INVESTIGATIONS", r"\b(?:investigation|test|x-ray|ultrasound|ct|mri|ecg|blood test|haemoglobin|hemoglobin)\b"),
            ("ASSESSMENT", r"\b(?:assessment|impression|diagnosis)\s*(?:is|was|:|-)"),
        ):
            if re.search(pattern, text, re.I):
                facts.append(self._fact(utterance, session_id, category, text))

        examination_absent = re.search(r"\b(?:no|not)\s+(?:physical\s+)?examination\s+(?:was\s+)?(?:done|performed)\b", text, re.I)
        if examination_absent:
            facts.append(self._fact(utterance, session_id, "PE", "examination performed", assertion=AssertionState.NEGATIVE))
        elif re.search(r"\b(?:on examination|physical examination|exam(?:ination)? showed|blood pressure|pulse|temperature)\b", text, re.I):
            facts.append(self._fact(utterance, session_id, "PE", text))
        return facts

    @staticmethod
    def _match(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, re.I)
        if not match:
            return None
        return next((group.strip() for group in match.groups() if group), match.group(0).strip())
