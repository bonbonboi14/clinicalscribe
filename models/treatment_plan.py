from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


NOT_MENTIONED = "Not mentioned."
DocumentedText = Annotated[str, Field(min_length=1)]


class TreatmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PharmacologicalTreatment(TreatmentModel):
    drug: DocumentedText = NOT_MENTIONED
    dose: DocumentedText = NOT_MENTIONED
    route: DocumentedText = NOT_MENTIONED
    frequency: DocumentedText = NOT_MENTIONED
    duration: DocumentedText = NOT_MENTIONED
    source_transcript_ref: DocumentedText = NOT_MENTIONED


def _missing_list() -> list[str]:
    return [NOT_MENTIONED]


def _missing_medications() -> list[PharmacologicalTreatment]:
    return [PharmacologicalTreatment()]


class TreatmentPlan(TreatmentModel):
    """Immutable, transcript-grounded management plan awaiting clinician review."""

    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    note_id: UUID | None = None
    version: int = Field(default=1, ge=1)
    non_pharmacological: list[DocumentedText] = Field(default_factory=_missing_list)
    pharmacological: list[PharmacologicalTreatment] = Field(default_factory=_missing_medications)
    investigations_ordered: list[DocumentedText] = Field(default_factory=_missing_list)
    referrals: list[DocumentedText] = Field(default_factory=_missing_list)
    follow_up: list[DocumentedText] = Field(default_factory=_missing_list)
    patient_education: list[DocumentedText] = Field(default_factory=_missing_list)
    pending_decisions: list[DocumentedText] = Field(default_factory=_missing_list)
    fact_ids: list[UUID] = Field(default_factory=list)
    evidence_by_field: dict[str, list[UUID]] = Field(default_factory=dict)
    status: Literal["DRAFT", "IN_REVIEW", "APPROVED", "REJECTED"] = "DRAFT"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    disclaimer: str = "Contains only treatment explicitly documented in the transcript."
    # Phase 7 compatibility: callers may still submit legacy unstructured items.
    # New generation code never populates this field.
    items: list[DocumentedText] = Field(default_factory=list, deprecated=True)

    @property
    def transcript_segment_ids(self) -> list[UUID]:
        """Compatibility view of all cited transcript segments."""
        return list(dict.fromkeys(item for refs in self.evidence_by_field.values() for item in refs))

    def render_text(self) -> str:
        def render(values: list[str]) -> str:
            return "\n".join(f"- {value}" for value in values)

        medications = []
        for item in self.pharmacological:
            if item.drug == NOT_MENTIONED:
                medications.append(f"- {NOT_MENTIONED}")
            else:
                medications.append(
                    f"- {item.drug}; dose: {item.dose}; route: {item.route}; "
                    f"frequency: {item.frequency}; duration: {item.duration} "
                    f"[transcript: {item.source_transcript_ref}]"
                )
        return "\n".join((
            "NON-PHARMACOLOGICAL", render(self.non_pharmacological), "",
            "PHARMACOLOGICAL", "\n".join(medications), "",
            "INVESTIGATIONS ORDERED", render(self.investigations_ordered), "",
            "REFERRALS", render(self.referrals), "",
            "FOLLOW-UP", render(self.follow_up), "",
            "PATIENT EDUCATION", render(self.patient_education), "",
            "PENDING DECISIONS", render(self.pending_decisions),
        ))
