from __future__ import annotations

from core.models import ClaimState, ClinicalFact, ClinicalNote, ValidationFinding
from core.validation import ArtifactValidationError, NoteValidator
from models.clerking_sheet import ClerkingSheet


class NoteValidationError(ArtifactValidationError):
    pass


class NoteHallucinationFirewall:
    """Compatibility facade for the Phase 6 note-validation interface."""

    def __init__(self, validator: NoteValidator | None = None) -> None:
        self.validator = validator or NoteValidator()

    def validate(self, note: ClinicalNote, sheet: ClerkingSheet, facts: list[ClinicalFact]) -> list[ValidationFinding]:
        if note.clerking_sheet_id != sheet.id or note.session_id != sheet.session_id:
            raise NoteValidationError("note provenance does not match its clerking sheet")
        for path, value in self._leaf_values(sheet):
            if value not in note.content or value not in note.plain_text:
                raise NoteValidationError(f"rendered note omitted or changed clerking-sheet value at {path}")
        try:
            result = self.validator.validate(note, sheet, facts)
        except ArtifactValidationError as exc:
            raise NoteValidationError(str(exc)) from exc
        rejected = [finding for finding in result.findings if finding.state in {ClaimState.UNSUPPORTED, ClaimState.CONTRADICTED}]
        if rejected:
            raise NoteValidationError(f"hallucination firewall rejected {len(rejected)} note claim(s)")
        return result.findings

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
