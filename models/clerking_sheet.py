from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

NOT_DISCUSSED = "Not discussed."
NOT_PERFORMED = "Not performed"
DocumentedText = Annotated[str, Field(min_length=1)]


class ClerkingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HPCSection(ClerkingModel):
    onset: DocumentedText = NOT_DISCUSSED
    duration: DocumentedText = NOT_DISCUSSED
    character: DocumentedText = NOT_DISCUSSED
    radiation: DocumentedText = NOT_DISCUSSED
    severity: DocumentedText = NOT_DISCUSSED
    alleviating: DocumentedText = NOT_DISCUSSED
    aggravating: DocumentedText = NOT_DISCUSSED
    associated: DocumentedText = NOT_DISCUSSED


class DrugHistoryEntry(ClerkingModel):
    drug: DocumentedText = NOT_DISCUSSED
    dose: DocumentedText = NOT_DISCUSSED
    frequency: DocumentedText = NOT_DISCUSSED
    duration: DocumentedText = NOT_DISCUSSED


class AllergyEntry(ClerkingModel):
    drug: DocumentedText = NOT_DISCUSSED
    food: DocumentedText = NOT_DISCUSSED
    environmental: DocumentedText = NOT_DISCUSSED
    reaction: DocumentedText = NOT_DISCUSSED


class ClerkingSheet(ClerkingModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    version: int = Field(default=1, ge=1)
    presenting_complaint: DocumentedText = NOT_DISCUSSED
    history_of_presenting_complaint: HPCSection = Field(default_factory=HPCSection)
    systemic_review: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    past_medical_history: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    drug_history: list[DrugHistoryEntry] = Field(default_factory=lambda: [DrugHistoryEntry()])
    allergies: list[AllergyEntry] = Field(default_factory=lambda: [AllergyEntry()])
    family_history: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    social_history: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    physical_examination: list[DocumentedText] = Field(default_factory=lambda: [NOT_PERFORMED])
    investigations: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    assessment: list[DocumentedText] = Field(default_factory=lambda: [NOT_DISCUSSED])
    fact_ids: list[UUID] = Field(default_factory=list)
    evidence_by_field: dict[str, list[UUID]] = Field(default_factory=dict)
    status: Literal["DRAFT", "IN_REVIEW", "APPROVED", "REJECTED"] = "DRAFT"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Stable clinical abbreviations for callers that use section names directly.
    @property
    def pc(self) -> str:
        return self.presenting_complaint

    @property
    def hpc(self) -> HPCSection:
        return self.history_of_presenting_complaint

    @property
    def pmh(self) -> list[str]:
        return self.past_medical_history

    @property
    def dh(self) -> list[DrugHistoryEntry]:
        return self.drug_history

    @property
    def allergy(self) -> list[AllergyEntry]:
        return self.allergies

    @property
    def fh(self) -> list[str]:
        return self.family_history

    @property
    def sh(self) -> list[str]:
        return self.social_history

    @property
    def pe(self) -> list[str]:
        return self.physical_examination
