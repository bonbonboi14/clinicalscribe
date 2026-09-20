from __future__ import annotations

import json
from collections import defaultdict
from uuid import UUID

from core.clerking.validation import DoNotInferValidator
from core.models import AssertionState, ClinicalFact
from models.clerking_sheet import AllergyEntry, ClerkingSheet, DrugHistoryEntry, HPCSection, NOT_DISCUSSED


class ClerkingSheetGenerator:
    def __init__(self, validator: DoNotInferValidator | None = None) -> None:
        self.validator = validator or DoNotInferValidator()

    def generate(self, session_id: UUID | str, facts: list[ClinicalFact], *, version: int = 1) -> ClerkingSheet:
        session_uuid = UUID(str(session_id))
        if any(fact.session_id != session_uuid for fact in facts):
            raise ValueError("all structured facts must belong to the clerking session")
        evidence: dict[str, list[UUID]] = defaultdict(list)

        pc_facts = self._presentable(facts, "PC")
        pc = self._first_value(pc_facts)
        if pc_facts:
            evidence["presenting_complaint"].append(pc_facts[0].id)

        hpc_values = {}
        for field in ("onset", "duration", "character", "radiation", "severity", "alleviating", "aggravating", "associated"):
            matches = self._presentable(facts, f"HPC_{field.upper()}")
            hpc_values[field] = self._first_value(matches)
            if matches:
                evidence[f"history_of_presenting_complaint.{field}"].append(matches[0].id)

        systemic = []
        for fact in [item for item in facts if item.category == "SYSTEMIC_REVIEW"]:
            value = f"Denied: {fact.name}" if fact.assertion == AssertionState.NEGATIVE else self._fact_text(fact)
            systemic.append(value)
            evidence[f"systemic_review.{len(systemic) - 1}"].append(fact.id)
        systemic = systemic or [NOT_DISCUSSED]

        pmh = self._list_section(facts, "PMH", evidence, "past_medical_history")
        fh = self._list_section(facts, "FH", evidence, "family_history")
        sh = self._list_section(facts, "SH", evidence, "social_history")
        investigations = self._list_section(facts, "INVESTIGATIONS", evidence, "investigations")
        assessment = self._list_section(facts, "ASSESSMENT", evidence, "assessment")

        drugs: list[DrugHistoryEntry] = []
        for fact in self._presentable(facts, "DH"):
            payload = self._json_value(fact)
            entry = DrugHistoryEntry(**{key: payload.get(key) or NOT_DISCUSSED for key in ("drug", "dose", "frequency", "duration")})
            index = len(drugs)
            drugs.append(entry)
            for key, value in entry.model_dump().items():
                if value != NOT_DISCUSSED:
                    evidence[f"drug_history.{index}.{key}"].append(fact.id)
        drugs = drugs or [DrugHistoryEntry()]

        allergies: list[AllergyEntry] = []
        for fact in self._presentable(facts, "ALLERGY"):
            payload = self._json_value(fact)
            entry = AllergyEntry(**{key: payload.get(key) or NOT_DISCUSSED for key in ("drug", "food", "environmental", "reaction")})
            index = len(allergies)
            allergies.append(entry)
            for key, value in entry.model_dump().items():
                if value != NOT_DISCUSSED:
                    evidence[f"allergies.{index}.{key}"].append(fact.id)
        allergies = allergies or [AllergyEntry()]

        pe_facts = self._presentable(facts, "PE")
        physical_examination = []
        for fact in pe_facts:
            value = self._fact_text(fact)
            physical_examination.append(value)
            evidence[f"physical_examination.{len(physical_examination) - 1}"].append(fact.id)
        physical_examination = physical_examination or ["Not performed"]

        sheet = ClerkingSheet(
            session_id=session_uuid, version=version, presenting_complaint=pc,
            history_of_presenting_complaint=HPCSection(**hpc_values), systemic_review=systemic,
            past_medical_history=pmh, drug_history=drugs, allergies=allergies,
            family_history=fh, social_history=sh, physical_examination=physical_examination,
            investigations=investigations, assessment=assessment,
            fact_ids=[fact.id for fact in facts], evidence_by_field=dict(evidence),
        )
        self.validator.validate(sheet, facts)
        return sheet

    @staticmethod
    def _presentable(facts: list[ClinicalFact], category: str) -> list[ClinicalFact]:
        return [fact for fact in facts if fact.category == category and fact.assertion in {AssertionState.POSITIVE, AssertionState.UNCERTAIN}]

    @staticmethod
    def _fact_text(fact: ClinicalFact) -> str:
        return fact.value or fact.name

    def _first_value(self, facts: list[ClinicalFact]) -> str:
        return self._fact_text(facts[0]) if facts else NOT_DISCUSSED

    def _list_section(self, facts, category, evidence, path):
        values = []
        for fact in self._presentable(facts, category):
            values.append(self._fact_text(fact))
            evidence[f"{path}.{len(values) - 1}"].append(fact.id)
        return values or [NOT_DISCUSSED]

    @staticmethod
    def _json_value(fact: ClinicalFact) -> dict[str, str | None]:
        if not fact.value:
            return {}
        try:
            value = json.loads(fact.value)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
