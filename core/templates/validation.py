from __future__ import annotations

from uuid import UUID

from core.models import ClaimState, ClinicalFact, ClinicalNote, ValidationFinding
from models.clerking_sheet import ClerkingSheet, NOT_DISCUSSED, NOT_PERFORMED


class NoteValidationError(ValueError):
    pass


class NoteHallucinationFirewall:
    """Classifies each rendered clinical value before a note can be persisted."""

    def validate(self, note: ClinicalNote, sheet: ClerkingSheet, facts: list[ClinicalFact]) -> list[ValidationFinding]:
        if note.clerking_sheet_id != sheet.id or note.session_id != sheet.session_id:
            raise NoteValidationError("note provenance does not match its clerking sheet")
        fact_by_id = {fact.id: fact for fact in facts}
        findings: list[ValidationFinding] = []
        for path, value in self._leaf_values(sheet):
            if value not in note.content or value not in note.plain_text:
                raise NoteValidationError(f"rendered note omitted or changed clerking-sheet value at {path}")
            evidence_ids = sheet.evidence_by_field.get(path, [])
            supporting = [fact_by_id[item] for item in evidence_ids if item in fact_by_id]
            if value in {NOT_DISCUSSED, NOT_PERFORMED}:
                state = ClaimState.SUPPORTED
                explanation = "Explicit structured-sheet placeholder; no clinical fact was inferred."
            elif not supporting:
                state = ClaimState.UNSUPPORTED
                explanation = "No structured-fact provenance was found."
            elif any(fact.assertion.value == "UNCERTAIN" for fact in supporting):
                state = ClaimState.UNCERTAIN
                explanation = "The source structured fact is uncertain."
            else:
                state = ClaimState.SUPPORTED
                explanation = "Copied from a validated structured fact."
            findings.append(ValidationFinding(
                session_id=note.session_id, artefact_type="CLINICAL_NOTE", artefact_id=note.id,
                claim_text=value, state=state,
                evidence_segment_ids=list(dict.fromkeys(segment for fact in supporting for segment in fact.transcript_segment_ids)),
                explanation=f"{path}: {explanation}",
            ))
        rejected = [finding for finding in findings if finding.state in {ClaimState.UNSUPPORTED, ClaimState.CONTRADICTED}]
        if rejected:
            raise NoteValidationError(f"hallucination firewall rejected {len(rejected)} note claim(s)")
        return findings

    @staticmethod
    def _leaf_values(sheet: ClerkingSheet):
        excluded = {"id", "session_id", "version", "fact_ids", "evidence_by_field", "status", "created_at"}

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

        yield from walk(sheet.model_dump(mode="python"), "")
