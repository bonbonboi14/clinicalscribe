from __future__ import annotations

import json
from collections import defaultdict
from uuid import UUID

from core.models import AssertionState, ClinicalFact
from core.treatment.validation import TreatmentDoNotInferValidator
from core.validation import HallucinationFirewall
from models.treatment_plan import NOT_MENTIONED, PharmacologicalTreatment, TreatmentPlan


class TreatmentPlanGenerator:
    """Build a treatment plan solely from explicit TREATMENT/PLAN facts."""

    _LIST_SECTIONS = (
        "non_pharmacological", "investigations_ordered", "referrals", "follow_up",
        "patient_education", "pending_decisions",
    )

    def __init__(
        self,
        validator: TreatmentDoNotInferValidator | None = None,
        safety_firewall: HallucinationFirewall | None = None,
    ) -> None:
        self.validator = validator or TreatmentDoNotInferValidator()
        self.safety_firewall = safety_firewall or HallucinationFirewall()

    def generate(
        self,
        session_id: UUID | str,
        facts: list[ClinicalFact],
        *,
        note_id: UUID | str | None = None,
        version: int = 1,
    ) -> TreatmentPlan:
        session_uuid = UUID(str(session_id))
        if any(fact.session_id != session_uuid for fact in facts):
            raise ValueError("all structured facts must belong to the treatment-plan session")
        treatment_facts = [
            fact for fact in facts
            if fact.category in {"TREATMENT", "PLAN"}
            and fact.assertion != AssertionState.NOT_MENTIONED
        ]
        evidence: dict[str, list[UUID]] = defaultdict(list)
        sections: dict[str, list[str]] = {name: [] for name in self._LIST_SECTIONS}
        medications: list[PharmacologicalTreatment] = []

        for fact in treatment_facts:
            payload = self._payload(fact)
            section = str(payload.get("section") or self._section_from_name(fact.name)).casefold()
            if fact.assertion == AssertionState.NEGATIVE and section != "pending_decisions":
                continue
            if section == "pharmacological":
                index = len(medications)
                values = {
                    key: str(payload.get(key) or NOT_MENTIONED)
                    for key in ("drug", "dose", "route", "frequency", "duration")
                }
                reference = str(fact.transcript_segment_ids[0]) if fact.transcript_segment_ids else NOT_MENTIONED
                medication = PharmacologicalTreatment(**values, source_transcript_ref=reference)
                medications.append(medication)
                for key, value in medication.model_dump().items():
                    if value != NOT_MENTIONED:
                        evidence[f"pharmacological.{index}.{key}"].append(fact.id)
                continue
            if section not in sections:
                continue
            text = str(payload.get("text") or fact.value or fact.name).strip()
            if not text:
                continue
            index = len(sections[section])
            sections[section].append(text)
            evidence[f"{section}.{index}"].append(fact.id)

        plan = TreatmentPlan(
            session_id=session_uuid,
            note_id=UUID(str(note_id)) if note_id else None,
            version=version,
            pharmacological=medications or [PharmacologicalTreatment()],
            fact_ids=[fact.id for fact in treatment_facts],
            evidence_by_field=dict(evidence),
            **{name: values or [NOT_MENTIONED] for name, values in sections.items()},
        )
        self.validator.validate(plan, treatment_facts)
        self.safety_firewall.validate_treatment_plan(plan, treatment_facts)
        return plan

    @staticmethod
    def _payload(fact: ClinicalFact) -> dict[str, object]:
        if not fact.value:
            return {}
        try:
            payload = json.loads(fact.value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _section_from_name(name: str) -> str:
        aliases = {
            "medication": "pharmacological", "drug": "pharmacological",
            "investigation": "investigations_ordered", "referral": "referrals",
            "follow-up": "follow_up", "follow up": "follow_up",
            "education": "patient_education", "pending": "pending_decisions",
            "non-pharmacological": "non_pharmacological",
        }
        return aliases.get(name.strip().casefold(), name.strip().casefold())
