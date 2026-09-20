"""Hallucination firewall and claim-level evidence validation."""

from core.validation.claim_extractor import ClaimExtractor, ExtractedClaim
from core.validation.contradiction_checker import ContradictionChecker
from core.validation.fact_validator import FactValidationError, FactValidator
from core.validation.validator import ArtifactValidationError, HallucinationFirewall, NoteValidator
from core.models import NoteValidationResult

__all__ = [
    "ArtifactValidationError", "ClaimExtractor", "ContradictionChecker", "ExtractedClaim",
    "FactValidationError", "FactValidator", "HallucinationFirewall", "NoteValidationResult", "NoteValidator",
]
