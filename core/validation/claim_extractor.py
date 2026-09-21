from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from core.models import ClinicalNote, Differential, TreatmentPlan
from models.clerking_sheet import ClerkingSheet


@dataclass(frozen=True)
class ExtractedClaim:
    text: str
    path: str
    evidence_fact_ids: tuple[UUID, ...] = ()


class ClaimExtractor:
    """Extracts independently classifiable claims from every clinical artefact."""

    _MARKUP = re.compile(r"[*_`]+")
    _BULLET = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
    _KNOWN_LABEL = re.compile(
        r"^(?:presenting complaint|history of presenting complaint|systemic review|past medical history|"
        r"drug history|allergies?|family history|social history|physical examination|examination|"
        r"investigations?|assessment|plan|drug|dose|frequency|duration|onset|character|radiation|"
        r"severity|alleviating|aggravating|associated|food|environmental|reaction)\s*:\s*",
        re.IGNORECASE,
    )

    def from_note(self, note: ClinicalNote) -> list[ExtractedClaim]:
        claims = self.from_text(note.content, path="content") + self.from_text(note.plain_text, path="plain_text")
        unique: dict[str, ExtractedClaim] = {}
        for claim in claims:
            unique.setdefault(claim.text.casefold(), claim)
        return list(unique.values())

    def from_text(self, text: str, *, path: str = "text") -> list[ExtractedClaim]:
        claims: list[ExtractedClaim] = []
        for index, raw_line in enumerate(text.splitlines()):
            stripped = raw_line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or (
                    self._BULLET.match(stripped) is None
                    and stripped == stripped.upper()
                    and any(character.isalpha() for character in stripped)
                )
            ):
                continue
            stripped = self._BULLET.sub("", stripped)
            stripped = self._MARKUP.sub("", stripped).strip()
            previous = None
            while previous != stripped:
                previous = stripped
                stripped = self._KNOWN_LABEL.sub("", stripped).strip()
            if stripped:
                claims.append(ExtractedClaim(stripped, f"{path}.{index}"))
        return claims

    def from_clerking_sheet(self, sheet: ClerkingSheet) -> list[ExtractedClaim]:
        return [
            ExtractedClaim(value, path, tuple(sheet.evidence_by_field.get(path, [])))
            for path, value in self._leaf_strings(sheet.model_dump(mode="python"))
        ]

    def from_treatment_plan(self, plan: TreatmentPlan) -> list[ExtractedClaim]:
        excluded = {
            "id", "session_id", "note_id", "version", "fact_ids", "evidence_by_field",
            "status", "created_at", "disclaimer", "source_transcript_ref",
        }
        claims = []
        for path, value in self._leaf_strings(plan.model_dump(mode="python"), excluded=excluded):
            claims.append(ExtractedClaim(value, path, tuple(plan.evidence_by_field.get(path, []))))
        return claims

    def from_differential(self, differential: Differential) -> list[ExtractedClaim]:
        claims: list[ExtractedClaim] = []
        for index, candidate in enumerate(differential.candidates):
            if hasattr(candidate, "supporting_features") and hasattr(candidate, "evidence_refs"):
                evidence = tuple(candidate.evidence_refs)
                for key in ("supporting_features", "features_against"):
                    for feature_index, value in enumerate(getattr(candidate, key)):
                        claims.append(ExtractedClaim(
                            value, f"candidates.{index}.{key}.{feature_index}", evidence
                        ))
                continue
            evidence = tuple(differential.fact_ids)
            for key in ("diagnosis", "name", "candidate"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    claims.append(ExtractedClaim(value.strip(), f"candidates.{index}.{key}", evidence))
                    break
        return claims

    @staticmethod
    def _leaf_strings(value: Any, path: str = "", *, excluded: set[str] | None = None):
        excluded = excluded or {"id", "session_id", "version", "fact_ids", "evidence_by_field", "status", "created_at"}
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from ClaimExtractor._leaf_strings(item, f"{path}.{index}", excluded=excluded)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not path and key in excluded:
                    continue
                if key not in excluded:
                    yield from ClaimExtractor._leaf_strings(item, f"{path}.{key}" if path else key, excluded=excluded)
