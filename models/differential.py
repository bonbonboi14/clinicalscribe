from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


DIFFERENTIAL_DISCLAIMER = (
    "This is an AI-generated differential for clinician reference only. "
    "It does not replace clinical judgment."
)
DIFFERENTIAL_DISABLED_MESSAGE = "Differential diagnosis: [Disabled — enable in settings]"
DocumentedFeature = Annotated[str, Field(min_length=1)]


class DxLikelihood(StrEnum):
    SUPPORTED = "SUPPORTED"
    POSSIBLE = "POSSIBLE"
    UNLIKELY = "UNLIKELY"


class DifferentialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Differential(DifferentialModel):
    condition: DocumentedFeature
    likelihood: DxLikelihood
    supporting_features: list[DocumentedFeature] = Field(default_factory=list)
    features_against: list[DocumentedFeature] = Field(default_factory=list)
    evidence_refs: list[UUID] = Field(min_length=1)

    @model_validator(mode="after")
    def require_documented_features(self) -> "Differential":
        if not self.supporting_features and not self.features_against:
            raise ValueError("a differential candidate requires a documented supporting or opposing feature")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("differential evidence references must be unique")
        return self


class DifferentialResult(DifferentialModel):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    enabled: bool
    candidates: list[Differential] = Field(default_factory=list, max_length=5)
    fact_count_used: int = Field(default=0, ge=0)
    disclaimer: str = DIFFERENTIAL_DISCLAIMER
    disabled_message: str = DIFFERENTIAL_DISABLED_MESSAGE
    generation_note: str = ""
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def enforce_toggle_contract(self) -> "DifferentialResult":
        if not self.enabled and self.candidates:
            raise ValueError("disabled differential results cannot contain candidates")
        if self.disclaimer != DIFFERENTIAL_DISCLAIMER:
            raise ValueError("the mandatory differential disclaimer cannot be changed")
        if self.disabled_message != DIFFERENTIAL_DISABLED_MESSAGE:
            raise ValueError("the mandatory disabled message cannot be changed")
        return self

    @property
    def enabled_at_generation(self) -> bool:
        return self.enabled

    @property
    def fact_ids(self) -> list[UUID]:
        return list(dict.fromkeys(ref for candidate in self.candidates for ref in candidate.evidence_refs))

    def render_text(self) -> str:
        if not self.enabled:
            return self.disabled_message
        lines = ["DIFFERENTIAL DIAGNOSIS", self.disclaimer]
        if not self.candidates:
            lines.append("No differential generated from the documented facts.")
        for index, candidate in enumerate(self.candidates, start=1):
            lines.append(f"{index}. {candidate.condition} — {candidate.likelihood.value}")
            lines.append("   Supporting: " + ("; ".join(candidate.supporting_features) or "None documented"))
            lines.append("   Against: " + ("; ".join(candidate.features_against) or "None documented"))
            lines.append("   Evidence: " + ", ".join(str(item) for item in candidate.evidence_refs))
        return "\n".join(lines)


# Compatibility name used by the original Phase 8 handoff contract.
DifferentialCandidate = Differential
