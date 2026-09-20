from __future__ import annotations

import json
from uuid import UUID

from core.models import AssertionState, ClaimState, ClinicalFact, ValidationFinding
from models.clerking_sheet import ClerkingSheet, NOT_DISCUSSED, NOT_PERFORMED


class FabricatedClerkingFieldError(ValueError):
    pass


class DoNotInferValidator:
    """Rejects every non-placeholder clerking value without matching fact provenance."""

    _CATEGORIES = {
        "presenting_complaint": {"PC"},
        "history_of_presenting_complaint.onset": {"HPC_ONSET"},
        "history_of_presenting_complaint.duration": {"HPC_DURATION"},
        "history_of_presenting_complaint.character": {"HPC_CHARACTER"},
        "history_of_presenting_complaint.radiation": {"HPC_RADIATION"},
        "history_of_presenting_complaint.severity": {"HPC_SEVERITY"},
        "history_of_presenting_complaint.alleviating": {"HPC_ALLEVIATING"},
        "history_of_presenting_complaint.aggravating": {"HPC_AGGRAVATING"},
        "history_of_presenting_complaint.associated": {"HPC_ASSOCIATED"},
        "systemic_review": {"SYSTEMIC_REVIEW"},
        "past_medical_history": {"PMH"},
        "drug_history": {"DH"},
        "allergies": {"ALLERGY"},
        "family_history": {"FH"},
        "social_history": {"SH"},
        "physical_examination": {"PE"},
        "investigations": {"INVESTIGATIONS"},
        "assessment": {"ASSESSMENT"},
    }

    def validate(self, sheet: ClerkingSheet, facts: list[ClinicalFact]) -> list[ValidationFinding]:
        facts_by_id = {fact.id: fact for fact in facts}
        findings: list[ValidationFinding] = []
        if set(sheet.fact_ids) - facts_by_id.keys():
            raise FabricatedClerkingFieldError("clerking sheet references unknown facts")
        for path, value in self._leaf_values(sheet):
            if value in {NOT_DISCUSSED, NOT_PERFORMED}:
                continue
            evidence = sheet.evidence_by_field.get(path, [])
            if not evidence:
                raise FabricatedClerkingFieldError(f"{path} has no structured-fact evidence")
            supporting = [facts_by_id[item] for item in evidence if item in facts_by_id]
            if not supporting:
                raise FabricatedClerkingFieldError(f"{path} references no known fact")
            expected = next((categories for prefix, categories in self._CATEGORIES.items() if path == prefix or path.startswith(f"{prefix}.")), set())
            if not expected or any(fact.category not in expected for fact in supporting):
                raise FabricatedClerkingFieldError(f"{path} references a fact from the wrong clinical section")
            if path.startswith("past_medical_history") and any(f.assertion == AssertionState.NEGATIVE for f in supporting):
                raise FabricatedClerkingFieldError("negative PMH cannot be presented as past medical history")
            allowed = set()
            for fact in supporting:
                allowed.update(filter(None, (fact.name, fact.value, fact.original_text)))
                if fact.category == "SYSTEMIC_REVIEW" and fact.assertion == AssertionState.NEGATIVE:
                    allowed.add(f"Denied: {fact.name}")
                if fact.value and fact.value.startswith("{"):
                    allowed.update(str(item) for item in json.loads(fact.value).values() if item)
            if value not in allowed:
                raise FabricatedClerkingFieldError(f"{path} contains text not present in its structured facts")
            state = ClaimState.UNCERTAIN if any(fact.assertion == AssertionState.UNCERTAIN for fact in supporting) else ClaimState.SUPPORTED
            findings.append(ValidationFinding(
                session_id=sheet.session_id, artefact_type="CLERKING_SHEET", artefact_id=sheet.id,
                claim_text=value, state=state,
                evidence_segment_ids=list(dict.fromkeys(segment_id for fact in supporting for segment_id in fact.transcript_segment_ids)),
                explanation=f"Validated against structured fact evidence for {path}.",
            ))
        return findings

    @staticmethod
    def _leaf_values(sheet: ClerkingSheet):
        excluded = {"id", "session_id", "version", "fact_ids", "evidence_by_field", "status", "created_at"}
        data = sheet.model_dump(mode="python")

        def walk(value, path: str):
            if isinstance(value, str):
                yield path, value
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from walk(item, f"{path}.{index}")
            elif isinstance(value, dict):
                for key, item in value.items():
                    if not path and key in excluded:
                        continue
                    yield from walk(item, f"{path}.{key}" if path else key)

        yield from walk(data, "")
