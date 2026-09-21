from __future__ import annotations

import json
import re
from collections.abc import Iterable
from uuid import UUID

from core.models import AssertionState, ClaimState, ClinicalFact
from models.clerking_sheet import NOT_DISCUSSED, NOT_PERFORMED
from models.treatment_plan import NOT_MENTIONED


class ContradictionChecker:
    """Classifies claims against structured facts without adding medical knowledge."""

    _NEGATED = re.compile(r"\b(no|not|never|denied|denies|without|absent|negative for)\b", re.I)
    _NORMAL_EXAM = re.compile(
        r"\b(?:cardiovascular|cardiac|respiratory|chest|abdominal|neurological|physical)?\s*"
        r"exam(?:ination)?\s+(?:was|is|appears)?\s*normal\b|\bnormal\s+"
        r"(?:cardiovascular|cardiac|respiratory|chest|abdominal|neurological|physical)\s+exam",
        re.I,
    )
    _ALIASES = (
        (re.compile(r"\bdiabetes mellitus\b", re.I), "diabetes"),
        (re.compile(r"\bhigh blood pressure\b", re.I), "hypertension"),
    )

    def classify(
        self,
        claim_text: str,
        facts: Iterable[ClinicalFact],
        *,
        evidence_fact_ids: Iterable[UUID] = (),
    ) -> tuple[ClaimState, list[ClinicalFact], str]:
        facts = list(facts)
        claim = self._normalise(claim_text)
        evidence_ids = set(evidence_fact_ids)
        if claim_text in {NOT_DISCUSSED, NOT_PERFORMED, NOT_MENTIONED}:
            return ClaimState.SUPPORTED, [], "Explicit missing-information placeholder."

        if self._NORMAL_EXAM.search(claim_text) and any(
            fact.category == "PE" and fact.assertion in {AssertionState.NEGATIVE, AssertionState.NOT_MENTIONED}
            for fact in facts
        ):
            matched = [fact for fact in facts if fact.category == "PE"]
            return ClaimState.CONTRADICTED, matched, "A normal examination was claimed although examination was not performed."

        matched = [fact for fact in facts if self._matches_fact(claim, fact)]
        if evidence_ids:
            cited = [fact for fact in facts if fact.id in evidence_ids]
            if not cited:
                return ClaimState.UNSUPPORTED, [], "Claim cites no known structured fact."
            matched = [fact for fact in matched if fact.id in evidence_ids]

        claim_is_negative = bool(self._NEGATED.search(claim_text))
        opposed = [
            fact for fact in matched
            if (fact.assertion == AssertionState.NEGATIVE and not claim_is_negative)
            or (fact.assertion == AssertionState.POSITIVE and claim_is_negative)
        ]
        if opposed:
            return ClaimState.CONTRADICTED, opposed, "Claim polarity contradicts documented structured fact evidence."
        aligned = [
            fact for fact in matched
            if (fact.assertion == AssertionState.NEGATIVE and claim_is_negative)
            or (fact.assertion == AssertionState.POSITIVE and not claim_is_negative)
        ]
        if aligned:
            return ClaimState.SUPPORTED, aligned, "Claim is grounded in documented structured fact evidence."
        uncertain = [fact for fact in matched if fact.assertion == AssertionState.UNCERTAIN]
        if uncertain:
            return ClaimState.UNCERTAIN, uncertain, "Claim is grounded only in uncertain structured fact evidence."
        return ClaimState.UNSUPPORTED, [], "No matching structured fact supports this claim."

    def _matches_fact(self, claim: str, fact: ClinicalFact) -> bool:
        return any(self._term_match(claim, term) for term in self._fact_terms(fact))

    def _fact_terms(self, fact: ClinicalFact) -> set[str]:
        values: set[str] = {fact.name}
        if fact.value:
            values.add(fact.value)
            try:
                payload = json.loads(fact.value)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict):
                values.update(str(item) for item in payload.values() if item)
        if fact.original_text:
            values.add(fact.original_text)
        return {self._normalise(value) for value in values if value.strip()}

    @staticmethod
    def _term_match(claim: str, term: str) -> bool:
        if not claim or not term:
            return False
        return claim == term or f" {term} " in f" {claim} " or f" {claim} " in f" {term} "

    def _normalise(self, text: str) -> str:
        result = text.casefold()
        for pattern, replacement in self._ALIASES:
            result = pattern.sub(replacement, result)
        result = re.sub(r"[^a-z0-9]+", " ", result)
        return " ".join(result.split())
