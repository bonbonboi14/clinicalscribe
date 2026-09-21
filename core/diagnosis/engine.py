from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from config.loader import DiagnosisConfig, get_diagnosis_config
from core.models import AssertionState, ClinicalFact
from models.differential import Differential, DifferentialResult, DxLikelihood


@dataclass(frozen=True)
class _Rule:
    condition: str
    supporting_terms: tuple[str, ...]
    opposing_terms: tuple[str, ...] = ()


class DifferentialDiagnosisEngine:
    """Conservative local differential ranking over documented facts only."""

    _RULES = (
        _Rule("Viral upper respiratory tract infection", ("fever", "cough", "nasal congestion", "sore throat")),
        _Rule("Influenza-like illness", ("fever", "cough", "headache", "weakness")),
        _Rule("Community-acquired pneumonia", ("fever", "cough", "shortness of breath", "chest pain")),
        _Rule("Acute rhinosinusitis", ("nasal congestion", "facial pain", "headache", "fever")),
        _Rule("Allergic rhinitis", ("nasal congestion", "sneezing", "itching", "rhinorrhoea"), ("fever",)),
        _Rule("Acute tonsillitis", ("sore throat", "fever", "tonsillar swelling", "odynophagia")),
        _Rule("Acute bronchitis", ("cough", "fever", "wheeze", "sputum")),
        _Rule("Gastroenteritis", ("diarrhoea", "diarrhea", "vomiting", "nausea", "fever")),
    )

    def __init__(self, config: DiagnosisConfig | None = None) -> None:
        self.config = config or get_diagnosis_config()

    def is_enabled(self, session_enabled: bool | None = None) -> bool:
        return self.config.enabled and session_enabled is not False

    def condition_names(self) -> list[str]:
        return [rule.condition for rule in self._RULES]

    def generate(
        self,
        session_id: UUID | str,
        facts: list[ClinicalFact],
        *,
        session_enabled: bool | None = None,
    ) -> DifferentialResult:
        session_uuid = UUID(str(session_id))
        if any(fact.session_id != session_uuid for fact in facts):
            raise ValueError("all structured facts must belong to the differential session")
        if not self.is_enabled(session_enabled):
            return DifferentialResult(
                session_id=session_uuid, enabled=False, candidates=[], fact_count_used=0,
                generation_note="Differential diagnosis is disabled.",
            )

        eligible = [
            fact for fact in facts
            if fact.assertion in {AssertionState.POSITIVE, AssertionState.NEGATIVE}
        ]
        if len(eligible) < 2:
            return DifferentialResult(
                session_id=session_uuid, enabled=True, candidates=[], fact_count_used=len(eligible),
                generation_note="Insufficient documented POSITIVE/NEGATIVE facts for a differential.",
            )
        ranked: list[tuple[float, Differential]] = []
        for rule in self._RULES:
            supporting: list[ClinicalFact] = []
            against: list[ClinicalFact] = []
            for fact in eligible:
                text = self._fact_text(fact)
                supports_rule = any(self._contains(text, term) for term in rule.supporting_terms)
                opposes_rule = any(self._contains(text, term) for term in rule.opposing_terms)
                if supports_rule and fact.assertion == AssertionState.POSITIVE:
                    supporting.append(fact)
                elif supports_rule and fact.assertion == AssertionState.NEGATIVE:
                    against.append(fact)
                elif opposes_rule and fact.assertion == AssertionState.POSITIVE:
                    against.append(fact)
                elif opposes_rule and fact.assertion == AssertionState.NEGATIVE:
                    supporting.append(fact)
            # Negative findings may rank a documented possibility down, but
            # cannot introduce a condition on their own.
            if not supporting:
                continue
            score = len(supporting) - len(against)
            likelihood = (
                DxLikelihood.SUPPORTED if score >= 2
                else DxLikelihood.POSSIBLE if score >= 1
                else DxLikelihood.UNLIKELY
            )
            evidence = list(dict.fromkeys(fact.id for fact in [*supporting, *against]))
            ranked.append((score, Differential(
                condition=rule.condition,
                likelihood=likelihood,
                supporting_features=[self._feature_text(fact) for fact in supporting],
                features_against=[self._feature_text(fact) for fact in against],
                evidence_refs=evidence,
            )))

        ranked.sort(key=lambda item: (-item[0], item[1].condition))
        candidates = [candidate for _, candidate in ranked[:5]] if len(ranked) >= 3 else []
        return DifferentialResult(
            session_id=session_uuid,
            enabled=True,
            candidates=candidates,
            fact_count_used=len(eligible),
            generation_note=(
                "Ranked from documented POSITIVE and NEGATIVE facts only."
                if candidates else "Insufficient matching documented facts for a differential."
            ),
        )

    @staticmethod
    def _fact_text(fact: ClinicalFact) -> str:
        return " ".join(filter(None, (fact.name, fact.value, fact.original_text))).casefold()

    @staticmethod
    def _feature_text(fact: ClinicalFact) -> str:
        prefix = "Denied" if fact.assertion == AssertionState.NEGATIVE else "Documented"
        return f"{prefix}: {fact.value or fact.name}"

    @staticmethod
    def _contains(text: str, term: str) -> bool:
        return bool(re.search(rf"\b{re.escape(term.casefold())}\b", text))
