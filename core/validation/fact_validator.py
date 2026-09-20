from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from core.models import AssertionState, ClinicalFact


class FactValidationError(ValueError):
    """Raised when a structured fact cannot satisfy the safety contract."""


class FactValidator:
    """Validates the mandatory four-state and provenance contract for facts."""

    def validate(self, fact: ClinicalFact) -> ClinicalFact:
        if not isinstance(fact.assertion, AssertionState):
            raise FactValidationError("every ClinicalFact requires an assertion state")
        if not fact.category.strip() or not fact.name.strip():
            raise FactValidationError("ClinicalFact category and name cannot be empty")
        if len(set(fact.transcript_segment_ids)) != len(fact.transcript_segment_ids):
            raise FactValidationError("ClinicalFact evidence segment ids must be unique")
        if fact.assertion == AssertionState.NOT_MENTIONED and fact.transcript_segment_ids:
            raise FactValidationError("NOT_MENTIONED facts cannot cite transcript evidence")
        return fact

    def validate_all(self, facts: Iterable[ClinicalFact]) -> list[ClinicalFact]:
        validated = [self.validate(fact) for fact in facts]
        sessions = {fact.session_id for fact in validated}
        if len(sessions) > 1:
            raise FactValidationError("facts from different sessions cannot be validated together")
        return validated

    @staticmethod
    def assertion_groups(facts: Iterable[ClinicalFact]) -> dict[AssertionState, list[ClinicalFact]]:
        groups: dict[AssertionState, list[ClinicalFact]] = defaultdict(list)
        for fact in facts:
            groups[fact.assertion].append(fact)
        return dict(groups)
