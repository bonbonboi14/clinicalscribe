from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from core.models import ClaimState, ClinicalFact, ClinicalNote, Differential, NoteValidationResult, TreatmentPlan, ValidationFinding
from core.validation.claim_extractor import ClaimExtractor, ExtractedClaim
from core.validation.contradiction_checker import ContradictionChecker
from core.validation.fact_validator import FactValidator
from models.clerking_sheet import ClerkingSheet


class ArtifactValidationError(ValueError):
    pass


class NoteValidator:
    """Produces the mandatory per-claim safety classification for clinical artefacts."""

    def __init__(self, *, extractor: ClaimExtractor | None = None, checker: ContradictionChecker | None = None, fact_validator: FactValidator | None = None) -> None:
        self.extractor = extractor or ClaimExtractor()
        self.checker = checker or ContradictionChecker()
        self.fact_validator = fact_validator or FactValidator()

    def validate(self, note: ClinicalNote, sheet: ClerkingSheet, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        facts = self.fact_validator.validate_all(facts)
        if note.clerking_sheet_id != sheet.id or note.session_id != sheet.session_id:
            raise ArtifactValidationError("note provenance does not match its clerking sheet")
        if any(fact.session_id != note.session_id for fact in facts):
            raise ArtifactValidationError("note facts belong to a different session")
        findings = self._classify(self.extractor.from_note(note), facts, session_id=note.session_id, artefact_type="CLINICAL_NOTE", artefact_id=note.id)
        return self._result("CLINICAL_NOTE", note.id, findings)

    def validate_clerking_sheet(self, sheet: ClerkingSheet, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        facts = self.fact_validator.validate_all(facts)
        findings = self._classify(self.extractor.from_clerking_sheet(sheet), facts, session_id=sheet.session_id, artefact_type="CLERKING_SHEET", artefact_id=sheet.id)
        return self._result("CLERKING_SHEET", sheet.id, findings)

    def validate_treatment_plan(self, plan: TreatmentPlan, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        facts = self.fact_validator.validate_all(facts)
        treatment_facts = [fact for fact in facts if fact.category in {"TREATMENT", "PLAN"}]
        findings = self._classify(self.extractor.from_treatment_plan(plan), treatment_facts, session_id=plan.session_id, artefact_type="TREATMENT_PLAN", artefact_id=plan.id)
        return self._result("TREATMENT_PLAN", plan.id, findings)

    def validate_differential(self, differential: Differential, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        facts = self.fact_validator.validate_all(facts)
        findings = self._classify(self.extractor.from_differential(differential), facts, session_id=differential.session_id, artefact_type="DIFFERENTIAL", artefact_id=differential.id)
        if differential.candidates and not differential.enabled_at_generation:
            findings.append(ValidationFinding(session_id=differential.session_id, artefact_type="DIFFERENTIAL", artefact_id=differential.id, claim_text="Differential feature disabled", state=ClaimState.CONTRADICTED, explanation="Differential candidates cannot be generated while the feature is disabled."))
        return self._result("DIFFERENTIAL", differential.id, findings)

    def _classify(self, claims: Iterable[ExtractedClaim], facts: list[ClinicalFact], *, session_id: UUID, artefact_type: str, artefact_id: UUID) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        for claim in claims:
            state, matched, explanation = self.checker.classify(claim.text, facts, evidence_fact_ids=claim.evidence_fact_ids)
            findings.append(ValidationFinding(
                session_id=session_id, artefact_type=artefact_type, artefact_id=artefact_id,
                claim_text=claim.text, state=state,
                evidence_segment_ids=list(dict.fromkeys(segment_id for fact in matched for segment_id in fact.transcript_segment_ids)),
                explanation=f"{claim.path}: {explanation}",
            ))
        return findings

    @staticmethod
    def _result(artefact_type: str, artefact_id: UUID, findings: list[ValidationFinding]) -> NoteValidationResult:
        safe = not any(finding.state in {ClaimState.UNSUPPORTED, ClaimState.CONTRADICTED} for finding in findings)
        return NoteValidationResult(artefact_type=artefact_type, artefact_id=artefact_id, findings=findings, is_safe=safe)


class HallucinationFirewall:
    """Blocks unsafe artefacts before persistence or clinician presentation."""

    def __init__(self, validator: NoteValidator | None = None) -> None:
        self.validator = validator or NoteValidator()

    @staticmethod
    def require_safe(result: NoteValidationResult) -> NoteValidationResult:
        if not result.is_safe:
            raise ArtifactValidationError(f"hallucination firewall rejected {len(result.blocked_findings)} claim(s)")
        return result

    def validate_clerking_sheet(self, sheet: ClerkingSheet, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        return self.require_safe(self.validator.validate_clerking_sheet(sheet, facts))

    def validate_treatment_plan(self, plan: TreatmentPlan, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        return self.require_safe(self.validator.validate_treatment_plan(plan, facts))

    def validate_differential(self, differential: Differential, facts: Iterable[ClinicalFact]) -> NoteValidationResult:
        return self.require_safe(self.validator.validate_differential(differential, facts))
