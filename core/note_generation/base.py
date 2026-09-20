from __future__ import annotations

from typing import Protocol

from core.models import ClerkingSheet, ClinicalNote


class NoteGenerationEngine(Protocol):
    """Swappable local-first note generator operating only on a clerking sheet."""

    @property
    def name(self) -> str: ...

    def generate(self, clerking_sheet: ClerkingSheet) -> ClinicalNote: ...

