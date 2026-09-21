from __future__ import annotations

import json
from uuid import UUID

from core.models import AssertionState, ClaimState, ClinicalFact, ValidationFinding
from models.treatment_plan import NOT_MENTIONED, TreatmentPlan


class FabricatedTreatmentError(ValueError):
    pass


class TreatmentDoNotInferValidator:
    """Reject every treatment value that is not directly backed by treatment facts."""

    _SECTIONS = {
        "non_pharmacological",
        "pharmacological",
        "investigations_ordered",
        "referrals",
        "follow_up",
        "patient_education",
        "pending_decisions",
        "items",
    }

    def validate(self, plan: TreatmentPlan, facts: list[ClinicalFact]) -> list[ValidationFinding]:
        facts_by_id = {fact.id: fact for fact in facts}
        if set(plan.fact_ids) - facts_by_id.keys():
            raise FabricatedTreatmentError("treatment plan references unknown facts")
        findings: list[ValidationFinding] = []
        for path, value in self._leaf_values(plan):
            if value == NOT_MENTIONED:
                continue
            evidence = plan.evidence_by_field.get(path, [])
            if not evidence:
                raise FabricatedTreatmentError(f"{path} has no structured-fact evidence")
            supporting = [facts_by_id[item] for item in evidence if item in facts_by_id]
            if not supporting:
                raise FabricatedTreatmentError(f"{path} references no known fact")
            if any(fact.category not in {"TREATMENT", "PLAN"} for fact in supporting):
                raise FabricatedTreatmentError(f"{path} references a non-treatment fact")
            if any(
                fact.assertion not in {AssertionState.POSITIVE, AssertionState.UNCERTAIN}
                and not (path.startswith("pending_decisions.") and fact.assertion == AssertionState.NEGATIVE)
                for fact in supporting
            ):
                raise FabricatedTreatmentError(f"{path} references treatment that was not explicitly proposed")
            root = path.split(".", 1)[0]
            if root not in self._SECTIONS:
                raise FabricatedTreatmentError(f"unsupported treatment field: {path}")
            if path.endswith(".source_transcript_ref"):
                allowed_refs = {str(segment_id) for fact in supporting for segment_id in fact.transcript_segment_ids}
                if value not in allowed_refs:
                    raise FabricatedTreatmentError(f"{path} does not cite its source transcript segment")
            elif value not in self._allowed_values(supporting):
                raise FabricatedTreatmentError(f"{path} contains text absent from its structured facts")
            state = ClaimState.UNCERTAIN if any(
                fact.assertion == AssertionState.UNCERTAIN for fact in supporting
            ) else ClaimState.SUPPORTED
            findings.append(ValidationFinding(
                session_id=plan.session_id,
                artefact_type="TREATMENT_PLAN",
                artefact_id=plan.id,
                claim_text=value,
                state=state,
                evidence_segment_ids=list(dict.fromkeys(
                    segment_id for fact in supporting for segment_id in fact.transcript_segment_ids
                )),
                explanation=f"Validated against explicit transcript-grounded treatment evidence for {path}.",
            ))
        return findings

    @staticmethod
    def _allowed_values(facts: list[ClinicalFact]) -> set[str]:
        allowed: set[str] = set()
        for fact in facts:
            allowed.update(filter(None, (fact.name, fact.value, fact.original_text)))
            if fact.value:
                try:
                    payload = json.loads(fact.value)
                except (json.JSONDecodeError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    allowed.update(str(item) for item in payload.values() if item is not None and not isinstance(item, (dict, list)))
        return allowed

    @staticmethod
    def _leaf_values(plan: TreatmentPlan):
        excluded = {
            "id", "session_id", "note_id", "version", "fact_ids", "evidence_by_field",
            "status", "created_at", "disclaimer",
        }
        data = plan.model_dump(mode="python")

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
