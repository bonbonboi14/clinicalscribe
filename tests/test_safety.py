from __future__ import annotations

from uuid import uuid4

import pytest

from core.clerking import ClerkingSheetGenerator
from core.examination import ExaminationInterpreter
from core.models import AssertionState, ClaimState, ClinicalFact, Differential, TreatmentPlan, TranscriptSegment
from core.structuring import ClinicalFactExtractor
from core.templates import NoteHallucinationFirewall, NoteValidationError, TemplateNoteEngine, TemplateRegistry
from core.validation import ArtifactValidationError, FactValidator, HallucinationFirewall, NoteValidator


def _facts(text: str) -> tuple[list[ClinicalFact], object]:
    session_id = uuid4()
    segment = TranscriptSegment(
        session_id=session_id, sequence_number=0, start_ms=0, end_ms=1000,
        source_language="en", original_text=text, confidence=0.99,
    )
    facts = ClinicalFactExtractor().extract([segment])
    return facts, ClerkingSheetGenerator().generate(session_id, facts)


def _note(sheet):
    return TemplateNoteEngine(TemplateRegistry("templates").load("primary_care")).generate(sheet)


def _append_claim(note, claim: str):
    return note.model_copy(update={
        "content": f"{note.content}\n- {claim}\n",
        "plain_text": f"{note.plain_text}\n- {claim}\n",
    })


def test_every_clinical_fact_has_one_of_the_four_explicit_assertion_states() -> None:
    session_id = uuid4()
    facts = [
        ClinicalFact(session_id=session_id, category="TEST", name=state.value, assertion=state, confidence=1.0)
        for state in AssertionState
    ]
    assert {fact.assertion for fact in FactValidator().validate_all(facts)} == set(AssertionState)


def test_denied_diabetes_blocks_positive_diabetes_claim_in_note_and_clerking() -> None:
    facts, sheet = _facts("Patient denies diabetes.")
    forged_note = _append_claim(_note(sheet), "diabetes mellitus")
    result = NoteValidator().validate(forged_note, sheet, facts)
    assert result.is_safe is False
    assert any(item.claim_text == "diabetes mellitus" and item.state == ClaimState.CONTRADICTED for item in result.findings)
    with pytest.raises(NoteValidationError, match="hallucination firewall"):
        NoteHallucinationFirewall().validate(forged_note, sheet, facts)

    forged_sheet = sheet.model_copy(update={
        "past_medical_history": ["diabetes mellitus"],
        "evidence_by_field": {**sheet.evidence_by_field, "past_medical_history.0": [facts[0].id]},
    })
    with pytest.raises(ArtifactValidationError, match="hallucination firewall"):
        HallucinationFirewall().validate_clerking_sheet(forged_sheet, facts)


def test_unnamed_tablet_blocks_invented_amlodipine_everywhere() -> None:
    facts, sheet = _facts("Patient takes a small white tablet, doesn't remember name.")
    assert sheet.drug_history[0].drug == "Unnamed tablet"
    forged_note = _append_claim(_note(sheet), "amlodipine")
    result = NoteValidator().validate(forged_note, sheet, facts)
    assert any(item.claim_text == "amlodipine" and item.state == ClaimState.UNSUPPORTED for item in result.findings)
    with pytest.raises(NoteValidationError, match="hallucination firewall"):
        NoteHallucinationFirewall().validate(forged_note, sheet, facts)

    plan = TreatmentPlan(session_id=sheet.session_id, items=["Start amlodipine"])
    with pytest.raises(ArtifactValidationError, match="hallucination firewall"):
        HallucinationFirewall().validate_treatment_plan(plan, facts)


def test_no_examination_blocks_normal_cardiovascular_examination_claim() -> None:
    facts, sheet = _facts("No examination was performed.")
    forged_note = _append_claim(_note(sheet), "Cardiovascular examination was normal")
    result = NoteValidator().validate(forged_note, sheet, facts)
    assert any(item.state == ClaimState.CONTRADICTED for item in result.findings)
    with pytest.raises(NoteValidationError, match="hallucination firewall"):
        NoteHallucinationFirewall().validate(forged_note, sheet, facts)


def test_turbinate_mapping_is_supported_but_sinusitis_is_blocked() -> None:
    session_id, segment_id = uuid4(), uuid4()
    finding = ExaminationInterpreter().interpret_phrase(
        "left nose swollen", session_id=session_id, transcript_segment_ids=[segment_id]
    )
    fact = ClinicalFact(
        session_id=session_id, category="PE", name=finding.interpreted_text,
        value=finding.interpreted_text, assertion=AssertionState.POSITIVE,
        confidence=finding.confidence, transcript_segment_ids=[segment_id], original_text=finding.raw_text,
    )
    sheet = ClerkingSheetGenerator().generate(session_id, [fact])
    assert sheet.physical_examination == ["left inferior turbinate hypertrophy"]
    safe = NoteValidator().validate(_note(sheet), sheet, [fact])
    assert safe.is_safe is True

    forged_note = _append_claim(_note(sheet), "sinusitis")
    result = NoteValidator().validate(forged_note, sheet, [fact])
    assert any(item.claim_text == "sinusitis" and item.state == ClaimState.UNSUPPORTED for item in result.findings)
    with pytest.raises(NoteValidationError, match="hallucination firewall"):
        NoteHallucinationFirewall().validate(forged_note, sheet, [fact])


def test_differential_uses_same_firewall_and_default_off_is_blocking() -> None:
    facts, sheet = _facts("Patient has fever.")
    differential = Differential(
        session_id=sheet.session_id, enabled_at_generation=False,
        candidates=[{"diagnosis": "pneumonia"}], fact_ids=[facts[0].id],
        disclaimer="Decision support only; clinician review required.",
    )
    result = NoteValidator().validate_differential(differential, facts)
    assert result.is_safe is False
    assert {finding.state for finding in result.blocked_findings} == {ClaimState.UNSUPPORTED, ClaimState.CONTRADICTED}
    with pytest.raises(ArtifactValidationError, match="hallucination firewall"):
        HallucinationFirewall().validate_differential(differential, facts)
